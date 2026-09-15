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
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
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

# ── macOS Keychain 里的 Codex 凭据 ──
#
# Codex 的凭据有两个存放处：~/.codex/auth.json 与 macOS Keychain 的
# service="Codex Auth"。account 名按 CODEX_HOME 算出来，规则（与 Codex 及
# Cockpit 的 build_codex_keychain_account 一致，实测对得上真实条目）：
#     account = "cli|" + sha256(CODEX_HOME 绝对路径)[:16]
# 所以不同 CODEX_HOME 的条目互不相干，删自己那条不会碰到别的 profile。
#
# 为什么出厂态必须动它：只删 auth.json 不够。实测 Keychain 里那条是**完整的
# OAuth**（含 access/refresh token），桌面端在 keyring 模式下会从它恢复登录态。
# 用户要的是「未登录、官网原样」，那这条也得清。
KEYCHAIN_SERVICE = "Codex Auth"


def keychain_account():
    """当前 CODEX_HOME 对应的 Keychain account 名。"""
    home = os.path.realpath(os.path.expanduser(CODEX_HOME))
    digest = hashlib.sha256(home.encode("utf-8")).hexdigest()
    return "cli|%s" % digest[:16]


def _keychain_run(args, timeout=25):
    """跑 `security`，返回 (returncode, stdout+stderr)。非 macOS 直接报不支持。"""
    if not IS_MACOS:
        return 127, "keychain 仅 macOS 有"
    try:
        p = subprocess.run(["security"] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "找不到 security 命令"
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)


def keychain_auth_present():
    """Keychain 里有没有当前 profile 的 Codex 凭据。"""
    rc, _ = _keychain_run(["find-generic-password", "-s", KEYCHAIN_SERVICE,
                           "-a", keychain_account()])
    return rc == 0


def read_keychain_auth():
    """读出 Keychain 里的凭据原文（供出厂态先备份再删）。读不到返回 None。"""
    rc, out = _keychain_run(["find-generic-password", "-s", KEYCHAIN_SERVICE,
                             "-a", keychain_account(), "-w"])
    return out if rc == 0 and out else None


def delete_keychain_auth():
    """删掉 Keychain 里当前 profile 的 Codex 凭据。

    返回 dict：ok=True 表示「现在确实没有这条了」——包括本来就不存在
    （那不算失败）。真删不掉才 ok=False。
    """
    if not IS_MACOS:
        return {"ok": True, "supported": False, "detail": "非 macOS，无 Keychain 要清"}
    acct = keychain_account()
    if not keychain_auth_present():
        return {"ok": True, "supported": True, "existed": False,
                "account": acct, "detail": "Keychain 里本来就没有该条目"}
    rc, out = _keychain_run(["delete-generic-password", "-s", KEYCHAIN_SERVICE,
                             "-a", acct])
    gone = not keychain_auth_present()
    return {"ok": gone, "supported": True, "existed": True, "account": acct,
            "detail": ("已删除 Keychain 凭据" if gone
                       else "删除 Keychain 凭据失败：%s" % (out[:200] or "未知错误"))}


def write_keychain_auth(secret):
    """把凭据写回 Keychain（出厂态还原时用）。"""
    if not IS_MACOS:
        return {"ok": False, "supported": False, "detail": "非 macOS，无法写 Keychain"}
    if not secret:
        return {"ok": False, "supported": True, "detail": "没有可写回的内容"}
    acct = keychain_account()
    rc, out = _keychain_run(["add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                             "-a", acct, "-w", secret])
    return {"ok": rc == 0, "supported": True, "account": acct,
            "detail": "已写回 Keychain 凭据" if rc == 0
                      else "写回 Keychain 失败：%s" % (out[:200] or "未知错误")}


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


# ─────────── 运行模式：自定义(中转) ↔ 原生(ChatGPT 登录) ↔ 出厂(未登录) ───────────
#
# 「完全割裂」：切换只整组替换"模式专属键"，各态互不残留——
#   custom  : model / model_provider=router / review_model / model_catalog_json /
#             model_reasoning_effort / model_providers(中转+第三方) 全开；auth.json=apikey 占位
#   native  : 上述键全部退回"自定义之前"的原生值；原生没有的键（model_provider / review_model /
#             model_catalog_json / model_providers 整张表）直接删除；auth.json=还原真实 ChatGPT OAuth
#   factory : 模式专属键**全部删除**（连 model_reasoning_effort 一起，Codex 用自己的默认），
#             auth.json **删除文件**、macOS Keychain 里的 Codex 凭据也删掉 → 真·未登录。
#             等价于「刚装好、还没登录」的官方 Codex，交给 Cockpit Tools 之类的第三方
#             切号器接管时，它看到的就是一份干净配置，不会被我们的残留键绊住。
# 共享键（hooks / mcp_servers / plugins / features / desktop / projects …）三态都不碰，
# 避免来回切 drift；它们是你自己在 Codex 里配的东西，不属于「我们的中转」。
# 离开某态时把该态的**整份 config.toml 文本**快照进 .console-run-mode.json，切回时精确还原。
# 改前对 config.toml + auth.json 各备份一份。绝不自动重启 codex。
#
# factory 的两个实测坑（不照做就"看着登出了其实没有"）：
#   1. `auth.json = {}` 或 `{"OPENAI_API_KEY": null}` 时，`codex login status` 仍报
#      **"Logged in using ChatGPT"**；只有**文件不存在**才报 "Not logged in"。
#      所以出厂态必须删文件，不能清空内容。
#   2. macOS 上 Keychain(service="Codex Auth") 里可能存着完整 OAuth，桌面端在 keyring
#      模式下会从它恢复登录态。删 auth.json 之前必须先把这份凭据存成
#      auth.json.bak-chatgpt-*（既是备份，也让 native 态之后还能还原），再删 Keychain 条目。

MODE_STATE_PATH = os.path.join(CODEX_HOME, ".console-run-mode.json")
NATIVE_DEFAULT_MODEL = "gpt-5.6-sol"   # 取不到原生备份时的兜底
# 出厂态备份 Keychain 凭据用的文件名（chmod 600）。
KEYCHAIN_BACKUP_PATH = os.path.join(CODEX_HOME, ".console-keychain-auth.bak")


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


def _remove_auth_for_factory():
    """把登录态清成"从没登录过"。返回 (ok, detail, 备份文件名列表)。

    三步，顺序不能换：
      1. 现有 auth.json 里若有 OAuth tokens → 先存成 auth.json.bak-chatgpt-<时间戳>。
         这份备份有双重作用：既是撤销删除的后路，也是 native 态之后还原登录的来源
         （_find_chatgpt_auth_backup 就是按这个前缀找的）。
      2. macOS 上 Keychain 里若有凭据 → 存成 .console-keychain-auth.bak（600），再删条目。
         只删 auth.json 不动 Keychain 的话，桌面端在 keyring 模式下仍是登录态。
      3. 删掉 auth.json 文件本身。注意**必须是删文件**：实测 auth.json = {} 或
         {"OPENAI_API_KEY": null} 时 `codex login status` 仍报 "Logged in using ChatGPT"，
         只有文件不存在才报 "Not logged in"。
    """
    backups = []

    # 1) auth.json 里的 OAuth → 带时间戳备份
    if os.path.exists(AUTH_PATH):
        try:
            with open(AUTH_PATH, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
        has_oauth = bool((cur or {}).get("tokens", {}) and
                         (cur.get("tokens") or {}).get("access_token"))
        if has_oauth:
            bak = "%s.bak-chatgpt-%s" % (AUTH_PATH, time.strftime("%Y%m%d-%H%M%S"))
            try:
                shutil.copy2(AUTH_PATH, bak)
                os.chmod(bak, 0o600)
                backups.append(os.path.basename(bak))
            except Exception as e:
                return False, "备份 auth.json 失败（已中止，未删任何东西）：%s" % e, backups

    # 2) Keychain 凭据 → 备份后删除
    kc = None
    try:
        kc = read_keychain_auth()
    except Exception:
        kc = None
    if kc:
        try:
            _atomic_write(KEYCHAIN_BACKUP_PATH, kc, chmod=0o600)
            backups.append(os.path.basename(KEYCHAIN_BACKUP_PATH))
        except Exception as e:
            return False, "备份 Keychain 凭据失败（已中止）：%s" % e, backups
    if IS_MACOS:
        res = delete_keychain_auth()
        if not res.get("ok"):
            return False, res.get("detail") or "删除 Keychain 凭据失败", backups
        if res.get("existed"):
            backups.append("keychain:%s" % KEYCHAIN_SERVICE)

    # 3) 删 auth.json 文件本身
    if os.path.exists(AUTH_PATH):
        try:
            os.remove(AUTH_PATH)
        except Exception as e:
            return False, "删除 auth.json 失败：%s" % e, backups

    return True, "已清除登录态（auth.json 删除" + \
        ("、Keychain 凭据删除）" if kc or (IS_MACOS and backups and
                                           any(b.startswith("keychain:") for b in backups))
         else "）"), backups


def _restore_auth_for_native():
    """native 态还原登录：优先 auth.json 备份，其次 Keychain 备份。返回 (ok, detail)。"""
    restored = False
    backup = _find_chatgpt_auth_backup()
    if backup:
        try:
            shutil.copy2(backup, AUTH_PATH)
            os.chmod(AUTH_PATH, 0o600)
            restored = True
        except Exception as e:
            return False, "还原 auth.json 失败：%s" % e
    # Keychain 备份：桌面端在 keyring 模式下靠它。写回去，别让用户重新扫码登录。
    if os.path.exists(KEYCHAIN_BACKUP_PATH):
        try:
            with open(KEYCHAIN_BACKUP_PATH, "r", encoding="utf-8") as f:
                secret = f.read()
        except Exception:
            secret = None
        if secret:
            res = write_keychain_auth(secret)
            if res.get("ok"):
                restored = True
                try:
                    os.remove(KEYCHAIN_BACKUP_PATH)
                except Exception:
                    pass
            else:
                return False, res.get("detail") or "写回 Keychain 失败"
    return restored, ("已还原登录态" if restored
                      else "没有可还原的登录备份，需要重新 codex login")


def _apply_mode_keys(doc, profile):
    """把 profile（{key: value|None}）应用到 tomlkit doc：None=删除该键，否则赋值。"""
    for k in MODE_KEYS:
        v = profile.get(k)
        if v is None:
            if k in doc:
                del doc[k]
        else:
            doc[k] = v


_TBL_RE = re.compile(r'^\s*\[\s*([^\[\]]+?)\s*\]\s*$')
_CMT_RE = re.compile(r'^\s*#')


def _table_path(header):
    """`[a.b.c]` / `["a"."b"]` → ('a','b','c')。"""
    parts = []
    for seg in header.split("."):
        seg = seg.strip().strip("\"'")
        if seg:
            parts.append(seg)
    return tuple(parts)


def _scalar_assign_re(key):
    esc = re.escape(key)
    return re.compile(r'^\s*(?:%s|"%s"|\'%s\')\s*=' % (esc, esc, esc))


def _absorb_comments(lines, idx, drop):
    """把 idx 上方**紧邻连续**的注释行也标记删除（遇到空行就停）。

    只吸收紧邻的，是为了不把上一个键的说明误删。
    """
    j = idx
    while j > 0 and _CMT_RE.match(lines[j - 1]) and (j - 1) not in drop:
        j -= 1
    for k in range(j, idx):
        drop.add(k)


def strip_mode_key_blocks(text, keys=MODE_KEYS):
    """按文本删掉这些键（含表），连同它们头上紧邻的注释。

    为什么不用 tomlkit 删：实测 `del doc["review_model"]` 会把它头上的注释**留在
    原地**，于是注释漂到下一个键头上。出厂态要的是一份"干净的官方配置"，留下几段
    讲中转/讲 ideaLAB 的孤儿注释会很误导（用户会以为 notify 跟 review 有关）。

    删除范围：
      * 顶层标量键（model / review_model / model_catalog_json / model_reasoning_effort
        / model_provider）——只在第一个表头之前找，避免误删表内的同名键。
      * 表键（model_providers）——匹配所有以它为前缀的表头
        （`[model_providers]`、`[model_providers.router]`、`[model_providers.router.env]`），
        连表体一起删，直到下一个不属于它的表头。
    共享键（hooks / mcp_servers / features / desktop / projects …）一个都不碰。
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    first_table = len(lines)
    for i, ln in enumerate(lines):
        if _TBL_RE.match(ln):
            first_table = i
            break

    drop = set()
    keys = set(keys)

    # 1) 顶层标量
    for key in keys:
        pat = _scalar_assign_re(key)
        for i in range(first_table):
            if pat.match(lines[i]):
                drop.add(i)
                _absorb_comments(lines, i, drop)
                break

    # 2) 表（含子表）
    for key in keys:
        i = 0
        while i < len(lines):
            m = _TBL_RE.match(lines[i])
            if not m:
                i += 1
                continue
            path = _table_path(m.group(1))
            if not path or path[0] != key:
                i += 1
                continue
            # 命中：删表头 + 表体（到下一个表头为止），并吸收上方注释
            start = i
            _absorb_comments(lines, start, drop)
            drop.add(i)
            j = i + 1
            while j < len(lines) and not _TBL_RE.match(lines[j]):
                drop.add(j)
                j += 1
            i = j

    if not drop:
        return text
    out = "".join(ln for i, ln in enumerate(lines) if i not in drop)
    # 删完可能留下连续空行，收敛成一个，免得文件头出现一串空白
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.lstrip("\n")


def _load_mode_state():
    if os.path.exists(MODE_STATE_PATH):
        try:
            return json.load(open(MODE_STATE_PATH, "r", encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_mode_state(state):
    _atomic_write(MODE_STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False),
                  chmod=0o600)


def _shared_keys(doc):
    """顶层键里不属于模式专属的那些（hooks / mcp_servers / features / tui …）。"""
    return [str(k) for k in doc.keys() if str(k) not in MODE_KEYS]


def _overlay_shared(base, live):
    """把 live 的共享键搬到 base 上，**按 tomlkit item 搬**（连注释一起）。

    为什么不能走"解包成纯值再赋值"：那样每个键都会变成一个全新 item，
    丢掉前导注释，而且只能追加到文档末尾——实测会把 model_providers 整张表
    挪到文件最后，后面的 [tui] 块与 `# END otel-codex-hook trust` 标记全部错位，
    原本 review_model 头上的中文注释还会孤儿化漂到别的键头上。

    只在值真的不同时才赋值，所以"没人动过共享键"的往返是逐字节不变的。
    """
    changed, removed = [], []
    live_shared = _shared_keys(live)
    for k in list(base.keys()):
        sk = str(k)
        if sk in MODE_KEYS:
            continue
        if sk not in live_shared:
            del base[sk]
            removed.append(sk)
        elif tomlkit.dumps({sk: live[sk]}) != tomlkit.dumps({sk: base[sk]}):
            base[sk] = live[sk]
            changed.append(sk)
    for sk in live_shared:
        if sk not in base:
            base[sk] = live[sk]
            changed.append(sk)
    return base, changed, removed


def _jwt_exp(token):
    """解 access_token 的 JWT exp（秒级时间戳）；解不出来返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def router_base_url():
    """config.toml 里实际配置的中转 base_url；没配则回落默认端口。"""
    try:
        providers = load_config_doc().get("model_providers") or {}
        url = (providers.get("router") or {}).get("base_url")
        if url:
            return str(url).rstrip("/")
    except Exception:
        pass
    return os.environ.get("CODEX_ROUTER_URL", "http://127.0.0.1:8317/v1").rstrip("/")


def router_alive(timeout=3):
    """中转 /v1/models 通不通。切回自定义时用它判断"配置对了但服务没起"。"""
    import urllib.request
    url = router_base_url()
    try:
        with urllib.request.urlopen(f"{url}/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def login_liveness():
    """诚实回答"这份 ChatGPT 登录还能用吗"。

    不能用 `codex login status`：它只读本地文件，对过期 52 天、每次调用都 401
    的 token 照样报 "Logged in using ChatGPT"（实测）。切过去之后第一次调用才炸，
    用户会以为是切换坏了。这里直接解 JWT 的 exp，零网络、瞬时。
    """
    if not os.path.exists(AUTH_PATH):
        return {"exists": False, "alive": False, "auth_mode": None,
                "reason": "auth.json 不存在"}
    try:
        d = json.load(open(AUTH_PATH, "r", encoding="utf-8"))
    except Exception as e:
        return {"exists": True, "alive": False, "auth_mode": None,
                "reason": f"auth.json 解析失败：{e}"}
    if d.get("auth_mode") == "apikey":
        return {"exists": True, "alive": True, "auth_mode": "apikey",
                "reason": "apikey 模式（中转免登录，不存在过期问题）"}
    tok = d.get("tokens") or {}
    out = {"exists": True, "auth_mode": "chatgpt" if tok else None,
           "has_refresh_token": bool(tok.get("refresh_token")),
           "account_id": (tok.get("account_id") or "")[:8],
           "last_refresh": d.get("last_refresh")}
    at = tok.get("access_token")
    if not at:
        out.update(alive=False, reason="没有 access_token")
        return out
    exp = _jwt_exp(at)
    now = time.time()
    if exp is None:
        out.update(alive=None, reason="access_token 不是可解析的 JWT，无法判定有效期")
    elif exp > now:
        out.update(alive=True,
                   exp_str=time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)),
                   hours_left=round((exp - now) / 3600, 1),
                   reason=f"access_token 有效（还剩 {(exp - now) / 3600:.1f} 小时）")
    else:
        days = (now - exp) / 86400
        out.update(alive=False, expired_days=round(days, 1),
                   exp_str=time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)),
                   reason=(f"access_token 已过期 {days:.0f} 天"
                           + ("，有 refresh_token，可尝试自动续期，失败再重新登录"
                              if tok.get("refresh_token") else "，需要重新登录")))
    return out


def read_run_mode():
    """判定当前模式 + 隔离状态。

    custom  = auth.json 是 apikey 模式，或 model_provider 指向我们的中转
              （`router` 是配置台写的，`codex_local_access` 是 Cockpit Tools 写的）
    factory = auth.json 不存在 → 没有任何登录可用，等价于"刚装好还没登录"
    native  = 其余（有 ChatGPT OAuth 登录态）

    factory 与 native 的区别就在 auth.json 在不在：native 一定能登录，factory 一定不能。
    另外把 factory 下**仍然残留**的模式专属键列出来（factory_residual_keys）——
    正常情况下应该是空的；非空说明有别的工具动过配置，UI 会提示再点一次 Restore。
    """
    auth = read_auth()
    doc = load_config_doc()
    provider = doc.get("model_provider")
    is_custom = ((auth.get("auth_mode") == "apikey")
                 or (provider in ("router", "codex_local_access")))
    auth_exists = os.path.exists(AUTH_PATH)
    is_factory = (not auth_exists) and not is_custom
    if is_custom:
        mode = "custom"
    elif is_factory:
        mode = "factory"
    else:
        mode = "native"
    backup = _find_chatgpt_auth_backup()
    live = login_liveness()
    residuals = [k for k in MODE_KEYS if k in doc] if is_factory else []
    return {
        "mode": mode,
        "model": _plain(doc.get("model")),
        "model_provider": _plain(provider),
        "model_reasoning_effort": _plain(doc.get("model_reasoning_effort")),
        "has_custom_catalog": "model_catalog_json" in doc,
        "has_custom_providers": "model_providers" in doc,
        "auth_exists": auth_exists,
        "keychain_auth_present": keychain_auth_present(),
        "factory_residual_keys": residuals,
        "auth_mode": auth.get("auth_mode"),
        "has_oauth_tokens": auth.get("has_oauth_tokens", False),
        "has_oauth_backup": bool(backup),
        "has_keychain_backup": os.path.exists(KEYCHAIN_BACKUP_PATH),
        "oauth_backup": os.path.basename(backup) if backup else None,
        # 原生模式能不能真的用起来，取决于这份登录是否还活着
        "login_alive": live.get("alive"),
        "login_reason": live.get("reason"),
        "login_exp_str": live.get("exp_str"),
        "login_hours_left": live.get("hours_left"),
        "login_expired_days": live.get("expired_days"),
        "has_refresh_token": live.get("has_refresh_token"),
        "router_alive": router_alive(),
        "native_profile": _detect_native_profile(),
    }


def set_run_mode(target):
    """切换运行模式（custom / native / factory）：整文件快照 + 共享键叠加。

    离开某态时把**整份 config.toml 文本**存进 .console-run-mode.json，切回时先原样
    写回，再把当前态的共享键（hooks / mcp_servers / features / tui …）按 tomlkit
    item 叠上去。于是：模式专属键精确还原（含注释与键序），而你在另一态里改过的
    共享设置不会丢——两边都满足，且没人动过共享键时往返逐字节不变。

    factory（出厂/未登录）与其它两态的差别在登录态：config.toml 的模式专属键全删，
    auth.json **删除文件**（不是清空，见 _remove_auth_for_factory 里的实测说明），
    macOS 上还删 Keychain 凭据。删之前都会先备份，所以切回 custom/native 能完整还原。

    改前对 config.toml + auth.json 各备份一份；绝不自动重启 codex。
    """
    if target not in ("custom", "native", "factory"):
        raise ValueError("target 必须是 custom、native 或 factory")
    cur = read_run_mode()
    # factory 下若还残留模式专属键（别的工具动过 config.toml），不能因为"已经是
    # factory"就直接返回，否则用户点了 Restore 却什么都没清掉。
    already_there = (cur["mode"] == target
                     and not (target == "factory" and cur.get("factory_residual_keys")))
    if already_there:
        return {"ok": True, "changed": False, "mode": target,
                "detail": f"已是 {target} 模式"}

    live_text = open(CONFIG_PATH, "r", encoding="utf-8").read()
    live_doc = tomlkit.parse(live_text)
    state = _load_mode_state()
    _backup(CONFIG_PATH)
    _backup(AUTH_PATH)

    # 快照"当前态"的整份文本。旧版存的是 {key: 纯值} 字典——那种快照保不住注释与
    # 键序，只认它是字符串时才当文本用；是字典时留给首次切自定义的兼容分支。
    legacy_custom = state.get("custom") if isinstance(state.get("custom"), dict) else None
    # factory 不存快照：它是规范态、每次现推（见下面 target_snapshot 的说明），
    # 存了也没人读，留着只会在状态文件里积一份过时文本。
    if cur["mode"] != "factory":
        state[cur["mode"]] = live_text
    else:
        state.pop("factory", None)

    # 目标态的快照：优先用存过的文本；没有就从当前态推导。
    #
    # factory 是例外——它**永远现推**，不用快照。因为 factory 的定义就是"把模式专属
    # 键删干净"，是一个规范态而非某次配置的历史留影。用快照会带来两个问题：
    #   1. 快照可能是旧版逻辑（比如还没修孤儿注释之前）留下的，于是 Restore 之后
    #      仍然带着残留注释，而用户看到的是"我点了恢复出厂却没干净"。
    #   2. 快照是死的，而 live 的共享配置会随 Codex 升级、装插件不断变化；
    #      现推则天然只反映"此刻的共享配置减去我们的键"。
    # custom / native 继续用快照：那两态各自有用户手改过的模式专属键值，
    # 需要逐字节还原（含注释与键序），这正是快照存在的理由。
    target_snapshot = None if target == "factory" else state.get(target)
    if not (isinstance(target_snapshot, str) and target_snapshot.strip()):
        target_snapshot = None

    if target_snapshot:
        base = tomlkit.parse(target_snapshot)
        base, shared_changed, shared_removed = _overlay_shared(base, live_doc)
        out_text = tomlkit.dumps(base)
    else:
        base = tomlkit.parse(live_text)
        if target == "native":
            # 首次切原生：按 native_profile 改模式专属键，原生没有的键删掉
            _apply_mode_keys(base, cur.get("native_profile") or _detect_native_profile())
            out_text = tomlkit.dumps(base)
        elif target == "factory":
            # 首次切出厂：模式专属键全删（连注释一起），交给 Codex 用自己的默认。
            # 走文本级删除而不是 tomlkit，理由见 strip_mode_key_blocks。
            out_text = strip_mode_key_blocks(live_text, MODE_KEYS)
        else:
            # 首次切自定义：有旧版字典快照就沿用它的键值，否则保底最小集
            saved = legacy_custom or {}
            if not saved:
                saved = {"model": cur.get("model") or _first_routed_model(),
                         "model_provider": "router"}
            _apply_mode_keys(base, saved)
            out_text = tomlkit.dumps(base)
        shared_changed, shared_removed = [], []

    _atomic_write(CONFIG_PATH, out_text)
    out_doc = tomlkit.parse(out_text)

    if target == "factory":
        ok, auth_detail, auth_backups = _remove_auth_for_factory()
        state["mode"] = "factory"
        _save_mode_state(state)
        removed = [k for k in MODE_KEYS if k not in out_doc]
        detail = ("已恢复出厂：删除 %d 个中转专属键，%s" % (len(removed), auth_detail))
        if auth_backups:
            detail += "；备份：%s" % "、".join(auth_backups)
        if not ok:
            # config 已经写干净了，但登录态没清掉 —— 如实报错，别假装成功。
            return {"ok": False, "changed": True, "mode": "factory",
                    "removed_custom_keys": removed, "auth_backups": auth_backups,
                    "detail": detail + "；⚠ " + auth_detail}
        detail += "。现在这份配置等价于刚装好、未登录的官方 Codex，可以直接交给 Cockpit Tools 等切号器接管。"
        if shared_changed or shared_removed:
            detail += f"（共享设置已跟随：改 {shared_changed} 删 {shared_removed}）"
        return {"ok": True, "changed": True, "mode": "factory",
                "removed_custom_keys": removed,
                "auth_backups": auth_backups,
                "need_restart": True,
                "shared_changed": shared_changed, "shared_removed": shared_removed,
                "detail": detail}

    if target == "native":
        restored_login, auth_detail = _restore_auth_for_native()
        login_after = login_liveness()
        state["mode"] = "native"
        _save_mode_state(state)
        # need_login 只看 JWT 有效期，不信 `codex login status`（它会假阳性）
        need_login = login_after.get("alive") is not True
        removed = [k for k in ("model_provider", "review_model", "model_catalog_json",
                               "model_providers") if k not in out_doc]
        detail = ("已切到原生：ChatGPT 登录、删除中转 model_provider/自定义模型目录/"
                  "review_model/model_providers 表，model 与思考档退回原生")
        if need_login:
            detail += f"；⚠ {login_after.get('reason')}，请在下方登录一次"
        elif shared_changed or shared_removed:
            detail += f"；共享设置已跟随（改 {shared_changed} 删 {shared_removed}）"
        return {"ok": True, "changed": True, "mode": "native",
                "model": _plain(out_doc.get("model")), "restored_login": restored_login,
                "auth_detail": auth_detail,
                "need_login": need_login,
                "login_alive": login_after.get("alive"),
                "login_reason": login_after.get("reason"),
                "login_exp_str": login_after.get("exp_str"),
                "shared_changed": shared_changed, "shared_removed": shared_removed,
                "removed_custom_keys": removed, "detail": detail}

    ensure_auth_bypass()
    state["mode"] = "custom"
    _save_mode_state(state)
    detail = ("已切回自定义（中转）：还原 model/provider/模型目录/review/思考档 + apikey 免登录")
    if not cur.get("router_alive"):
        detail += "；⚠ 中转没在跑，先启动它，否则模型调不通"
    elif shared_changed or shared_removed:
        detail += f"；共享设置已跟随（改 {shared_changed} 删 {shared_removed}）"
    return {"ok": True, "changed": True, "mode": "custom",
            "model": _plain(out_doc.get("model")),
            "model_provider": _plain(out_doc.get("model_provider")),
            "router_alive": cur.get("router_alive"),
            "shared_changed": shared_changed, "shared_removed": shared_removed,
            "restored_keys": sorted(k for k in MODE_KEYS if k in out_doc),
            "detail": detail}


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
