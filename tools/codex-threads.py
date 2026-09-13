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
import json
import os
import sqlite3
import sys

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
DB = os.path.join(CODEX_HOME, "state_5.sqlite")


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
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
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
        print(f"{i:>3}. {r['id']}")
        print(f"     {title}")
        print(f"     {humanize(r['recency_at'])}  |  {cwd}  |  cli {r['cli_version'] or '?'}"
              f"  |  provider {r['model_provider'] or '?'}"
              + ("  |  已归档" if r["archived"] else ""))
        if args.copy_cmd:
            print(f"     happy codex --resume {r['id']}")
        if i < len(rows):
            print()

    print(f"共 {len(rows)} 条。接管：happy codex --resume <thread-id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
