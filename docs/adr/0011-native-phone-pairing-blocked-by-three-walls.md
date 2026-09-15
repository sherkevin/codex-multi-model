# 0011. 原生手机配对（remote-control）被三道独立的墙挡住，且控制面地址不可改向

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`tools/happy-codex-shim/`（现行手机通道）
- 关联：[0003](0003-phone-remote-control-via-happy.md)（手机远控走 Happy）、
  [0008](0008-mirror-mode-for-live-session-takeover.md)、
  [0010](0010-mirror-daemon-auto-discovery.md)
- 取代：无。本条把 0003 里「认证方式问题」的推断升级为三道墙的实测结论。

## 背景

用户想要的体验是手机 ChatGPT 里那一套：桌面端 → 设置 → 「控制这台 Mac/PC」→
出配对码 → 手机接管。0003 当时的结论是「要求 ChatGPT 认证，apikey 不支持」，
依据是日志里的一行报错，属于推断。本轮把它做成实验：在**隔离 CODEX_HOME**
里放一份真实的 ChatGPT OAuth 备份，再跑 `codex remote-control`，看它到底
卡在哪一步。所有操作不碰生产 `~/.codex/auth.json`，生产 router（8317）全程在线。

## 复现步骤（隔离，可重复）

```bash
CODEX_TEST_HOME=/tmp/rc-test
mkdir -p $CODEX_TEST_HOME/packages/standalone/current
ln -s /Applications/ChatGPT.app/Contents/Resources/codex \
      $CODEX_TEST_HOME/packages/standalone/current/codex
cp ~/.codex/auth.json.bak-chatgpt-* $CODEX_TEST_HOME/auth.json   # 真实 OAuth
CODEX_HOME=$CODEX_TEST_HOME codex remote-control start --json
```

注意 `current` 必须是**目录**，里面放名为 `codex` 的二进制；直接把它做成
指向二进制的软链会报「managed standalone Codex install not found」。

## 三道墙（每道都独立成立，绕过任何一道都不够）

### 墙 1：OAuth 令牌刷新被地域策略拒绝，refresh_token 救不回来

```text
Failed to refresh token: 403 Forbidden:
{"error":{"code":"unsupported_country_region_territory",
 "message":"Country, region, or territory not supported","type":"request_forbidden"}}
```

备份里的 `refresh_token` 是有效的（请求被正确发出并鉴权到了地域判断这一步），
是账号出口地域被策略拒绝。这不是过期问题，重新登录也解决不了。

### 墙 2：控制面 `chatgpt.com` 在本机网络不可达

```text
curl https://chatgpt.com/    -> (35) Recv failure: Connection reset by peer
curl https://api.openai.com/ -> (35) Recv failure: Connection reset by peer
curl https://auth.openai.com/-> 403  （TLS 握手通，说明不是全网断）
```

enroll 因此失败，websocket 无限重连（实测到第 7 次仍在退避重试）：

```text
failed to enroll remote control server at
`https://chatgpt.com/backend-api/wham/remote/control/server/enroll`
```

配对码这一步同样依赖云端：`remote-control pair` 走
`remoteControl/pairing/start`，在 enroll 没成功时直接超时
（`timed out waiting for remoteControl/pairing/start response`）。
**所以「先拿到配对码再说」也不成立——码本身要从云端签发。**

### 墙 3：控制面地址硬编码，不能像模型那样改向

模型面可以改向（`[model_providers.router] base_url` 指向本地 8317），
控制面不行。二进制里那个看起来像开关的环境变量实测无效：

```bash
CODEX_APP_SERVER_CHATGPT_BASE_URL="http://127.0.0.1:9999" \
  codex remote-control start
# 日志里仍然是：
# websocket_url":"wss://chatgpt.com/backend-api/wham/remote/control/server"
```

验证方法：本地起一个只打印请求的 mock（`127.0.0.1:9999`），
`curl` 打过去有 `[HIT]`，codex 跑完整流程一条都没有。
`CODEX_APP_SERVER_CHATGPT_BASE_URL` 只影响 app 目录类请求
（`/backend-api/wham/app/appcast` 等），不影响 remote-control 的 enroll/websocket。

这也顺带否掉了「自建一个假控制面来发配对码」的想法：即便把地址改向成功，
手机端 ChatGPT 只会连 OpenAI 的真控制面去查「我账号下有哪些 environment」，
自建的那个永远不在手机列表里。

## 决策

**不实现原生手机配对。** 手机控制继续走 [0003](0003-phone-remote-control-via-happy.md)
的 Happy 通道，配合 [0008](0008-mirror-mode-for-live-session-takeover.md)
的镜像模式与 [0010](0010-mirror-daemon-auto-discovery.md) 的自动探知守护进程。

这条通道与原生配对的差距，用户能感知到的只有「入口不是手机 ChatGPT，
而是手机 Happy」；能力上镜像模式反而更强一点：它能接管**已经在电脑上跑的**
任意窗口（CLI 或桌面端），而原生 remote-control 接管的是它自己那套
environment，桌面端窗口不在其列。

## 什么情况下应当重新评估

三道墙里只有墙 2 是环境性的，墙 1 和墙 3 是策略与实现性的：

1. 出口网络能稳定直连 `chatgpt.com`（墙 2 消失），**且**
2. 该出口的账号地域不再触发 `unsupported_country_region_territory`（墙 1 消失）。

两条同时满足时，原生配对才可能跑通——而且此时仍要走 ChatGPT OAuth，
与控制台的「原生模式」（`codex-console/config_io.py` 的 `set_run_mode`）
是同一套切换：还原 OAuth + 删除中转 `model_provider`。也就是说**原生配对与
自定义模型是互斥的两种登录态**，不能同时拥有。墙 3 无法绕过。

重测只要跑上面那四行复现命令，看 `app-server.stderr.log` 里
`websocket_url` 是否仍是 chatgpt.com、以及 `Refreshing token` 是否还报 403。

## 影响

- 不再把「原生配对」列为待办或可选项，避免重复投入。
- Happy 通道成为手机控制的唯一方案，其守护进程（launchd
  `com.jingwu.happy-codex-mirror`）需要保持运行。
- 隔离测试目录 `/tmp/rc-test` 属于临时产物，不进仓库；mock 控制面脚本
  `/tmp/mock_chatgpt.py` 同理。本 ADR 已把它们的构造方式记录下来，可复现。
