#!/usr/bin/env python3
"""config_io — 安全读写 Codex 配置、模型目录与密钥。

设计原则：
  * config.toml 用 tomlkit 读写，**逐字符保留用户手写的注释与格式**。
  * 真实密钥绝不写进 config.toml；它只存 env_key 名字。密钥值落在
    ~/.codex/router-secrets.env（chmod 600），由中转在启动时 source。
  * 任何写入前都先备份成 *.bak-<时间戳>，写坏了能回滚。
"""
import json
import glob
import os
import re
import shutil
import subprocess
import sys
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
    # PATH 里找；找不到再试各平台常见安装位置。/opt/homebrew 只是 macOS+Homebrew
    # 的约定，Windows/Linux 完全不同，不能只留这一个兜底。
    found = shutil.which("codex")
    if found:
        return found
    home = os.path.expanduser("~")
    for cand in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex",
                 os.path.join(home, ".local", "bin", "codex"),
                 os.path.join(os.environ.get("APPDATA", home), "npm", "codex.cmd")):
        if cand and os.path.exists(cand):
            return cand
    return "codex"


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

# 进程重启是**平台相关**的：macOS 用 launchctl/killall/open，Linux 用 systemctl，
# Windows 用服务或计划任务。配置台不猜你的常驻方式，非 macOS 一律返回"怎么做"的指引
# 而不是抛异常——配置读写本身是全平台可用的，只有"一键重启"这两个按钮受限。
IS_MACOS = sys.platform == "darwin"
RESTART_UNSUPPORTED = (
    "配置台的「一键重启」仅支持 macOS（launchctl）。当前平台请手动重启："
    "Windows 用「服务」/计划任务或结束进程后重跑；"
    "Linux 用 systemctl --user restart <unit>。"
    "配置读写不受影响。")


def _detect_router_label():
    if not IS_MACOS:
        return None
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
    if not IS_MACOS:
        return {"ok": False, "detail": RESTART_UNSUPPORTED}
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
    if not IS_MACOS:
        return {"ok": False, "detail": RESTART_UNSUPPORTED}
    label = _detect_router_label()
    if not label:
        return {"ok": False, "detail": "未找到中转的 launchd label（设 CODEX_ROUTER_LABEL 或确认中转已装为服务）"}
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "label": label,
            "detail": "已重启中转" if r.returncode == 0 else f"失败：{r.stderr.strip()[:160]}"}


# ─────────────────── 运行模式：自定义(中转) ↔ 原生(ChatGPT 登录) ───────────────────
#
# 「完全割裂」：切换只整组替换"模式专属键"，两态互不残留——
#   custom : model / model_provider=router / review_model / model_catalog_json /
#            model_reasoning_effort / model_providers(中转+第三方) 全开；auth.json=apikey 占位
#   native : 上述键全部退回"自定义之前"的原生值；原生没有的键（model_provider / review_model /
#            model_catalog_json / model_providers 整张表）直接删除；auth.json=还原真实 ChatGPT OAuth
# 共享键（hooks / mcp_servers / plugins / features / desktop / projects …）两态都不碰，避免来回切 drift。
# 离开某态时把该态的模式专属键快照进 .console-run-mode.json，切回时精确还原。
# 改前对 config.toml + auth.json 各备份一份。绝不自动重启 codex。

MODE_STATE_PATH = os.path.join(CODEX_HOME, ".console-run-mode.json")
NATIVE_DEFAULT_MODEL = "gpt-5.6-sol"   # 取不到原生备份时的兜底


def _first_routed_model():
    """从 router-routes.json 取第一个模型名，作为切回自定义时最后一级兜底。

    正常情况下 custom 快照里就存着用户原本的 model，根本走不到这里。写死某个模型名
    会让换网关的人切回自定义时落到一个他路由表里没有的模型上（router 会明确报错，
    但用户会以为是切换坏了）。
    """
    try:
        data = read_routes_file() or {}
        for name in (data.get("models") or {}):
            return name
    except Exception:
        pass
    return NATIVE_DEFAULT_MODEL


# 模式专属键：切换时整组替换；其余键视为共享、两态保留不动。
MODE_KEYS = ("model", "model_provider", "review_model", "model_catalog_json",
             "model_reasoning_effort", "model_providers")


def _plain(v):
    """tomlkit 值 → 纯 Python（可 JSON 序列化），用于快照。"""
    if v is None:
        return None
    unwrap = getattr(v, "unwrap", None)
    if callable(unwrap):
        try:
            return unwrap()
        except Exception:
            pass
    return v


def _find_chatgpt_auth_backup():
    """最近的 auth.json.bak-chatgpt-*（含真实 ChatGPT OAuth 登录态）；没有则 None。"""
    cands = sorted(glob.glob(os.path.join(CODEX_HOME, "auth.json.bak-chatgpt-*")))
    return cands[-1] if cands else None


def _detect_native_profile():
    """从"自定义之前"的 config.toml.bak 推原生 profile：每个模式专属键的原生值，
    原生没有的键记为 None（=切到 native 时应删除）。识别特征：有 model、无 model_provider。"""
    for b in ("config.toml.bak", "config.toml.bak.bak"):
        p = os.path.join(CODEX_HOME, b)
        if not os.path.exists(p):
            continue
        try:
            d = tomlkit.parse(open(p, "r", encoding="utf-8").read())
        except Exception:
            continue
        if d.get("model") and "model_provider" not in d:
            return {k: (_plain(d[k]) if k in d else None) for k in MODE_KEYS}
    return {"model": NATIVE_DEFAULT_MODEL, "model_provider": None, "review_model": None,
            "model_catalog_json": None, "model_reasoning_effort": None, "model_providers": None}


def _apply_mode_keys(doc, profile):
    """把 profile（{key: value|None}）应用到 tomlkit doc：None=删除该键，否则赋值。"""
    for k in MODE_KEYS:
        v = profile.get(k)
        if v is None:
            if k in doc:
                del doc[k]
        else:
            doc[k] = v


def _load_mode_state():
    if os.path.exists(MODE_STATE_PATH):
        try:
            return json.load(open(MODE_STATE_PATH, "r", encoding="utf-8"))
        except Exception:
            return {}
    return {}


def read_run_mode():
    """判定当前模式 + 隔离状态。custom=apikey 或 model_provider=router；否则 native。"""
    auth = read_auth()
    doc = load_config_doc()
    provider = doc.get("model_provider")
    is_custom = (auth.get("auth_mode") == "apikey") or (provider == "router")
    backup = _find_chatgpt_auth_backup()
    return {
        "mode": "custom" if is_custom else "native",
        "model": _plain(doc.get("model")),
        "model_provider": _plain(provider),
        "model_reasoning_effort": _plain(doc.get("model_reasoning_effort")),
        "has_custom_catalog": "model_catalog_json" in doc,
        "has_custom_providers": "model_providers" in doc,
        "auth_mode": auth.get("auth_mode"),
        "has_oauth_tokens": auth.get("has_oauth_tokens", False),
        "has_oauth_backup": bool(backup),
        "oauth_backup": os.path.basename(backup) if backup else None,
        "native_profile": _detect_native_profile(),
    }


def set_run_mode(target):
    """切换运行模式（custom/native），完全割裂：整组替换模式专属键 + 换 auth.json。
    改前备份 config.toml + auth.json；快照当前态的模式专属键，切回时精确还原。"""
    if target not in ("custom", "native"):
        raise ValueError("target 必须是 custom 或 native")
    cur = read_run_mode()
    if cur["mode"] == target:
        return {"ok": True, "changed": False, "mode": target, "detail": f"已是 {target} 模式"}

    doc = load_config_doc()
    state = _load_mode_state()
    _backup(CONFIG_PATH)
    _backup(AUTH_PATH)

    # 快照"当前态"的模式专属键，供日后切回时精确还原（含整张 model_providers 表）
    state[cur["mode"]] = {k: _plain(doc[k]) for k in MODE_KEYS if k in doc}

    if target == "native":
        _apply_mode_keys(doc, cur.get("native_profile") or _detect_native_profile())
        _atomic_write(CONFIG_PATH, tomlkit.dumps(doc))
        backup = _find_chatgpt_auth_backup()
        restored_login = False
        if backup:
            shutil.copy2(backup, AUTH_PATH)
            os.chmod(AUTH_PATH, 0o600)
            restored_login = True
        state["mode"] = "native"
        _atomic_write(MODE_STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False), chmod=0o600)
        login = codex_login_status()
        need_login = not (login.get("logged_in") and login.get("mode") == "chatgpt")
        removed = [k for k in ("model_provider", "review_model", "model_catalog_json",
                               "model_providers") if k not in doc]
        return {"ok": True, "changed": True, "mode": "native",
                "model": _plain(doc.get("model")), "restored_login": restored_login,
                "need_login": need_login, "login_raw": login.get("raw", ""),
                "removed_custom_keys": removed,
                "detail": ("已切到原生：还原 ChatGPT 登录、删除中转 model_provider/自定义模型目录/"
                           "review_model/model_providers 表，model 与思考档退回原生"
                           + ("" if not need_login else "；登录态可能过期，请跑一次 codex login"))}

    # target == custom：还原快照的 custom 模式专属键 + apikey 占位登录
    saved = state.get("custom") or {}
    if not saved:
        saved = {"model": cur.get("model") or _first_routed_model(),
                 "model_provider": "router"}
    _apply_mode_keys(doc, saved)
    _atomic_write(CONFIG_PATH, tomlkit.dumps(doc))
    ensure_auth_bypass()
    state["mode"] = "custom"
    _atomic_write(MODE_STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False), chmod=0o600)
    return {"ok": True, "changed": True, "mode": "custom",
            "model": _plain(doc.get("model")), "model_provider": _plain(doc.get("model_provider")),
            "restored_keys": sorted(saved.keys()),
            "detail": "已切回自定义（中转）：还原 model/provider/模型目录/review/思考档 + apikey 免登录；确认中转在跑"}


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
