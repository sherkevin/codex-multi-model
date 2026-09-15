#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cockpit-bridge.py — 把本地 router 注册成 Cockpit Tools 的一个可切换账号。

为什么用 Cockpit 而不是自己写切换：Cockpit Tools（MIT，github.com/jlcodes99/cockpit-tools）
已经是成熟的账号切换器，它管 auth.json / config.toml 投影 / 备份 / 重启客户端 / 配额显示。
我们自己再实现一遍切换，等于把它已经踩过坑的逻辑重抄一份，将来它改了我们就得跟着改。
所以这里只做**注册**：把 router 变成一个它认识的账号，切换动作全部交给它。

关键机制（源码 v1.3.53 实证，非猜测）：

  * 账号 `api_provider_mode = "custom"` + `api_wire_api = "responses"` + loopback base_url
    → Cockpit 走 `write_api_key_bearer_provider_override_to_config_toml`，写出
      `model_provider = "codex_local_access"` 与对应的 provider 段，Codex **直连**这个
      base_url（direct 模式），不经过 Cockpit 内嵌的 CPA sidecar。

  * `api_provider_id` 必须是 `codex_local_access`。它在 Cockpit 自己的
    `collect_managed_api_key_provider_ids()` 托管清单里，所以切回 OAuth 原生账号时，
    `model_provider` 键与整个 provider 段会被它**自动删除**——不留残留，不共存。

  * `api_sync_model_catalog_to_codex` 必须为 false。为 true 时 Cockpit 会把我们的模型名
    判成「需要上游改写」，改道它内嵌的 CPA sidecar 并套上 gpt-5.5 之类的壳名，
    于是 router 收不到真实模型名，路由直接错。

  * `api_supports_websockets` 必须为 false。为 true 时 Codex 会先试
    ws://127.0.0.1:<port>/v1/responses，吃一次 404 再回落 HTTP（实测多花约 10 秒）。

  * 账号详情文件写**明文 JSON** 即可：`deserialize_account_file()` 认旧版明文并在下次
    保存时自动升级为 AES-256-GCM。所以本脚本零第三方依赖，不需要它的加密密钥。

  * `app_speed` 按当前 config.toml 的 `service_tier` 推。Cockpit 切换时按账号写回
    service_tier，不设就会被降成 standard → `default`，丢掉用户原本的 priority。

用法：
  python3 tools/cockpit-bridge.py                 # 注册（幂等）
  python3 tools/cockpit-bridge.py --status        # 看当前注册状态
  python3 tools/cockpit-bridge.py --remove        # 注销
  python3 tools/cockpit-bridge.py --dry-run       # 只打印将写入的内容，不落盘

注册后重启 Cockpit Tools，账号列表里会出现「本地中转 Router」，点它即切到中转；
点你的 ChatGPT OAuth 账号即切回原生。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
COCKPIT_HOME = os.environ.get(
    "COCKPIT_HOME", os.path.join(os.path.expanduser("~"), ".antigravity_cockpit"))

# router 只监听回环，且**不校验**入站 Bearer（它按模型名分发、密钥从环境变量取）。
# 这里填的是 Cockpit 写进 config.toml / auth.json 的占位串，不是凭证。
# 固定值很重要：账号 id = md5(key)，key 变则 id 变，幂等就失效了。
PLACEHOLDER_KEY = os.environ.get("COCKPIT_BRIDGE_KEY", "local-router-placeholder")

# Cockpit 托管的 provider id。见文件头说明——用别的 id 会导致切回原生时残留。
MANAGED_PROVIDER_ID = "codex_local_access"
PROVIDER_DISPLAY_NAME = "Codex Multi-Model Router"
ACCOUNT_DISPLAY_NAME = "本地中转 Router"

ACCOUNTS_DIR_NAME = "codex_accounts"
INDEX_FILE_NAME = "codex_accounts.json"
PROVIDERS_FILE_NAME = "codex_model_providers.json"

# config.toml 顶层 service_tier → Cockpit 的 CodexAppSpeed（snake_case）。
# 对照 Cockpit 源码 codex_speed.rs：priority/fast/flex → fast，ultrafast → ultrafast，
# 其余（含 default）→ standard。
SPEED_BY_SERVICE_TIER = {
    "priority": "fast", "fast": "fast", "flex": "fast",
    "ultrafast": "ultrafast",
}


# ─────────────────────────── 通用小工具 ───────────────────────────

def _now():
    return int(time.time())


def _backup(path):
    """改前留一份带时间戳的备份。文件不存在返回 None。"""
    if os.path.exists(path):
        bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, bak)
        return bak
    return None


def _atomic_write(path, text, chmod=None):
    """临时文件 + os.replace，避免写一半被 Cockpit 读到。"""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if chmod is not None:
        os.chmod(tmp, chmod)
    os.replace(tmp, path)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
        if not txt.strip():
            return default
        return json.loads(txt)
    except Exception as e:
        # 读不懂就不动它：宁可报出来让人处理，也不要覆盖掉别人的数据。
        raise SystemExit("读取 %s 失败（%s）。已中止，未做任何修改。" % (path, e))


def _md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ─────────────────────────── 从现状推参数 ───────────────────────────

def _read_config_text():
    try:
        with open(os.path.join(CODEX_HOME, "config.toml"), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def router_base_url():
    """config.toml 里 router provider 的 base_url；没配就回落默认端口。

    优先读用户真实配置，这样改了端口的人不用改这个脚本。不引 toml 解析器：
    只要 router 段里的 base_url 一行，正则足够且保持零依赖。
    """
    m = re.search(r'\[model_providers\.router\][^\[]*?base_url\s*=\s*"([^"]+)"',
                  _read_config_text(), re.S)
    if m:
        return m.group(1).rstrip("/")
    return os.environ.get("CODEX_ROUTER_URL", "http://127.0.0.1:8317/v1").rstrip("/")


def routed_models():
    """router-routes.json 里的模型名 + 各自的上下文窗口（供 Cockpit UI 显示）。

    窗口值优先取 custom-model-catalog.json 里的 `context_window`——那才是 Codex
    真正生效、决定进度条分母与自动压缩时机的数。路由表里的 `input_token_limit`
    是上游硬顶，两者不是一回事：我们特意把 catalog 的窗口设得**远低于**硬顶
    （983616 → 300000，留约 3.3 倍余量），好让压缩在撞墙前就触发（见 docs/adr/0005）。
    拿硬顶去填这里，UI 会显示一个比实际生效值大三倍的窗口，误导判断。

    另外这个字段在 Cockpit 侧只喂给它内嵌 sidecar 的配置（我们走 direct、不启用），
    所以它纯属展示用；填准确值是为了 UI 不骗人。
    """
    data = _read_json(os.path.join(CODEX_HOME, "router-routes.json"), {})
    catalog_windows = _catalog_context_windows()
    names, windows = [], {}
    for name, cfg in (data.get("models") or {}).items():
        names.append(name)
        win = catalog_windows.get(name)
        if win is None:
            lim = (cfg or {}).get("input_token_limit")
            if isinstance(lim, int) and lim > 0:
                win = lim
        if win:
            windows[name] = win
    return names, windows


def _catalog_context_windows():
    """从 config.toml 指向的 model_catalog_json 读每个模型的 context_window。"""
    m = re.search(r'^\s*model_catalog_json\s*=\s*"([^"]+)"', _read_config_text(), re.M)
    if not m:
        return {}
    ref = m.group(1).strip()
    path = ref if os.path.isabs(ref) else os.path.join(CODEX_HOME, ref)
    data = _read_json(path, {})
    out = {}
    for entry in (data.get("models") or []):
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        win = entry.get("context_window")
        if slug and isinstance(win, int) and win > 0:
            out[slug] = win
    return out


def current_app_speed():
    """按 config.toml 的 service_tier 推 Cockpit 的 app_speed。读不到就 standard。"""
    m = re.search(r'^\s*service_tier\s*=\s*"([^"]+)"', _read_config_text(), re.M)
    if m:
        return SPEED_BY_SERVICE_TIER.get(m.group(1).strip().lower(), "standard")
    return "standard"


def account_id():
    """Cockpit 的 API Key 账号 id 规则：codex_apikey_<md5(key)>。

    必须与它一致（见 codex_account_provider.rs::build_api_key_account_id），
    否则用户在 Cockpit UI 里改这个账号时会新建一条，出现重复项。
    """
    return "codex_apikey_%s" % _md5_hex(PLACEHOLDER_KEY)


def account_email():
    """同 Cockpit 规则：api-key-<md5(key)[:8]>。"""
    return "api-key-%s" % _md5_hex(PLACEHOLDER_KEY)[:8]


# ─────────────────────────── 构造账号 ───────────────────────────

def build_account(base_url, models, windows, speed):
    """构造一份 Cockpit 能直接反序列化的 CodexAccount（明文，它会自动加密）。

    字段依据 src-tauri/src/models/codex.rs 的 `struct CodexAccount`。
    注意：`user_id / plan_type / account_id / organization_id / quota / tags`
    在源码里是 Option 但**没有** #[serde(default)]，缺字段会反序列化失败，
    所以这里显式给 null，不能省。
    """
    ts = _now()
    return {
        "id": account_id(),
        "email": account_email(),
        "auth_mode": "apikey",
        "openai_api_key": PLACEHOLDER_KEY,
        "api_base_url": base_url,
        "api_provider_mode": "custom",
        "api_provider_id": MANAGED_PROVIDER_ID,
        "api_provider_name": PROVIDER_DISPLAY_NAME,
        "api_model_catalog": models,
        "api_model_context_windows": windows,
        # false = 不改道 CPA sidecar，Codex 直连 router（见文件头）。
        "api_sync_model_catalog_to_codex": False,
        "api_wire_api": "responses",
        # false = 不让 Codex 先试 WebSocket 吃 404（见文件头）。
        "api_supports_websockets": False,
        # router 会转发原生 image_url part，识图可用。
        "api_supports_vision": True,
        "user_id": None,
        "plan_type": "API_KEY",
        "account_id": None,
        "organization_id": None,
        "account_name": ACCOUNT_DISPLAY_NAME,
        "account_note": ("由 codex-multi-model 的 tools/cockpit-bridge.py 注册。"
                         "指向本地 router，切换即改 ~/.codex 的 auth.json 与 config.toml。"),
        "app_speed": speed,
        # API Key 账号没有 OAuth token；Cockpit 的 new_api_key() 也是给空串。
        "tokens": {"id_token": "", "access_token": ""},
        "token_source_mode": "managed",
        "quota": None,
        "tags": ["router", "local"],
        "created_at": ts,
        "last_used": ts,
    }


def build_provider(base_url, models, windows):
    """codex_model_providers.json 里的一条（明文存储，无加密）。

    这份是给 Cockpit「模型供应商」管理页看的，与账号是两套数据；两边都写，
    UI 里供应商与账号才能对上（它按 api_provider_id 或 base_url 关联）。
    字段依据 src/services/codexModelProviderService.ts 的 interface CodexModelProvider。
    """
    now_ms = int(time.time() * 1000)
    return {
        "id": MANAGED_PROVIDER_ID,
        "name": PROVIDER_DISPLAY_NAME,
        "baseUrl": base_url,
        "modelCatalog": models,
        "modelContextWindows": windows,
        "supportsVision": True,
        "wireApi": "responses",
        "supportsWebsockets": False,
        "enableModePreference": "direct",
        "apiKeys": [{
            "id": "cmk_%s" % _md5_hex(PLACEHOLDER_KEY)[:12],
            "name": ACCOUNT_DISPLAY_NAME,
            "apiKey": PLACEHOLDER_KEY,
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }],
        "createdAt": now_ms,
        "updatedAt": now_ms,
    }


# ─────────────────────────── 落盘 ───────────────────────────

def cockpit_ready():
    if not os.path.isdir(COCKPIT_HOME):
        raise SystemExit(
            "没找到 Cockpit Tools 的数据目录：%s\n"
            "先安装并至少启动一次 Cockpit Tools（github.com/jlcodes99/cockpit-tools），"
            "再跑本脚本。" % COCKPIT_HOME)


def register(dry_run=False):
    cockpit_ready()
    base_url = router_base_url()
    models, windows = routed_models()
    if not models:
        raise SystemExit(
            "%s 里没有任何模型路由，注册出来的账号会是空的。"
            "先配好 router-routes.json（或用 codex-console 配置台）。"
            % os.path.join(CODEX_HOME, "router-routes.json"))
    speed = current_app_speed()
    aid = account_id()

    acc = build_account(base_url, models, windows, speed)
    prov = build_provider(base_url, models, windows)

    acc_dir = os.path.join(COCKPIT_HOME, ACCOUNTS_DIR_NAME)
    acc_path = os.path.join(acc_dir, "%s.json" % aid)
    idx_path = os.path.join(COCKPIT_HOME, INDEX_FILE_NAME)
    prov_path = os.path.join(COCKPIT_HOME, PROVIDERS_FILE_NAME)

    plan = {
        "account_id": aid,
        "email": acc["email"],
        "base_url": base_url,
        "models": models,
        "app_speed": speed,
        "account_file": acc_path,
        "account_file_exists": os.path.exists(acc_path),
        "index_file": idx_path,
        "providers_file": prov_path,
    }
    if dry_run:
        plan["account_json"] = acc
        plan["provider_json"] = prov
        return plan

    # 幂等：已存在就保留原 created_at / last_used，只刷新会变的部分。
    old = _read_json(acc_path, None)
    if isinstance(old, dict):
        for k in ("created_at", "last_used"):
            if isinstance(old.get(k), int):
                acc[k] = old[k]

    if not os.path.isdir(acc_dir):
        os.makedirs(acc_dir)
    _backup(acc_path)
    _atomic_write(acc_path, json.dumps(acc, indent=2, ensure_ascii=False) + "\n", chmod=0o600)

    # 索引：追加/更新一条 summary。不动 current_account_id —— 切换由用户在 UI 里点。
    idx = _read_json(idx_path, None)
    if not isinstance(idx, dict):
        idx = {"version": "1.0", "detail_schema_version": 2,
               "accounts": [], "current_account_id": None}
    summaries = idx.get("accounts")
    if not isinstance(summaries, list):
        summaries = []
    summary = {
        "id": aid,
        "email": acc["email"],
        "plan_type": acc["plan_type"],
        "created_at": acc["created_at"],
        "last_used": acc["last_used"],
    }
    replaced = False
    for i, s in enumerate(summaries):
        if isinstance(s, dict) and s.get("id") == aid:
            merged = dict(s)          # 保留它可能已有的 subscription_active_until 等字段
            merged.update(summary)
            summaries[i] = merged
            replaced = True
            break
    if not replaced:
        summaries.append(summary)
    idx["accounts"] = summaries
    idx.setdefault("version", "1.0")
    idx.setdefault("detail_schema_version", 2)
    _backup(idx_path)
    _atomic_write(idx_path, json.dumps(idx, indent=2, ensure_ascii=False) + "\n", chmod=0o600)

    # 供应商表：按 id 或 baseUrl 去重后 upsert。
    provs = _read_json(prov_path, None)
    if not isinstance(provs, list):
        provs = []
    norm = base_url.lower().rstrip("/")
    hit = None
    for i, p in enumerate(provs):
        if not isinstance(p, dict):
            continue
        if p.get("id") == MANAGED_PROVIDER_ID or str(p.get("baseUrl") or "").lower().rstrip("/") == norm:
            hit = i
            break
    if hit is None:
        provs.append(prov)
    else:
        merged = dict(provs[hit])
        merged.update(prov)
        merged["createdAt"] = provs[hit].get("createdAt") or prov["createdAt"]
        provs[hit] = merged
    _backup(prov_path)
    _atomic_write(prov_path, json.dumps(provs, indent=2, ensure_ascii=False) + "\n", chmod=0o600)

    plan["wrote"] = [acc_path, idx_path, prov_path]
    return plan


def remove(dry_run=False):
    cockpit_ready()
    aid = account_id()
    acc_path = os.path.join(COCKPIT_HOME, ACCOUNTS_DIR_NAME, "%s.json" % aid)
    idx_path = os.path.join(COCKPIT_HOME, INDEX_FILE_NAME)
    prov_path = os.path.join(COCKPIT_HOME, PROVIDERS_FILE_NAME)
    plan = {"account_id": aid, "removed": [], "kept": []}

    if os.path.exists(acc_path):
        plan["removed"].append(acc_path)
        if not dry_run:
            _backup(acc_path)
            os.remove(acc_path)

    idx = _read_json(idx_path, None)
    if isinstance(idx, dict) and isinstance(idx.get("accounts"), list):
        before = len(idx["accounts"])
        idx["accounts"] = [s for s in idx["accounts"]
                           if not (isinstance(s, dict) and s.get("id") == aid)]
        if len(idx["accounts"]) != before:
            # 当前账号正是被删的这个时，把 current 清空，避免 Cockpit 指向不存在的账号。
            if idx.get("current_account_id") == aid:
                idx["current_account_id"] = None
            plan["removed"].append(idx_path)
            if not dry_run:
                _backup(idx_path)
                _atomic_write(idx_path,
                              json.dumps(idx, indent=2, ensure_ascii=False) + "\n",
                              chmod=0o600)

    provs = _read_json(prov_path, None)
    if isinstance(provs, list):
        norm = router_base_url().lower().rstrip("/")
        before = len(provs)
        provs = [p for p in provs
                 if not (isinstance(p, dict)
                         and (p.get("id") == MANAGED_PROVIDER_ID
                              or str(p.get("baseUrl") or "").lower().rstrip("/") == norm))]
        if len(provs) != before:
            plan["removed"].append(prov_path)
            if not dry_run:
                _backup(prov_path)
                _atomic_write(prov_path,
                              json.dumps(provs, indent=2, ensure_ascii=False) + "\n",
                              chmod=0o600)

    if not plan["removed"]:
        plan["kept"].append("未注册过，无需注销")
    return plan


def status():
    aid = account_id()
    acc_path = os.path.join(COCKPIT_HOME, ACCOUNTS_DIR_NAME, "%s.json" % aid)
    idx_path = os.path.join(COCKPIT_HOME, INDEX_FILE_NAME)
    prov_path = os.path.join(COCKPIT_HOME, PROVIDERS_FILE_NAME)
    out = {
        "cockpit_home": COCKPIT_HOME,
        "cockpit_installed": os.path.isdir(COCKPIT_HOME),
        "account_id": aid,
        "account_registered": os.path.exists(acc_path),
        "router_base_url": router_base_url(),
        "models": routed_models()[0],
        "app_speed_would_be": current_app_speed(),
    }
    idx = _read_json(idx_path, None)
    if isinstance(idx, dict):
        out["current_account_id"] = idx.get("current_account_id")
        out["indexed"] = any(isinstance(s, dict) and s.get("id") == aid
                             for s in (idx.get("accounts") or []))
        out["total_accounts"] = len(idx.get("accounts") or [])
    provs = _read_json(prov_path, None)
    out["provider_registered"] = isinstance(provs, list) and any(
        isinstance(p, dict) and p.get("id") == MANAGED_PROVIDER_ID for p in provs)
    # 当前 ~/.codex 生效的是哪一态：指向 codex_local_access 或 router 都算中转态。
    txt = _read_config_text()
    if 'model_provider = "%s"' % MANAGED_PROVIDER_ID in txt:
        out["codex_now"] = "router"
    elif 'model_provider = "router"' in txt:
        out["codex_now"] = "router(legacy-provider-id)"
    else:
        out["codex_now"] = "native"
    return out


def main():
    ap = argparse.ArgumentParser(
        description="把本地 router 注册成 Cockpit Tools 的可切换账号")
    ap.add_argument("--remove", action="store_true",
                    help="注销（删掉注册进去的账号与供应商）")
    ap.add_argument("--status", action="store_true", help="只看状态，不改任何东西")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入的内容，不落盘")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        return 0

    res = remove(dry_run=args.dry_run) if args.remove else register(dry_run=args.dry_run)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。", file=sys.stderr)
        return 0
    if args.remove:
        print("\n已注销。重启 Cockpit Tools 后账号列表里不再有「%s」。"
              % ACCOUNT_DISPLAY_NAME, file=sys.stderr)
        return 0
    print("\n已注册。请**重启 Cockpit Tools**（它的供应商表有内存缓存，不重启看不到），"
          "然后在账号列表里选「%s」即切到中转，选你的 ChatGPT 账号即切回原生。"
          "\n注意：切换会重启 ChatGPT.app，当前正在跑的 Codex 会话会被中断。"
          % ACCOUNT_DISPLAY_NAME, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
