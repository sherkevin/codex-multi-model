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

CAT = os.path.expanduser("~/.codex/custom-model-catalog.json")

# 自定义模型示例。换成你自己的：slug 必须与中转 router-routes.json 里的模型名一致。
# context_window 是保守估计，确认真实上限后可上调。
CUSTOM = [
    {"slug": "example/responses-model", "display_name": "Example Responses Model",
     "description": "example-gateway", "context_window": 128000},
    {"slug": "example/chat-model",      "display_name": "Example Chat Model",
     "description": "example-gateway", "context_window": 128000},
]


def fetch_builtin():
    """用一个极简临时 CODEX_HOME 取内置目录——真实 home 里有自定义目录会把它挡住。"""
    d = tempfile.mkdtemp(prefix="codex-builtin-")
    try:
        open(os.path.join(d, "config.toml"), "w").write('model = "gpt-5.5"\n')
        env = dict(os.environ, CODEX_HOME=d)
        out = subprocess.run(["codex", "debug", "models"], env=env, cwd="/tmp",
                             capture_output=True, text=True, timeout=180)
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError(f"codex debug models 失败: {out.stderr[:300]}")
        return json.loads(out.stdout)["models"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_entry(spec, template):
    e = json.loads(json.dumps(template))
    e.update({
        "slug": spec["slug"],
        "display_name": spec["display_name"],
        "description": spec["description"],
        "context_window": spec["context_window"],
        "max_context_window": spec["context_window"],
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


if __name__ == "__main__":
    sys.exit(main())
