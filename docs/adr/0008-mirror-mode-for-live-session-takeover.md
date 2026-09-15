# 0008. 接管运行中会话走「镜像模式」：只读 rollout 监控 + 官方队列注入

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`tools/happy-codex-shim/`、`tools/codex-threads.py`
- 关联：[0002](0002-cli-desktop-version-parity.md)（rollout 是共享存储）、
  [0003](0003-phone-remote-control-via-happy.md)（手机远控走 Happy）

## 背景

0003 定下手机远控走 Happy，但那条路只覆盖「Happy 自己新开的会话」和
「接管一个**空闲**会话」。用户真正要的是三件事同时成立：

1. 在 CLI 或桌面端**任意一个窗口**里开对话，手机端立刻探知到并监控其输入输出
2. 手机端填的消息能传进那个**正在运行**的窗口去执行
3. 两边不互相干扰，是真同步而非互相顶替

挡路的是官方的一条硬约束（实测复现）：

```text
thread/resume -> {"code":-32600,"message":"thread <id> already has an active writer"}
```

每个正在运行的窗口都对 `~/.codex/thread-writer-locks/<id>.lock` 持有排他 flock，
第二个进程无法把该线程加载进自己内存，因此**收不到它的通知流**
（实测：B 进程在 A 跑回合期间收到的通知数为 0）。

## 决策

不绕过这条约束，而是承认它，把「监控」和「注入」拆到两条各自合法的通道上：

- **监控（读）**：尾随该线程的 `rollout-*.jsonl`。rollout 是共享存储（0002 已确认），
  且在回合进行中就持续追加。垫片把其中的 snake_case `event_msg` 翻译成
  app-server 线格式的 camelCase 通知（`item/started`、`item/completed`、
  `turn/started`、`turn/completed`、`thread/status/changed`、`thread/tokenUsage/updated`）。
- **注入（写）**：走官方跨进程队列 `thread/queue/add`（落 `~/.codex/queue_1.sqlite`，
  消费端靠 `PRAGMA data_version` + `queued_thread_revisions` 轮询）。
  等价原生命令：`codex queue --thread <id> --message <text>`。
- **不干扰**：镜像模式只读 rollout、只追加 queue，**从不申请 writer lock、从不写 rollout**。

实现形态是 `tools/happy-codex-shim/codex`——一个夹在 Happy 与真 codex 之间的
app-server 垫片，靠 `PATH` 前置注入，**不改 Happy 一行代码**。默认纯透传；
只有当真 codex 亲口回 `already has an active writer` 时才切镜像并自行合成响应。

历史回放不参与镜像：`thread/read` 在被锁线程上可用（实测通过），
所以 Happy 的 `resume-backfill` 补丁照常拿到完整历史，垫片只负责「从现在起」的实时部分。

## 为什么不选别的路

| 备选 | 否决理由（均为实测，非推测） |
|---|---|
| 第二进程 `thread/resume` 接管活跃线程 | 直接被拒：`already has an active writer` |
| `turn/steer` 插话 | 需要 `expectedTurnId` 前置条件，且只有持有线程的进程才知道当前 turn id |
| 连桌面端的 `~/.codex/ipc/ipc.sock` | `initialize` 超时；那是 Electron IPC，不是 app-server 控制面 |
| `codex remote-control` / daemon | 需要 `~/.codex/packages/standalone/current/codex`，且 `chatgpt authentication required ... api key auth is not supported`（与 0003 同一死结） |
| 共享 app-server（`--listen ws://`，两客户端连同一个） | 可用但通知**按发起方定向、不广播**：C2 在 C1 跑回合期间只收到 2 条 `thread/status/changed`，拿不到任何 item。且要求所有窗口都改成连同一个 server，侵入性远大于垫片 |
| 轮询 `thread/turns/list` 做监控 | 保真度不足：同一回合内 rollout 尾随拿到 8 个事件（含 reasoning 与两次 commandExecution），轮询只有 2 个快照且丢了全部中间项，滞后 20~40s |
| 手写 `INSERT` 进 `queue_1.sqlite` | 可行但绕过了官方校验，且 payload 是 Rust 枚举标签形态（`{"UserInput":{"content":...}}`）与 RPC 入参不同，易随版本漂移。用 RPC 让 codex 自己写，格式永远正确 |

## 实测结论（2026-09-13，codex-cli 0.152.1）

| 检查项 | 结果 |
|---|---|
| 跨进程 `thread/queue/add` 注入被锁线程 | 通过。宿主 ~2s 收到 `thread/queue/changed`，当前回合结束后自动开新回合执行注入内容 |
| `codex queue --thread --message`（官方 CLI） | 通过，行为与 RPC 一致 |
| rollout 回合中实时增长 | 通过，41s 内 11 次不同大小 |
| 第二进程在被锁线程上 `thread/read` / `thread/list` / `thread/turns/list` | 通过（只读调用不受 writer lock 限制） |
| 第二进程在被锁线程上 `thread/resume` | 失败，`already has an active writer`（预期，是设计依据） |
| flock 试锁探测运行中会话 | 通过，与 `lsof` ground truth 一致 4/4 |
| 翻译器回归（真实 rollout） | 18617 条 item、189019 项断言、0 失败 |
| 端到端（镜像接管→监控→注入→执行→不干扰） | 23/23 |
| Happy 真实调用序列（含 resume-backfill 路径） | 12/12 |
| Happy 经垫片启动 | 通过，`initialize` 握手透传，`clientInfo` 为 `happy-codex 1.2.3` |

## 影响

- **手机侧无流式 delta**：rollout 按 item 完成时落盘，所以看到的是一条条完整 item，
  不是逐字打字机效果。`item/*/delta` 只发给持有 writer lock 的进程——正是被官方
  禁止接管的那条路。这是「不干扰」的代价，不是缺陷。
- **插话是排队语义**：注入消息在对方**当前回合结束后**执行，不插队。垫片会给手机端
  一条 `warning` 说明这一点，避免用户以为丢了。
- **不支持中断**：镜像模式下 `turn/interrupt` 如实告知不可用，要去那个窗口里停。
  打断别人窗口的回合会直接破坏「不干扰」这条硬要求。
- **中途接入需要补偿**：垫片从文件末尾开始尾随（历史交给 `thread/read`），于是会错过
  本轮的 `task_started`。实测发现手机端因此显示成「空闲」，不知道后面那串 item 属于
  哪个回合。修法：接入时回扫最后 512KB，若有未配对的 `task_started` 就补发
  `thread/status/changed(active)` + `turn/started`，且**用真实 turn id**，
  后续 item 才能挂到同一回合上。
- **不能吞掉自己已应答的请求的真响应**：垫片自行应答后，真 codex 那条
  `already has an active writer` 仍会到达；必须按 request id 吞掉，否则 Happy
  会收到一条它没在等的错误。实现上用 `answered` 集合，而非只标记 resume。
- **版本耦合**：垫片依赖 rollout 的 snake_case 字段名，CLI 与桌面端必须同版本
  （0002）。`test-translate.mjs` 是这个漂移的哨兵——字段对不上时它会失败，
  而 Happy 只会静默丢事件、手机上表现为白屏。
- **Happy 升级安全**：垫片不改 Happy 代码，升级不会弄坏它。但若上游改了
  `CodexAppServerClient` 的方法集或通知白名单，需重跑 `test-happy-sequence.mjs`。
- **`--live-only` 的过滤必须在 SQL 里做**：先 `LIMIT` 再过滤 live 会漏掉排在
  15 条之外但正在跑的会话（实测 4 个 held 只报出 3 个）。已下推为 `WHERE id IN (...)`。
