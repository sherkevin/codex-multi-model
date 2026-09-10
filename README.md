# codex-multi-model

让 **OpenAI Codex**（桌面端 / CLI）同时接入**多个模型提供商**、各自用各自的密钥，并在 `/model` 里无感切换——包括那些只提供旧式 `chat/completions`、原本接不进 Codex 的模型。附带一个**本地可视化配置台**。

Codex 的 `model_provider` 是**单值**：一次请求只用一个提供商，`/model` 切换只改模型名、不改提供商。本项目用一个**本地回环中转**绕过这个限制：Codex 只认这一个中转，中转按模型名把请求分发到不同真实上游、换上各自的 AK，并在需要时做 Responses ↔ chat 协议翻译。

---

## 组成

| 组件 | 路径 | 作用 |
|---|---|---|
| **中转 router** | `codex-model-router.py` | 聚合多上游为单个 Responses 端点；按模型分发；passthrough / translate 两种模式 |
| **配置台 console** | `codex-console/` | 本地网页：可视化配 provider / model / AK / 登录绕过，一键重启 Codex |
| **目录生成器** | `regen-model-catalog.py` | 重建 `custom-model-catalog.json`（保留内置模型 + 追加自定义） |
| **接入手册** | `多模型接入手册.md` | 原理、分步实施、协议不变式、排错（通用版，已脱敏） |

数据流：

```
Codex 桌面端/CLI ──▶ 中转 :8317 ──按模型名分发──▶ 各真实上游（各自的 AK）
                          ▲
              配置台 :8420 ┘（读写 ~/.codex 下同一批配置文件）
```

---

## 快速开始

> 详细步骤、原理与排错见 [`多模型接入手册.md`](多模型接入手册.md)。

**1. 中转**：把 `codex-model-router.py` 放进 `~/.codex/`，在 `~/.codex/router-routes.json` 里登记你的 provider 与 model（参考文件内 `_DEFAULT_ROUTING` 示例），用 launchd 常驻（启动命令里务必 `source ~/.zshrc`，否则拿不到 AK 环境变量）。

**2. Codex 指向中转**：`~/.codex/config.toml` 设 `model_provider = "router"`，并定义 `[model_providers.router]`（`base_url = http://127.0.0.1:8317/v1`，`wire_api = "responses"`）。

**3. 模型目录**：编辑 `regen-model-catalog.py` 的 `CUSTOM`，运行它生成 `custom-model-catalog.json`，让模型出现在 `/model`。

**4. 配置台（可选但推荐）**：
```bash
cd codex-console
./run.sh              # 前台试用 → http://127.0.0.1:8420
./install-service.sh  # 或装成 launchd 常驻服务
```

依赖：Python 3.9+，配置台额外需要 `tomlkit`（`run.sh` 会自动装）。

---

## 安全

- **密钥永不落配置文件**：真实 AK 只在环境变量 / `~/.codex/router-secrets.env`（chmod 600）；`config.toml` 只存变量名。配置台界面只显示掩码，绝不回显明文。
- **只绑回环**：中转与配置台都只监听 `127.0.0.1`，不对网络暴露，因此无需登录鉴权。
- **改配置可回滚**：`config.toml` / 目录 / `auth.json` / 路由文件写入前都自动备份 `*.bak-<时间戳>`。
- **不静默换模型**：未知模型直接报错，绝不偷偷回落到别的模型回答。

> 本仓库内的默认路由、模型名、provider 均为**占位示例**（`example-gateway` / `example/*`），不含任何真实端点或凭证。

---

## 仓库结构

```
.
├── README.md                  ← 本文件
├── LICENSE                    ← MIT
├── codex-model-router.py      ← 中转（核心）
├── regen-model-catalog.py     ← 模型目录生成器
├── 多模型接入手册.md            ← 原理 / 实施 / 排错手册
└── codex-console/             ← 可视化配置台（含自己的 README）
    ├── server.py  config_io.py
    ├── web/  run.sh  install-service.sh
    └── README.md
```

## 许可

MIT © 2026 codex-multi-model authors. 见 [`LICENSE`](LICENSE)。
