#!/usr/bin/env python3
"""实测某个上游模型能否被 Codex **原生**对接（决定 router 用 passthrough 还是 translate）。

为什么要实测而不是查文档：判据是"上游能不能吃下 Codex 真实发出的 Responses 请求"，
这只在运行时才知道，而且上游会改（实测见过某网关的文档声称支持 /responses，
但个别模型根本没配该通道，请求直接报网关侧错误码）。
手工维护 mode 表必然过期 —— 用这个脚本重新判定。

四级递进，任何一级失败都意味着必须走 chat 桥（translate）：
  1. 端点可用性     最小文本请求能否 200
  2. 流式完整性     stream=true 是否发 response.completed（Codex 靠它收尾）
  3. 工具调用       能否返回 function_call
  4. 工具输出回传   能否吃下 function_call_output ← **agentic 会话的必经之路**
第 4 级是决定性的：Codex 每轮都要把 exec_command 等工具的输出回传给模型，
回传不了就等于没法用，前三级全过也没意义。

用法：
    python3 ~/.codex/probe-responses-support.py                      # 探测 routes 里全部模型
    python3 ~/.codex/probe-responses-support.py <provider> <model>   # 只探一个

AK 只从环境变量读（变量名由 router-routes.json 里每个 provider 的 key_env 指定），不落盘。
"""
import json
import os
import sys
import urllib.error
import urllib.request

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
ROUTES_PATH = os.path.join(CODEX_HOME, "router-routes.json")

# 内置默认与 codex-model-router.py 的 _DEFAULT_ROUTING 保持一致（占位示例）。
# 正常用法是读你自己的 ~/.codex/router-routes.json；这两份默认只在文件缺失时兜底。
DEFAULT_PROVIDERS = {
    "example-gateway": {"base": "https://api.example-gateway.com/v1",
                        "key_env": "EXAMPLE_GATEWAY_API_KEY"},
    "openai": {"base": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
}
DEFAULT_MODELS = {
    "example/responses-model": "example-gateway",
    "example/chat-model": "example-gateway",
}

TOOL = {"type": "function", "name": "calc",
        "description": "multiply two integers and return the product",
        "parameters": {"type": "object",
                       "properties": {"a": {"type": "integer"},
                                      "b": {"type": "integer"}},
                       "required": ["a", "b"]}}
PROMPT = "Use the calc tool to compute 6*7. You must call the tool."

# 与"协议是否支持"无关、纯粹挡住探测的临时状态：不能据此判定 mode，
# 否则会误标 translate（充值/换 key 后结论就变了）。
INCONCLUSIVE_SIG = ("token plan", "用量上限", "积分不足", "余额不足",
                    "insufficient", "quota", "arrearage", "欠费")


def is_inconclusive(status, raw):
    t = (raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)).lower()
    return status in (401, 403) or any(s in t for s in INCONCLUSIVE_SIG)


def load_targets():
    """返回 (providers, {model: provider_name})，优先用 router-routes.json。"""
    providers, models = DEFAULT_PROVIDERS, dict(DEFAULT_MODELS)
    if os.path.exists(ROUTES_PATH):
        try:
            with open(ROUTES_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("providers") and d.get("models"):
                providers = d["providers"]
                models = {m: c.get("provider") for m, c in d["models"].items()}
        except Exception as e:
            print(f"[warn] {ROUTES_PATH} 解析失败，用内置默认：{e}", file=sys.stderr)
    return providers, models


def post(url, key, body, timeout=60, stream=False):
    """返回 (status, raw_bytes)。"""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def err_text(raw):
    """从上游响应里挖出人话错误（有些网关会把真错误嵌在 message 字符串里）。"""
    try:
        d = json.loads(raw.decode(errors="replace"))
    except Exception:
        return raw.decode(errors="replace")[:200]
    e = d.get("error") or {}
    m = e.get("message") or ""
    if isinstance(m, str) and m.startswith("{"):
        try:
            inner = json.loads(m)
            m = (inner.get("error") or {}).get("message") or m
        except Exception:
            pass
    return (m or e.get("code") or json.dumps(d, ensure_ascii=False))[:220]


def probe(base, key, model):
    """逐级探测，返回 (verdict, stage_failed, detail)。

    verdict: "passthrough"（四级全过）| "translate"（协议不支持）|
             "inconclusive"（额度/鉴权等临时问题挡住了探测，不能据此配 mode）
    """
    url = base.rstrip("/") + "/responses"

    # 1. 端点可用性
    st, raw = post(url, key, {"model": model, "stream": False,
                              "input": "Reply with the single word OK."})
    if st != 200:
        if is_inconclusive(st, raw):
            return "inconclusive", "1 端点不可用", f"HTTP {st}: {err_text(raw)}"
        return "translate", "1 端点不可用", f"HTTP {st}: {err_text(raw)}"

    # 2. 流式完整性：必须出现 response.completed，否则 Codex 收不到收尾事件
    st, raw = post(url, key, {"model": model, "stream": True,
                              "input": "Reply with the single word OK."},
                   stream=True)
    if st != 200:
        if is_inconclusive(st, raw):
            return "inconclusive", "2 流式失败", f"HTTP {st}: {err_text(raw)}"
        return "translate", "2 流式失败", f"HTTP {st}: {err_text(raw)}"
    txt = raw.decode(errors="replace")
    if "response.completed" not in txt:
        evs = sorted({l[7:].strip() for l in txt.splitlines() if l.startswith("event:")})
        return "translate", "2 流式缺 response.completed", f"实收事件：{evs}"

    # 3. 工具调用：必须返回 function_call
    st, raw = post(url, key, {
        "model": model, "stream": False,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": PROMPT}]}],
        "tools": [TOOL], "tool_choice": "auto"})
    if st != 200:
        if is_inconclusive(st, raw):
            return "inconclusive", "3 工具调用失败", f"HTTP {st}: {err_text(raw)}"
        return "translate", "3 工具调用失败", f"HTTP {st}: {err_text(raw)}"
    try:
        d = json.loads(raw.decode(errors="replace"))
    except Exception:
        return "translate", "3 工具响应不可解析", raw.decode(errors="replace")[:150]
    calls = [o for o in (d.get("output") or []) if o.get("type") == "function_call"]
    if not calls:
        return "translate", "3 未返回 function_call", \
            f"output types={[o.get('type') for o in (d.get('output') or [])]}"

    # 4. 工具输出回传（决定性）
    c = calls[0]
    st, raw = post(url, key, {
        "model": model, "stream": False,
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": PROMPT}]},
            {"type": "function_call", "name": c.get("name") or "calc",
             "call_id": c.get("call_id") or "call_probe_1",
             "arguments": c.get("arguments") or '{"a": 6, "b": 7}'},
            {"type": "function_call_output",
             "call_id": c.get("call_id") or "call_probe_1",
             "output": "42"},
        ],
        "tools": [TOOL], "tool_choice": "auto"})
    if st != 200:
        if is_inconclusive(st, raw):
            return "inconclusive", "4 工具输出回传", f"HTTP {st}: {err_text(raw)}"
        return "translate", "4 无法回传 function_call_output", f"HTTP {st}: {err_text(raw)}"
    return "passthrough", None, "四级全过"


def main():
    providers, models = load_targets()
    args = sys.argv[1:]
    if len(args) == 2:
        targets = {args[1]: args[0]}
    elif args:
        print(__doc__)
        return 2
    else:
        targets = models

    print(f"{'模型':<22} {'判定':<14} 失败级 / 说明")
    print("-" * 104)
    tally = {"passthrough": 0, "translate": 0, "inconclusive": 0, "skipped": 0}
    rows = []
    for model, pname in sorted(targets.items()):
        p = providers.get(pname or "")
        if not p:
            tally["skipped"] += 1
            rows.append((model, "?", f"provider {pname!r} 未定义"))
            continue
        key = os.environ.get(p.get("key_env") or "")
        if not key:
            tally["skipped"] += 1
            rows.append((model, "skipped", f"环境变量 {p.get('key_env')} 未设置"))
            continue
        verdict, stage, detail = probe(p["base"], key, model)
        tally[verdict] = tally.get(verdict, 0) + 1
        note = detail if verdict == "passthrough" else f"{stage} → {detail}"
        rows.append((model, verdict, note))
    for model, verdict, note in rows:
        print(f"{model:<22} {verdict:<14} {note}")

    print("-" * 104)
    print(f"可原生 passthrough：{tally['passthrough']}  "
          f"必须 chat 桥 translate：{tally['translate']}  "
          f"无法判定 inconclusive：{tally['inconclusive']}  "
          f"跳过：{tally['skipped']}")
    print("把判定写进 ~/.codex/router-routes.json 的 models.<slug>.mode。")
    print("（passthrough = 原样转发、Codex 升级零影响；translate = 必须经 chat 桥；")
    print("  inconclusive = 额度/鉴权挡住探测，别据此改 mode，解决后重跑本脚本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
