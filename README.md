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

## Native Codex capabilities survive `translate` mode

This is the part that quietly breaks when you bolt a chat-only gateway onto Codex, so it
is worth stating explicitly: **`translate` mode keeps Codex's native tools working.**
You keep thread creation, multi-agent, automations, `apply_patch`, `request_user_input`,
MCP namespaces and `tool_search` — you are only swapping the model endpoint.

The problem is that Codex does not send those as plain functions. It serializes tools
with `#[serde(tag="type")]` into five shapes, and a naive translator that only
understands `type == "function"` silently drops the rest:

| Responses tool type | What it carries | Bridge does |
|---|---|---|
| `function` | ordinary tools | passed through |
| `namespace` | `create_thread`, `spawn_agent`, MCP namespaces | flattened to chat functions |
| `custom` | `apply_patch` (freeform, not JSON) | wrapped as `function(input: string)`, unwrapped on the way back |
| `tool_search` | the meta-tool that loads deferred tools | synthesized as a callable function |
| `web_search` | upstream-side web access | dropped (see below) |

On the way back up, each call is restored to the exact Responses item shape Codex
expects — `tool_search_call` with object arguments, `custom_tool_call` with a bare
string input, and `function_call` with a **separate `namespace` field**. That last one
matters: some models invent `mcp__codex_app__create_thread` even when sent the bare
name, and Codex does not recognize prefixed names, so the call gets silently discarded.
The router normalizes them back.

Verified end-to-end against a real captured Codex request: 15 Responses tools become 32
chat tools, and a real upstream round-trip produces correct `tool_search_call`,
namespaced `create_thread`, and freeform `apply_patch` items.

Two deliberate limits:

- **Namespace name collisions.** `chat/completions` has no namespace field, so two
  namespaces containing the same tool name (real example: `mcp__node_repl.js` and
  `mcp__cua_repl.js`) would collide. Those get prefixed with their namespace. A bare
  ambiguous name is never guessed — executing in the wrong runtime is worse than a
  clear failure.
- **`web_search` is not bridged.** It is not a function at all; its semantics are "the
  upstream performs the search server-side", and chat gateways generally do not offer
  that. Pretending otherwise would just make the model call something that cannot work.
  Use an MCP search tool instead — those are ordinary functions and work normally.
  Note `web_search_mode = "disabled"` in `config.toml` does *not* remove the tool spec;
  it only flips `external_web_access` to false.

Third-party models also tend to ignore the deferred-tool protocol, since they were not
tuned for it the way `gpt-5-codex` was: no tool in the list reads as "no such
capability", so they improvise with shell scripts. `ROUTER_NATIVE_HINT=1` (default)
appends a short note naming that protocol, which is enough to unlock the whole deferred
set. Both switches are on by default and log their state at startup.

---

## Components

| Component | Path | Role |
|---|---|---|
| **Router** | `codex-model-router.py` | Aggregates upstreams behind one Responses endpoint; per-model dispatch; passthrough/translate |
| **Config console** | `codex-console/` | Local web UI: configure providers / models / keys / login bypass. Cross-platform for config; one-click restart is macOS-only |
| **Catalog generator** | `regen-model-catalog.py` | Rebuilds `custom-model-catalog.json` (keeps builtins, appends custom) |
| **Config dedup tool** | `tools/codex-config-dedup.py` | Self-heals `config.toml` duplicate-key parse errors (see below) |
| **Service templates** | `service/` | launchd (macOS) / systemd (Linux) / Scheduled Task (Windows) templates to run the router always-on |
| **Responses probe** | `probe-responses-support.py` | Empirically decides passthrough vs translate per model (see ADR 0001) |
| **Byte-limit probe** | `probe-body-byte-limit.py` | Measures the upstream's real request-body cap and *how it counts bytes* (see ADR 0009) |
| **Setup guide (zh)** | `多模型接入手册.md` | Principles, step-by-step, protocol invariants, troubleshooting |

---

## Quick start

Works on macOS, Linux and Windows. Python 3.9+ is the only hard requirement; the
router itself is stdlib-only. Full steps and rationale in
[`多模型接入手册.md`](多模型接入手册.md) (Chinese).

**0. Dependencies (optional but recommended)**
```bash
pip install -r requirements.txt   # Pillow (image downscaling) + tomlkit (console)
```
Neither is required for the router to run. Without Pillow, macOS falls back to the
built-in `sips`; on Linux/Windows images are then only dropped by byte budget rather
than downscaled. Pillow is worth installing: same dimensions and quality, but 2.5x
smaller output and 4.5x faster than `sips` (measured on a real 488 KB screenshot).

**1. Router** — register your upstreams and models in `~/.codex/router-routes.json`
(schema documented at `_DEFAULT_ROUTING` in `codex-model-router.py`), then run it:

| OS | Foreground | Always-on |
|---|---|---|
| macOS | `python3 codex-model-router.py` | `service/com.example.codex-model-router.plist` → launchd |
| Linux | `python3 codex-model-router.py` | `service/codex-model-router.service.example` → `systemd --user` |
| Windows | `run-router.bat` | `service\install-windows-service.ps1` → Scheduled Task |

Whichever way you start it, the upstream keys **must be exported into that process's
environment**. Service managers do not read your `~/.zshrc` / `~/.bashrc` / user
profile, so put the keys in the unit file, the plist's `EnvironmentVariables`, or use
`setx` on Windows. A missing key produces an explicit 401 naming the variable — the
router never silently substitutes a different model.

On startup the router logs its effective configuration; check it once to confirm the
bridge and per-route limits came out as intended:
```
bridge_deferred=True (原生延迟工具/tool_search 桥接)  native_hint=True
image_shrink=True backend=pil
  example/chat-model   translate    -> https://api.example-gateway.com/v1  ($EXAMPLE_GATEWAY_API_KEY)  body<=6,291,456B  input<983,616tok
```

**2. Point Codex at the router** — in `~/.codex/config.toml`:
```toml
model_provider = "router"
[model_providers.router]
name = "Local Router"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
experimental_bearer_token = "local-router-placeholder"   # loopback only, not a credential
```

**3. Model catalog** — edit `CUSTOM` in `regen-model-catalog.py`, run it to generate
`custom-model-catalog.json`, so your models appear in `/model`. Restart the desktop app.

> **Set `context_window` to your upstream's real input limit.** That single number is
> both the denominator of the context-percentage readout *and* what triggers Codex's
> auto-compaction. Overstating it produces the worst failure mode in this whole setup:
> the upstream is already rejecting with 400 while the UI still reads "23%", compaction
> never fires, and the thread deadlocks with "ran out of room". The generator also emits
> `auto_compact_token_limit` (85% of the window by default) so compaction happens while
> there is still headroom — the compaction request itself has to carry the whole history,
> so setting it flush against the limit means it can never succeed.
>
> Re-run this script after **every** Codex upgrade: `model_catalog_json` *replaces* the
> builtin catalog rather than merging, so new builtin models disappear otherwise.

**4. Config console (optional)**
```bash
cd codex-console
./run.sh              # macOS / Linux → http://127.0.0.1:8420
python server.py      # any OS (needs tomlkit)
./install-service.sh  # macOS only: install as a launchd service
```

The console runs on all three platforms. Config reads and writes work everywhere; the
two **restart buttons are macOS-only** (they shell out to `launchctl` / `killall`). On
other platforms they return explicit instructions instead of failing silently.

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

## Tests

```bash
python3 tests/test_body_byte_limit.py
```

Stdlib only, no network, no upstream quota consumed — it starts a mock upstream that
enforces the limit the way a real one does (counting escaped bytes) and drives a real
router process against it. It covers the two byte-guard bugs documented in ADR 0009:
the measurement convention, and the offload loop that used to discard its own result.
Run it after changing anything in the byte guard.

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
├── requirements.txt           ← optional deps (Pillow, tomlkit)
├── codex-model-router.py      ← the proxy (core)
├── regen-model-catalog.py     ← model catalog generator
├── probe-responses-support.py ← passthrough-vs-translate probe (4-stage, see ADR 0001)
├── probe-body-byte-limit.py   ← measures the upstream's real body cap + counting convention (ADR 0009)
├── 多模型接入手册.md            ← setup guide (zh)
├── run-router.bat             ← Windows foreground launcher
├── service/                   ← always-on templates (macOS / Linux / Windows)
│   ├── com.example.codex-model-router.plist
│   ├── codex-model-router.service.example
│   └── install-windows-service.ps1
├── docs/adr/                  ← architecture decision records (the "why")
├── tests/
│   └── test_body_byte_limit.py ← regression tests for the byte guard (stdlib only, no upstream calls)
├── tools/
│   ├── codex-config-dedup.py          ← config.toml duplicate-key self-healer
│   ├── codex-config-dedup.plist.example
│   ├── codex-threads.py               ← list Codex thread ids from the desktop DB
│   └── happy-patch.py                 ← patch the Happy CLI for use behind this proxy
└── codex-console/             ← visual config console (has its own README)
    ├── server.py  config_io.py
    ├── web/  run.sh  install-service.sh
    └── README.md
```

## Configuration reference

Everything is read from `~/.codex/router-routes.json` and environment variables — no
secrets in the repo, no code edits needed to add a provider.

**Per-route limits** (optional fields on a model entry in `router-routes.json`):

| Field | Meaning | Default |
|---|---|---|
| `body_byte_limit` | upstream hard cap on request body bytes, **measured the way the upstream measures it** (see below) | `4718592` |
| `input_token_limit` | upstream hard cap on input tokens | `0` = unset |

Set `input_token_limit` whenever your gateway enforces one. It caps the size of the
compaction request so compaction can always succeed, and it lets the router tell a real
over-limit response from transient flakiness. Both fields can also be set on a provider
to apply to all its models.

> **Measure `body_byte_limit`, do not read it off the error message.** Two things bite
> here, both found the hard way (ADR 0009):
>
> 1. The number in `Exceeded limit on max bytes to request body : 6291456` is *not* the
>    threshold that actually rejects your request. Measured on that same gateway, the real
>    limit was `4,718,592` B (4.5 MiB) — 25% lower. The advertised figure belongs to an
>    outer layer; the model layer is what actually refuses.
> 2. The upstream counts **ASCII-escaped** bytes, not the UTF-8 bytes we send. A CJK
>    character is 3 B in UTF-8 but 6 B as `\uXXXX`. Bisecting with fills of different
>    language mixes shows the sendable-bytes threshold falling to exactly half for pure
>    CJK, while the escaped-bytes threshold stays constant within ±1.7%.
>
> The router budgets in escaped bytes to match. Sending with `ensure_ascii=False` still
> halves your bandwidth — it just does not buy back any room against this limit.
> Run `probe-body-byte-limit.py` to measure both the convention and the threshold for
> your own gateway; it prints a `body_byte_limit` value you can paste straight in.

**Environment variables** (all optional):

| Variable | Default | Effect |
|---|---|---|
| `CODEX_HOME` | `~/.codex` | where routes/secrets/config live |
| `CODEX_ROUTER_PORT` | `8317` | router listen port |
| `ROUTER_BRIDGE_DEFERRED` | `1` | bridge Codex's native deferred tools (`create_thread`, multi-agent, `apply_patch`, `tool_search`). **This is what makes native capabilities work.** Set `0` to forward plain functions only |
| `ROUTER_NATIVE_HINT` | `1` | tell third-party models the deferred-tool protocol exists. Only useful with the bridge on |
| `ROUTER_IMAGE_BACKEND` | auto | `pil` / `sips` / `none` — image downscaling encoder |
| `ROUTER_IMAGE_SHRINK` | `1` | `0` disables downscaling entirely |
| `ROUTER_BODY_BYTE_LIMIT` | `4718592` | global fallback for `body_byte_limit` (escaped bytes — measure yours, see above) |
| `ROUTER_BODY_BYTE_BUDGET_RATIO` | `0.92` | budget = limit × ratio; the slack absorbs upstream threshold jitter (measured ±1.7%) |
| `ROUTER_BODY_OFFLOAD_MAX` | `16` | max in-place offload rounds after a `TooLarge`. Counted separately from `ROUTER_MAX_RETRY` so an offloaded body is always re-sent |
| `ROUTER_BODY_OFFLOAD_DECAY` | `0.88` | budget multiplier per offload round, so one round usually converges |
| `ROUTER_INPUT_LIMIT` | `0` | global fallback for `input_token_limit` |
| `ROUTER_OVERLIMIT_SIG` | `range of input length` | upstream "input too long" signature |
| `ROUTER_TOOLARGE_SIG` | `max bytes to request body` | upstream "body too large" signature |
| `ROUTER_RETRYABLE_SIG` | see source | comma-separated transient-error signatures |
| `ROUTER_LOG_FILE` | unset (stderr) | also append logs here — needed on Windows services |
| `ROUTER_DUMP_REQUESTS` | unset | `1` dumps each request body for debugging |
| `ROUTER_DUMP_PATH` | `<tmp>/codex-router-last-request.json` | where that dump goes |

Changing the error signatures is the first thing to try if your gateway reports
over-limit differently: without a matching signature the router cannot translate the
error into the `context_length_exceeded` that wakes Codex's compaction.

Both numbers that decide *how* the router shrinks a request are properties of your
upstream, not of this code, and both are measurable:

| Probe | Question it answers | Feeds |
|---|---|---|
| `probe-responses-support.py` | can the upstream eat Codex's real Responses request? | `mode`: passthrough vs translate |
| `probe-body-byte-limit.py` | how many body bytes does it actually accept, and counted how? | `body_byte_limit` |

## License

MIT © 2026 codex-multi-model authors. See [`LICENSE`](LICENSE).
