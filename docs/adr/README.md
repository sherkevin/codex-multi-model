# 架构决议记录（ADR）

一条决议一个文件，自包含（含背景、决策、理由、影响），编号稳定以便引用。
后续对话推翻旧决议时，不直接改旧文件，而是把旧的标为 `Superseded by NNNN` 并新开一条。

| ID | 标题 | 状态 |
|---|---|---|
| [0001](0001-router-mode-criterion.md) | Router mode 判据：上游能否吃下 Codex 的真实 Responses 请求 | Accepted |
| [0002](0002-cli-desktop-version-parity.md) | CLI 必须与桌面端同版本：rollout 文件是共享存储，格式由桌面端定义 | Accepted |
| [0003](0003-phone-remote-control-via-happy.md) | 手机远控走 Happy，不走 Codex 原生 remote-control，也不走桌面级远控 | Accepted |
| [0004](0004-byte-limit-vs-token-limit.md) | 字节超限与 token 超限是两类故障，不得混用同一个信号 | Accepted |
| [0005](0005-context-window-and-compaction-trigger.md) | 上下文窗口与压缩触发点必须等于上游真实上限 | Accepted |
| [0006](0006-deferred-tool-bridge.md) | 延迟工具桥接：translate 模式必须还原 Codex 的全部原生工具形态 | Accepted |
| [0007](0007-cross-platform-by-default.md) | 跨平台是默认要求，平台特性必须可选且可降级 | Accepted |
| [0008](0008-mirror-mode-for-live-session-takeover.md) | 接管运行中会话走「镜像模式」：只读 rollout 监控 + 官方队列注入 | Accepted |
| [0009](0009-upstream-byte-limit-is-escaped-and-undersold.md) | 上游字节硬顶按「ASCII 转义后」计量，且宣称值不可信（实测 4.5MiB，非 6291456） | Accepted |
| [0010](0010-mirror-daemon-auto-discovery.md) | 镜像守护进程把「探知」变成自动的：只镜像持锁线程，每个镜像必须自证 MIRROR ON | Accepted |
| [0011](0011-native-phone-pairing-blocked-by-three-walls.md) | 原生手机配对被三道墙挡住：OAuth 地域 403、控制面不可达、地址硬编码不可改向 | Accepted |
| [0012](0012-happy-pairing-identity-must-match.md) | 手机 Happy 账号必须与本机 CLI 账号同一个；重新配对会让运行中的镜像失效 | Accepted |
| [0013](0013-phone-model-list-must-come-from-catalog.md) | 手机的模型清单必须来自 `model/list`；镜像模式换模型够不着官方 RPC，只能如实告知 | Accepted |
| [0014](0014-cursor-byok-via-codex-router.md) | Cursor 助手经 Codex router 接模型：绕过失效的系统代理，且配置与 Codex 同源 | Accepted |
| [0015](0015-factory-state-is-canonical-and-auth-is-deleted.md) | 出厂态是「规范态、每次现推」，且登录态靠删文件而非清空 | Accepted |
