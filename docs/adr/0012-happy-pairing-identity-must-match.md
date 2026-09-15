# 0012. 手机 Happy 账号必须与本机 CLI 账号同一个；重新配对会让运行中的镜像失效

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`~/.happy/settings.json`（machineId）、`~/.happy/access.key`（账号 JWT）、
  `tools/happy-codex-shim/happy-mirror-daemon.py`
- 关联：[0003](0003-phone-remote-control-via-happy.md)（手机远控走 Happy）、
  [0010](0010-mirror-daemon-auto-discovery.md) 第 6、7 条（守护对这两条的自动兜底）、
  [0011](0011-native-phone-pairing-blocked-by-three-walls.md)（原生配对不可用，Happy 是唯一通道）

## 背景

2026-09-13 22:00 前后用户报告：手机 Happy 提示终端错误、要求
`npm i -g happy-coder` 后扫码，扫完配对成功，但选 machine 时**列表里一个机器都没有**。

排查发现两件事各自独立成立，叠在一起把故障表现伪装成了「配对没成功」：

1. 手机 App 上登录的是 6 月之后新注册的账号（JWT `sub` = `cmtzvjud…`），
   而本机 `~/.happy/access.key` 里存的是 6 月 1 日的旧账号（`sub` = `cmpuyi7ex…`）。
   两边不是同一个账号，手机上自然什么都看不到。App 因此退回通用 onboarding 页，
   显示「安装 CLI 并扫码」——看起来像 CLI 没装好，其实是账号不匹配。
2. 用 `happy auth login --force` 重新配对修好账号之后，machine 列表**仍然是空的**。
   原因是 `--force` 会停掉 happy daemon，认证完成后不会重启它；
   而 machine 列表只显示在线机器，在线状态由 daemon 的长连接维持。

## 决策

**手机 App 与本机 CLI 必须是同一个 Happy 账号。** 这不是可选项，
是整套手机通道的身份前提：会话、machine、加密密钥都按账号隔离。
账号不一致时所有日志都显示健康，故障却完全不可见，所以必须先排除这一条再查别的。

**重新配对是一次会波及运行中镜像的破坏性操作。** 镜像进程启动时把凭据读进内存，
换账号后它们仍连着旧账号推流。配对之后必须确认镜像已用新凭据重连，
不能只看「进程还活着」。

**这两件事由守护进程自动兜底，不靠人记。** 具体实现记在
[ADR 0010](0010-mirror-daemon-auto-discovery.md) 第 6、7 条：
守护每轮保证 happy daemon 在线，并比对身份指纹
（`settings.json` 的 `machineId` + `access.key` 里 JWT 的 `sub`），
不一致就把全部镜像收掉重起。

**旧凭据保留备份，不删。** 位置 `~/.happy/.bak-before-repair-20260913-220400/`，
含旧 `access.key`、`settings.json`、`sessions.json`、`daemon.state.json`。
旧账号下那些会话的加密密钥只存在这份备份里，删了就再也解不开。

## 排查入口

```bash
python3 tools/happy-codex-shim/happy-mirror-daemon.py --status
```

第一屏依次是：happy daemon 是否在线（pid / port / HTTP 是否正常）、
当前 Happy 身份（machineId + 账号 `sub`）、镜像列表（并标出还连着旧账号的）。

两种典型成因对应两个动作：

- machine 列表空 → daemon 不在线 → `--fix-happy-daemon`
- 机器在、会话不在 → 账号漂移 → 守护会自动重启镜像；要立刻生效就 `--stop` 再等守护重起

## 影响

- `--status` 从「只列镜像」升级为「先看通道再看镜像」，因为通道断了的时候
  镜像列表本身没有诊断价值。
- 守护状态文件每条镜像记录新增 `identity` 字段。旧记录没有该字段时就地补齐、
  不触发重启（实测：重启守护后三个镜像 pid 原封不动被接管）。
- 身份读不到时一律当成「没变化」，绝不触发全量重启：
  凭据文件一时读不到就重启镜像，是把好状态改坏。
- 重新配对后不需要手动重启镜像，但需要知道它**会**被重启——
  手机端会话 id 会变，正在手机上看着的那条需要重新点开。
