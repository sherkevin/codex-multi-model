#!/usr/bin/env python3
"""
happy-patch.py — 给 Happy CLI 打两个本地补丁，让它在「自定义 API 中转」环境下可用。

背景：Happy（github.com/slopus/happy，MIT）是手机/网页端控制 Claude Code 与 Codex 的
客户端，桌面侧用 `codex app-server --listen stdio://` 驱动 Codex，因此完全沿用
~/.codex/config.toml，与自定义 provider、apikey 认证不冲突。但上游有两处不适配：

  1. resume-backfill
     `happy codex --resume <thread-id>` 只往客户端发一条 "Resumed thread ..."，
     不回放历史，手机端看不到既有对话。上游只在 daemon 的 fork 路径调用了
     buildCodexThreadBackfillEnvelopes，终端 --resume 路径漏了。本补丁补上。
     运行时可用 HAPPY_CODEX_RESUME_BACKFILL=0 关闭。

  2. config-model-default
     上游硬编码 DEFAULT_CODEX_MODEL = "gpt-5.6-sol"，会盖掉 config.toml 里的 model，
     导致裸跑 `happy codex` 用了你 provider 上不存在的模型而报错（实测走 router 的
     openai 前缀路由，返回 401 且重试 5 次）。本补丁改成优先读 config.toml 的
     model / model_reasoning_effort，读不到才回落到上游默认值。
     可用 HAPPY_CODEX_MODEL / HAPPY_CODEX_EFFORT 覆盖。

实现要点：happy 的 bundle 有两份（.mjs 用裸 `logger`，.cjs 用 `api.logger`），
补丁代码里统一写 `logger.debug(...)`，按 bundle 形态改写，避免 cjs 下 ReferenceError。

特性：自动定位 bundle（hash 文件名随版本变化）、幂等、打前备份 <bundle>.orig-bak、
      打完 node --check 自检，失败自动回滚。

用法：
  python3 tools/happy-patch.py            # 打全部补丁
  python3 tools/happy-patch.py --check    # 只看状态
  python3 tools/happy-patch.py --revert   # 从备份还原
  python3 tools/happy-patch.py --only resume-backfill
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

# ── 补丁 1：--resume 时把线程历史回放给客户端 ─────────────────────────
P1_ANCHOR = (
    "    opts.messageBuffer.addMessage("
    "`Resumed thread ${trimIdent(resumedThread.threadId)}`, \"status\");"
)
P1_BODY = """    if (opts.announce !== false && process.env.HAPPY_CODEX_RESUME_BACKFILL !== "0") {
      try {
        const { thread } = await opts.client.readThread({
          threadId: resumedThread.threadId,
          includeTurns: true
        });
        const envelopes = await buildCodexThreadBackfillEnvelopes({
          thread,
          uploadLocalImage: (attachment, imageOpts) => opts.session.uploadLocalImageAttachmentEnvelope(attachment, imageOpts)
        });
        for (const envelope of envelopes) {
          opts.session.sendSessionProtocolMessage(envelope);
        }
        logger.debug(`[CODEX RESUME BACKFILL] Replayed ${envelopes.length} historical envelopes from thread ${resumedThread.threadId}`);
        opts.messageBuffer.addMessage(`Loaded ${envelopes.length} historical messages from thread ${trimIdent(resumedThread.threadId)}`, "status");
      } catch (error) {
        logger.debug(`[CODEX RESUME BACKFILL] Failed to read thread ${resumedThread.threadId}:`, error);
      }
    }
"""

# ── 补丁 2：默认模型取自 config.toml，而非硬编码 ───────────────────────
P2_ANCHOR = 'const DEFAULT_CODEX_MODEL = "gpt-5.6-sol";\nconst DEFAULT_CODEX_EFFORT = "medium";'
P2_BODY = '''const DEFAULT_CODEX_MODEL = "gpt-5.6-sol";
const DEFAULT_CODEX_EFFORT = "medium";
// PATCH(local): prefer ~/.codex/config.toml over Happy's hardcoded defaults so a
// custom model_provider keeps working without passing --model on every launch.
function readCodexConfigDefaults() {
  const cached = globalThis.__codexConfigDefaults;
  if (cached !== void 0) return cached;
  let out = { model: DEFAULT_CODEX_MODEL, effort: DEFAULT_CODEX_EFFORT };
  try {
    const fsMod = require("node:fs");
    const osMod = require("node:os");
    const pathMod = require("node:path");
    const home = osMod.homedir();
    const cfgDir = process.env.CODEX_HOME || pathMod.join(home, ".codex");
    const text = fsMod.readFileSync(pathMod.join(cfgDir, "config.toml"), "utf8");
    const head = text.split("\\n[")[0];
    const m = head.match(/^model\\s*=\\s*"([^"]+)"/m);
    const e = head.match(/^model_reasoning_effort\\s*=\\s*"([^"]+)"/m);
    out = {
      model: process.env.HAPPY_CODEX_MODEL || (m ? m[1] : DEFAULT_CODEX_MODEL),
      effort: process.env.HAPPY_CODEX_EFFORT || (e ? e[1] : DEFAULT_CODEX_EFFORT)
    };
    logger.debug(`[CODEX CONFIG DEFAULTS] model=${out.model} effort=${out.effort}`);
  } catch (error) {
    logger.debug("[CODEX CONFIG DEFAULTS] Failed to read config.toml, using upstream defaults:", error);
  }
  globalThis.__codexConfigDefaults = out;
  return out;
}'''
P2_ANCHOR2 = (
    "    model: opts.model ?? DEFAULT_CODEX_MODEL,\n"
    "    effort: opts.effort ?? DEFAULT_CODEX_EFFORT"
)
P2_BODY2 = (
    "    model: opts.model ?? readCodexConfigDefaults().model,\n"
    "    effort: opts.effort ?? readCodexConfigDefaults().effort"
)

PATCHES = [
    {
        "id": "resume-backfill",
        "marker": "[CODEX RESUME BACKFILL]",
        "steps": [(P1_ANCHOR, P1_BODY + P1_ANCHOR)],
        "desc": "--resume 时回放线程历史给手机/网页端",
    },
    {
        "id": "config-model-default",
        "marker": "[CODEX CONFIG DEFAULTS]",
        "steps": [(P2_ANCHOR, P2_BODY), (P2_ANCHOR2, P2_BODY2)],
        "desc": "默认模型取自 config.toml，不再硬编码 gpt-5.6-sol",
    },
]


def find_bundles():
    """定位 happy 安装目录里含 resumeExistingThread 的 bundle（mjs + cjs）。"""
    roots = []
    # 显式指定优先：非标准安装位置（nvm/fnm/pnpm/Windows 自定义前缀）下
    # `npm root -g` 可能找不到，此时用 HAPPY_DIR 直接给。
    env_dir = os.environ.get("HAPPY_DIR")
    if env_dir:
        roots.append(os.path.expanduser(env_dir))
    try:
        out = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            roots.append(os.path.join(out.stdout.strip(), "happy"))
    except Exception:
        pass
    # 下面只是**候选**目录，不存在会自动跳过；跨平台靠上面的 npm root -g / HAPPY_DIR。
    roots += [
        "/opt/homebrew/lib/node_modules/happy",
        "/usr/local/lib/node_modules/happy",
        os.path.expanduser("~/.npm-global/lib/node_modules/happy"),
    ]
    # Windows: %APPDATA%\npm\node_modules\happy（APPDATA 不存在时不产生相对路径）
    if os.environ.get("APPDATA"):
        roots.append(os.path.join(os.environ["APPDATA"], "npm",
                                  "node_modules", "happy"))
    hits, seen = [], set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path in sorted(glob.glob(os.path.join(root, "dist", "index-*.*js"))):
            if path.endswith(".orig-bak"):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            try:
                body = open(path, "r", encoding="utf-8").read()
            except Exception:
                continue
            if "async function resumeExistingThread" in body and P1_ANCHOR in body:
                seen.add(real)
                hits.append(path)
    return hits


def uses_bare_logger(path):
    """.mjs bundle 顶层 import 了 logger；.cjs bundle 只有 api.logger。"""
    body = open(path, "r", encoding="utf-8").read(6000)
    if " l as logger" in body or "logger as logger" in body:
        return True
    # cjs: 顶部 require 后不会出现裸 logger 定义
    return False


def adapt_logger(snippet, path):
    """按 bundle 形态改写 logger 引用。cjs 下必须用 api.logger，否则运行时 ReferenceError。"""
    if uses_bare_logger(path):
        return snippet
    return snippet.replace("logger.debug(", "api.logger.debug(")


def syntax_ok(path):
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    return r.returncode == 0, r.stderr[:600]


def patch_status(path):
    body = open(path, "r", encoding="utf-8").read()
    return {p["id"]: p["marker"] in body for p in PATCHES}


def do_check(paths):
    print("happy bundle 状态:")
    if not paths:
        print("  未找到 happy bundle。先执行: npm install -g happy")
        return 0
    for p in paths:
        st = patch_status(p)
        backup = "yes" if os.path.exists(p + ".orig-bak") else "no"
        form = "mjs(裸 logger)" if uses_bare_logger(p) else "cjs(api.logger)"
        print(f"  {p}  [{form}]")
        print(f"    backup={backup}  "
              + "  ".join(f"{k}={'on' if v else 'off'}" for k, v in st.items()))
    for patch in PATCHES:
        print(f"  - {patch['id']}: {patch['desc']}")
    return 0


def do_apply(paths, only=None):
    if not paths:
        print("未找到 happy bundle。先执行: npm install -g happy")
        return 1
    todo = [p for p in PATCHES if only in (None, p["id"])]
    changed = 0
    for path in paths:
        body = open(path, "r", encoding="utf-8").read()
        original = body
        backup = path + ".orig-bak"
        applied_here = []
        for patch in todo:
            if patch["marker"] in body:
                print(f"  跳过 {patch['id']}（已打过）: {os.path.basename(path)}")
                continue
            ok = True
            for anchor, replacement in patch["steps"]:
                if body.count(anchor) != 1:
                    print(f"  跳过 {patch['id']}: 锚点出现 {body.count(anchor)} 次"
                          f"（happy 版本可能变了）: {os.path.basename(path)}")
                    ok = False
                    break
                body = body.replace(anchor, adapt_logger(replacement, path), 1)
            if ok:
                applied_here.append(patch["id"])
        if body == original:
            continue
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        open(path, "w", encoding="utf-8").write(body)
        ok, err = syntax_ok(path)
        if not ok:
            shutil.copy2(backup, path)
            print(f"  失败并回滚（node --check 不通过）: {path}\n{err}")
            return 1
        print(f"  已打补丁 [{', '.join(applied_here)}]: {os.path.basename(path)}")
        changed += 1
    print(f"完成，改动 {changed} 个文件。")
    if changed and not only:
        print("验证: happy codex --resume <thread-id>   （id 用 tools/codex-threads.py 查）")
    return 0


def do_revert(paths):
    for p in paths:
        backup = p + ".orig-bak"
        if os.path.exists(backup):
            shutil.copy2(backup, p)
            print(f"  已还原: {p}")
        else:
            print(f"  无备份，跳过: {p}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--only", choices=[p["id"] for p in PATCHES])
    args = ap.parse_args()
    paths = find_bundles()
    if args.check:
        return do_check(paths)
    if args.revert:
        return do_revert(paths)
    return do_apply(paths, args.only)


if __name__ == "__main__":
    sys.exit(main())
