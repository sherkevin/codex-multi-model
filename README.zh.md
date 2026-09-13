# codex-multi-model

**语言**: [English](README.md) | 中文

让 **OpenAI Codex**（桌面端 / CLI）同时接入**多个模型提供商**、各自用各自的密钥，并在 `/model` 里无感切换——包括那些只提供旧式 `chat/completions`、原本接不进 Codex 的模型。附带一个**本地可视化配置台**。

---

## 问题

Codex 的 `model_provider` 是**单值**：一次请求只用一个提供商，`/model` 切换只改模型*名*、从不改提供商。模型目录里也没有 provider 字段，更没有「按模型指定 provider」的入口。所以你无法原生地让模型 A 走 provider X、模型 B 走 provider Y。

雪上加霜的是，codex 0.142.5 移除了 `wire_api = "chat"`，于是只提供 `chat/completions` 的网关根本接不进来。

## 思路

用一个**本地回环中转**，让 Codex 把它当成唯一的 provider。Codex 的 `model_provider` 指向它；中转按模型名把每个请求分发到真实上游、换上各自的密钥，并在需要时做协议翻译：

```
Codex ──▶ 中转 :8317 ──按模型名分发──▶ 各真实上游（各自的 AK）
                          │
              配置台 :8420（读写 Codex 实际读取的同一批文件）
```

每个模型两种路由模式：

- **passthrough** — 上游原生支持 Responses API，原样转发。
- **translate** — 上游只有 `chat/completions`，中转做 Responses ↔ chat 双向翻译，并始终补发 `response.completed`。

---

## translate 模式下 Codex 原生能力不丢

这是把 chat-only 网关接到 Codex 上最容易悄悄坏掉的一环，所以值得单独说清楚：
**translate 模式保留 Codex 的全部原生工具。** 创建线程、多智能体、automations、
`apply_patch`、`request_user_input`、MCP 命名空间、`tool_search` 都还在 ——
你换掉的只是模型端点。

难点在于 Codex 并不把这些当普通 function 发。它用 `#[serde(tag="type")]` 把工具序列化成
五种形态，而只认 `type == "function"` 的朴素翻译会把其余四种静默丢掉：

| Responses 工具类型 | 承载什么 | 桥接怎么处理 |
|---|---|---|
| `function` | 普通工具 | 原样转发 |
| `namespace` | `create_thread`、`spawn_agent`、各 MCP 命名空间 | 摊平成 chat function |
| `custom` | `apply_patch`（freeform，参数不是 JSON） | 包成 `function(input: string)`，上行再拆回 |
| `tool_search` | 加载延迟工具的元工具 | 合成一个可调用的 function |
| `web_search` | 上游侧联网搜索 | 丢弃（见下） |

上行时每个调用都还原成 Codex 认得的 Responses item 形态 —— `tool_search_call` 的
arguments 是**对象**、`custom_tool_call` 的 input 是**裸字符串**、`function_call` 带
**独立的 `namespace` 字段**。最后这个尤其关键：有些模型即使收到裸名 `create_thread`，
也会自己拼成 `mcp__codex_app__create_thread`，而 Codex 认不出带前缀的名字，调用会被
静默丢弃。中转会把它纠回来。

已用真实抓包的 Codex 请求端到端验证：15 个 Responses 工具翻成 32 个 chat 工具；
真实上游往返正确产出 `tool_search_call`、带 namespace 的 `create_thread`、
freeform 的 `apply_patch`。

两个有意为之的边界：

- **命名空间重名不猜。** `chat/completions` 没有 namespace 字段，两个命名空间里的同名
  工具（真实例子：`mcp__node_repl.js` 与 `mcp__cua_repl.js`）摊平后会撞车，这些会加上
  命名空间前缀消歧。**歧义时绝不把裸名猜成其中一个** —— 猜错意味着在错误的 runtime 里
  执行代码，代价远高于一次明确的失败。
- **`web_search` 不桥接。** 它根本不是 function，语义是"上游服务端自己联网"，chat 网关
  普遍不提供。假装桥接只会让模型去调一个必然失败的工具，不如直接丢弃 —— 模型看不到就
  不会乱调。要联网搜索请走 MCP 搜索工具，那些本就是普通 function，天然可用。
  另外注意：`config.toml` 里的 `web_search_mode = "disabled"` **不会**移除这个工具声明，
  只是把 `external_web_access` 置为 false（已实测）。

第三方模型还常常无视"延迟工具"协议，因为它们没像 `gpt-5-codex` 那样被专门调过：
工具表里没有 = 以为没这个能力，于是转而写 shell 脚本硬凑。`ROUTER_NATIVE_HINT=1`
（默认开）会附加一小段说明点破该协议，一段话就能解锁全部延迟工具。两个开关默认都开，
且启动时会把状态打进日志。

详见 [ADR 0006](docs/adr/0006-deferred-tool-bridge.md)。

---

## 组成

| 组件 | 路径 | 作用 |
|---|---|---|
| **中转 router** | `codex-model-router.py` | 聚合多上游为单个 Responses 端点；按模型分发；passthrough / translate 两种模式 |
| **配置台 console** | `codex-console/` | 本地网页：可视化配 provider / model / AK / 登录绕过。配置读写全平台可用，「一键重启」仅 macOS |
| **目录生成器** | `regen-model-catalog.py` | 重建 `custom-model-catalog.json`（保留内置模型 + 追加自定义） |
| **配置去重工具** | `tools/codex-config-dedup.py` | 自愈 `config.toml` 的「重复键」解析错误（见下文） |
| **服务模板** | `service/` | launchd（macOS）/ systemd（Linux）/ 计划任务（Windows）三套常驻模板 |
| **Responses 探针** | `probe-responses-support.py` | 实测判定每个模型该走 passthrough 还是 translate（见 ADR 0001） |
| **接入手册（中文）** | `多模型接入手册.md` | 原理、分步实施、协议不变式、排错 |

---

## 快速开始

macOS / Linux / Windows 都能跑。硬性要求只有 Python 3.9+，中转本体纯标准库。
详细步骤与原理见 [`多模型接入手册.md`](多模型接入手册.md)。

**0. 依赖（可选但推荐）**
```bash
pip install -r requirements.txt   # Pillow（图片降档）+ tomlkit（配置台）
```
两个都不是中转运行的必要条件。没装 Pillow 时 macOS 回落到系统自带的 `sips`；
Linux/Windows 则只能按字节预算丢图、不降档。Pillow 值得装：同样尺寸同样质量下，
输出比 `sips` 小 2.5 倍、快 4.5 倍（实测于一张真实的 488KB 截图）。

**1. 中转**：在 `~/.codex/router-routes.json` 里登记你的 provider 与 model
（schema 参考 `codex-model-router.py` 里的 `_DEFAULT_ROUTING`），然后跑起来：

| 系统 | 前台试跑 | 常驻 |
|---|---|---|
| macOS | `python3 codex-model-router.py` | `service/com.example.codex-model-router.plist` → launchd |
| Linux | `python3 codex-model-router.py` | `service/codex-model-router.service.example` → `systemd --user` |
| Windows | `run-router.bat` | `service\install-windows-service.ps1` → 计划任务 |

不管用哪种方式启动，上游 AK **必须导出到那个进程的环境里**。服务管理器不会读你的
`~/.zshrc` / `~/.bashrc` / 用户配置文件，所以要么写进 unit 文件、要么写进 plist 的
`EnvironmentVariables`、要么在 Windows 上用 `setx`。AK 缺失时中转会明确回 401 并
点出是哪个变量，**绝不静默换用别的模型**。

启动时中转会把生效配置打进日志，跑一次核对一下桥接开关和每条路由的限流值：
```
bridge_deferred=True (原生延迟工具/tool_search 桥接)  native_hint=True
image_shrink=True backend=pil
  example/chat-model   translate    -> https://api.example-gateway.com/v1  ($EXAMPLE_GATEWAY_API_KEY)  body<=6,291,456B  input<983,616tok
```

**2. Codex 指向中转**：在 `~/.codex/config.toml`：
```toml
model_provider = "router"
[model_providers.router]
name = "Local Router"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
experimental_bearer_token = "local-router-placeholder"   # 仅回环占位，不是真凭证
```

**3. 模型目录**：编辑 `regen-model-catalog.py` 的 `CUSTOM`，运行它生成
`custom-model-catalog.json`，让模型出现在 `/model`。重启桌面端。

> **`context_window` 必须填上游的真实输入上限。** 这一个数字同时是界面右上角上下文
> 百分比的分母，也是 Codex 自动压缩的触发依据。填大了会引出整套配置里最糟的故障：
> 上游已经 400 拒绝，界面还显示「23%」，压缩永远不触发，线程直接卡死在
> "ran out of room"。生成器还会一并输出 `auto_compact_token_limit`（默认为窗口的
> 85%），让压缩在还有余量时就发生 —— 压缩请求自己要把整段历史发上去，触发点贴着上限
> 设就等于永远压不下去。详见 [ADR 0005](docs/adr/0005-context-window-and-compaction-trigger.md)。
>
> **每次 Codex 升级后都要重跑这个脚本**：`model_catalog_json` 是整体替换内置目录、
> 不是合并，否则新版本带来的内置模型会消失。

**4. 配置台（可选）**：
```bash
cd codex-console
./run.sh              # macOS / Linux → http://127.0.0.1:8420
python server.py      # 任意系统（需要 tomlkit）
./install-service.sh  # 仅 macOS：装成 launchd 常驻服务
```

配置台三个平台都能跑。配置读写全平台可用；两个**重启按钮仅 macOS**（内部调
`launchctl` / `killall`），其它平台会返回明确的操作指引而不是静默失败。

---

## 路由透明、可编辑

配置台把中转的路由暴露成数据（`~/.codex/router-routes.json`），所以中转不是黑盒：

- **Provider 卡** — 定义真实上游：名字、`base_url`、`key_env`（存放密钥的环境变量名）。
- **Model 卡** — 每个模型选一个 **provider**（决定走谁的额度）+ **模式**（`passthrough` / `translate`），外加显示名 / 描述 / 可见性 / 默认思考档。
- **后端模式卡** — 显示中转实时的 `/v1/routes` 表（模型 → 真实 provider → base → key 环境变量 → 模式），并自动判定该直连还是走中转：

  - 所有模型同属一个 provider、且全是 passthrough → 把 `model_provider` 直接设成那个 provider，**无需中转**；
  - 模型跨多个 provider，或有任何一个需要 translate → **必须走中转**（受 Codex 单 `model_provider` 限制）。

---

## 绕过账号登录

Codex 的登录门槛只检查 `~/.codex/auth.json` 处于 `apikey` 模式且密钥非空。一个占位字符串就能过这道门；真正的模型鉴权由中转用各上游的密钥完成，与这个占位无关。若 `codex logout` 或升级把它重置了，配置台有「修复登录绕过」按钮可一键还原。

---

## `tools/codex-config-dedup.py` — 配置自愈器

如果 `~/.codex/config.toml` 出现**重复键**（TOML 解析错误），Codex 会拒绝启动。常见成因：hook 信任状态（`[hooks.state."…"]`）被写了两次——比如一次由 Codex 自己的配置序列化器写、一次由某个第三方 hook 安装器写——而且用了两种等价语法（`[hooks.state."K"]` 与 `["hooks"."state"."K"]`），TOML 视它们为同一个键。

本工具检测到 `config.toml` 无法解析时，删除多余的重复定义（每个键保留一份有效副本，因此 hook 信任不丢），再原子写回并备份。**它只在文件解析失败时动手；健康文件绝不被碰。** 可手动运行，或安装随附的 launchd 模板（`tools/codex-config-dedup.plist.example`），让它在每次 `config.toml` 变动时自动触发。

---

## 安全

- **密钥永不落配置文件**：真实 AK 只在环境变量 / `~/.codex/router-secrets.env`（chmod 600）；`config.toml` 只存变量*名*。配置台界面只显示掩码，绝不回显明文。
- **只绑回环**：中转与配置台都只监听 `127.0.0.1`，不对网络暴露，因此无需登录鉴权。
- **改配置可回滚**：`config.toml` / 目录 / `auth.json` / 路由文件写入前都自动备份 `*.bak-<时间戳>`。
- **不静默换模型**：未知模型直接报错，绝不偷偷回落到别的模型回答。

> 本仓库内的所有 provider、模型名、端点与标识均为**通用占位示例**（`example-gateway` / `example/*`），不含任何真实凭证或内部主机。

---

## 仓库结构

```
.
├── README.md / README.zh.md   ← English / 本文件（中文）
├── LICENSE                    ← MIT
├── requirements.txt           ← 可选依赖（Pillow、tomlkit）
├── codex-model-router.py      ← 中转（核心）
├── regen-model-catalog.py     ← 模型目录生成器
├── probe-responses-support.py ← passthrough/translate 探针（四级递进，见 ADR 0001）
├── 多模型接入手册.md            ← 接入手册（中文）
├── run-router.bat             ← Windows 前台启动脚本
├── service/                   ← 常驻模板（macOS / Linux / Windows）
│   ├── com.example.codex-model-router.plist
│   ├── codex-model-router.service.example
│   └── install-windows-service.ps1
├── docs/adr/                  ← 架构决议记录（"为什么这么做"）
├── tools/
│   ├── codex-config-dedup.py          ← config.toml 重复键自愈器
│   ├── codex-config-dedup.plist.example
│   ├── codex-threads.py               ← 从桌面端数据库列出 Codex thread id
│   └── happy-patch.py                 ← 给 Happy CLI 打补丁，使其在中转环境下可用
└── codex-console/             ← 可视化配置台（含自己的 README）
    ├── server.py  config_io.py
    ├── web/  run.sh  install-service.sh
    └── README.md
```

## 配置参考

所有东西都从 `~/.codex/router-routes.json` 和环境变量读取 —— 仓库里没有密钥，加 provider
也不需要改代码。

**每条路由的限流值**（`router-routes.json` 里模型条目的可选字段）：

| 字段 | 含义 | 默认 |
|---|---|---|
| `body_byte_limit` | 上游请求体字节硬顶 | `6291456` |
| `input_token_limit` | 上游输入 token 硬顶 | `0`（不设） |

只要你的网关有 token 硬顶，就把 `input_token_limit` 填上。它决定压缩请求能有多大
（从而保证压缩一定压得下去），也让中转能区分「真超限」与「上游瞬时抖动」。
两个字段也可以写在 provider 上，对该 provider 下所有模型生效。

**环境变量**（全部可选）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `CODEX_HOME` | `~/.codex` | 路由/密钥/配置所在目录 |
| `CODEX_ROUTER_PORT` | `8317` | 中转监听端口 |
| `ROUTER_BRIDGE_DEFERRED` | `1` | 桥接 Codex 原生延迟工具（`create_thread`、多智能体、`apply_patch`、`tool_search`）。**这就是原生能力的开关。** 设 `0` 退回只转发普通 function |
| `ROUTER_NATIVE_HINT` | `1` | 告诉第三方模型「延迟工具」协议的存在，需与桥接同开 |
| `ROUTER_IMAGE_BACKEND` | 自动 | `pil` / `sips` / `none`，图片降档编码器 |
| `ROUTER_IMAGE_SHRINK` | `1` | `0` 完全关闭降档 |
| `ROUTER_BODY_BYTE_LIMIT` | `6291456` | `body_byte_limit` 的全局兜底 |
| `ROUTER_INPUT_LIMIT` | `0` | `input_token_limit` 的全局兜底 |
| `ROUTER_OVERLIMIT_SIG` | `range of input length` | 上游「输入超限」的错误签名 |
| `ROUTER_TOOLARGE_SIG` | `max bytes to request body` | 上游「请求体过大」的错误签名 |
| `ROUTER_RETRYABLE_SIG` | 见源码 | 逗号分隔的瞬时错误签名 |
| `ROUTER_LOG_FILE` | 未设（只写 stderr） | 同时把日志追加到这个文件 —— Windows 计划任务必需 |
| `ROUTER_DUMP_REQUESTS` | 未设 | `1` 时把每个请求体落盘，便于排查 |
| `ROUTER_DUMP_PATH` | `<临时目录>/codex-router-last-request.json` | 落盘到哪 |

如果你的网关报错措辞不同，**改错误签名是第一件要试的事**：签名对不上，中转就没法把它
翻译成 `context_length_exceeded`，Codex 的原生压缩也就唤不醒。

## 许可

MIT © 2026 codex-multi-model authors. 见 [`LICENSE`](LICENSE)。
