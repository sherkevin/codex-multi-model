#!/usr/bin/env python3
r"""运行模式切换的回归测试：custom ↔ factory ↔ native 三态往返。纯 stdlib，不打真实上游。

为什么要有它：这三个坑都属于「看着切成功了、实际没切干净」，肉眼看不出来，
而且每一个都是实测踩出来的（不照做就复现）：

  1. auth.json = {} 或 {"OPENAI_API_KEY": null} 时，`codex login status` 仍报
     **"Logged in using ChatGPT"**。只有**文件不存在**才报 "Not logged in"。
     所以出厂态必须删文件而不是清空内容——否则用户点了 Restore，桌面端还是登录态，
     交给 Cockpit Tools 接管时它看到的是「已登录」，切号器不工作。
  2. tomlkit 的 `del doc["review_model"]` 会把键头上**紧邻的注释留在原地**，注释于是
     漂到下一个键头上。出厂态要的是一份干净的官方配置，留着几段讲中转/讲 ideaLAB 的
     孤儿注释会很误导。所以走文本级删除（strip_mode_key_blocks）。
  3. factory 必须是**规范态、每次现推**，不能用快照。快照是死的：旧版逻辑留下的快照
     会让 Restore 之后仍带残留，而 live 的共享配置会随 Codex 升级、装插件不断变化。

测试分两部分：
  Part 1/1b 单元  直接导入 config_io，断言文本级删键、三态判定、往返逐字节还原（无网络）。
  Part 2 端到端   起真 console server（隔离 CODEX_HOME），用 HTTP 打 /api/mode，再拿
                  **真实 codex 二进制**验证「出厂态确实未登录、配置确实能加载」。

所有测试都在 tempfile 里跑，绝不碰用户真实的 ~/.codex 与 Keychain。

用法：
    python3 tests/test_run_mode_switch.py
退出码非 0 = 有断言失败。
"""
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSOLE = os.path.join(REPO, "codex-console")
CONFIG_IO = os.path.join(CONSOLE, "config_io.py")
SERVER = os.path.join(CONSOLE, "server.py")

FAILURES = []


def check(ok, label, detail=""):
    print(("  OK   " if ok else "  FAIL ") + label + ((" — " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# 一份「自定义中转」态的真实感配置：模式专属键（带中文注释）+ 共享键（hooks/mcp/features/tui）。
CUSTOM_CONFIG = '''model = "moonshot/kimi-k3"
model_reasoning_effort = "xhigh"
# ideaLAB 原生 /responses 在 review 这类请求下不发 response.completed，
# 走代理翻译模式的模型反而稳定（完成事件由代理保证完整），实测可用。
review_model = "bailian/glm-5.2"
notify = ["/bin/echo", "turn-ended"]
service_tier = "priority"

# 自定义模型目录：让 MiniMax-M3 出现在 /model 列表里。
# 注意 model_catalog_json 是【整体替换】内置目录，不是合并。
model_catalog_json = "/Users/jingwu/.codex/custom-model-catalog.json"
model_provider = "router"

["hooks"."state"]

["hooks"."state"."/Users/jingwu/.codex/hooks.json:session_start:1:0"]
trusted_hash = "sha256:7105194fca83a5a1cfaf84943dea5fe63e93adbb7d7ec09b3bc19641daaa0dea"

# BEGIN otel-codex-hook trust
["hooks"."state"."/Users/jingwu/.codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:4feb27e89d7be75a6d72b60777fe13450a83c7ed7fdd5dbb779060bc57e21649"
# END otel-codex-hook trust

# stdio 型 MCP server 必须带 command，否则 codex 会报 invalid transport。
[mcp_servers.arxiv]
command = "npx"
args = ["-y", "arxiv-mcp-server"]
startup_timeout_sec = 30

[model_providers.router]
name = "codex-router"
base_url = "http://127.0.0.1:8317/v1"
env_key = "ROUTER_API_KEY"
wire_api = "responses"
query_params = { reasoning_effort = "xhigh" }

[model_providers.router.http_headers]
X-Trace = "router"

[model_providers.idealab]
name = "ideaLAB"
base_url = "https://idealab.example.com/api/openai/v1"
env_key = "IDEALAB_API_KEY"
wire_api = "responses"

[features]
hooks = true

[tui]
theme = "dark"
'''

APIKEY_AUTH = ('{"OPENAI_API_KEY":"sk-placeholder-not-a-real-key",'
               '"auth_mode":"apikey","last_refresh":"2026-09-15T09:00:00Z"}')

# 「原生 ChatGPT」态的配置：没有任何中转专属键（无 model_provider / review_model /
# model_catalog_json / model_providers 表），只有 model 与思考档，外加共享键。
# 不能复用 CUSTOM_CONFIG —— 它带着 model_provider="router"，会被判成 custom。
# 这个优先级是对的：Cockpit Tools 也是靠 provider 认态的，登录方式反而是次要信号。
NATIVE_CONFIG = '''model = "gpt-5.6-sol"
model_reasoning_effort = "high"
notify = ["/bin/echo", "turn-ended"]
service_tier = "priority"

["hooks"."state"]

# BEGIN otel-codex-hook trust
["hooks"."state"."/Users/jingwu/.codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:4feb27e89d7be75a6d72b60777fe13450a83c7ed7fdd5dbb779060bc57e21649"
# END otel-codex-hook trust

# stdio 型 MCP server 必须带 command，否则 codex 会报 invalid transport。
[mcp_servers.arxiv]
command = "npx"
args = ["-y", "arxiv-mcp-server"]
startup_timeout_sec = 30

[features]
hooks = true

[tui]
theme = "dark"
'''


def _fake_jwt(hours_ahead=24 * 365):
    """假 access_token：exp 设在远未来，让 login_liveness 判它 alive，不用真去刷新。"""
    import base64
    head = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = {"exp": int(time.time()) + hours_ahead * 3600, "sub": "user_test"}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{head}.{body}.fakesig"


CHATGPT_AUTH = json.dumps({
    "auth_mode": "chatgpt",
    "tokens": {"access_token": _fake_jwt(), "refresh_token": "rt_fake",
               "id_token": _fake_jwt(), "account_id": "acct_fake_0001"},
    "last_refresh": "2026-09-15T09:00:00Z",
})


def make_home(config=CUSTOM_CONFIG, auth=APIKEY_AUTH, native_backup=None):
    """造一个隔离的 CODEX_HOME 并返回路径。auth=None 表示不放 auth.json。"""
    home = tempfile.mkdtemp(prefix="cx-mode-test-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as f:
        f.write(config)
    if auth is not None:
        p = os.path.join(home, "auth.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(auth)
        os.chmod(p, 0o600)
    if native_backup:
        with open(os.path.join(home, "config.toml.bak"), "w", encoding="utf-8") as f:
            f.write(native_backup)
    return home


def load_config_io(home):
    """按路径导入 config_io，并把 CODEX_HOME 指到隔离目录。

    config_io 在模块顶层就把 CODEX_HOME/CONFIG_PATH/AUTH_PATH 算成常量，所以必须先设
    环境变量再导入；而且每个 home 都要导入一份新的（缓存的常量不会跟着变）。
    """
    os.environ["CODEX_HOME"] = home
    name = "config_io_%s" % abs(hash(home))
    spec = importlib.util.spec_from_file_location(name, CONFIG_IO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────── Part 1: 单元 ───────────────────────────

def part1():
    print("== Part 1: 文本级删键与三态判定（单元）==")

    home = make_home()
    try:
        cio = load_config_io(home)
        import tomlkit

        # 1. strip_mode_key_blocks 删干净所有模式专属键，且共享键一个不动。
        stripped = cio.strip_mode_key_blocks(CUSTOM_CONFIG, cio.MODE_KEYS)
        for k in cio.MODE_KEYS:
            check(not any(ln.strip().startswith(k + " ") or ln.strip().startswith(k + "=")
                          for ln in stripped.splitlines()),
                  "出厂态删掉了模式专属键 %s" % k)
        check("model_provider" not in stripped, "model_provider 已删")
        check("[model_providers" not in stripped, "model_providers 整张表已删（含子表）")
        check("router" not in stripped and "idealab" not in stripped,
              "中转/第三方 provider 名不再出现")

        # 2. 注释跟着键一起走：不能留下讲中转的孤儿注释。
        check("response.completed" not in stripped, "review_model 头上的中文注释一并删除")
        check("custom-model-catalog" not in stripped, "model_catalog_json 的注释一并删除")
        check("整体替换" not in stripped, "model_catalog_json 的第二行注释一并删除")

        # 3. 共享键必须逐字保留（含 hooks 的 BEGIN/END 标记与 mcp 的中文注释）。
        for keep in ('notify = ["/bin/echo", "turn-ended"]', "service_tier",
                     "# BEGIN otel-codex-hook trust", "# END otel-codex-hook trust",
                     "[mcp_servers.arxiv]", 'command = "npx"',
                     "stdio 型 MCP server 必须带 command",
                     "[features]", "hooks = true", "[tui]", 'theme = "dark"',
                     'trusted_hash = "sha256:7105194'):
            check(keep in stripped, "共享内容保留：%s" % keep[:44])

        # 4. 删完不能留下三连空行（文件头一串空白很难看）。
        check("\n\n\n" not in stripped, "连续空行已收敛")

        # 5. 结果仍是合法 TOML，且只剩共享键。
        doc = tomlkit.parse(stripped)
        left = set(str(k) for k in doc.keys())
        check(not (left & set(cio.MODE_KEYS)), "解析后无模式专属键残留：%s" % sorted(left))
        check({"notify", "service_tier", "hooks", "mcp_servers", "features", "tui"} <= left,
              "共享键齐全：%s" % sorted(left))

        # 6. 幂等：对已删干净的文本再删一次，逐字节不变。
        check(cio.strip_mode_key_blocks(stripped, cio.MODE_KEYS) == stripped, "strip 幂等")

        # 7. 三态判定：apikey → custom；无 auth.json → factory；OAuth → native。
        check(cio.read_run_mode()["mode"] == "custom", "apikey 登录判为 custom")

        home_f = make_home(config=stripped, auth=None)
        cio_f = load_config_io(home_f)
        m_f = cio_f.read_run_mode()
        check(m_f["mode"] == "factory", "auth.json 不存在判为 factory")
        check(m_f["auth_exists"] is False, "factory 下 auth_exists=False")
        check(m_f["factory_residual_keys"] == [], "干净出厂态无残留键")

        home_n = make_home(config=NATIVE_CONFIG, auth=CHATGPT_AUTH)
        cio_n = load_config_io(home_n)
        m_n = cio_n.read_run_mode()
        check(m_n["mode"] == "native",
              "OAuth 登录 + 原生配置（无 model_provider）判为 native", m_n["mode"])
        check(m_n["login_alive"] is True, "假 JWT（exp 在一年后）判为 alive")

        # 8a. Cockpit Tools 写的 provider 名要认得出来：判成 custom，不能当 factory。
        #     否则用户用着 Cockpit，配置台却显示「出厂未登录」，点 Restore 会白干一场。
        cfg_ck = 'model_provider = "codex_local_access"\n' + stripped
        home_ck = make_home(config=cfg_ck, auth=None)
        cio_ck = load_config_io(home_ck)
        m_ck = cio_ck.read_run_mode()
        check(m_ck["mode"] == "custom", "codex_local_access（Cockpit 写的）判为 custom",
              m_ck["mode"])

        # 8b. 真·出厂残留：auth.json 没了（=factory），但 config.toml 里还留着模式专属键。
        #     场景是别的工具删了登录态却把 provider 表留下了。这种残留必须报出来，
        #     而且再点一次 Restore 要真的清掉它 —— already_there 不能因为「已经是
        #     factory」就直接返回 ok，否则用户点了 Restore 却什么都没发生。
        #     注意残留键不能用 model_provider="router"/"codex_local_access"，那两个值
        #     会把状态判成 custom（见 8a），就走不到 factory 的残留分支了。
        cfg_dirty = ('review_model = "bailian/glm-5.2"\n' + stripped
                     + '\n[model_providers.somewhere_else]\n'
                       'name = "别的工具留下的"\n'
                       'base_url = "http://127.0.0.1:9999/v1"\n'
                       'env_key = "OTHER_KEY"\n'
                       'wire_api = "responses"\n')
        home_d = make_home(config=cfg_dirty, auth=None)
        cio_d = load_config_io(home_d)
        m_d = cio_d.read_run_mode()
        check(m_d["mode"] == "factory", "无 auth.json 仍判为 factory（残留不改变态）",
              m_d["mode"])
        check(set(m_d["factory_residual_keys"]) >= {"review_model", "model_providers"},
              "检测出别的工具残留的键", str(m_d["factory_residual_keys"]))

        rd = cio_d.set_run_mode("factory")
        check(rd["ok"] is True and rd["changed"] is True,
              "残留态下 Restore 会真的清一次（不被 already_there 挡住）",
              str(rd.get("detail")))
        m_d2 = cio_d.read_run_mode()
        check(m_d2["factory_residual_keys"] == [], "清完无残留",
              str(m_d2["factory_residual_keys"]))
        dirty_text = open(os.path.join(home_d, "config.toml"), encoding="utf-8").read()
        check("somewhere_else" not in dirty_text and "review_model" not in dirty_text,
              "残留键与其表体都从文件里消失了")
        check("[mcp_servers.arxiv]" in dirty_text, "清残留时没误伤共享键")
        # 清完之后已经干净，再点 Restore 就该是 no-op 了
        rd2 = cio_d.set_run_mode("factory")
        check(rd2["changed"] is False, "干净后再点 Restore 是 no-op", str(rd2.get("detail")))

        for h in (home_f, home_n, home_d, home_ck):
            shutil.rmtree(h, ignore_errors=True)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def part1b():
    print("== Part 1b: set_run_mode 三态往返（进程内）==")

    # custom → factory → custom，config.toml 必须逐字节还原。
    home = make_home()
    cfg_path = os.path.join(home, "config.toml")
    auth_path = os.path.join(home, "auth.json")
    before_cfg, before_auth = md5(cfg_path), md5(auth_path)
    try:
        cio = load_config_io(home)

        r = cio.set_run_mode("factory")
        check(r["ok"] is True, "切 factory 返回 ok", str(r.get("detail")))
        check(r["changed"] is True, "切 factory 确实改了东西")
        check(r.get("need_restart") is True, "切 factory 提示需要重启")
        check(not os.path.exists(auth_path), "auth.json 被删除（不是清空）")
        check(cio.read_run_mode()["mode"] == "factory", "切完判为 factory")
        check(cio.read_run_mode()["factory_residual_keys"] == [], "切完无残留键")
        check(len(r.get("removed_custom_keys") or []) >= 5,
              "删掉了模式专属键", str(r.get("removed_custom_keys")))

        # 出厂态必须留了备份，否则切不回来。
        baks = [f for f in os.listdir(home) if f.startswith("auth.json.bak")]
        check(len(baks) >= 1, "auth.json 已备份", str(baks))
        # apikey 登录没有 OAuth 可存，所以不该产生 auth.json.bak-chatgpt-*。
        # Apply 时会由 ensure_auth_bypass 重新写占位 key —— 这正是中转免登录的设计。
        check(cio.read_run_mode()["has_oauth_backup"] is False,
              "apikey 态不产生 OAuth 备份（Apply 时重建占位 key）")
        check([b for b in baks if "chatgpt" in b] == [],
              "确认没有误存 chatgpt 备份", str(baks))

        # 幂等：已经是 factory 再切一次，不该重复备份/报错。
        r2 = cio.set_run_mode("factory")
        check(r2["ok"] is True and r2["changed"] is False, "重复切 factory 是 no-op",
              str(r2.get("detail")))

        # 切回 custom：逐字节还原。
        r3 = cio.set_run_mode("custom")
        check(r3["ok"] is True, "切回 custom 返回 ok", str(r3.get("detail")))
        check(md5(cfg_path) == before_cfg, "config.toml 逐字节还原",
              "%s vs %s" % (before_cfg, md5(cfg_path)))
        check(os.path.exists(auth_path), "auth.json 已恢复")
        # auth.json 是**语义**还原而非逐字节：ensure_auth_bypass 会重写成 indent=2 的
        # 规范形式并补上占位 key。config.toml 才要求逐字节（那上面有用户的注释与键序）。
        restored_auth = json.load(open(auth_path, encoding="utf-8"))
        check(restored_auth.get("auth_mode") == "apikey"
              and bool(restored_auth.get("OPENAI_API_KEY")),
              "auth.json 语义还原：apikey 模式 + 有 key", str(restored_auth))
        check(cio.login_liveness().get("alive") is True,
              "还原后登录态可用（apikey 不存在过期问题）")
        check(cio.read_run_mode()["mode"] == "custom", "切回后判为 custom")
        check("model_provider" in open(cfg_path, encoding="utf-8").read(),
              "model_provider 回来了")
    finally:
        shutil.rmtree(home, ignore_errors=True)

    # factory → native：ChatGPT 登录态必须还原（从备份里），config 退回原生。
    # 起点必须是真正的原生配置：若用 CUSTOM_CONFIG（带 model_provider="router"），
    # read_run_mode 会判成 custom，快照存进 state["custom"]，切回 native 就取不到它。
    home2 = make_home(config=NATIVE_CONFIG, auth=CHATGPT_AUTH,
                      native_backup=NATIVE_CONFIG)
    cfg2 = os.path.join(home2, "config.toml")
    try:
        cio2 = load_config_io(home2)
        check(cio2.read_run_mode()["mode"] == "native", "前置：判为 native")
        native_cfg_md5 = md5(cfg2)

        rf = cio2.set_run_mode("factory")
        check(rf["ok"] is True and not os.path.exists(os.path.join(home2, "auth.json")),
              "native → factory：登录态被清除")

        rn = cio2.set_run_mode("native")
        check(rn["ok"] is True, "factory → native 返回 ok", str(rn.get("detail")))
        check(rn.get("restored_login") is True, "native 还原了登录态",
              str(rn.get("auth_detail")))
        check(os.path.exists(os.path.join(home2, "auth.json")), "auth.json 回来了")
        a = json.load(open(os.path.join(home2, "auth.json"), encoding="utf-8"))
        check(a.get("auth_mode") == "chatgpt" and (a.get("tokens") or {}).get("access_token"),
              "还原的是完整 OAuth（含 access_token）")
        check(rn.get("need_login") is not True, "不需要重新登录", str(rn.get("login_reason")))
        check(md5(cfg2) == native_cfg_md5, "native 的 config.toml 逐字节还原")
        check("model_provider" not in open(cfg2, encoding="utf-8").read(),
              "native 态没有 model_provider")
    finally:
        shutil.rmtree(home2, ignore_errors=True)

    # factory 是现推的：共享配置在出厂态期间被别人改了，切回 custom 时要跟过来。
    home3 = make_home()
    cfg3 = os.path.join(home3, "config.toml")
    try:
        cio3 = load_config_io(home3)
        cio3.set_run_mode("factory")
        # 模拟用户在出厂态（或 Cockpit）里加了一个共享键
        with open(cfg3, "a", encoding="utf-8") as f:
            f.write('\n[shell_environment_policy]\ninherit = "all"\n')
        r = cio3.set_run_mode("custom")
        out = open(cfg3, encoding="utf-8").read()
        check("shell_environment_policy" in out, "出厂态期间新增的共享键切回后仍在")
        check("model_provider" in out, "同时模式专属键也还原了")
        check(r.get("ok") is True, "这次切换本身成功", str(r.get("detail")))
    finally:
        shutil.rmtree(home3, ignore_errors=True)


# ─────────────────── Part 2: 端到端（真 server + 真 codex 二进制）───────────────────

def _wait_port(port, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _api(port, path, target=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if target is None:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    req = urllib.request.Request(url, data=json.dumps({"target": target}).encode(),
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def part2():
    print("== Part 2: HTTP 往返 + 真实 codex 二进制校验 ==")

    port = _free_port()
    home = make_home()
    cfg_path = os.path.join(home, "config.toml")
    before_cfg = md5(cfg_path)
    env = dict(os.environ, CODEX_HOME=home, CODEX_CONSOLE_PORT=str(port))
    proc = subprocess.Popen([sys.executable, SERVER], cwd=CONSOLE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        check(_wait_port(port), "console server 起来了（:%d）" % port)

        st, m0 = _api(port, "/api/mode")
        check(st == 200 and m0["mode"] == "custom", "GET /api/mode 起始为 custom",
              "%s %s" % (st, m0.get("mode")))

        # 非法 target 必须 400，不能默默当成某一态
        st_bad, bad = _api(port, "/api/mode", target="bogus")
        check(st_bad == 400, "非法 target 返回 400", "%s %s" % (st_bad, bad))

        # Restore（factory）
        st, rf = _api(port, "/api/mode", target="factory")
        check(st == 200 and rf.get("ok") is True, "POST factory 成功",
              "%s %s" % (st, rf.get("detail")))
        check(not os.path.exists(os.path.join(home, "auth.json")), "HTTP：auth.json 已删")
        st, m1 = _api(port, "/api/mode")
        check(m1["mode"] == "factory", "HTTP：判为 factory")
        check(m1["factory_residual_keys"] == [], "HTTP：无残留键")

        # 真实 codex 二进制：出厂态必须报「未登录」，且配置必须能加载
        cb = shutil.which("codex")
        ce = dict(os.environ, CODEX_HOME=home)
        if cb:
            p = subprocess.run([cb, "login", "status"], env=ce, capture_output=True,
                               text=True, timeout=90)
            out = (p.stdout + p.stderr).strip()
            check("Not logged in" in out, "真 codex：出厂态确实未登录", out[:160])
            check("Error loading configuration" not in out,
                  "真 codex：出厂配置能加载（无解析错误）", out[:160])
        else:
            check(True, "真 codex 二进制不在 PATH，跳过二进制校验")

        # Apply（custom）：逐字节还原
        st, ac = _api(port, "/api/mode", target="custom")
        check(st == 200 and ac.get("ok") is True, "POST custom 成功",
              "%s %s" % (st, ac.get("detail")))
        check(md5(cfg_path) == before_cfg, "HTTP：config.toml 逐字节还原",
              "%s vs %s" % (before_cfg, md5(cfg_path)))
        check(os.path.exists(os.path.join(home, "auth.json")), "HTTP：auth.json 回来了")
        st, m2 = _api(port, "/api/mode")
        check(m2["mode"] == "custom", "HTTP：判回 custom")

        if cb:
            p = subprocess.run([cb, "login", "status"], env=ce, capture_output=True,
                               text=True, timeout=90)
            out = (p.stdout + p.stderr).strip()
            check("API key" in out or "api key" in out.lower(),
                  "真 codex：Apply 后恢复成 apikey 免登录态", out[:160])
            check("Error loading configuration" not in out,
                  "真 codex：Apply 后配置能加载", out[:160])

        # 静态资源还在（UI 没被改坏）
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=10) as r:
            html = r.read().decode()
        check("btnRestore" in html and "btnApply" in html, "UI：Restore/Apply 按钮存在")
        with urllib.request.urlopen("http://127.0.0.1:%d/app.js" % port, timeout=10) as r:
            js = r.read().decode()
        check("quickSwitch" in js and '"factory"' in js, "UI：app.js 接了 factory 分支")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)


def main():
    print("运行模式切换回归测试（custom ↔ factory ↔ native）\n")
    t0 = time.time()
    part1()
    print()
    part1b()
    print()
    part2()
    print()
    if FAILURES:
        print("✗ %d 项失败（%.1fs）：" % (len(FAILURES), time.time() - t0))
        for f in FAILURES:
            print("   - " + f)
        return 1
    print("✓ 全部通过（%.1fs）" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
