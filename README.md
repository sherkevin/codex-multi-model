# codex-multi-model

Use **multiple model providers** in OpenAI Codex (desktop app / CLI), each with its own API key, and switch between them seamlessly in `/model` — including chat-only gateways that Codex normally can't talk to. Ships with a **local visual config console**.

**Language**: English | [中文](README.zh.md)

---

## The problem

Codex's `model_provider` is a **single value**: every request uses one provider, and the `/model` picker only changes the model *name*, never the provider. The model catalog carries no provider field, and there's no per-model provider override. So you can't natively point model A at provider X and model B at provider Y.

On top of that, codex 0.142.5 dropped `wire_api = "chat"`, so gateways that only expose `chat/completions` can't be attached at all.

## The approach

A **loopback proxy** that Codex sees as the one provider. Codex points `model_provider` at it; the proxy dispatches each model to its real upstream with the right key, and translates protocols when needed:

```
Codex ──▶ proxy :8317 ──dispatch by model──▶ real upstreams (each with its own key)
                          │
              config console :8420 (reads/writes the same files Codex reads)
```

Two routing modes per model:

- **passthrough** — upstream natively speaks the Responses API; forwarded as-is.
- **translate** — upstream only has `chat/completions`; the proxy converts Responses ↔ chat both ways and always emits `response.completed`.

---

## Components

| Component | Path | Role |
|---|---|---|
| **Router** | `codex-model-router.py` | Aggregates upstreams behind one Responses endpoint; per-model dispatch; passthrough/translate |
| **Config console** | `codex-console/` | Local web UI: configure providers / models / keys / login bypass, one-click Codex restart |
| **Catalog generator** | `regen-model-catalog.py` | Rebuilds `custom-model-catalog.json` (keeps builtins, appends custom) |
| **Config dedup tool** | `tools/codex-config-dedup.py` | Self-heals `config.toml` duplicate-key parse errors (see below) |
| **Setup guide (zh)** | `多模型接入手册.md` | Principles, step-by-step, protocol invariants, troubleshooting |

---

## Quick start

> Full steps and rationale in [`多模型接入手册.md`](多模型接入手册.md) (Chinese).

**1. Router** — put `codex-model-router.py` in `~/.codex/`, register your providers and models in `~/.codex/router-routes.json` (see `_DEFAULT_ROUTING` in the file for the schema), and run it under launchd. The launch command **must `source ~/.zshrc`** (or otherwise export your key env vars), or the proxy won't see the keys.

**2. Point Codex at the router** — in `~/.codex/config.toml`:
```toml
model_provider = "router"
[model_providers.router]
name = "Local Router"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
experimental_bearer_token = "local-router-placeholder"   # loopback only, not a credential
```

**3. Model catalog** — edit `CUSTOM` in `regen-model-catalog.py`, run it to generate `custom-model-catalog.json`, so your models appear in `/model`. Restart the desktop app.

**4. Config console (optional but recommended)**
```bash
cd codex-console
./run.sh              # foreground → http://127.0.0.1:8420
./install-service.sh  # or install as a launchd service (auto-start, auto-restart)
```

Requirements: Python 3.9+. The console additionally needs `tomlkit` (`run.sh` installs it).

---

## Provider / model routing is transparent and editable

The console exposes the proxy's routing as data (`~/.codex/router-routes.json`), so the proxy is not a black box:

- **Provider card** — define real upstreams: name, `base_url`, `key_env` (the env var holding the key).
- **Model card** — per model pick a **provider** (whose quota it uses) + **mode** (`passthrough` / `translate`), plus display name / description / visibility / default reasoning level.
- **Backend mode card** — shows the proxy's live `/v1/routes` table (model → real provider → base → key env → mode) and auto-decides direct vs proxy:

  - all models on one provider, all passthrough → set `model_provider` directly to that provider, **no proxy needed**;
  - models span providers, or any needs translate → **proxy required** (Codex's single-`model_provider` constraint).

---

## Bypassing account login

Codex's login gate only checks that `~/.codex/auth.json` is in `apikey` mode with any non-empty key. A placeholder string passes the gate; real model auth is done by the proxy with each upstream's key, unrelated to this placeholder. The console has a "repair login bypass" button to restore it if `codex logout` or an upgrade resets it.

---

## `tools/codex-config-dedup.py` — config self-healer

Codex refuses to start if `~/.codex/config.toml` has a **duplicate key** (a TOML parse error). A common cause: hook-trust state (`[hooks.state."…"]`) getting written twice — e.g. once by Codex's own config serializer and once by a third-party hook installer — in two equivalent syntaxes (`[hooks.state."K"]` vs `["hooks"."state"."K"]`), which TOML treats as the same key.

This tool detects an unparseable `config.toml`, removes the redundant duplicate definitions (keeping one valid copy of each key, so hook trust is preserved), and atomically rewrites with a backup. **It only acts when the file fails to parse; a healthy file is never touched.** Run it manually, or install the bundled launchd template (`tools/codex-config-dedup.plist.example`) to run it on every `config.toml` change.

---

## Security

- **Keys never touch config files**: real keys live only in env vars / `~/.codex/router-secrets.env` (chmod 600). `config.toml` stores only variable *names*. The console shows keys masked and never echoes plaintext.
- **Loopback only**: both proxy and console bind `127.0.0.1` — not exposed to the network, hence no auth needed.
- **Reversible writes**: `config.toml` / catalog / `auth.json` / routes are backed up (`*.bak-<timestamp>`) before every write.
- **No silent model substitution**: an unknown model errors out instead of quietly falling back to a different model.

> All providers, model slugs, endpoints and labels in this repo are **genericized placeholders** (`example-gateway`, `example/*`). No real credentials or internal hosts.

---

## Repository layout

```
.
├── README.md / README.zh.md   ← this file (EN) / 中文
├── LICENSE                    ← MIT
├── codex-model-router.py      ← the proxy (core)
├── regen-model-catalog.py     ← model catalog generator
├── 多模型接入手册.md            ← setup guide (zh)
├── tools/
│   ├── codex-config-dedup.py          ← config.toml duplicate-key self-healer
│   └── codex-config-dedup.plist.example
└── codex-console/             ← visual config console (has its own README)
    ├── server.py  config_io.py
    ├── web/  run.sh  install-service.sh
    └── README.md
```

## License

MIT © 2026 codex-multi-model authors. See [`LICENSE`](LICENSE).
