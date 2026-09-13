#!/usr/bin/env python3
"""codex-config-dedup — 自愈 ~/.codex/config.toml 的「重复键」解析错误。

背景：codex 启动时若 config.toml 有重复键（TOML 不允许同一键定义两次）会直接拒绝启动。
常见成因：同一份配置（典型是 hook 信任状态 `[hooks.state."…"]`）被两个写入方各写一遍，
且用了等价但不同的语法——`[hooks.state."K"]` 与 `["hooks"."state"."K"]` 在 TOML 里是同一个键。

本工具通用、保守：
  * 文件能正常解析 → 什么都不做（绝不碰健康文件）。
  * 解析失败 → 依次尝试：
      A. 切除「注释标记块」（任意 `# BEGIN <tag>` … `# END <tag>` 区块）——
         第三方安装器常把冗余副本写在这种带标记的块里；逐个试删，删完能解析就采用。
      B. 按归一化键删除重复的表头 / 顶层点号键定义，保留首次出现。
  * 每次候选修复都先校验「能解析」再落盘；原子写 + 备份；都修不好就保留原文件、退出非零等人工。

不依赖任何具体工具名，纯按 TOML 结构判重。可手动运行，或配 launchd 在 config.toml 变动时触发。
"""
import os
import re
import shutil
import sys
import time

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
CFG = os.path.join(CODEX_HOME, "config.toml")


# ─────────────────────────── 解析能力探测 ───────────────────────────

def _parses(text):
    """能解析返回 True；坏返回 False；无任何 TOML 库时返回 None（交给正则判重）。"""
    try:
        import tomllib  # Python 3.11+
        try:
            tomllib.loads(text)
            return True
        except Exception:
            return False
    except ImportError:
        pass
    try:
        import tomlkit
        try:
            tomlkit.parse(text)
            return True
        except Exception:
            return False
    except ImportError:
        return None


# ─────────────────────────── 键归一化 ───────────────────────────

def _split_key(s):
    """按「引号外」的点切分键，去掉引号。
    `[hooks.state."a.b:c"]` 与 `["hooks"."state"."a.b:c"]` 都归一为 ('hooks','state','a.b:c')。"""
    parts, cur, inq = [], "", None
    for ch in s:
        if inq:
            cur += ch
            if ch == inq:
                inq = None
        elif ch in "\"'":
            inq = ch
        elif ch == ".":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return tuple(p.strip().strip("\"'") for p in parts if p.strip() != "")


_TABLE_RE = re.compile(r'^\s*\[\s*([^\[\]]+?)\s*\]\s*$')
_DOTKEY_RE = re.compile(r'^\s*((?:"[^"]+"|\'[^\']+\'|[\w-]+)(?:\s*\.\s*(?:"[^"]+"|\'[^\']+\'|[\w-]+))+)\s*=')
_TAGGED_RE = re.compile(r'^#[ \t]*BEGIN[ \t]+(.*?)[ \t]*$', re.I)
_TAGGED_END_RE = re.compile(r'^#[ \t]*END[ \t]+(.*?)[ \t]*$', re.I)


def _regex_dup_keys(text):
    """无 TOML 库时的兜底判重：统计每个表头/顶层点号键被定义几次。"""
    counts = {}
    for line in text.splitlines():
        m = _TABLE_RE.match(line)
        if m:
            k = _split_key(m.group(1))
        else:
            m = _DOTKEY_RE.match(line)
            if not m:
                continue
            k = _split_key(m.group(1))
        counts[k] = counts.get(k, 0) + 1
    return {k: n for k, n in counts.items() if n > 1}


def is_broken(text):
    ok = _parses(text)
    if ok is True:
        return False
    if ok is False:
        return True
    return bool(_regex_dup_keys(text))  # 无库时用正则判重


# ─────────────────────────── 修复策略 ───────────────────────────

def _tagged_blocks(text):
    """返回所有 (start_idx, end_idx, tag) 注释标记块（行号，含首尾）。"""
    lines = text.splitlines(keepends=True)
    blocks, open_tag, open_i = [], None, None
    for i, ln in enumerate(lines):
        m = _TAGGED_RE.match(ln.rstrip("\n"))
        if m and open_tag is None:
            open_tag, open_i = m.group(1).strip(), i
            continue
        m = _TAGGED_END_RE.match(ln.rstrip("\n"))
        if m and open_tag is not None:
            blocks.append((open_i, i, open_tag))
            open_tag, open_i = None, None
    return lines, blocks


def strategy_remove_tagged_block(text):
    """策略 A：逐个尝试删掉一个注释标记块，删完能解析就返回。"""
    lines, blocks = _tagged_blocks(text)
    for (s, e, tag) in blocks:
        candidate = "".join(lines[:s] + lines[e + 1:])
        if not is_broken(candidate):
            return candidate, f"切除标记块 # BEGIN {tag}"
    return None, None


def strategy_dedup_definitions(text):
    """策略 B：按归一化键删重复的表头块 / 顶层点号键行，保留首次出现。"""
    lines = text.splitlines(keepends=True)
    seen = set()
    drop = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _TABLE_RE.match(line)
        if m:
            key = _split_key(m.group(1))
            if key in seen:
                # 删表头 + 其body（到下一个表头/文件尾）
                drop.add(i)
                j = i + 1
                while j < len(lines) and not _TABLE_RE.match(lines[j]):
                    drop.add(j)
                    j += 1
                i = j
                continue
            seen.add(key)
        else:
            mk = _DOTKEY_RE.match(line)
            if mk:
                key = _split_key(mk.group(1))
                if key in seen:
                    drop.add(i)
                else:
                    seen.add(key)
        i += 1
    if not drop:
        return None, None
    candidate = "".join(ln for idx, ln in enumerate(lines) if idx not in drop)
    if not is_broken(candidate):
        return candidate, f"删除 {len(drop)} 行重复定义"
    return None, None


# ─────────────────────────── 主流程 ───────────────────────────

def log(msg):
    print(f"[codex-config-dedup {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          file=sys.stderr, flush=True)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else CFG
    if not os.path.exists(path):
        return 0
    try:
        text = open(path, "r", encoding="utf-8").read()
    except Exception as e:
        log(f"读取失败，跳过：{e}")
        return 0

    if not is_broken(text):
        return 0  # 健康，绝不碰

    log("检测到无法解析（疑似重复键），尝试自动修复")
    for fn in (strategy_remove_tagged_block, strategy_dedup_definitions):
        fixed, how = fn(text)
        if fixed and not is_broken(fixed):
            bak = f"{path}.bak-dedup-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                shutil.copy2(path, bak)
                tmp = f"{path}.tmp-{os.getpid()}"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(fixed)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                log(f"已修复（{how}）并写回；备份 {os.path.basename(bak)}")
                return 0
            except Exception as e:
                log(f"写回失败，原文件未动：{e}")
                return 1

    log("自动修复未能使其可解析，保留原文件待人工处理")
    return 1


if __name__ == "__main__":
    sys.exit(main())
