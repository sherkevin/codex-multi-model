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
