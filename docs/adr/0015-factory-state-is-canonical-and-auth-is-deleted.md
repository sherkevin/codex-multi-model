# 0015 出厂态是「规范态、每次现推」，且登录态靠删文件而非清空

状态：Accepted（2026-09-15）

## 背景

配置台要支持一键 **Restore / Apply**：Restore 把 Codex 恢复成「刚装好、还没登录」
的官方状态（交给 Cockpit Tools 等第三方切号器接管时，它看到的必须是一份干净配置），
Apply 一键拿回我们这套中转配置。用户的固定动作是「用 Cockpit 前先 Restore，用完再
Apply」，所以这两个动作必须各自一步到位、可反复来回。

实现时踩到三个实测坑，每一个都决定了最终形态：

1. `auth.json = {}` 或 `{"OPENAI_API_KEY": null}` 时，`codex login status` 仍报
   **"Logged in using ChatGPT"**；只有**文件不存在**才报 "Not logged in"。
   桌面端在 keyring 模式下还会从 macOS Keychain（service=`Codex Auth`）恢复登录态。
2. tomlkit 的 `del doc["review_model"]` 会把键头上紧邻的注释**留在原地**，注释于是
   漂到下一个键头上。出厂态留下一堆讲中转/讲 ideaLAB 的孤儿注释，用户会误读。
3. 用「整文件快照」存出厂态会复活旧版逻辑的残留（早期快照里带着没删干净的注释），
   而且快照是死的：live 的共享配置（hooks / mcp_servers / plugins / features）会随
   Codex 升级、装插件不断变化，死快照切回去就把这些变化抹掉了。

## 决策

1. **出厂态（factory）是规范态，永远现推，不存快照。** 它的定义就是「把模式专属键
   （`model` / `model_provider` / `review_model` / `model_catalog_json` /
   `model_reasoning_effort` / `model_providers`）从当前文本里删干净」，与历史无关。
   `custom` / `native` 两态继续用整文件文本快照——那两态各自有用户手改过的键值，
   需要逐字节还原（含注释与键序），这正是快照存在的理由。
2. **删登录态 = 删 auth.json 文件 + 删 Keychain 条目**，绝不写空内容。删之前先备份：
   OAuth tokens → `auth.json.bak-chatgpt-<时间戳>`，Keychain 凭据 →
   `.console-keychain-auth.bak`（600）。切回 native 时从这两份备份还原。
3. **删键走文本级删除**（`strip_mode_key_blocks`），键与它头上紧邻的注释一起走；
   不用 tomlkit 删。共享键三态都不碰。
4. **判态优先级：provider 名 > 登录方式。** `model_provider` 是 `router`（我们写的）
   或 `codex_local_access`（Cockpit 写的）就判 custom，哪怕 auth.json 不存在；
   auth.json 不存在且无上述 provider 才判 factory。否则用户正用着 Cockpit，配置台
   却显示「出厂未登录」，点 Restore 会白干一场。
5. **出厂态下若仍有残留的模式专属键**（别的工具动过 config.toml），`read_run_mode`
   把它们列进 `factory_residual_keys`，UI 提示再点一次 Restore；`set_run_mode`
   在这种情况下**不**走 already_there 的 no-op 分支，保证 Restore 真的清掉它。

## 理由

规范态 + 现推让 Restore 的语义稳定：无论何时点，结果都是「此刻的共享配置减去我们的
键」，不会带历史包袱，也不会抹掉用户在另一态里改过的共享设置。删文件而非清空是
Codex 自身行为决定的（它只认文件在不在），不是我们的偏好。

## 影响

- `config_io.set_run_mode("factory")` 每次现推；`.console-run-mode.json` 里不再存
  factory 快照（旧快照会被主动 pop 掉）。
- 回归测试 `tests/test_run_mode_switch.py` 固化了上述全部行为，含真实 `codex`
  二进制校验「出厂态确实 Not logged in、配置确实能加载」。
- 共享键（hooks / mcp_servers / plugins / features / desktop / projects）三态不动，
  避免来回切 drift；这是刻意的边界，不属于「我们的中转」。
