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

## 组成

| 组件 | 路径 | 作用 |
|---|---|---|
| **中转 router** | `codex-model-router.py` | 聚合多上游为单个 Responses 端点；按模型分发；passthrough / translate 两种模式 |
| **配置台 console** | `codex-console/` | 本地网页：可视化配 provider / model / AK / 登录绕过，一键重启 Codex |
| **目录生成器** | `regen-model-catalog.py` | 重建 `custom-model-catalog.json`（保留内置模型 + 追加自定义） |
| **配置去重工具** | `tools/codex-config-dedup.py` | 自愈 `config.toml` 的「重复键」解析错误（见下文） |
| **接入手册（中文）** | `多模型接入手册.md` | 原理、分步实施、协议不变式、排错 |

---

## 快速开始

> 详细步骤与原理见 [`多模型接入手册.md`](多模型接入手册.md)。

**1. 中转**：把 `codex-model-router.py` 放进 `~/.codex/`，在 `~/.codex/router-routes.json` 里登记你的 provider 与 model（schema 参考文件内 `_DEFAULT_ROUTING`），用 launchd 常驻。启动命令**务必 `source ~/.zshrc`**（或用别的方式导出密钥环境变量），否则中转拿不到 AK。

**2. Codex 指向中转**：在 `~/.codex/config.toml`：
```toml
model_provider = "router"
[model_providers.router]
name = "Local Router"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
experimental_bearer_token = "local-router-placeholder"   # 仅回环占位，不是真凭证
```

**3. 模型目录**：编辑 `regen-model-catalog.py` 的 `CUSTOM`，运行它生成 `custom-model-catalog.json`，让模型出现在 `/model`。重启桌面端。

**4. 配置台（可选但推荐）**：
```bash
cd codex-console
./run.sh              # 前台试用 → http://127.0.0.1:8420
./install-service.sh  # 或装成 launchd 常驻服务（自启 + 崩溃自拉起）
```

依赖：Python 3.9+，配置台额外需要 `tomlkit`（`run.sh` 会自动装）。

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
├── codex-model-router.py      ← 中转（核心）
├── regen-model-catalog.py     ← 模型目录生成器
├── 多模型接入手册.md            ← 接入手册（中文）
├── tools/
│   ├── codex-config-dedup.py          ← config.toml 重复键自愈器
│   └── codex-config-dedup.plist.example
└── codex-console/             ← 可视化配置台（含自己的 README）
    ├── server.py  config_io.py
    ├── web/  run.sh  install-service.sh
    └── README.md
```

## 许可

MIT © 2026 codex-multi-model authors. 见 [`LICENSE`](LICENSE)。
