#!/usr/bin/env python3
"""重建 ~/.codex/custom-model-catalog.json

model_catalog_json 是【整体替换】内置目录而不是合并，所以 codex 升级后带来的
新内置模型不会自动出现。本脚本每次都从当前 codex 二进制重新取一遍内置目录，
原样保留它们（含 visibility），再追加自定义模型。

**每次 codex 升级后运行一次。**

    python3 ~/.codex/regen-model-catalog.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
CAT = os.path.join(CODEX_HOME, "custom-model-catalog.json")

# 自定义模型示例。换成你自己的：slug 必须与中转 router-routes.json 里的模型名一致。
#
# context_window 必须填**上游真实输入上限**，不能随便估。它同时决定两件事：
#   1) 界面右上角那个百分比的分母；
#   2) Codex 什么时候认为"满了"。
# 填大了（比如照抄内置 gpt 的 1M，而上游实际只收 30 万）会让百分比严重偏低——
# 上游已经 400 拒绝了，界面还显示 23%，自动压缩永远不会被触发，会话直接死锁。
#
# auto_compact_token_limit：自动压缩的触发点。不填则按 AUTO_COMPACT_RATIO 自动算。
# 留足余量是必要的：压缩请求自己也要把整段历史发上去，贴着上限设会导致"压缩请求
# 本身就超限"→压不下去→死锁（见 docs/adr/0005）。
AUTO_COMPACT_RATIO = 0.85

CUSTOM = [
    {"slug": "example/responses-model", "display_name": "Example Responses Model",
     "description": "example-gateway", "context_window": 128000},
    # 显式指定触发点的写法（与上面的自动计算等价，按需覆盖）：
    # {"slug": "example/chat-model", "display_name": "Example Chat Model",
    #  "description": "example-gateway", "context_window": 300000,
    #  "auto_compact_token_limit": 256000},
]


def fetch_builtin():
    """用一个极简临时 CODEX_HOME 取内置目录——真实 home 里有自定义目录会把它挡住。"""
    d = tempfile.mkdtemp(prefix="codex-builtin-")
    try:
        open(os.path.join(d, "config.toml"), "w").write('model = "gpt-5.5"\n')
        env = dict(os.environ, CODEX_HOME=d)
        # cwd 用临时目录而不是 /tmp：Windows 没有 /tmp。
        out = subprocess.run(["codex", "debug", "models"], env=env, cwd=d,
                             capture_output=True, text=True, timeout=180)
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError(f"codex debug models 失败: {out.stderr[:300]}")
        return json.loads(out.stdout)["models"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_entry(spec, template):
    e = json.loads(json.dumps(template))
    ctx = int(spec["context_window"])
    # effective_context_window_percent：内置目录都是 95，留 5% 给系统提示/工具定义。
    eff = int(spec.get("effective_context_window_percent", 95))
    # 自动压缩触发点。显式给了就用，否则按窗口比例算——两者都必须**低于**真实上限，
    # 否则压缩请求自己超限，压不下去（死锁）。
    auto = spec.get("auto_compact_token_limit")
    if auto is None:
        auto = int(ctx * AUTO_COMPACT_RATIO)
    e.update({
        "slug": spec["slug"],
        "display_name": spec["display_name"],
        "description": spec["description"],
        "context_window": ctx,
        "max_context_window": ctx,
        "effective_context_window_percent": eff,
        "auto_compact_token_limit": auto,
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "base_instructions": (
            f"You are Codex, a coding agent based on {spec['slug']}. You and the "
            "user share the same workspace and collaborate to achieve the user's goals."),
    })
    return e


def main():
    builtin = fetch_builtin()
    print(f"内置目录: {len(builtin)} 个模型 —— 原样保留（含 visibility）")

    # 拿一个内置条目当模板，字段结构才跟当前 codex 版本一致
    tpl = max(builtin, key=lambda m: len(m.keys()))
    custom = [build_entry(s, tpl) for s in CUSTOM]

    merged = {"models": custom + builtin}
    if os.path.exists(CAT):
        shutil.copy2(CAT, f"{CAT}.bak-regen-{time.strftime('%Y%m%d-%H%M%S')}")
    json.dump(merged, open(CAT, "w"), indent=2, ensure_ascii=False)
    os.chmod(CAT, 0o600)

    print(f"已写入 {CAT}")
    print(f"总计 {len(merged['models'])} 个模型:")
    for m in merged["models"]:
        tag = "自定义" if m["slug"] in {c["slug"] for c in CUSTOM} else "内置"
        print(f"   {m['slug']:22s} {m['visibility']:6s} {tag}")

    # 自定义模型的窗口/压缩配置直接决定界面百分比和压缩时机，打印出来便于核对。
    if custom:
        print("\n自定义模型窗口配置（核对 context_window 是否等于上游真实上限）:")
        for m in custom:
            print(f"   {m['slug']:22s} window={m['context_window']:>9,}  "
                  f"auto_compact={m['auto_compact_token_limit']:>9,}  "
                  f"eff={m['effective_context_window_percent']}%")


if __name__ == "__main__":
    sys.exit(main())
