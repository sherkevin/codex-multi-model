# 0007. 跨平台是默认要求，平台特性必须可选且可降级

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`codex-model-router.py`、`codex-console/config_io.py`、`regen-model-catalog.py`、`service/`、`run-router.bat`、`.gitattributes`、`requirements.txt`

## 背景

这套东西最初是在 macOS 上长出来的，于是 macOS 的便利被当成了前提。要给别人复用
（Windows / Linux 都能直接跑）时，这些前提一个个变成了硬伤：

| 位置 | 原状 | 在非 macOS 上的后果 |
|---|---|---|
| 图片降档 | 只有 `sips`（macOS 自带） | Windows/Linux 上降档**静默失效**，退化成丢图，长会话保真度掉一截 |
| 请求转储 | 硬编码 `/tmp/...` | Windows 没有 `/tmp`，写入抛异常（被吞掉，调试时找不到文件） |
| 报错文案 | 让用户跑 `launchctl kickstart ...` | 非 macOS 用户照着敲必然失败，且没有替代指引 |
| 内置兜底路由 | 写死某个内部网关 | 别人 clone 下来跑不通，且**泄漏内部主机名** |
| 上游限流值 | 字节/token 硬顶写死在代码里 | 换个网关就莫名其妙丢图或死锁，且无从配置 |
| 配置台重启按钮 | `launchctl` / `killall` / `open` | 非 macOS 直接抛异常 |
| codex 二进制定位 | `shutil.which() or "/opt/homebrew/bin/codex"` | 非 Homebrew 环境兜底路径不存在 |

## 决策

**跨平台是默认要求**，不是事后补丁。三条硬规矩：

1. **能力必须有降级路径，且降级要出声。** 图片编码器按 `Pillow → sips → none`
   顺序选（`SHRINK_BACKEND`），启动日志打印选中的后端。`none` 不报错，退回"只按字节
   预算丢图"——功能少一层但会话不崩。
2. **平台相关的操作要么给出全平台替代，要么明确说"仅此平台"并给指引。** 常驻服务
   给三套模板（launchd / systemd --user / 计划任务）；配置台的两个重启按钮在非 macOS
   返回一段"该怎么做"的说明，而不是抛异常。配置读写本身全平台可用。
3. **上游特性一律配置化，不写死。** 字节硬顶、token 硬顶、三组错误签名都做成
   路由级可选字段 + 环境变量覆盖。默认值取"判错代价不对称"里更安全的那个
   （字节默认 6MB：多降一档图只是画质损失，漏设则 TooLarge → 死锁，见 0004）。

顺带把两个纯工程细节也定死：

- **不硬编码 `/tmp`**，一律 `tempfile.gettempdir()`，且路径可用环境变量覆盖。
- **Windows 脚本的编码/行尾是正确性问题，不是风格问题。** `.bat` 用 ASCII-only +
  CRLF（`cmd.exe` 按 OEM 代码页解析批处理，中文会花）；`.ps1` 加 UTF-8 BOM
  （PowerShell 5.1 把无 BOM 的文件当 ANSI 读，中文注释会花）。`.gitattributes`
  钉住行尾，防止 `core.autocrlf` 在 macOS/Linux 上把它们改写回 LF。

## 内置默认路由必须是占位符

这是本条里唯一带安全含义的决策：`_DEFAULT_ROUTING` 与探针脚本的内置默认，
**只放 `example-gateway` 这类占位符**，绝不放真实内部端点。

原因有二：一是发布仓库不该带内部主机名；二是占位符天然跑不通，逼着使用者去写自己的
`router-routes.json`，而不是误以为内置那套能用。真实路由永远从
`~/.codex/router-routes.json` 读，该文件已在 `.gitignore` 里。

## 影响

- 依赖策略：核心路由**纯标准库可跑**。Pillow / tomlkit 都是可选增强，写进
  `requirements.txt` 并注明用途。
- 路由新增可选字段 `body_byte_limit` / `input_token_limit`（model 或 provider 级）。
- 日志新增 `ROUTER_LOG_FILE`：计划任务没有 stdout/stderr 重定向，不靠它就完全没日志。
- 启动日志打印生效配置（桥接开关、图片后端、每条路由的限流值），排查时不必猜。
- 配置台与工具的兜底路径改为多平台候选 + 环境变量覆盖（如 `HAPPY_DIR`）。
