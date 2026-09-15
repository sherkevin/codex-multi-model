# codex-console

Codex 多模型的**本地可视化配置台**。在浏览器里配好 API / AK / 模型 / 登录方式，然后**回到 Codex 本体里正常用**——这个网页只负责配置，不是聊天客户端。

配置读写**全平台可用**（macOS / Linux / Windows）；「一键重启」是 macOS 专属便利，其它平台会返回操作指引而不是静默失败（见能力③）。

> 它是 [`codex-model-router`](../多模型接入手册.md) 的配套前端。先把中转跑起来（默认 `127.0.0.1:8317`），本工具才有意义。

---

## 为什么配置在这里改、却在 Codex 里用，两边还不会打架

因为**网页和 Codex 读写的是同一批文件**，不存在「两份配置」需要同步：

| 你在网页改的 | 落到的文件 | Codex 怎么读到 |
|---|---|---|
| Provider（真实上游 base_url / AK 变量） | `~/.codex/router-routes.json` | 中转启动时读取 |
| Model → 走哪个 provider + 模式 | `~/.codex/router-routes.json` | 中转按模型名分发 |
| API base_url / wire_api / env_key | `~/.codex/config.toml` 的 provider | 直接读同一文件 |
| 默认模型 / 思考档 / 评审模型 | `~/.codex/config.toml` 顶层 | 新会话读取 |
| 模型显示名/描述/可见性/默认档 | `~/.codex/custom-model-catalog.json` | App 启动时读取 |
| 上游 AK | `~/.codex/router-secrets.env`（chmod 600） | 中转启动时 source |
| 登录绕过 | `~/.codex/auth.json`（apikey 模式） | 启动时校验 |

顶部那条**一致性面板**不是网页自己的状态，而是**实时跑 `codex login status` + 重读 config.toml** 得到的「Codex 此刻真实读到的值」。所以你能直接看到两边一致。

> 反向坑（已处理）：Codex 桌面端切模型时会**回写** `config.toml`。本网页**每次实时重读、从不缓存**，并提供「↻ 刷新」，因此你在 App 里改了，网页刷新即同步。

---

## 五个核心能力

**① 配 API 和 AK** — 可视化增删改 provider（base_url / wire_api / env_key），AK 单独写入 `router-secrets.env`，界面只显示掩码（如 `677••••••64e9`），**绝不回显明文、绝不写进 config.toml**。改 config.toml 用 `tomlkit`，**逐字符保留你手写的注释**，每次写入前自动备份 `*.bak-<时间戳>`。

**② 绕过账号登录** — Codex 登录门只校验 `auth.json` 处于 `apikey` 模式且有任意非空 key；用一个占位串即可过门，真实模型鉴权由中转各自的 AK 完成，与这个占位 key 无关。「修复登录绕过」按钮在 `codex logout` 或升级把它重置后一键恢复。

**③ Restore / Apply 一键换档** — 三态互切，只动「模式专属键」（`model` / `model_provider` / `review_model` / `model_catalog_json` / `model_reasoning_effort` / `model_providers`）+ `auth.json`，你的 `hooks` / `mcp_servers` / `plugins` / `features` 等共享设置三态都不碰：

- **自定义（中转）**：走我们的中转 + 第三方模型 + apikey 免登录。
- **原生（ChatGPT 登录）**：还原真实 OAuth 登录、model/思考档退回原生、删掉中转专属键。
- **出厂（未登录）**：**Restore** 按钮把 Codex 恢复成「刚装好、还没登录」的官方状态——删 auth.json 文件（不是清空，实测清空仍报已登录）、清 macOS Keychain 凭据、删全部中转专属键（连注释一起）。这份配置可以直接交给 Cockpit Tools 等第三方切号器接管。

**Apply** 按钮一键拿回中转配置，`config.toml` 逐字节还原（含你手写的注释与键序）。登录态在 Restore 前自动备份（`auth.json.bak-chatgpt-*` / `.console-keychain-auth.bak`），切回原生或 Apply 时自动还原，不用重新扫码。出厂态是规范态、每次现推（见 [ADR 0015](../docs/adr/0015-factory-state-is-canonical-and-auth-is-deleted.md)）。

**④ 一键重启 Codex（仅 macOS）** — 主按钮重启 **Codex 桌面 App**（`/Applications/ChatGPT.app`，即 `killall ChatGPT` + `open`），让模型目录等改动立即生效；**会关闭当前在途会话**，故带二次确认。另有「重启中转」按钮，供改 AK / 路由后生效（`launchctl kickstart`，label 自动探测）。这两个按钮依赖 `launchctl` / `killall` / `open`，只在 macOS 有效；Linux / Windows 上点它们会返回一段「该怎么手动重启」的说明，不会抛异常——配置本身已经写好了，重启只是让它生效。

**⑤ 在 Codex 里用** — 配置写完、重启完，回到 Codex 桌面 App 或 CLI，`/model` 里就是你配的模型，照常工作。本工具到此功成身退。

**⑥ 回归测试兜底** — `python3 ../tests/test_run_mode_switch.py` 覆盖三态往返、逐字节还原、残留键清理，并用**真实 `codex` 二进制**校验「出厂态确实 Not logged in、配置确实能加载」。改这块逻辑前先跑它。

---

## 模型 → 真实 provider：把中转这层黑盒掀开

Codex 的 `model_provider` 是**单值**：同一时刻所有模型共用一个 provider，`/model` 切换只换模型名、不换 provider。目录里也没有任何 provider 字段，CLI 也没有 per-model provider 机制。所以「不同模型走不同真实上游、还能在 `/model` 里无感切换」**只能靠一个统一层**——中转：Codex 把 `model_provider` 指向 `router`，中转再按模型名分发到真实上游、换上各自的 AK。

配置台用三张卡把这层暴露成可配置、可见的数据（落在 `~/.codex/router-routes.json`，中转启动时读取，缺失则回退内置默认）：

- **① Provider 卡** — 定义真实上游：名字、`base_url`、`key_env`（AK 变量名）。
- **② Model 卡** — 每个模型选 **provider**（用哪家的额度）+ **模式**（`passthrough` 上游原生 Responses / `translate` 上游仅 chat 需翻译），外加显示名/描述/可见性/默认思考档。
- **后端模式 & 中转卡** — 展示中转 `/v1/routes` 此刻**真实生效**的路由表（模型 │ 真实 provider │ base_url │ AK 变量 │ 模式），并自动判定该直连还是走中转。

**自动判定「能直连就直连，否则才用中转」**：

$$
\text{后端}=\begin{cases}\text{直连该 provider（model\_provider 设为它，不经中转）}, & \text{所有模型同一 provider 且全为 passthrough}\\ \text{中转 router}, & \text{跨 provider，或含 translate 模型}\end{cases}
$$

「应用推荐后端」按钮据此把 `config.toml` 的 `model_provider` 一键设对。例如当你的模型跨多个 provider、或含 translate 模型时，会判定为**必须走中转**——这不是偷懒，是 Codex 单 provider 的硬约束决定的。

---

## 安装与运行

**前置**：① 中转 `codex-model-router` 已跑（见《多模型接入手册》）；② Python 3.9+；③ 唯一依赖 `tomlkit`；④ `codex` CLI 在 PATH（用于读登录态）。

```bash
./run.sh                 # 前台试用 → http://127.0.0.1:8420
./install-service.sh     # 装成 launchd 常驻服务（开机自起、崩溃自拉、带密钥环境）
```

`run.sh` 与 `install-service.sh` 是 macOS / Linux 的便捷脚本（后者用 launchd 常驻）。
任意平台都可以直接 `python server.py` 起前台。常驻方式见仓库根 `service/` 下的模板
（launchd / systemd --user / 计划任务）。

`install-service.sh` 用 `zsh -c 'source ~/.zshrc; …'` 启动——这样服务进程才拿得到你的 AK 环境变量，密钥面板才能正确显示「已设置」。非 macOS 平台若用 systemd / 计划任务常驻，需在那边显式给出 AK 环境变量，否则密钥面板会显示「未设置」。

### 环境变量
| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEX_CONSOLE_PORT` | `8420` | 本工具端口 |
| `CODEX_CONSOLE_HOST` | `127.0.0.1` | 监听地址（**保持回环，勿改 0.0.0.0**） |
| `CODEX_ROUTER_URL` | `http://127.0.0.1:8317` | 中转地址 |
| `CODEX_HOME` | `~/.codex` | Codex 配置目录 |
| `CODEX_DESKTOP_APP` | `/Applications/ChatGPT.app` | 重启目标 App（**仅 macOS 重启功能用到**） |
| `CODEX_DESKTOP_PROCESS` | `ChatGPT` | 重启目标进程名（**仅 macOS**） |
| `CODEX_ROUTER_LABEL` | 自动探测 | 中转的 launchd label（**仅 macOS**） |

---

## 让控制台写入的 AK 生效

控制台把 AK 写进 `~/.codex/router-secrets.env`（权限 600），但中转默认只 source `~/.zshrc`。要让中转用上控制台写的 AK，在中转的 launchd plist 启动命令里、`source ~/.zshrc` 之后加一行：

```
source ~/.codex/router-secrets.env 2>/dev/null;
```

然后「重启中转」。若你习惯把 AK 直接放 `~/.zshrc`，则无需此步——控制台一样能读到并掩码显示。

---

## 安全模型

- **只绑 `127.0.0.1`**：不对局域网/公网暴露，因此无需登录鉴权（「免登录」= 回环本地工具，不是裸奔上网）。
- **AK 永不进 config.toml、永不回显**：只存 `router-secrets.env`（600）与你的 shell 环境；界面只显示掩码。
- **写操作可回滚**：config.toml / catalog / auth.json 写入前都自动备份。
- **重启是破坏性操作**：重启桌面 App 会关会话，故前端二次确认；后端不主动触发。

---

## 它不是什么

- **不是聊天客户端**：用模型请回到 Codex 本体。
- **不替代中转**：它只配置，转发仍由 `codex-model-router` 做。
- **不管手机远控**：手机控制真正的 Codex 用 [`remodex`](https://github.com/Emanuele-web04/remodex)（iPhone ↔ Mac 加密配对），与本工具共用同一中转，互不冲突。

---

## 文件 / API

| 文件 | 作用 |
|---|---|
| `server.py` | 标准库 HTTP 服务：静态托管 + `/api/*` |
| `config_io.py` | tomlkit 安全读写 config/目录/密钥 + auth.json + codex 登录态 + 重启 |
| `web/` | 单页前端（`index.html`/`app.js`/`styles.css`），零依赖零构建 |
| `run.sh` / `install-service.sh` | 前台启动 / 装常驻服务 |

API：`GET /api/status`（一致性面板）`GET /api/models` `GET|POST /api/routes`（model→真实 provider 路由表，读时取自中转 `/v1/routes`）`GET|POST /api/config` `GET|POST /api/catalog` `POST /api/secrets` `POST /api/auth`（修复登录绕过）`GET|POST /api/mode`（target=custom|native|factory，即 Restore/Apply）`POST /api/restart`（target=desktop|router）。

## 许可

MIT © 2026 codex-console authors（`LICENSE` 里版权持有人可自行改成你的名字）。
