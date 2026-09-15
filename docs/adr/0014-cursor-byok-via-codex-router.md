# 0014. Cursor 助手（cursor-byok）经 Codex router 接模型，不直连上游

- 状态：Accepted
- 日期：2026-09-15
- 相关文件：`~/.cursor-byok-v3/cursor-byok.db`（`model_configs` 表）、
  `~/.codex/codex-model-router.py`、`~/.codex/router-routes.json`
- 关联：[0001](0001-router-mode-criterion.md)（router mode 判据）、
  [0002](0002-cli-desktop-version-parity.md)

## 背景

用户要求把本地已装的「Cursor 助手」配置成与 Codex 一致，并接上
`qwen3.8-max` / `moonshot/kimi-k3` / `MiniMax-M3`。

实测先弄清了三件事，它们决定了后面的选择：

1. 装的是 `cursor-byok-desktop` v0.1.7（Rust/Tauri），配置存在
   `~/.cursor-byok-v3/cursor-byok.db` 的 `model_configs` 表，**不是**
   `~/.cursor-local-assistant-v2/config.yaml`（那是上一代 Go 版 v0.0.39 的，
   本机 `~/work/cursor-byok` 仓库对应的就是它）。初始状态该表为空。
2. 它有干净的本地 REST API（`http://127.0.0.1:59424/__byok-api__/api/...`，
   注意**不带**结尾斜杠，带了会被 SPA 兜走返回 HTML），
   `POST /models` 建、`PUT /models/{hash}` 改、`PUT /models/order` 排序、
   `POST /models/{hash}/test/{test_id}` 自带连通性测试。所以不必写 sqlite。
3. 本机有系统代理 `127.0.0.1:7890`，而**没有任何进程在监听 7890**。
   cursor-byok 的 HTTP 客户端（reqwest）默认继承系统代理，于是直连
   `idealab.alibaba-inc.com` 一律失败：
   `http error: error sending request for url (.../v1/chat/completions)`。
   同一个 URL 用 `curl --noproxy '*'` 直连是 200。

## 决策

**三个模型一律把 `base_url` 指向 `http://127.0.0.1:8317`、
`openai_endpoint` 设为 `/v1/responses`、`api_key` 填占位值 `router`。**
不直连 idealab / minimax。

两个理由，第一条是必须的，第二条是额外收益：

1. **绕过失效的系统代理。** macOS 的代理 `ExceptionsList` 里含 `127.0.0.1`
   与 `localhost`，所以指向本地 router 的请求按规则就不走代理
   （是规则，不是侥幸）。这是唯一一个既不用改系统代理设置、
   也不用改应用二进制就能让 idealab 通的办法。应用自己的代理设置只有
   `default` / `custom` 两档，没有 `direct`，堵不住这条路。
2. **配置真正与 Codex 同源。** router 读的就是 Codex 用的那份
   `router-routes.json`，模型路由、translate/passthrough 模式、上游 key
   全部一份。以后加模型只改 router 的路由表，两边同时生效，
   不会出现「Codex 能用、Cursor 不能」的漂移。

**上下文窗口按 Codex 的 catalog 填，不按 router 的 `input_token_limit`。**
`custom-model-catalog.json` 里 idealab 三个模型声明 `context_window = 300000`，
MiniMax-M3 是 `1000000`；而 router 的 `input_token_limit` 是 983616，
那是**请求体字节上限**，不是上下文窗口（见
[ADR 0004](0004-byte-limit-vs-token-limit.md)：两者是不同量）。
填错会让 Cursor 过早或过晚触发压缩。

**`reasoning_effort` 填 `xhigh`，与 `config.toml` 的 `model_reasoning_effort` 一致。**
这是安全的：cursor-byok 的解析顺序是「运行时（UI 里选的）优先，通道配置兜底」
（`internal/backend/agent/model/router.go`：`runtimeThinkingEffort != ""` 时覆盖
`channel.ReasoningEffort`），与 Codex 的「`--model`/UI 优先，config.toml 兜底」同构。
所以填了不会剥夺 Cursor UI 里选档位的自由。
实测 `xhigh` / `high` / `medium` 经 router 都能正常返回。

**排序让 `moonshot/kimi-k3` 第一**，因为它就是 `config.toml` 里的
`model = "moonshot/kimi-k3"`。

## 验证

用应用自带的 `POST /models/{hash}/test/{test_id}`（不是自己写的探针，
是它真正会用的那条链路）：

```text
Qwen3.8-Max (ideaLAB)  PASS  17.7s  first_token=2134ms  863 tokens  48.8/s
Kimi-K3 (ideaLAB)      PASS  11.7s  first_token=2793ms  288 tokens  24.6/s
MiniMax-M3             FAIL  502    已达到 Token Plan 用量上限 (2056)
```

MiniMax 那条是**账号额度**，不是配置问题：请求已经到达 MiniMax 并拿到
真实的套餐上限报错，而且 Codex 走 router 打同一个模型得到的是同一条错误。
配置是对的，额度补上就能用。

Cursor 侧集成状态：

```text
integration=enabled  models=3 configured / 3 enabled
settings_applied=true  ca=ready  proxy=http://127.0.0.1:60680
```

并且实测 Cursor 自己的 `aiserver.v1.AiService/AvailableModels` 经助手代理返回的
就是我们这三个模型（带 `300000` 自定义上下文项与 low/medium/high/xhigh/max 档位），
说明 Cursor UI 里能看到、能选。

## 影响与注意

- **依赖 router 常驻。** `launchctl` 里的 `com.jingwu.codex-model-router`
  是 `KeepAlive = true`，正常不会断。但它一停，Cursor 这边三个模型同时失效，
  报错会是 502/连接失败而不是「模型不存在」。排查先看
  `curl --noproxy '*' http://127.0.0.1:8317/v1/models`。
- **`model_hash` 会随 `base_url` / `api_key` 变。** 它是这几项的派生值，
  `PUT` 改完旧 hash 立即失效（拿旧 hash 测试会 `404 run not found`）。
  脚本化操作必须每次重新 `GET /models` 取 hash，不能缓存。
- **`api_key` 里存的是占位串 `router`**，真实上游 key 只存在
  `~/.zshrc` 与 router 进程环境里。这比把 key 复制到第二个应用的库里更安全，
  也少一处要同步的地方。
- 一个遗留项（本次**未改**）：`~/Library/LaunchAgents/com.user.node-extra-ca.plist`
  把 `NODE_EXTRA_CA_CERTS` 指向上一代的 v2 CA（whistle 那张），
  而当前助手用的是 v3 CA（`~/.cursor-byok-v3/ca/ca.crt`，已在 admin 域受信任）。
  实测 Cursor 走系统信任即可通过 MITM（`curl` 不带 `--cacert` 经代理返回 200/405），
  所以暂不影响。若以后出现 Cursor 侧 "Network disconnected"，
  把这个变量改指 v3 CA 是第一处要查的。
