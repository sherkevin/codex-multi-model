#!/usr/bin/env python3
"""config_io — 安全读写 Codex 配置、模型目录与密钥。

设计原则：
  * config.toml 用 tomlkit 读写，**逐字符保留用户手写的注释与格式**。
  * 真实密钥绝不写进 config.toml；它只存 env_key 名字。密钥值落在
    ~/.codex/router-secrets.env（chmod 600），由中转在启动时 source。
  * 任何写入前都先备份成 *.bak-<时间戳>，写坏了能回滚。
"""
import json
import os
import re
import shutil
import subprocess
import time

try:
    import tomlkit
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "缺少依赖 tomlkit。请先安装：pip3 install tomlkit  （或 uv pip install tomlkit）"
    ) from e

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
SECRETS_PATH = os.path.join(CODEX_HOME, "router-secrets.env")

# 顶层可编辑标量字段（白名单，避免误改无关项）
TOP_SCALARS = ["model", "model_provider", "model_reasoning_effort", "review_model"]


# ─────────────────────────── 备份 ───────────────────────────

def _backup(path):
    if os.path.exists(path):
        bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, bak)
        return bak
    return None


def _atomic_write(path, text, chmod=None):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if chmod is not None:
        os.chmod(tmp, chmod)
    os.replace(tmp, path)


# ─────────────────────────── config.toml ───────────────────────────

def load_config_doc():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return tomlkit.parse(f.read())


def read_config():
    """返回给前端的配置摘要（不含任何密钥明文）。"""
    doc = load_config_doc()
    providers = {}
    mp = doc.get("model_providers") or {}
    for name, p in mp.items():
        providers[name] = {
            "name": p.get("name", ""),
            "base_url": p.get("base_url", ""),
            "env_key": p.get("env_key", ""),
            "wire_api": p.get("wire_api", ""),
            "experimental_bearer_token": p.get("experimental_bearer_token", ""),
        }
    desktop = doc.get("desktop") or {}
    return {
        "top": {k: doc.get(k) for k in TOP_SCALARS},
        "model_catalog_json": doc.get("model_catalog_json", ""),
        "providers": providers,
        "enabled_reasoning_efforts": list(desktop.get("enabled-reasoning-efforts") or []),
        "service_tier": doc.get("service_tier", ""),
    }


def write_config(edits):
    """把前端的编辑应用到 config.toml，保留注释。edits 结构见 README。"""
    doc = load_config_doc()

    for k in TOP_SCALARS:
        if k in edits and edits[k] is not None:
            doc[k] = edits[k]

    if "model_catalog_json" in edits:
        doc["model_catalog_json"] = edits["model_catalog_json"]

    # provider 增改
    if "providers" in edits:
        if "model_providers" not in doc:
            doc["model_providers"] = tomlkit.table()
        mp = doc["model_providers"]
        for pname, pv in edits["providers"].items():
            if pv.get("_delete"):
                if pname in mp:
                    del mp[pname]
                continue
            if pname not in mp:
                mp[pname] = tomlkit.table()
            tbl = mp[pname]
            for fld in ("name", "base_url", "env_key", "wire_api",
                        "experimental_bearer_token"):
                if fld in pv and pv[fld] is not None:
                    tbl[fld] = pv[fld]

    # 思考档按钮集合
    if "enabled_reasoning_efforts" in edits:
        if "desktop" not in doc:
            doc["desktop"] = tomlkit.table()
        doc["desktop"]["enabled-reasoning-efforts"] = edits["enabled_reasoning_efforts"]

    bak = _backup(CONFIG_PATH)
    _atomic_write(CONFIG_PATH, tomlkit.dumps(doc))
    return {"ok": True, "backup": bak}


# ─────────────────────────── 模型目录 ───────────────────────────

def _catalog_path():
    cfg = load_config_doc()
    p = cfg.get("model_catalog_json")
    return os.path.expanduser(p) if p else os.path.join(CODEX_HOME, "custom-model-catalog.json")


def read_catalog():
    path = _catalog_path()
    if not os.path.exists(path):
        return {"path": path, "models": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    slim = []
    for m in data.get("models", []):
        slim.append({
            "slug": m.get("slug"),
            "display_name": m.get("display_name"),
            "description": m.get("description"),
            "visibility": m.get("visibility"),
            "context_window": m.get("context_window"),
            "default_reasoning_level": m.get("default_reasoning_level"),
            "supported_reasoning_levels": [
                lv.get("effort") for lv in (m.get("supported_reasoning_levels") or [])
            ],
        })
    return {"path": path, "models": slim}


def write_catalog(edits):
    """仅改显示层字段（display_name/description/visibility/默认档），结构其余原样保留。"""
    path = _catalog_path()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    by_slug = {m.get("slug"): m for m in data.get("models", [])}
    for e in edits.get("models", []):
        m = by_slug.get(e.get("slug"))
        if not m:
            continue
        for fld in ("display_name", "description", "visibility",
                    "default_reasoning_level", "context_window"):
            if fld in e and e[fld] is not None:
                m[fld] = e[fld]
    bak = _backup(path)
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False), chmod=0o600)
    return {"ok": True, "backup": bak}


# ─────────────────────────── 密钥 ───────────────────────────

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _parse_secrets_file():
    out = {}
    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#"):
                    continue
                m = _ENV_LINE.match(line)
                if m:
                    out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _mask(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}{'•' * 6}{value[-4:]}"


def read_secret_status(names):
    """对给定 env_key 名字，报告是否已设置 + 掩码尾号。优先看进程环境，再看 secrets 文件。"""
    file_secrets = _parse_secrets_file()
    status = {}
    for n in names:
        env_val = os.environ.get(n)
        file_val = file_secrets.get(n)
        val = env_val or file_val
        status[n] = {
            "set": bool(val),
            "source": "environment" if env_val else ("secrets-file" if file_val else None),
            "masked": _mask(val) if val else "",
        }
    return status


def write_secret(name, value):
    """把 KEY=VALUE 写进 ~/.codex/router-secrets.env（chmod 600）。绝不回显、绝不入 config。"""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError("非法的环境变量名")
    secrets = _parse_secrets_file()
    secrets[name] = value
    _backup(SECRETS_PATH)
    lines = [f"{k}={v}" for k, v in secrets.items()]
    body = ("# 由 codex-console 管理的密钥。中转启动时 source 本文件。\n"
            "# 真实密钥只存这里与你的 shell 环境，绝不写入 config.toml。\n"
            + "\n".join(lines) + "\n")
    _atomic_write(SECRETS_PATH, body, chmod=0o600)
    return {"ok": True, "masked": _mask(value)}


# ─────────────────────────── 登录绕过 / auth.json ───────────────────────────

AUTH_PATH = os.path.join(CODEX_HOME, "auth.json")
# Codex 桌面端就是 ChatGPT.app；codex app-server 是它的子进程，重启它即重启 Codex 桌面端。
CODEX_APP = os.environ.get("CODEX_DESKTOP_APP", "/Applications/ChatGPT.app")
APP_PROCESS = os.environ.get("CODEX_DESKTOP_PROCESS", "ChatGPT")
# 登录门只校验 auth.json 有 apikey 模式 + 任意非空 key；用占位串即可过门，
# 真实模型鉴权由中转各自的 AK 完成，与这个 key 无关。
LOGIN_PLACEHOLDER = "sk-local-router-placeholder"


def _codex_bin():
    return shutil.which("codex") or "/opt/homebrew/bin/codex"


def read_auth():
    """读 auth.json，只回掩码，绝不回明文 key。"""
    if not os.path.exists(AUTH_PATH):
        return {"exists": False, "auth_mode": None, "has_key": False}
    try:
        d = json.load(open(AUTH_PATH, "r", encoding="utf-8"))
    except Exception as e:
        return {"exists": True, "error": str(e)}
    key = d.get("OPENAI_API_KEY") or ""
    return {"exists": True, "auth_mode": d.get("auth_mode"),
            "has_key": bool(key), "key_hint": _mask(key) if key else "",
            "has_oauth_tokens": "tokens" in d}


def ensure_auth_bypass():
    """确保 auth.json 处于 apikey 模式（绕过 ChatGPT 账号登录）。
    保守：已存在 key 则保留，只在缺失/模式不对时修复；改前备份。"""
    cur = read_auth()
    if cur.get("auth_mode") == "apikey" and cur.get("has_key"):
        return {"ok": True, "changed": False, "detail": "已是 apikey 模式，无需修复"}
    d = {}
    if os.path.exists(AUTH_PATH):
        try:
            d = json.load(open(AUTH_PATH, "r", encoding="utf-8"))
        except Exception:
            d = {}
        _backup(AUTH_PATH)
    d["auth_mode"] = "apikey"
    if not d.get("OPENAI_API_KEY"):
        d["OPENAI_API_KEY"] = LOGIN_PLACEHOLDER
    d.pop("tokens", None)  # apikey 模式不需要 OAuth tokens
    _atomic_write(AUTH_PATH, json.dumps(d, indent=2), chmod=0o600)
    return {"ok": True, "changed": True, "detail": "已写入 apikey 模式 + 占位 key"}


def codex_login_status():
    """跑 `codex login status`，解析 Codex 自己认为的登录态（一致性面板的真相来源）。"""
    try:
        out = subprocess.run([_codex_bin(), "login", "status"],
                             capture_output=True, text=True, timeout=20)
        text = (out.stdout or out.stderr or "").strip()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    logged = "logged in" in text.lower() and "not logged" not in text.lower()
    mode = None
    if "api key" in text.lower():
        mode = "apikey"
    elif "chatgpt" in text.lower():
        mode = "chatgpt"
    hint = ""
    m = re.search(r"(sk-[A-Za-z0-9*]+)", text)
    if m:
        hint = m.group(1)
    return {"ok": True, "logged_in": logged, "mode": mode,
            "key_hint": hint, "raw": text.splitlines()[0] if text else ""}


# ─────────────────────────── 重启 ───────────────────────────

def _detect_router_label():
    env = os.environ.get("CODEX_ROUTER_LABEL")
    if env:
        return env
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if "codex-model-router" in line:
                return line.split("\t")[-1].strip()
    except Exception:
        pass
    return None


def restart_desktop():
    """重启 Codex 桌面 App（ChatGPT.app）。会关闭当前所有在途会话——用户已确认。
    不用 osascript（会卡自动化权限），直接 killall + open。"""
    killed = subprocess.run(["killall", APP_PROCESS],
                            capture_output=True, text=True).returncode == 0
    time.sleep(1.2)
    opened = subprocess.run(["open", "-a", CODEX_APP],
                            capture_output=True, text=True).returncode == 0
    return {"ok": opened, "killed": killed, "reopened": opened,
            "app": CODEX_APP,
            "detail": ("已重启桌面 App" if opened else
                       f"重启失败：open 返回非零（{CODEX_APP} 是否存在？）")}


def restart_router():
    label = _detect_router_label()
    if not label:
        return {"ok": False, "detail": "未找到中转的 launchd label（设 CODEX_ROUTER_LABEL 或确认中转已装为服务）"}
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "label": label,
            "detail": "已重启中转" if r.returncode == 0 else f"失败：{r.stderr.strip()[:160]}"}


# ─────────────────── 路由表（model → 真实 provider）───────────────────

ROUTES_PATH = os.path.join(CODEX_HOME, "router-routes.json")


def read_routes_file():
    if not os.path.exists(ROUTES_PATH):
        return None
    try:
        return json.load(open(ROUTES_PATH, "r", encoding="utf-8"))
    except Exception:
        return None


def write_routes_file(data):
    """写 router-routes.json。校验：providers/models 非空；每个 model 的 provider 已定义、mode 合法。"""
    providers = data.get("providers") or {}
    models = data.get("models") or {}
    if not providers or not models:
        raise ValueError("providers 与 models 都不能为空")
    for m, cfg in models.items():
        if cfg.get("provider") not in providers:
            raise ValueError(f"模型 {m} 引用了未定义的 provider {cfg.get('provider')!r}")
        if cfg.get("mode") not in ("passthrough", "translate"):
            raise ValueError(f"模型 {m} 的 mode 必须是 passthrough 或 translate")
    out = {"providers": providers, "models": models,
           "prefix_routes": data.get("prefix_routes") or []}
    bak = _backup(ROUTES_PATH)
    _atomic_write(ROUTES_PATH, json.dumps(out, indent=2, ensure_ascii=False), chmod=0o600)
    return {"ok": True, "backup": bak, "backend": compute_backend_mode(out)}


def compute_backend_mode(routing):
    """判定该「直连真实 provider」还是「走中转」。

    直连成立：所有模型同属一个 provider 且全是 passthrough（上游原生 Responses）。
    否则必须走中转——Codex 的 model_provider 是单值，跨 provider 或需翻译只能由中转统一。
    """
    models = routing.get("models") or {}
    provs = {cfg.get("provider") for cfg in models.values()}
    modes = {cfg.get("mode") for cfg in models.values()}
    if len(provs) == 1 and modes and modes <= {"passthrough"}:
        p = next(iter(provs))
        return {"mode": "direct", "provider": p,
                "reason": f"全部模型同属 {p} 且都是 passthrough → model_provider 可直接设为 {p}，不经中转"}
    why = []
    if len(provs) > 1:
        why.append(f"跨 {len(provs)} 个 provider（{', '.join(sorted(str(x) for x in provs))}）")
    if "translate" in modes:
        why.append("含 translate 模型（上游只有 chat，需中转翻译）")
    return {"mode": "router", "provider": "router",
            "reason": "；".join(why) + " → Codex model_provider 单值，必须经中转统一分发"}
