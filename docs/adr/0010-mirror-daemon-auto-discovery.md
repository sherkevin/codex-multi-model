# 0010. 镜像守护进程：把「探知」变成自动的，只镜像持锁线程，且每个镜像必须自证 MIRROR ON

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`tools/happy-codex-shim/happy-mirror-daemon.py`、
  `tools/happy-codex-shim/com.jingwu.happy-codex-mirror.plist`
- 关联：[0008](0008-mirror-mode-for-live-session-takeover.md)（镜像模式本身）、
  [0003](0003-phone-remote-control-via-happy.md)（手机远控走 Happy）

## 背景

0008 解决了「能不能接管一个正在跑的窗口」，但接管仍要人在终端里敲
`happy-mirror <thread-id>`。用户要的是：无论 CLI 还是桌面端开一个窗口，
手机端立马探知到。手敲命令满足不了「立马」，也覆盖不了人不在电脑前的场景。

补这一层时有五条必须守住的约束，全部由实测得出，不是保守假设。

## 决策

### 1. 只镜像「此刻被占住」的线程，空闲线程绝不镜像

判据是 `~/.codex/thread-writer-locks/<id>.lock` 的 flock（`LOCK_NB` 试锁，
拿不到 = 有窗口在跑），与 0008 同一份事实来源。

这条是安全边界而非优化：对空闲线程跑 `happy codex --resume`，垫片撞不到
`already has an active writer`，于是走透传，镜像进程自己就成了 writer，
反过来把用户想开的那个窗口挡住。所以「先持锁才镜像」必须严格成立。

### 2. 每个镜像必须自证 MIRROR ON，否则立刻杀掉

垫片日志里出现 `MIRROR ON` 是「确实走了镜像分支」的唯一硬证据。
探测到持锁与 resume 落地之间存在竞态窗口（窗口可能刚好关掉），
所以守护在限时内（默认 30s）等不到这行就杀掉镜像，绝不留一个可能抢到
writer lock 的进程。`--once` 模式同样执行这条，否则一次性运行会留下没人回收的危险进程。

### 3. 排除 Happy 自己起的会话

`happy codex` 新开会话时，持锁者是垫片透传出来的真 codex，祖先进程链里有垫片。
这种线程手机上本来就有一条原生会话，再镜像就是同一个线程出现两条。
用 `lsof` 拿持锁 pid，向上走 ppid 链，命中垫片/happy 即跳过。

### 4. 失联进程必须在「已决定停止」的路径上按命令行查杀

实测踩到的真实残留：包装进程（`happy.mjs`）退出后变僵尸，
`os.kill(pid, 0)` 对僵尸照样成功，于是 `alive()` 永远说「活着」，
那条镜像记录再也回收不掉；而它派生的 node 会话进程（实测 pid 19376）
还在跑，持续占着中继上的会话。守护重启后更糟——僵尸被 launchd 收走、
pid 凭空消失，`alive(pid)` 判假，整段 kill 逻辑被直接跳过，孤儿永久残留。

修法三件套：

- `alive()` 排除僵尸（问 `ps -o stat=`）
- 每轮对持有的 `Popen` 调 `poll()` 回收退出状态，避免僵尸堆积
- 记录 `pgid`（`start_new_session` 保证 `pgid == pid`，旧状态文件缺字段时用 pid 兜底），
  并按命令行加 `ppid == 1` 查杀失联进程

`ppid == 1` 这条判据只能用在 `kill_mirror` 内，不能拿它判断镜像是否该回收：
守护自己重启后，健康镜像的包装进程同样被 init 收养而 `ppid == 1`，
拿它当回收条件会误杀正在服务的镜像。回收与否只看两件事——
writer lock 还在不在、镜像进程还在不在。实测验证：守护重启后两个健康镜像
（pid 68967/68970、68989/68998）被正确接管，未被误杀。

### 5. launchd 环境必须自带 PATH，不能依赖用户 rc 文件

本机已有的 `com.jingwu.codex-model-router` 用 `zsh -c 'source ~/.zshrc'` 那条路子，
这里刻意不用：`happy.mjs` 的 shebang 是 `#!/usr/bin/env node`，
而 launchd 给的 PATH 实测只有 `/usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.`，
里面没有 node，起 happy 直接 ENOENT，整个机制静默失效。
同理 `lsof` 在 `/usr/sbin`，也不在这个 PATH 里，少了它第 3 条会静默失效
（`FileNotFoundError` 被吞，Happy 自有会话被重复镜像，手机上出现两条）。
守护启动时自己 `ensure_path()`，并把 `lsof` 解析成绝对路径。

### 6. 守护必须自己保证 happy daemon 在线

手机的 machine 列表只显示**在线**机器，而在线状态靠 happy daemon 的长连接维持。
2026-09-13 实测事故：`happy auth login --force` 会停掉 daemon，
认证完成后**不会**自动重启它。结果用户看到的是「手机配对成功、
machine 列表空空如也」——一个完全指向错误方向的故障表现，
因为它看起来像配对失败，实际是配对之后少了个进程。

守护每轮调 `ensure_happy_daemon()`：

- 判定分三种情况，动作各不相同，不能合并成「不在就起」：
  - **A. `daemon.state.json` 里的 pid 活着** → 再做 HTTP 检查；健康即在线。
    不健康则**先收掉旧进程再起新的**：happy 自己的幂等判断
    （`checkIfDaemonRunningAndCleanupStaleState`）也读这个 state 文件，
    而它此刻已不可信，拦不住第二个 daemon 被起出来。收不掉就**本轮不拉起**
    ——宁可手机上暂时看不到机器，也不能多出一台。
  - **B. pid 死了或没有，但按命令行查得到 `daemon start-sync` 进程**
    （state 文件失效而 daemon 其实在跑）→ 不拉起，只记一次日志并给出人工修复指令。
    起下去就是手机上凭空多一台机器。判据刻意只用 `daemon start-sync`
    （happy 真 daemon 专有；我们派出去的是 `happy daemon start`，不带 `-sync`），
    **不**再叠加「路径里要有 happy 字样」这类修饰：实测某安装布局下路径不含该字样，
    判据会静默失效而照样起出第二个。误判成 True 只是本轮不拉起，下一轮自愈；
    误判成 False 才是难收拾的。
  - **C. 既没有活 pid，也没有 daemon 进程** → 真死了，拉起。
- 判活分两层。pid 存活每轮查（`kill(pid,0)` 加僵尸排除，复用 `alive()`）；
  HTTP 健康检查（`POST /list`，与 happy 自己 `checkIfDaemonRunningAndCleanupStaleState`
  同一判据）节流到 30s 一次，且连续两次失败才判死。
  单次失败不足以定罪：误判的代价是重启 daemon、手机端短暂离线、丢掉它正在跟踪的会话。
- 拉起走 `happy daemon start`，冷却 30s。**冷却时间戳在尝试之前就写**：
  若只在成功时写，起不来的情况下永远不写，于是每 4 秒重试一次刷爆日志。
- `happy daemon start` 自身幂等（已有同版本 daemon 时打印
  "Daemon already running with matching version" 后 exit 0），
  所以同版本的重复请求由 happy 自己消化；但它的幂等判断依赖 state 文件，
  文件失效时救不了场，这才是 B 分支必须由我们自己判的原因。
- **不**用 happy 自带的开机自启：那条路要往 `/Library/LaunchDaemons/` 写文件、
  需要 root，在守护里弹权限不可接受。这台机器的自启由 launchd
  `com.jingwu.happy-codex-mirror` 加本条共同负责。
- `ensure_happy_daemon()` 的返回值只表示「**此刻**在线」。刚拉起的 daemon
  还没写 state、HTTP 口也没起来，返回 True 是谎报（实测踩到：
  `daemon.state.json` 不存在时 `--fix-happy-daemon` 直接打印「本来就在线」）。
  主循环下一轮自然看到它在线；`--fix-happy-daemon` 自己有等待循环。
  该等待循环最长 40s，超时返回非 0 并指向日志，绝不把「没起来」报成「起来了」。

### 7. Happy 身份变了，全部镜像必须重启

镜像进程启动时把凭据读进内存。之后重新扫码配对（换账号），
它们仍连着**旧**账号推流，新账号侧永远看不到——而且所有日志都显示健康，
人眼几乎不可能发现。同一次事故里实测到：三个镜像在旧账号下跑得好好的。

身份指纹取 `settings.json` 的 `machineId` **加** `access.key` 里 JWT 的 `sub`：
重新配对必然换 `sub`，但 `machineId` 不一定变，只看前者会漏。
事故当天两者确实都变了（`5963217b…` → `c98978cc…`，
`cmpuyi7ex…` → `cmtzvjud…`），但把判据建在两个信号上是必要的。

守护在每条镜像记录里存起镜像那一刻的身份，每轮比对：

- 不一致 → `kill_mirror` 整组收掉，交给同一轮的发现逻辑用新凭据重起。
- 读不到身份（文件缺失或损坏）→ 返回 None，**当成「没变化」处理**。
  反过来写会让凭据文件一时读不到就触发全量重启，把好的弄坏。
- 旧状态文件没有 `identity` 字段的（升级前留下的）→ 就地补当前身份，
  不当成漂移。那些镜像是健康的，不该为一次代码升级重启一遍。
  实测验证：`launchctl kickstart -k` 重启守护后，三个镜像 pid
  （96374/96382/96390）原封不动被接管，字段补齐，writer lock 仍全在原窗口 78123。

排查入口随之改进：`--status` 现在先打 daemon 在线状态（pid/port/HTTP）、
当前 Happy 身份（machineId + 账号），并标出「还连着旧账号」的镜像；
`--fix-happy-daemon` 一条命令把 daemon 拉起来并等到真的在线。
第 6 条那个事故的排查耗时，主要花在这些信息原本要人去翻
`~/.happy/daemon.state.json` 和解 JWT 才看得到。

## 实测结论（2026-09-13，happy-cli 1.2.3 + codex 0.152.1）

| 检查项 | 结果 |
|---|---|
| 自动发现新窗口（桌面端 app-server） | 通过。线程 18:57:24 创建，守护 18:57:28 起镜像，18:57:34 确认 |
| 自动发现 CLI 发起的线程（`codex exec`） | 通过。20:15:03 起镜像，20:15:21 确认 MIRROR ON |
| 三个真实窗口同时镜像，原窗口 writer lock 未被夺走 | 通过。`lsof` 显示三个锁仍全部由原窗口 pid 78123 持有 |
| 历史回放（resume-backfill 补丁） | 通过，单线程回放 5860 条 envelope |
| 中继注册（手机能看到会话） | 通过，daemon 日志逐条 `Session webhook` 与 `Registered externally-started session` |
| 监控事件实时推送到中继 | 通过，镜像会话日志出现 `exec_command_begin/end`、`agent_message`、`patch_apply_begin/end`、`token_count` |
| 跨进程注入并被宿主执行 | 通过。宿主 `turn/completed` 到 `thread/queue/changed` 约 4ms，随后自动开新 turn 执行注入内容 |
| 窗口关闭后回收 | 通过。关窗即标记宽限期，180s 后整组收掉（含失联进程） |
| 守护重启接管既有镜像 | 通过，0.15s 完成接管与确认，未误杀健康镜像 |
| launchd 极简 PATH 下依赖解析 | 通过。`node`、`happy`、`codex`、`lsof` 全部解析到绝对路径 |
| 翻译器回归（垫片未受影响） | 20243 条 item、204285 项断言、0 失败（22:47 复跑：20842 / 210051 / 0 失败） |
| happy daemon 判死并拉起（第 6 条） | 通过。隔离 `HAPPY_HOME_DIR` 下写入 pid 不存在的 stale state，`ensure_happy_daemon()` 判死并触发拉起；假 happy 二进制验证 `daemon start` 参数、cwd、脱离进程组、重复调用被拒（上一个未结束时返回 False）、结束后允许再起、Popen 被 poll 回收无僵尸 |
| HTTP 健康检查节流与「连续两次才判死」（第 6 条） | 通过。pid 活着但端口无人听时：第 1 次只计数不判死，第 2 次判死并拉起；线上 daemon 日志显示 `Listing` 间隔约 32s（非每 4s 一次） |
| 冷却限流（第 6 条） | 通过。首次尝试后立刻再调返回 False、不重复拉起 |
| `--fix-happy-daemon` 两条分支（第 6 条） | 通过。daemon 不存在 → 拉起后 1.1s 确认在线并打印 pid/port；已在线 → 报「本来就在线」不重复起 |
| 身份漂移守卫（第 7 条） | 通过。四场景：无 `identity` 字段的旧记录 → 就地补齐、不重启；身份匹配 → 不动；一条漂移一条匹配 → 只收掉漂移那条；身份读不到 → 全部不动 |
| 守卫上线不误伤既有镜像 | 通过。`launchctl kickstart -k` 重启守护，三个镜像 pid 96374/96382/96390 原封不动接管，`identity` 字段补齐，三个 writer lock 仍全部由原窗口 pid 78123 持有，`MIRROR ON` 仍在，守护零异常 |

## 影响

- 名额按「线程还在跑」算，不按记录数算：窗口关掉后镜像进入宽限期，
  这段时间它不该占住 `HAPPY_MIRROR_MAX` 名额（实测会挡住刚启动的新窗口，
  且每轮刷一条上限日志）。
- 每个镜像约 100MB 常驻（happy node 加垫片加真 codex app-server），
  所以默认上限 4，可用 `HAPPY_MIRROR_MAX` 调。
- 起完立刻落盘状态：守护随后崩溃时，状态文件还留着 pid 与 pgid，
  下次启动能接管或回收，不至于把镜像变成没人管的孤儿。
- 守护退出不带走镜像：`start_new_session=True` 让镜像脱离守护进程组，
  守护重启期间手机端不会黑屏，重启后自动接管。
- CLI TUI 的边界：纯 `codex` TUI 在发出第一条消息之前不建线程、不持锁，
  所以那一刻手机上还看不到它，它连 rollout 都还没有。
  第一条消息之后即被自动发现（`codex exec` 路径已实测）。
