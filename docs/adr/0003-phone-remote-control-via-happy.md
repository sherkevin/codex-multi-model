# 0003. 手机远控走 Happy，不走 Codex 原生 remote-control，也不走桌面级远控

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`tools/happy-patch.py`、`tools/codex-threads.py`、`~/.codex/config.toml`

## 背景

需求是用手机原生聊天界面接管电脑上正在跑的 Codex 会话，而不是远控桌面。
桌面级远控（网易 UU 远程 `uuyc-cli lterm`）已实测可用——会话可跨设备接管、
断连不掉线、多端同时镜像同一屏——但它本质是把终端画面搬到手机，字小、
不适合长对话，只作为兜底保留。

Codex 原生 remote-control 在本机配置下**不可用**，与自定义 API 无直接关系，
而是认证方式问题：`codex remote-control start` 要求
`~/.codex/packages/standalone/current/codex` 存在，且 app-server 日志明确报
`chatgpt authentication required ... api key auth is not supported`，
`remoteControl/status = disabled`。本机 `auth.json` 是 `auth_mode: apikey`，
走本地 router 代理，因此这条路堵死。

关键事实是：**`codex app-server`（stdio）在 apikey 认证下完全正常**。
实测 `initialize` → `thread/start` → `turn/start` → 流式 `item/agentMessage/delta`
→ `turn/completed` 全链路走通 router，模型正常返回。

Happy（github.com/slopus/happy，MIT，23.7k star）的桌面侧正是用
`codex app-server --listen stdio://` 驱动 Codex，并且完整继承进程环境变量，
因此天然沿用 `~/.codex/config.toml` 的 `model_provider = "router"`。
它的中继服务端 `api.cluster-fluster.com` 实测直连可达（0.8s，无需代理），
网页端 `app.happy.engineering` 返回 200。所以选 Happy。

## 决策

手机端走 Happy。桌面侧用 `happy codex` 起会话，手机/网页端接管；
接管既有桌面会话用 `happy codex --resume <thread-id>`。

上游有两处不适配，用 `tools/happy-patch.py` 打本地补丁解决，不改 fork：

1. **resume-backfill** — `--resume` 只发一条 "Resumed thread ..."，不回放历史，
   手机端看不到既有对话。上游只在 daemon 的 fork 路径调用了
   `buildCodexThreadBackfillEnvelopes`，终端 `--resume` 路径漏了。补丁复用同一函数
   补上。实测回放 231 条 envelope。可用 `HAPPY_CODEX_RESUME_BACKFILL=0` 关闭。

2. **config-model-default** — 上游硬编码 `DEFAULT_CODEX_MODEL = "gpt-5.6-sol"`，
   会盖掉 config.toml 的 `model`。实测裸跑 `happy codex` 时该模型命中 router 的
   `gpt-` 前缀路由走到 openai provider，因 launchd 环境缺 `OPENAI_API_KEY`
   返回 401 并重试 5 次后失败。补丁改为优先读 config.toml 的
   `model` / `model_reasoning_effort`，读不到才回落上游默认值。
   实测生效：切回自定义后取到 config.toml 里的 model 与 effort，不再被上游默认值覆盖。

补丁工具的设计约束：

- **幂等**，重复执行只跳过；`--check` 看状态，`--revert` 从 `.orig-bak` 还原
- **自动定位 bundle**：happy 的 dist 文件名带 hash（`index-K72yHyB6.mjs`），
  升级后会变，不能写死
- **按 bundle 形态改写 logger**：`.mjs` 顶层 import 了裸 `logger`，`.cjs` 只有
  `api.logger`。两份都要打，cjs 里写裸 `logger` 会在 catch 分支抛 ReferenceError，
  把整个 resume 拖垮——这是个真实踩过的坑
- 打完 `node --check` 自检，失败自动回滚

`thread-id` 用 `tools/codex-threads.py` 查，数据源是桌面端自己的
`state_5.sqlite`（只读打开，避免与桌面端抢 WAL 写锁），不是扫 rollout 文件，
所以标题/时间/项目与桌面端一致。

## 实测结论（2026-09-13）

| 检查项 | 结果 |
|---|---|
| `codex app-server` 在 apikey 认证下初始化 | 通过 |
| `thread/start` + `turn/start` 走 router | 通过，流式 delta 与 `turn/completed` 均正常 |
| `thread/read` 读桌面端会话历史 | 通过（需 CLI 与桌面端同版本，见 [0002](0002-cli-desktop-version-parity.md)） |
| `thread/resume` 桌面端会话 | 通过，且**只读**，不改 rollout 文件 |
| `happy codex --resume` 历史回放 | 通过，231 条 envelope |
| 裸跑 `happy codex` 的默认模型 | 补丁后取 config.toml，不再是 `gpt-5.6-sol` |
| 走 translate 桥的网关模型（实测三个） | 三个全通 |
| 走 passthrough 的第三方模型 | 失败，上游额度问题，与 Happy 无关 |
| `gpt-*` 前缀路由 | 失败，launchd 环境缺 `OPENAI_API_KEY` |

## 影响与遗留

- Happy 升级会覆盖 dist，需重跑 `python3 tools/happy-patch.py`。
  补丁锚点若失配，工具会打印"锚点出现 N 次"并跳过，不会写出坏文件。
- 手机端：安卓走 Play Store（`com.ex3ndr.happy`），仓库不发 APK；
  任意手机浏览器可直接用 `app.happy.engineering`。
- 中继走 Happy 官方云端（内容为端到端加密）。若要完全自主，仓库提供
  `packages/happy-server-self-host`：`npm i -g happy-server-self-host && happy server`，
  内嵌 PGlite + 本地文件存储，不需要 Postgres/Redis/S3，会自动写入
  `settings.serverUrl`。本次未部署。
- `gpt-*` 前缀路由缺 `OPENAI_API_KEY` 是既有问题，与 Happy 无关，但会让人
  误以为是 Happy 坏了。要么补 key，要么在 router 里收窄前缀路由。
