# 0002. CLI 必须与桌面端同版本：rollout 文件是共享存储，格式由桌面端定义

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`~/.codex/sessions/**/rollout-*.jsonl`、`~/.codex/thread_history_1.sqlite`、`~/.codex/state_5.sqlite`

## 背景

CLI 与桌面端共用 `~/.codex/config.toml`、同一份 `sessions/` 目录和同一套
SQLite 索引，因此"会话互通"是文件层面的事实，不需要任何额外同步机制：

- 会话正文：`sessions/YYYY/MM/DD/rollout-<时间>-<thread-id>.jsonl`，两端都往同一个文件追加
- 桌面端列表：`state_5.sqlite` 的 `threads` 表（CLI 新建的会话也会出现在桌面端）
- 桌面端正文索引：`thread_history_1.sqlite` 的 `thread_items` / `thread_turns`，
  用 `thread_history_projection_state(thread_id, next_rollout_byte_offset, next_rollout_ordinal)`
  记录已消费到 rollout 文件的哪个字节

但 2026-09-13 实测发现：本机 CLI 是 0.142.5，桌面端是 0.152.1，两者写的
rollout 记录格式不兼容。0.152.1 在每行 JSON 顶层写了 `ordinal` 字段（从 0 连续递增），
0.142.5 不写。后果是**单向损坏**：

1. 0.142.5 CLI 追加到桌面端会话后，桌面端 0.152.1 恢复该会话直接失败：
   `failed to resume local thread recorder: final paginated rollout record at
   ... is missing an ordinal (code=-32603)`
2. 0.142.5 自己也读不全：对同一线程调 `thread/read`，桌面端写的 turn 返回
   `items: []`，只有 0.142.5 自己写的 turn 有内容
3. 0.152.1 读则完全正常（139 条 items 与 SQLite 投影一致）

## 决策

**CLI 版本必须与桌面端对齐**，且以桌面端为准。发现不兼容时优先升级 CLI，
而不是转换或降级数据。

理由：rollout 文件是两端共享的持久存储，而桌面端是随 ChatGPT.app 分发的、
格式演进更激进的一方（它先引入 `ordinal`）。让落后的一方去迁就领先的一方，
比反向兼容可靠——我们控制不了桌面端的发版节奏。

对齐方式：`npm install -g @openai/codex@<桌面端版本>`。桌面端版本从
`/Applications/ChatGPT.app/Contents/Resources/codex --version` 读取，
npm 上有完整的 stable 版本序列（0.150.x–0.154.x 均可装）。

注意 `~/.codex/sessions` 里 fork 出的文件（文件名含 `_`，如
`rollout-...-<父id>_<子id>.jsonl`）ordinal 不从 0 开始，而是继承父线程的偏移。
这是设计使然，不是缺陷，不要"修复"它。判定损坏只看一条：**有没有 ordinal 为
null 的行**。

## 2026-09-13 执行结果

- CLI 0.142.5 → 0.152.1（与桌面端一致）
- 修复 3 个被旧 CLI 写坏的 rollout 文件，补齐缺失的 `ordinal`（均先备份为
  `*.bak-ordinal-repair`）：`01a0615c`、`01a09369`、`fcc58e05`（测试会话，已隔离）
- 全量扫描 259 个会话文件：ordinal 缺失 0 个
- 修复后 `thread/read` 与 `thread/resume` 在桌面端会话上均正常

## 影响

- 桌面端升级后需要重新对齐 CLI 版本，否则跨端会话互通会再次静默损坏。
  这个失效模式很隐蔽：CLI 端一切正常，只有桌面端恢复时才报错。
- 若必须让旧 CLI 接触新格式会话，先用 `codex fork` 派生副本，不要直接 resume。
