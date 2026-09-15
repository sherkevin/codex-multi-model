#!/usr/bin/env python3
"""
codex-threads.py — 列出本机 Codex 会话，输出可直接用于接管的 thread-id。

用途：用 Happy 从手机接管某个桌面端会话时，需要先拿到 thread-id。
数据源是桌面端自己的索引 ~/.codex/state_5.sqlite（threads 表），
不是扫 rollout 文件，所以标题/时间/项目都是桌面端认的那一份。

用法：
  python3 tools/codex-threads.py                    # 最近 15 个
  python3 tools/codex-threads.py -n 40              # 最近 40 个
  python3 tools/codex-threads.py --all              # 含已归档
  python3 tools/codex-threads.py --cwd ~/work/vault # 只看某目录下的会话
  python3 tools/codex-threads.py --grep 路由         # 按标题/路径过滤
  python3 tools/codex-threads.py --copy-cmd          # 每行附带 happy 接管命令
  python3 tools/codex-threads.py --json              # 机器可读

拿到 id 后：
  happy codex --resume <thread-id>          # 终端里接管
  happy codex --resume <thread-id> --model <你的模型slug>
"""
import argparse
import datetime
import glob
import json
import os
import sqlite3
import sys

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
DB = os.path.join(CODEX_HOME, "state_5.sqlite")
LOCK_DIR = os.path.join(CODEX_HOME, "thread-writer-locks")


def live_thread_ids():
    """探测此刻正被某个 Codex 窗口持有（writer lock 被 flock 占住）的线程。

    原理：每个正在运行的 CLI/桌面窗口都会对
    ~/.codex/thread-writer-locks/<thread-id>.lock 持有排他 flock。
    用 LOCK_NB 试锁一次：拿得到 = 空闲，拿不到 = 有窗口正在跑。
    只试探、立刻释放，不会干扰任何窗口。

    这批 id 正是「镜像接管」的目标：普通 resume 会撞
    "already has an active writer"，需要走垫片。

    fcntl 是 POSIX 专属：Windows 上 import 就 ImportError，会让整个脚本起不来。
    所以这里**惰性导入**、失败即降级为空集合（ADR 0007：平台特性可选且可降级）。
    调用方拿不到 live 标记时，--live-only 自然输出空列表，不报错。
    """
    try:
        import fcntl
    except ImportError:
        return set()
    held = set()
    for p in glob.glob(os.path.join(LOCK_DIR, "*.lock")):
        name = os.path.basename(p)
        if name.startswith("."):
            continue
        fd = None
        try:
            fd = os.open(p, os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)  # 试到了就说明没人占，释放
        except BlockingIOError:
            held.add(name[: -len(".lock")])
        except OSError:
            continue
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return held


def humanize(ts):
    if not ts:
        return "-"
    try:
        d = datetime.datetime.fromtimestamp(int(ts))
    except Exception:
        return str(ts)
    now = datetime.datetime.now()
    delta = now - d
    if delta < datetime.timedelta(minutes=1):
        return "刚刚"
    if delta < datetime.timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}分钟前"
    if delta < datetime.timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}小时前"
    if delta < datetime.timedelta(days=30):
        return f"{delta.days}天前"
    return d.strftime("%m-%d")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("-n", "--num", type=int, default=15, help="列出条数（默认 15）")
    ap.add_argument("--all", action="store_true", help="包含已归档会话")
    ap.add_argument("--cwd", help="只列出该目录下的会话（支持 ~ 展开与前缀匹配）")
    ap.add_argument("--grep", help="按标题或路径子串过滤")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--copy-cmd", action="store_true", help="附带 happy 接管命令")
    ap.add_argument("--live-only", action="store_true",
                    help="只列出此刻正被某个窗口占用的会话（镜像接管的目标）")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"找不到桌面端索引：{DB}", file=sys.stderr)
        print("（该文件由 Codex 桌面端维护；只用过 CLI 的话可能不存在）", file=sys.stderr)
        return 1

    where, params = [], []
    if not args.all:
        where.append("archived = 0")
    if args.cwd:
        where.append("cwd LIKE ?")
        params.append(os.path.expanduser(args.cwd).rstrip("/") + "%")
    if args.grep:
        where.append("(title LIKE ? OR cwd LIKE ? OR first_user_message LIKE ?)")
        g = f"%{args.grep}%"
        params += [g, g, g]

    # 运行中检测必须在 SQL 里做：否则 LIMIT 先截断、再过滤 live，
    # 会漏掉排在 15 条之外但正在跑的会话（实测 4 个 held 只报出 3 个）。
    live = live_thread_ids()
    if args.live_only:
        if not live:
            where.append("1 = 0")
        else:
            where.append(f"id IN ({','.join('?' * len(live))})")
            params += sorted(live)

    sql = f"""
        SELECT id, title, cwd, recency_at, cli_version, archived, model_provider,
               substr(first_user_message, 1, 120) AS first_msg
        FROM threads
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY COALESCE(NULLIF(recency_at, 0), updated_at, created_at) DESC
        LIMIT ?
    """
    params.append(args.num)

    # 只读打开，避免与桌面端抢 WAL 写锁
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    if args.json:
        print(json.dumps(
            [{**dict(r), "live": r["id"] in live} for r in rows],
            ensure_ascii=False, indent=1))
        return 0

    if not rows:
        print("没有匹配的会话。")
        return 0

    width = max(8, min(os.get_terminal_size().columns if sys.stdout.isatty() else 100, 118))
    for i, r in enumerate(rows, 1):
        title = (r["title"] or "").strip().replace("\n", " ")
        if not title:
            title = (r["first_msg"] or "").strip().replace("\n", " ") or "(无标题)"
        title = title[:60]
        cwd = (r["cwd"] or "").replace(os.path.expanduser("~"), "~")
        mark = " *运行中" if r["id"] in live else ""
        print(f"{i:>3}. {r['id']}{mark}")
        print(f"     {title}")
        print(f"     {humanize(r['recency_at'])}  |  {cwd}  |  cli {r['cli_version'] or '?'}"
              f"  |  provider {r['model_provider'] or '?'}"
              + ("  |  已归档" if r["archived"] else ""))
        if args.copy_cmd:
            print(f"     happy codex --resume {r['id']}")
        if i < len(rows):
            print()

    live_n = sum(1 for r in rows if r["id"] in live)
    print(f"共 {len(rows)} 条，其中 {live_n} 条正在运行（标 *）。")
    print("接管空闲会话： happy codex --resume <thread-id>")
    print("接管运行中会话：tools/happy-codex-shim/happy-mirror <thread-id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
