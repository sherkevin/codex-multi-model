# happy-codex-shim — 手机镜像接管电脑上任意 Codex 窗口

让手机上的 Happy 能「看到」并「插话」一个正在电脑上跑的 Codex 会话
（CLI TUI 或桌面端），而不是像 `happy codex` 那样自己另开一个新会话。

> **平台范围**：镜像接管的核心（flock 试锁探知、尾随 rollout、官方队列注入）是
> POSIX 语义，**macOS / Linux 可用**；开机常驻用 launchd 模板（见下），Linux 需自行
> 换 systemd。Windows 上 `fcntl` 不可用，`live_thread_ids()` 会降级返回空（不报错），
> 即「探知」这一环失效、其余透传路径不受影响——符合 ADR 0007「平台特性可选且可降级」。

## 它解决的那个死结

官方 app-server 明确禁止第二个进程接管活跃线程：

```text
thread/resume -> {"code":-32600,"message":"thread <id> already has an active writer"}
```

所以 `happy codex --resume <正在运行的会话>` 必然失败。垫片夹在 Happy 与真 codex
之间，**只在撞上这条错误时**切镜像模式，其余情况纯透传（`codex --version`、
`codex exec` 等非 app-server 调用直接 exec 真二进制，垫片不参与）。

## 三个动作各自怎么实现（都已实测）

| 需求 | 实现 | 实测 |
|---|---|---|
| 探知窗口已启动 | `thread-writer-locks/<id>.lock` 的 flock 试锁（`LOCK_NB`）：拿不到 = 有窗口在跑 | 与 `lsof` ground truth 完全一致，4/4 |
| 监控输入输出 | 尾随该线程的 `rollout-*.jsonl`，把 snake_case 事件翻译成 app-server 线格式的 camelCase 通知 | 41s 内 11 次增长；保真度高于 `thread/turns/list` 轮询（后者丢 reasoning 与中间 commandExecution，滞后 20~40s） |
| 手机填的信息传进窗口 | 官方跨进程队列 `thread/queue/add`（落 `~/.codex/queue_1.sqlite`） | 宿主 ~2s 内收到 `thread/queue/changed`，当前回合结束后自动开新回合执行 |
| 不干扰 | 镜像**只读** rollout、**只追加** queue，从不申请 writer lock、从不写 rollout | A 自己的回合照常跑完；A 的 `turn/started` 恰好 2 次（自己 1 + 注入 1） |

注入等价的原生命令是 `codex queue --thread <id> --message <text>`，垫片内部走
RPC 只是为了复用同一条 app-server 连接。

历史回放仍走真 codex：`thread/read` 在被锁线程上**可用**（实测通过），
所以 Happy 的 `resume-backfill` 补丁（见 [ADR 0003](../../docs/adr/0003-phone-remote-control-via-happy.md)）
照常工作，垫片不参与历史部分。

## 用法

```bash
# 列出此刻正在运行、可镜像接管的会话
tools/happy-codex-shim/happy-mirror

# 接管指定会话（手机上就能看能发）
tools/happy-codex-shim/happy-mirror <thread-id>

# 接管最近一个运行中的
tools/happy-codex-shim/happy-mirror --last

# 全部会话（含空闲的；空闲的用普通 happy codex --resume 即可）
tools/happy-codex-shim/happy-mirror --list
```

`tools/codex-threads.py` 也加了运行中标记：

```bash
python3 tools/codex-threads.py --live-only   # 只看正在跑的
python3 tools/codex-threads.py -n 20         # 运行中的带 *运行中
```

## 自动探知：镜像守护进程

上面那条命令要人敲。装了守护进程之后不用敲——电脑上任何 Codex 窗口
（CLI 或桌面端）一启动，几秒内手机 Happy 的会话列表里就出现它，可看可插话。

```bash
# 装成开机常驻（macOS）：模板里四处占位符换成你的实际值再装
cp tools/happy-codex-shim/happy-codex-mirror.plist.example ~/Library/LaunchAgents/com.<你的标识>.happy-codex-mirror.plist
# 编辑该文件：替换 <LABEL> <PYTHON> <SCRIPT> <REPO>
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<你的标识>.happy-codex-mirror.plist

# 日常
python3 tools/happy-codex-shim/happy-mirror-daemon.py --status   # 现在镜像了哪些窗口
python3 tools/happy-codex-shim/happy-mirror-daemon.py --once     # 只扫一轮（调试）
python3 tools/happy-codex-shim/happy-mirror-daemon.py --stop     # 停掉所有由守护起的镜像
python3 tools/happy-codex-shim/happy-mirror-daemon.py --fix-happy-daemon  # 手机看不到机器时用
launchctl bootout gui/$(id -u)/com.<你的标识>.happy-codex-mirror  # 卸载常驻

# 日志
tail -f ~/.happy/mirror-daemon.log     # 守护自己的决策日志
ls ~/.happy/mirrors/                   # 每个镜像一份垫片日志（含 MIRROR ON 自证）
```

守护只镜像**此刻被 writer lock 占住**的线程，空闲线程一律不碰：对空闲线程跑
`happy codex --resume` 不会触发镜像模式，镜像进程反而会自己成为 writer，
把你想开的窗口挡住。每个镜像必须在限时内自证 `MIRROR ON`，否则立刻杀掉——
这是防住上述风险的兜底，不是性能优化。细节与实测数据见
[ADR 0010](../../docs/adr/0010-mirror-daemon-auto-discovery.md)。

守护自己的环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `HAPPY_MIRROR_INTERVAL` | 4 | 轮询秒数（实测 4s 发现、10s 内确认） |
| `HAPPY_MIRROR_MAX` | 4 | 同时镜像上限，每个镜像约 100MB 常驻 |
| `HAPPY_MIRROR_GRACE` | 180 | 窗口关掉后镜像保留秒数，到点整组回收 |
| `HAPPY_MIRROR_VERIFY_S` | 30 | 等 `MIRROR ON` 的秒数，超时杀掉 |
| `HAPPY_MIRROR_LOG` | `~/.happy/mirror-daemon.log` | 守护日志 |
| `HAPPY_MIRROR_DIR` | `~/.happy/mirrors` | 每个镜像的垫片/happy 日志目录 |
| `HAPPY_MIRROR_DAEMON_COOLDOWN` | 30 | 拉起 happy daemon 的最小间隔秒数 |
| `HAPPY_MIRROR_DAEMON_HTTP_EVERY` | 30 | happy daemon HTTP 健康检查间隔秒数 |

**一个边界**：纯 `codex` TUI 在你发出第一条消息之前不建线程、不持锁，
那一刻手机上还看不到它（它连 rollout 都还没有）。第一条消息之后就会被自动发现。

### 手机端看不到机器 / 看不到会话

三种成因，`--status` 第一屏就能分开：

1. **machine 列表是空的** → happy daemon 不在线。手机的 machine 列表只显示
   在线机器，而在线状态靠 daemon 的长连接维持。跑 `--fix-happy-daemon` 一条命令修。
   注意 `happy auth login --force`（重新扫码配对）会停掉 daemon 且**不会**自动重启，
   所以「配对成功却看不到机器」是这条路的必然结果，不是配对失败。守护已每轮兜住。
2. **机器在、会话不在** → 多半是配对换了账号，而镜像还连着旧账号推流。
   `--status` 会把这种镜像标成「还连着旧账号」。守护每轮比对身份指纹
   （`machineId` + 账号 `sub`），不一致就自动全部重启，正常不需要人管。
3. **机器是空的，但守护日志说「有个 happy daemon 进程在跑，但 state 文件已失效」**
   → 这种情况守护**故意不拉起**：`daemon.state.json` 丢了而进程还在时，
   再起一个就会让手机上凭空多出一台机器（happy 自己的幂等判断也读这个文件，拦不住）。
   按日志给的指令人工修：`happy daemon stop`，再跑 `--fix-happy-daemon`。

旧账号凭据的备份在 `~/.happy/.bak-before-repair-20260913-220400/`。
细节与实测见 [ADR 0010](../../docs/adr/0010-mirror-daemon-auto-discovery.md) 第 6、7 条。

## 手机上能看到自定义模型吗

能，但要靠补丁。手机的模型选择器读的是会话 metadata 里的 `models` /
`currentModelCode`，上游只有 ACP 后端会填，codex 后端从不填，所以原生 Happy
只会给你 App 内置那份 GPT 清单。`tools/happy-patch.py` 的
`model-catalog-to-phone` 补丁在建连后调官方 `model/list`（返回的就是
`~/.codex/custom-model-catalog.json` 那 9 个），映射成手机端要的
`{code, value, description}` 写回 metadata，并在 resume / 新开会话时把
真实当前模型一并报上去。检查与回滚：

```bash
python3 tools/happy-patch.py --check                       # 看三个补丁是否都在
python3 tools/happy-patch.py --revert                      # 全部还原成上游
HAPPY_CODEX_MODEL_META=0 happy codex ...                   # 单次运行关掉
```

镜像模式下还有一条诚实性保证：`thread/queue/add` 会**静默忽略** model 字段
（实测：带 model 与带一个乱编字段的响应完全一样），所以手机换模型换不动那个
桌面窗口。垫片因此去 `state_5.sqlite` 读线程真实的 model / reasoning_effort，
只在「你选的」和「窗口在用的」真的不一致时弹一条说明，避免每条消息都误报
（happy 因为 config-model-default 补丁会恒定带上 model）。
细节与实测见 [ADR 0013](../../docs/adr/0013-phone-model-list-must-come-from-catalog.md)。

## 环境变量（垫片）

| 变量 | 作用 |
|---|---|
| `HAPPY_CODEX_SHIM=0` | 完全禁用垫片，`happy-mirror` 退回普通 `happy codex` |
| `CODEX_REAL` | 真 codex 路径（默认在 `PATH` 里找，靠 `realpath` 自识别跳过垫片自己） |
| `HAPPY_CODEX_SHIM_LOG` | 写垫片调试日志，例如 `/tmp/shim.log`；默认不写 |

## 测试

```bash
node tools/happy-codex-shim/test-translate.mjs        # 翻译器回归，秒级
node tools/happy-codex-shim/test-model-meta.mjs       # 手机能看到自定义模型，秒级
node tools/happy-codex-shim/test-e2e.mjs              # 镜像+监控+注入+不干扰，约 2 分钟
node tools/happy-codex-shim/test-happy-sequence.mjs   # 按 Happy 真实调用序列压，约 2 分钟
```

`test-translate.mjs` 拿真实 rollout 喂翻译器并断言线格式（required 齐全、
camelCase、类型正确）。这个断言是必需的：字段映射错了 Happy **不会报错**，
只会静默丢事件，手机上表现为白屏。

`test-model-meta.mjs` 守的是同一类静默故障，只是发生在模型清单上：它从打过
补丁的 happy bundle 里抠出真实的 `syncCodexModelMetadata`，喂真实的
`model/list` 返回值，再用从线上 App bundle 逐字抄来的解析函数
（`fixtures/webapp-model-picker.mjs`）走一遍，断言自定义模型可见、当前模型
角标如实、隐藏模型不泄漏、`model/list` 失败时静默降级。还有一组对照断言，
证明上游 bundle 确实不填 `metadata.models`、手机确实会回落——没有对照组，
一片 PASS 说明不了任何事。`--live` 会真调一次 `model/list` 而不是用 fixture。

`test-e2e.mjs` / `test-happy-sequence.mjs` 全程跑在隔离 CODEX_HOME 里
（`test-home.mjs` 临时建、跑完即删，`KEEP_TEST_HOME=1` 可保留）。这一条是必须的：
它们会真的 `thread/start` 跑回合，若沿用生产 `~/.codex`，测试线程会短暂持有
writer lock，而守护**只镜像持锁线程**，于是会把测试会话也镜像到手机上（实测踩过，
当时要 bootout 守护才能跑测试）。隔离之后守护读的是生产 home，看不见测试线程，
不必再停守护。`probe-mirror-model-boundary.mjs` 同理，是 ADR 0013 那条边界的
可复现探针。

## 已知边界

- **不支持中断**：`turn/interrupt` 在镜像模式下如实告知不可用。打断别人窗口的
  回合会破坏「不干扰」，要去那个窗口里停。
- **插话是排队语义**：注入的消息在对方**当前回合结束后**才执行，不会插队打断。
  这既是官方的队列语义，也是「不干扰」的必然结果。手机端会收到一条说明。
- **镜像模式不能换模型**：手机选的模型不会作用到那个桌面/CLI 窗口，
  垫片会如实提醒。要换模型请去那个窗口里换。
- **无流式 delta**：rollout 按 item 落盘（完成时），所以手机看到的是一条条完整
  item，不是逐字打字机效果。要 delta 得接 `item/*/delta` 通知，而那只有持有
  writer lock 的进程收得到——正是被官方禁止的那件事。
- **垫片依赖 rollout 格式**：CLI 与桌面端必须同版本（见
  [ADR 0002](../../docs/adr/0002-cli-desktop-version-parity.md)），否则 snake_case
  字段可能对不上。`test-translate.mjs` 就是这个漂移的哨兵。
- **Happy 升级**：垫片靠 `PATH` 前置注入，不改 Happy 一行代码，所以升级不会
  弄坏它。但若上游改了 `CodexAppServerClient` 的方法集或通知白名单，需要重跑
  `test-happy-sequence.mjs`。
