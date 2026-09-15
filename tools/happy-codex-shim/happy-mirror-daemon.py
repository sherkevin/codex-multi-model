#!/usr/bin/env python3
"""
happy-mirror-daemon.py — 自动把「电脑上每个正在跑的 Codex 窗口」暴露到手机 Happy。

它补的是镜像垫片的最后一块缺口：垫片解决了「能不能接管」，但接管仍要人敲命令。
守护进程让手机端的「探知」变成自动的——窗口一启动，几秒内手机上就出现一个会话，
可看可插话，且不打扰那个窗口本身。

判定依据（两条都是实测得出的硬约束，不是保守估计）：

  1. 只镜像「此刻被占住」的线程。
     ~/.codex/thread-writer-locks/<id>.lock 被 flock 占住 = 有窗口正在跑。
     空闲线程绝不能镜像：垫片撞不到 "already has an active writer" 就会走透传，
     于是镜像进程自己成了 writer，反过来把用户想开的那个窗口挡住。这是必须守住的边界。

  2. 排除 Happy 自己起的会话。
     `happy codex` 新开会话时，持锁者是垫片透传出来的真 codex，其祖先进程链里有垫片。
     这种线程手机上本来就有一个原生 Happy 会话，再镜像就是同一个线程出现两条。
     用 lsof 拿持锁 pid，向上走 ppid 链，命中垫片/happy 即跳过。

还有一条竞态防护：探测到「被占」与 resume 真正落地之间窗口可能刚好关掉，
那样 resume 会成功、镜像进程会抢到 writer lock。所以每个新镜像都要在限时内
于垫片日志里看到 "MIRROR ON"，看不到就杀掉，绝不留一个会挡路的进程。

另外两条是实测故障后补的，不是预防性设计：

  3. 守护必须自己保证 happy daemon 在跑。
     手机的 machine 列表只显示「在线」机器，而在线状态由 happy daemon 的长连接维持。
     `happy auth login --force` 会停掉 daemon 且完成后**不会**自动重启，
     实测结果就是手机配对成功、却看不到任何 machine。守护每轮拉起它。

  4. machineId 变了就必须重启全部镜像。
     镜像进程启动时把凭据读进内存，之后换账号（重新扫码配对）它们仍连着旧账号，
     手机上永远看不到。实测踩到：三个镜像在旧账号下一直推流，新账号侧空空如也。
     守护记录起镜像时的 machineId，与当前 settings.json 不一致就全部重启。

用法：
  python3 happy-mirror-daemon.py            # 前台跑，Ctrl-C 退出
  python3 happy-mirror-daemon.py --once     # 只扫一轮（调试/验证用）
  python3 happy-mirror-daemon.py --status   # 看当前镜像了哪些线程
  python3 happy-mirror-daemon.py --stop     # 停掉所有由守护起的镜像
  python3 happy-mirror-daemon.py --fix-happy-daemon   # 只把 happy daemon 拉起

环境变量：
  HAPPY_MIRROR_INTERVAL   轮询秒数，默认 4
  HAPPY_MIRROR_MAX        同时镜像上限，默认 4（每个镜像树约 100MB 常驻）
  HAPPY_MIRROR_LOG        守护自身日志，默认 ~/.happy/mirror-daemon.log
  HAPPY_MIRROR_DIR        每镜像的垫片/happy 日志目录，默认 ~/.happy/mirrors
  HAPPY_MIRROR_STATE      状态文件，默认 ~/.happy/mirror-daemon.json
  HAPPY_MIRROR_VERIFY_S   等 "MIRROR ON" 的秒数，默认 30
"""
import base64
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SHIM_BIN = os.path.join(HERE, "bin")

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
LOCK_DIR = os.path.join(CODEX_HOME, "thread-writer-locks")
SESSIONS_DIR = os.path.join(CODEX_HOME, "sessions")
HAPPY_HOME = os.environ.get("HAPPY_HOME_DIR", os.path.expanduser("~/.happy"))
DAEMON_STATE = os.path.join(HAPPY_HOME, "daemon.state.json")
SETTINGS = os.path.join(HAPPY_HOME, "settings.json")
ACCESS_KEY = os.path.join(HAPPY_HOME, "access.key")
DAEMON_START_LOG = os.path.join(HAPPY_HOME, "mirror-daemon-start.log")

INTERVAL = float(os.environ.get("HAPPY_MIRROR_INTERVAL", "4"))
MAX_MIRRORS = int(os.environ.get("HAPPY_MIRROR_MAX", "4"))
GRACE_S = float(os.environ.get("HAPPY_MIRROR_GRACE", "180"))
LOG_PATH = os.environ.get("HAPPY_MIRROR_LOG", os.path.join(HAPPY_HOME, "mirror-daemon.log"))
MIRROR_DIR = os.environ.get("HAPPY_MIRROR_DIR", os.path.join(HAPPY_HOME, "mirrors"))
STATE_PATH = os.environ.get("HAPPY_MIRROR_STATE", os.path.join(HAPPY_HOME, "mirror-daemon.json"))
VERIFY_S = float(os.environ.get("HAPPY_MIRROR_VERIFY_S", "30"))
# 拉起 happy daemon 的最小间隔：起不来时别每 4 秒重试一次，免得刷日志、堆进程
DAEMON_COOLDOWN = float(os.environ.get("HAPPY_MIRROR_DAEMON_COOLDOWN", "30"))
# HTTP 健康检查的最小间隔（秒）
DAEMON_HTTP_EVERY = float(os.environ.get("HAPPY_MIRROR_DAEMON_HTTP_EVERY", "30"))

# 祖先进程链里出现这些字样，说明这条线程是 Happy 自己起的，不再镜像
OWN_MARKERS = ("happy-codex-shim", "happy/dist/index.mjs", "happy codex", "/bin/happy")

# 因名额上限被跳过的线程：只记一次日志，免得每 4 秒刷一条同样的话
SKIP_LOGGED = set()

# 本轮运行期间还握着的 Popen 句柄：tid -> Popen。
# 必须留着，因为要 poll() 回收子进程；不回收的话它会变僵尸，
# 而 os.kill(pid, 0) 对僵尸**照样成功**，alive() 就永远说「活着」，
# 那条镜像记录再也回收不掉（实测踩到：包装进程 defunct，真 node 子进程还在跑）。
PROCS = {}

# happy daemon 的 HTTP 健康检查连续失败次数。
# 单次失败不足以判定它死了（端口偶尔忙），连续两次才动手——误判的代价
# 是重启 daemon、手机上 machine 短暂离线、丢掉它正在跟踪的会话。
DAEMON_HTTP_FAILS = 0
# 上次尝试拉起 happy daemon 的时间，配合 DAEMON_COOLDOWN 限流
DAEMON_SPAWN_AT = 0.0
# 拉起 happy daemon 的 Popen 句柄：必须留着 poll() 回收，否则变僵尸
DAEMON_SPAWN_PROC = None
# 「state 文件失效但有 daemon 进程在跑」只记一次日志，免得每 4 秒刷一条
DAEMON_ORPHAN_LOGGED = False
# 上次 HTTP 健康检查的时间。HTTP 检查每轮都做会往 happy 自己的日志里
# 每 4 秒塞一条 "Listing N sessions"，节流到 30s 一次。
# pid 检查仍然每轮做——真死掉的情况它当场就能发现，不用等 HTTP。
DAEMON_HTTP_AT = 0.0


def ensure_path():
    """把 node / happy / codex 所在目录补进 PATH。

    这一步不是保险措施，是 launchd 下能不能跑起来的前提：launchd 给的 PATH
    极简（实测 /usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.），里面没有 node。
    而 happy.mjs 的 shebang 是 `#!/usr/bin/env node`——PATH 里找不到 node，
    起 happy 就是 ENOENT，整个机制静默失效。
    """
    extra = []
    for tool in ("node", "happy", "codex"):
        found = shutil.which(tool)
        if found:
            extra.append(os.path.dirname(os.path.realpath(found)))
    for cand in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.isdir(cand):
            extra.append(cand)
    # lsof 在 /usr/sbin，而 launchd 的 PATH 不含它。少了这条，
    # owned_by_happy() 会静默失败（FileNotFoundError 被吞），
    # 于是 Happy 自己起的会话也被镜像一遍，手机上出现两条重复。
    for cand in ("/usr/sbin", "/sbin"):
        if os.path.isdir(cand):
            extra.append(cand)
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep) if cur else []
    for d in reversed(extra):  # reversed 让先列出的排在前面
        if d not in parts:
            parts.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(parts))


def resolve_happy():
    """裸 `happy` 在 launchd 下找不到，必须解析成绝对路径。"""
    found = shutil.which("happy")
    if found:
        return os.path.realpath(found)
    for cand in ("/opt/homebrew/bin/happy", "/usr/local/bin/happy",
                 os.path.expanduser("~/.npm-global/bin/happy")):
        if os.path.exists(cand):
            return os.path.realpath(cand)
    return "happy"


def log(*args):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(a) for a in args))
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(a) for a in args)))
    except OSError:
        pass


# ── 探测：谁正在跑 ─────────────────────────────────────────────
def live_thread_ids():
    """flock 试锁：拿不到 = 有窗口占着。只试探、立刻释放，不干扰任何窗口。

    fcntl 是 POSIX 专属，Windows 上 import 即 ImportError。守护本体是 macOS
    launchd 场景，但按 ADR 0007（平台特性可选且可降级）这里惰性导入：拿不到
    fcntl 就返回空列表，让上层走「没有可镜像的窗口」分支，而不是起不来。
    """
    try:
        import fcntl
    except ImportError:
        return []
    held = []
    for p in sorted(glob.glob(os.path.join(LOCK_DIR, "*.lock"))):
        name = os.path.basename(p)
        if name.startswith("."):
            continue
        fd = None
        try:
            fd = os.open(p, os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)  # 试到了就说明空闲
        except BlockingIOError:
            held.append(name[: -len(".lock")])
        except OSError:
            continue
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return held


def find_lsof():
    """绝对路径解析 lsof，不依赖 PATH（见 ensure_path 里的说明）。"""
    found = shutil.which("lsof")
    if found:
        return found
    for cand in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        if os.path.exists(cand):
            return cand
    return None


LSOF = find_lsof()


def lock_holder_pids(thread_id):
    lock = os.path.join(LOCK_DIR, thread_id + ".lock")
    if not LSOF:
        return []
    try:
        out = subprocess.run([LSOF, "-t", lock], capture_output=True, text=True, timeout=15)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except (subprocess.SubprocessError, OSError):
        return []


def pid_command(pid):
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def pid_ppid(pid):
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
        s = out.stdout.strip()
        return int(s) if s.isdigit() else 0
    except (subprocess.SubprocessError, OSError):
        return 0


def owned_by_happy(thread_id):
    """持锁者的祖先进程链里有垫片/happy → 这是 Happy 自己起的会话，手机上已有一条。"""
    if not LSOF:
        # 探测不了就不能瞎猜。返回 False 会让 Happy 自己的会话被重复镜像，
        # 所以这里显式记一条日志，便于发现环境缺 lsof 这种问题。
        log("警告：找不到 lsof，无法排除 Happy 自起会话，可能重复镜像")
        return False
    for pid in lock_holder_pids(thread_id):
        cur = pid
        for _ in range(8):  # 链路深度足够，且防止意外成环
            if cur <= 1:
                break
            cmd = pid_command(cur)
            if any(m in cmd for m in OWN_MARKERS):
                return True
            nxt = pid_ppid(cur)
            if nxt == cur:
                break
            cur = nxt
    return False


def thread_meta(thread_id):
    """取 cwd/标题。cwd 必须对：Happy 的会话元数据用的是 process.cwd()，
    起错目录，手机上这个项目就归错地方了。

    来源用 rollout 的 session_meta，而不是查 state_5.sqlite：
      - 这是垫片自己认的同一份事实，不会出现两个来源打架；
      - 只读文件，完全不碰 sqlite，避开 busy/锁的所有麻烦。
    """
    pat = os.path.join(SESSIONS_DIR, "*", "*", "*", "rollout-*%s*.jsonl" % thread_id)
    hits = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
    for p in hits:
        try:
            with open(p, errors="replace") as f:
                first = f.readline()
            d = json.loads(first)
            pay = d.get("payload", d) or {}
            if pay.get("type") == "session_meta" or "cwd" in pay:
                meta = {"cwd": pay.get("cwd"), "originator": pay.get("originator"),
                        "source": pay.get("source"), "rollout": p}
                # 标题取首条用户消息的前若干字，方便在手机列表里认出是哪个窗口
                meta["title"] = first_user_text(p)
                return meta
        except (OSError, ValueError):
            continue
    return {}


def first_user_text(rollout_path, limit=60):
    """扫前若干行找首条真实用户消息当标题。只读前 512KB，不整文件加载
    （大的 rollout 有 25MB+，逐行全扫会拖慢每一轮发现）。

    rollout 里 role=user 的行不全是用户打的字：开头会注入 AGENTS.md、
    <environment_context>、skills 清单这类上下文，必须跳过，否则标题变成一坨系统文本。
    """
    try:
        with open(rollout_path, errors="replace") as f:
            head = f.read(524288)
    except OSError:
        return ""
    skip = ("AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>",
            "<system-reminder>", "<user-prompt-submit-hook>")
    for line in head.splitlines():
        if '"role":"user"' not in line and '"role": "user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        pay = d.get("payload", d) or {}
        if pay.get("type") not in ("message", "user_message", None):
            continue
        txt = pay.get("message")
        if not txt:
            content = pay.get("content")
            if isinstance(content, list):
                txt = " ".join(str(c.get("text", "")) for c in content
                               if isinstance(c, dict) and c.get("text"))
            elif isinstance(content, str):
                txt = content
        if not txt:
            continue
        if txt.lstrip().startswith("<") or any(s in txt for s in skip):
            continue
        return " ".join(txt.split())[:limit]
    return ""


# ── 状态 ───────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log("状态写入失败:", e)


def alive(pid):
    """进程还在吗？僵尸算「不在」。

    os.kill(pid, 0) 对僵尸进程也成功，所以光靠它会误判。守护重启后从状态文件
    读回来的 pid 没有 Popen 句柄可以 poll()，只能问 ps 拿状态位。
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return not is_zombie(pid)


def is_zombie(pid):
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip().startswith("Z")
    except (subprocess.SubprocessError, OSError):
        return False


def short(tid):
    return tid[:8]


# ── 看护 happy daemon：手机端 machine 列表的生死开关 ─────────────
def daemon_state():
    """读 happy 自己写的 daemon.state.json。读不到/坏了就返回 {}。"""
    try:
        with open(DAEMON_STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def daemon_http_ok(port):
    """HTTP 健康检查，和 happy 自己用的判据一致（POST /list，2s 超时）。

    只看 pid 活不活是不够的：进程在、但连接断了/HTTP 死了的情况，
    手机上一样看不到这台机器，而 pid 检查会说「一切正常」。
    """
    if not port:
        return False
    req = urllib.request.Request(
        "http://127.0.0.1:%d/list" % port, data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def happy_daemon_proc_running():
    """按命令行查 happy daemon 进程是否在跑，不依赖 daemon.state.json。

    存在的理由：state 文件丢了/坏了而 daemon 其实在跑时，光看文件会判它死了，
    于是又起一个——手机上凭空多出一台机器。happy 自己的幂等判断同样读这个文件，
    也拦不住这种情况，所以必须自己按进程查一遍。

    判据用 `daemon start-sync`：这是 happy 真 daemon 的命令行，而我们派出去的是
    `happy daemon start`（不带 -sync），所以不会自己匹配自己。
    这里刻意**不**再加「路径里要有 happy 字样」之类的修饰——那会把判断绑死在
    安装布局上（实测某布局下路径不含该字样，判据就静默失效，
    于是照样起出第二个 daemon）。模式本身已足够特定，宁可保守认定「有人在跑」：
    误判成 True 只是本轮不拉起，下一轮 state 写好后自然恢复正常；
    误判成 False 会真的多起一个进程，那才是难收拾的。
    """
    try:
        out = subprocess.run(["pgrep", "-f", "daemon start-sync"],
                             capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return False
    mine = os.getpid()
    for line in out.stdout.split():
        if line.strip().isdigit() and int(line) != mine:
            return True
    return False


def stop_stale_daemon(pid):
    """收掉一个已判定不健康的 happy daemon。SIGTERM 先礼后兵，2s 不退就 SIGKILL。

    为什么必须先收再起：happy 自己的幂等判断（`checkIfDaemonRunningAndCleanupStaleState`）
    也读 daemon.state.json，而那个文件此刻已经不可信，它拦不住第二个 daemon 被起出来。
    happy 的 `stopDaemon` 走的同样是「按 state 里的 pid 杀」，这里与它一致。
    """
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except (OSError, ProcessLookupError):
            return True
        deadline = time.time() + wait
        while time.time() < deadline:
            if not alive(pid):
                return True
            time.sleep(0.2)
    if alive(pid):
        log("happy daemon pid %s 收不掉，本轮不拉起（避免起出第二个）" % pid)
        return False
    return True


def spawn_happy_daemon():
    """拉起 happy daemon。`happy daemon start` 本身是幂等的：
    已有同版本 daemon 在跑，它会打印 "Daemon already running" 然后 exit 0，
    所以这里不需要自己判断「是不是已经在起了」，交给 happy 去判。

    没用 happy 自带的开机自启（autostart）：那条路要往 /Library/LaunchDaemons/
    写文件、需要 root，在守护里弹权限是不可接受的。这台机器的自启由我们自己负责。
    """
    global DAEMON_SPAWN_PROC
    if DAEMON_SPAWN_PROC is not None and DAEMON_SPAWN_PROC.poll() is None:
        return False  # 上一次拉起还没结束，别叠着起
    if DAEMON_SPAWN_PROC is not None:
        DAEMON_SPAWN_PROC.poll()  # 回收，避免僵尸
        DAEMON_SPAWN_PROC = None
    try:
        os.makedirs(HAPPY_HOME, exist_ok=True)
        out = open(DAEMON_START_LOG, "ab")
    except OSError as e:
        log("无法打开 happy daemon 启动日志:", e)
        return False
    try:
        DAEMON_SPAWN_PROC = subprocess.Popen(
            [resolve_happy(), "daemon", "start"],
            cwd=os.path.expanduser("~"), env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True)
    except (OSError, ValueError) as e:
        log("拉起 happy daemon 失败:", type(e).__name__, e)
        return False
    finally:
        out.close()
    log("正在拉起 happy daemon（手机端 machine 列表依赖它在线）")
    return True


def ensure_happy_daemon(force=False):
    """每轮都确认 happy daemon 在线；不在线就拉起。返回 True = 现在在线。

    这是实测故障补的，不是预防性设计：`happy auth login --force` 会停掉 daemon
    且完成后不会自动重启，结果就是「手机配对成功、machine 列表空空如也」，
    看上去像配对没成功，其实是 daemon 没起来。守护每轮兜住这件事。

    成本很低：一次文件读 + kill(pid,0)，只有判死时才真的起进程。
    """
    global DAEMON_HTTP_FAILS, DAEMON_SPAWN_AT, DAEMON_HTTP_AT, DAEMON_ORPHAN_LOGGED

    st = daemon_state()
    pid = st.get("pid")
    port = st.get("httpPort")

    # 判定分三种情况，动作各不相同：
    #   A. state 里的 pid 活着 → 再做 HTTP 检查，健康就返回 True；
    #      不健康则**先收掉它**再起新的（见 stop_stale_daemon 的理由）。
    #   B. pid 死了/没有，但确实有个 daemon 进程在跑（state 文件失效）
    #      → 不拉起。起下去手机上会多出一台机器，而 happy 自己也拦不住
    #      （它的幂等判断同样读这个已经失效的文件）。只记一次日志。
    #   C. 既没有活 pid 也没有 daemon 进程 → 真死了，拉起。
    if pid and alive(pid):
        if not port:
            return True  # 没端口信息，pid 活着就先认它
        # HTTP 检查有节流：还没到点就只凭 pid 判活，直接返回。
        # pid 死了是当场就能发现的（上面 alive() 已经判过），不受节流影响。
        if not force and time.time() - DAEMON_HTTP_AT < DAEMON_HTTP_EVERY:
            return True
        DAEMON_HTTP_AT = time.time()
        # 连续两次 HTTP 失败才判死：单次可能只是端口忙。
        # 误判的代价是杀掉一个正常 daemon、手机端短暂离线。
        if daemon_http_ok(port):
            DAEMON_HTTP_FAILS = 0
            return True
        DAEMON_HTTP_FAILS += 1
        if DAEMON_HTTP_FAILS < 2:
            return False
        log("happy daemon pid %s 活着但 HTTP(%s) 连续 %d 次无响应，收掉重起"
            % (pid, port, DAEMON_HTTP_FAILS))
        if not stop_stale_daemon(pid):
            # 收不掉就别起：宁可手机上暂时看不到机器，也不能多出一台
            return False
    elif happy_daemon_proc_running():
        if not DAEMON_ORPHAN_LOGGED:
            DAEMON_ORPHAN_LOGGED = True
            log("有个 happy daemon 进程在跑，但 daemon.state.json 已失效"
                "（pid=%s）；不拉起以免起出第二个。手机端若仍看不到机器，"
                "手动 happy daemon stop 后再跑 --fix-happy-daemon" % pid)
        return False
    else:
        log("happy daemon 不在运行（state pid=%s）" % pid)

    DAEMON_HTTP_FAILS = 0

    # 冷却判定在「尝试之前」做，且无论成功失败都先记下时间。
    # 时间戳若由 spawn 成功时才写，一旦起不来就永远不写，
    # 于是每 4 秒重试一次、日志被刷爆（实测踩到）。
    if not force and time.time() - DAEMON_SPAWN_AT < DAEMON_COOLDOWN:
        return False  # 冷却中，别每 4 秒重试一次
    DAEMON_SPAWN_AT = time.time()
    # 返回 False 而不是 spawn 的结果：本函数的返回值只表示「此刻在线」。
    # 刚起的 daemon 还没写 state、HTTP 口也还没起来，说它在线是谎报
    # （实测踩到：state 文件不存在时 --fix-happy-daemon 直接打印"本来就在线"）。
    # 主循环下一轮自然会看到它在线；--fix-happy-daemon 自己有等待循环。
    spawn_happy_daemon()
    return False


# ── 看护凭据身份：换账号了，旧镜像必须全部重启 ───────────────────
def account_sub():
    """从 access.key 的 JWT 里解出账号 id（sub）。只解 payload，不验签。

    为什么要连账号一起看：重新扫码配对必然换账号，但 machineId 不一定变。
    只看 machineId 会漏掉「同机器换账号」这种情况，而那恰恰是实测踩到的坑。
    """
    try:
        with open(ACCESS_KEY) as f:
            d = json.load(f)
        tok = d.get("token") or ""
        parts = tok.split(".")
        if len(parts) < 2:
            return ""
        pay = parts[1] + "=" * (-len(parts[1]) % 4)
        j = json.loads(base64.urlsafe_b64decode(pay))
        return str(j.get("sub") or "")
    except (OSError, ValueError, TypeError):
        return ""


def happy_identity():
    """当前 Happy 身份指纹：machineId + 账号 sub。读不到就返回 None。

    返回 None 表示「不知道」，调用方必须当成「没变化」处理——
    否则凭据文件一时读不到就会触发全量重启镜像，反而把好的弄坏。
    """
    mid = ""
    try:
        with open(SETTINGS) as f:
            d = json.load(f)
        mid = str(d.get("machineId") or "")
    except (OSError, ValueError, TypeError):
        return None
    sub = account_sub()
    if not mid or not sub:
        return None
    return "%s|%s" % (mid, sub)


def restart_on_identity_drift(state):
    """凭据身份变了 → 停掉所有镜像，让发现逻辑用新凭据重起。返回是否有变化。

    镜像进程启动时就把凭据读进了内存，之后换账号它们仍连着旧账号推流，
    手机上永远看不到（实测踩到：三个镜像在旧账号下跑得好好的，新账号侧空空如也）。
    这件事人眼很难发现——所有日志都显示健康。

    旧状态文件里没有 identity 字段的（升级前留下的），就地补上当前身份，
    不当成漂移：那些镜像是健康的，没必要为一次代码升级把它们重启一遍。
    """
    cur = happy_identity()
    if cur is None:
        return False  # 读不到身份 = 不知道，不动
    drifted, unknown = [], []
    for tid, e in state.items():
        rec = e.get("identity")
        if rec is None:
            unknown.append(tid)
        elif rec != cur:
            drifted.append(tid)
    for tid in unknown:
        state[tid]["identity"] = cur
    if unknown:
        save_state(state)
    if not drifted:
        return bool(unknown)
    log("Happy 身份已变（%s），重启全部镜像：%s"
        % (cur.split("|")[0][:8], ", ".join(short(t) for t in drifted)))
    for tid in drifted:
        kill_mirror(state[tid], "Happy 账号/机器身份已变，需用新凭据重连")
        del state[tid]
    save_state(state)
    return True


# ── 起一个镜像 ─────────────────────────────────────────────────
def spawn_mirror(thread_id, meta):
    cwd = meta.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    os.makedirs(MIRROR_DIR, exist_ok=True)
    shim_log = os.path.join(MIRROR_DIR, "%s.shim.log" % short(thread_id))
    happy_out = os.path.join(MIRROR_DIR, "%s.happy.out" % short(thread_id))
    for p in (shim_log, happy_out):
        try:
            if os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass

    env = dict(os.environ)
    # 垫片目录必须排在真 codex 之前，这是整个机制成立的前提
    env["PATH"] = SHIM_BIN + os.pathsep + env.get("PATH", "")
    env.setdefault("CODEX_REAL", "/opt/homebrew/bin/codex"
                   if os.path.exists("/opt/homebrew/bin/codex") else "codex")
    env["HAPPY_CODEX_SHIM_LOG"] = shim_log
    env["HAPPY_MIRROR_CHILD"] = "1"  # 给垫片链留个自识别标记，避免守护镜像自己

    with open(happy_out, "ab") as out:
        proc = subprocess.Popen(
            [resolve_happy(), "codex", "--resume", thread_id],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True,  # 脱离守护的进程组：守护重启不该带走镜像
        )
    PROCS[thread_id] = proc
    # 记下进程组 id：start_new_session 后 pgid == pid，但显式存一份更稳。
    # 包装进程变僵尸后 getpgid() 会失败，而真 node 子进程还活着，
    # 只有预先存下的 pgid 还能用来整组收掉。
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = proc.pid
    log("起镜像", short(thread_id), "pid", proc.pid, "pgid", pgid, "cwd", cwd,
        "title", (meta.get("title") or "")[:24].replace("\n", " "))
    return {"pid": proc.pid, "pgid": pgid, "threadId": thread_id, "cwd": cwd,
            "startedAt": int(time.time()), "shimLog": shim_log, "verified": False,
            "happyOut": happy_out,
            # 起镜像那一刻的 Happy 身份。守护靠它发现「账号换了，这条镜像还连着旧的」
            "identity": happy_identity()}


def shim_says_mirror(entry):
    """垫片日志里出现 MIRROR ON = 它确实走了镜像分支（撞上了 active writer）。

    这是「不干扰」的唯一硬证据：走透传的镜像会自己申请 writer lock，
    反而挡住用户想开的那个窗口，所以这种进程宁可杀掉也不能留着。
    """
    try:
        with open(entry["shimLog"], errors="replace") as f:
            return "MIRROR ON" in f.read()
    except OSError:
        return False


def orphan_mirror_pids(thread_id, pgid):
    """找出该线程的镜像孤儿进程：happy 的 node 会话进程，父进程已死。

    为什么需要这条：包装进程退出后会变僵尸，守护若在此期间重启，僵尸被 launchd
    收走、pid 凭空消失，于是 alive(pid) 判假、整段 kill 逻辑被跳过，
    而它派生的 node 子进程还在跑（实测残留 pid 19376），永久占着中继上的会话。
    光靠 pgid 也救不回来——守护重启后内存里没有 Popen，旧状态文件也可能没存 pgid。
    所以按命令行精确匹配，且**只认父进程已死的**：用户在终端里手敲的
    `happy-mirror <tid>` 父进程还活着，不会被误杀。

    ⚠ 这条 ppid==1 判据只能用在「已经决定要停掉这条镜像」的路径里（即 kill_mirror 内），
    **不能拿它判断某条镜像是否该回收**：守护自己重启后，健康镜像的包装进程同样会被
    init 收养而 ppid==1，拿它当回收条件会误杀正在服务的镜像。
    回收与否一律看两件事：线程的 writer lock 还在不在（窗口开着吗）、
    镜像进程还在不在（alive，已排除僵尸）。
    """
    needle = "codex --resume %s" % thread_id
    try:
        out = subprocess.run(["pgrep", "-f", needle], capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return []
    mine = os.getpid()
    found = []
    for line in out.stdout.split():
        if not line.strip().isdigit():
            continue
        pid = int(line)
        if pid == mine:
            continue
        if pgid:
            try:
                if os.getpgid(pid) == pgid:
                    continue  # 已归 killpg 管，不算孤儿
            except (OSError, ProcessLookupError):
                pass
        if pid_ppid(pid) in (0, 1):  # 父进程已死 → 被 init 收养 = 真孤儿
            found.append(pid)
    return found


def kill_mirror(entry, why):
    """整组收掉镜像：happy 包装进程 + 它派生的 node 会话进程 + 垫片 + 真 codex。

    只杀包装进程是不够的——实测包装进程退出后，happy 的 node 子进程仍在跑，
    会继续尾随 rollout 并占着中继上的那条会话。必须按 pgid 整组发信号。
    """
    tid = entry.get("threadId", "?")
    pid = entry.get("pid")
    # start_new_session=True 保证 pgid == pid，所以旧状态文件缺这个字段时用 pid 兜底。
    # 少了这条，包装进程一旦变僵尸，就再也无法按组收掉还活着的 node 子进程。
    pgid = entry.get("pgid") or pid

    def group_alive():
        # pgid 里任何一个成员还在，就算还活着（包装进程僵尸时 pid 已不可靠）
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (OSError, ProcessLookupError):
                pass
        return bool(pid) and alive(pid)

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_alive():
            break
        for target in (pgid, pid):
            if not target:
                continue
            try:
                os.killpg(target, sig) if target == pgid else os.kill(target, sig)
            except (OSError, ProcessLookupError):
                pass
        for _ in range(10):
            if not group_alive():
                break
            time.sleep(0.5)

    # 孤儿收尾：包装进程没了但它派生的 node 会话进程还活着（守护重启后的典型残留）
    orphans = orphan_mirror_pids(tid, pgid)
    for opid in orphans:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(opid, sig)
            except (OSError, ProcessLookupError):
                break
            time.sleep(0.4)
            if not alive(opid):
                break
        log("收掉镜像孤儿进程", short(tid), "pid", opid)

    # 回收子进程，避免留僵尸
    proc = PROCS.pop(tid, None)
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    elif pid:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
    log("停镜像", short(tid), "-", why)


# ── 主循环 ─────────────────────────────────────────────────────
def sweep(state):
    """一轮：回收死掉的、给新出现的起镜像、给未验证的补验证。返回是否有变化。"""
    changed = False

    # 0a. 先保证 happy daemon 在线。镜像要靠它把会话注册到手机端，
    #     daemon 不在的话镜像起得再健康，手机上也是一片空白。
    ensure_happy_daemon()

    # 0b. 账号/机器身份变了就全部重启（详见 restart_on_identity_drift）。
    #     必须排在发现之前：这轮清掉的记录，下面第 3 步会用新凭据重起。
    if restart_on_identity_drift(state):
        changed = True

    # 1. 回收：进程没了就摘掉记录（线程可能已被用户关掉）
    for tid in list(state):
        e = state[tid]
        # 先 poll() 回收退出状态，否则包装进程会变僵尸，
        # 而僵尸对 os.kill(pid,0) 仍「存活」，这条记录就永远回收不掉
        proc = PROCS.get(tid)
        if proc is not None and proc.poll() is not None:
            PROCS.pop(tid, None)
        if not alive(e["pid"]):
            # 包装进程没了不等于收干净：它派生的 node 会话进程可能还活着，
            # 必须走一遍 kill_mirror（内部含孤儿查杀），不能直接删记录。
            kill_mirror(e, "镜像进程已退出")
            del state[tid]
            changed = True

    # 2. 窗口已关的镜像：宽限期到了就回收。
    #    历史已经同步到中继了，留着只是白占 ~100MB 常驻和名额。
    live_set = set(live_thread_ids())
    now = time.time()
    for tid, e in list(state.items()):
        if tid in live_set:
            if e.get("windowClosedAt"):
                e.pop("windowClosedAt", None)
                changed = True
            continue
        if not e.get("windowClosedAt"):
            e["windowClosedAt"] = int(now)
            log("窗口已关，%.0fs 后回收镜像" % GRACE_S, short(tid))
            changed = True
        elif now - e["windowClosedAt"] > GRACE_S:
            kill_mirror(e, "窗口已关超过 %.0fs" % GRACE_S)
            del state[tid]
            changed = True

    # 3. 发现：给新出现的运行中线程起镜像
    #    名额只按「线程还在跑」的镜像算：已关窗口的镜像正在宽限期里等着回收，
    #    不该挡住一个刚启动的新窗口（实测会挡住，且每轮刷一条上限日志）。
    busy = sum(1 for tid in state if tid in live_set)
    SKIP_LOGGED.intersection_update(live_set)  # 窗口关了又重开的，应该重新能报一次
    for tid in sorted(live_set):
        if tid in state:
            continue
        if busy >= MAX_MIRRORS:
            if tid not in SKIP_LOGGED:
                SKIP_LOGGED.add(tid)
                log("运行中会话已达上限 HAPPY_MIRROR_MAX=%d，暂不镜像" % MAX_MIRRORS, short(tid))
            continue
        if os.environ.get("HAPPY_MIRROR_CHILD") == "1":
            continue
        if owned_by_happy(tid):
            log("跳过 Happy 自己的会话", short(tid))
            continue
        state[tid] = spawn_mirror(tid, thread_meta(tid))
        SKIP_LOGGED.discard(tid)
        busy += 1
        changed = True
    # 起完立刻落盘：万一守护随后崩了，状态文件还留着 pid，
    # 下次启动能接管或回收，不至于把镜像变成没人管的孤儿
    if changed:
        save_state(state)

    # 4. 验证：所有未确认的镜像共享一个截止时间，一起等。
    #    不能逐个 verify——那样一个卡住的会把后面的发现全拖住 30 秒。
    #    这一步必须在返回前做完（--once 也一样）：万一某个镜像走了透传，
    #    它就成了 writer，会反过来把用户想开的那个窗口挡住。
    pending = [[tid, e] for tid, e in list(state.items()) if not e.get("verified")]
    if pending:
        deadline = time.time() + VERIFY_S
        while pending and time.time() < deadline:
            for item in list(pending):
                tid, e = item
                if not alive(e["pid"]):
                    log("镜像进程已退出（未取得镜像模式）", short(tid))
                    del state[tid]
                    pending.remove(item)
                    changed = True
                elif shim_says_mirror(e):
                    e["verified"] = True
                    e["verifiedAt"] = int(time.time())
                    log("镜像确认", short(tid), "pid", e["pid"],
                        "(只读 rollout + 队列注入，未抢 writer)")
                    pending.remove(item)
                    changed = True
            if pending:
                time.sleep(1)
        # 超时的：宁可杀掉，也不能留一个可能抢到 writer lock 的进程
        for tid, e in pending:
            kill_mirror(e, "等 %.0fs 仍未取得镜像模式，避免抢占 writer lock" % VERIFY_S)
            state.pop(tid, None)
            changed = True

    if changed:
        save_state(state)
    return changed


def cmd_status():
    state = load_state()
    # 手机端「machine 列表为空」几乎总是 daemon 不在线，先把这层状态打出来，
    # 免得每次都要人去翻 ~/.happy/daemon.state.json 和 lsof。
    st = daemon_state()
    pid, port = st.get("pid"), st.get("httpPort")
    if pid and alive(pid):
        http = daemon_http_ok(port) if port else False
        print("happy daemon：在线  pid=%s  port=%s  HTTP=%s"
              % (pid, port, "正常" if http else "无响应"))
        if not http:
            print("  ⚠ 进程活着但 HTTP 不通，手机端可能仍看不到这台机器")
    else:
        print("happy daemon：不在线（手机端 machine 列表会是空的）")
        print("  修复：python3 %s --fix-happy-daemon" % os.path.basename(__file__))
    ident = happy_identity()
    if ident:
        mid, sub = ident.split("|", 1)
        print("Happy 身份：machineId=%s  账号=%s" % (mid[:8], sub))
    else:
        print("Happy 身份：读不到（settings.json / access.key 有问题？）")
    print()
    if not state:
        print("当前没有由守护进程起的镜像。")
        live = live_thread_ids()
        print("此刻正在运行的 Codex 线程：%s" % (", ".join(short(t) for t in live) or "无"))
        return 0
    live = set(live_thread_ids())
    print("守护进程正在镜像 %d 个线程：" % len(state))
    for tid, e in sorted(state.items(), key=lambda kv: kv[1].get("startedAt", 0)):
        print("  %s  pid=%-7d %s  live=%s  %s" % (
            short(tid), e["pid"], "已确认" if e.get("verified") else "验证中",
            "是" if tid in live else "否(窗口已关)", e.get("cwd", "")))
    # 身份对不上的镜像是「连在旧账号上」的，手机上看不到，必须显式标出来
    cur = happy_identity()
    stale = [short(t) for t, e in state.items()
             if cur and e.get("identity") and e["identity"] != cur]
    if stale:
        print("  ⚠ 这些镜像还连着旧账号，手机端看不到：%s" % ", ".join(stale))
    return 0


def cmd_fix_daemon():
    """把 happy daemon 拉起来并等到它真的在线（`--fix-happy-daemon`）。

    配对完发现手机端 machine 列表是空的，用这条一键修，不用自己去猜。
    """
    if ensure_happy_daemon(force=True):
        print("happy daemon 本来就在线，无需处理。")
        st = daemon_state()
        print("  pid=%s  port=%s" % (st.get("pid"), st.get("httpPort")))
        return 0
    # 起了进程还要等它真的把 HTTP 口拉起来，否则只是「看起来做了」
    deadline = time.time() + 40
    while time.time() < deadline:
        if DAEMON_SPAWN_PROC is not None:
            DAEMON_SPAWN_PROC.poll()
        st = daemon_state()
        pid, port = st.get("pid"), st.get("httpPort")
        if pid and alive(pid) and port and daemon_http_ok(port):
            print("happy daemon 已拉起并在线。")
            print("  pid=%s  port=%s" % (pid, port))
            print("现在去手机上刷新 machine 列表，应该能看到这台机器。")
            return 0
        time.sleep(1)
    print("等了 40s，happy daemon 仍未在线。看日志：")
    print("  %s" % DAEMON_START_LOG)
    st = daemon_state()
    if st.get("daemonLogPath"):
        print("  %s" % st["daemonLogPath"])
    return 1


def cmd_stop():
    state = load_state()
    if not state:
        print("没有需要停的镜像。")
        return 0
    for tid, e in list(state.items()):
        kill_mirror(e, "--stop")
    save_state({})
    print("已停掉 %d 个镜像。" % len(state))
    return 0


def main():
    ensure_path()
    if "--status" in sys.argv:
        return cmd_status()
    if "--stop" in sys.argv:
        return cmd_stop()
    if "--fix-happy-daemon" in sys.argv:
        return cmd_fix_daemon()
    once = "--once" in sys.argv

    if not os.path.isdir(LOCK_DIR):
        log("找不到", LOCK_DIR, "（没有正在运行的 Codex 窗口？）")
    log("守护启动 interval=%ss max=%d 镜像日志目录=%s" % (INTERVAL, MAX_MIRRORS, MIRROR_DIR))

    state = load_state()
    # 接管上次留下的、仍活着的镜像（守护重启不该把它们变成孤儿）
    for tid in list(state):
        if not alive(state[tid]["pid"]):
            del state[tid]
    if state:
        log("接管上次留下的镜像:", ", ".join(short(t) for t in state))
    save_state(state)

    stop = {"v": False}

    def on_term(signum, _frame):
        stop["v"] = True
        log("收到信号", signum, "准备退出（镜像进程会继续跑，下次启动自动接管）")

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    while not stop["v"]:
        try:
            sweep(state)
        except Exception as e:  # 守护绝不能因为一轮异常就退出
            log("本轮异常:", type(e).__name__, e)
        if once:
            break
        for _ in range(int(INTERVAL * 2)):
            if stop["v"]:
                break
            time.sleep(0.5)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
