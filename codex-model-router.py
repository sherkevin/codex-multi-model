#!/usr/bin/env python3
"""codex-model-router

把多个上游聚合成单个 Responses API 端点，让 codex 只看到一个 provider，
从而能在 /model 列表里自由切换全部模型，并按模型走各自的 AK。

两种模式：
  passthrough  上游原生支持 Responses API，原样转发
  translate    上游只有 /chat/completions，做 Responses <-> Chat 双向翻译

事件格式：命名 SSE，event: 行 + data: 行，sequence_number 全局递增。

AK 只从环境变量读取，不落盘。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEX_ROUTER_PORT", "8317"))

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
ROUTES_PATH = os.path.join(CODEX_HOME, "router-routes.json")

# ── 内置默认路由：无 router-routes.json 时的兜底示例（占位，请换成你自己的上游）──
# provider = 真实上游（base + 用哪个环境变量当 AK）；model 只引用 provider 名 + 模式。
# 这样「model 接哪个 provider」是数据、可由配置台编辑，而不是写死在代码里。
_DEFAULT_ROUTING = {
    "providers": {
        # 示例：一个 OpenAI 兼容网关。换成你自己的上游 base 与 AK 环境变量名。
        "example-gateway": {"base": "https://api.example-gateway.com/v1", "key_env": "EXAMPLE_GATEWAY_API_KEY"},
        "openai":          {"base": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    },
    "models": {
        # 示例模型。mode: passthrough=上游原生 Responses；translate=上游仅 chat/completions。
        "example/responses-model": {"provider": "example-gateway", "mode": "passthrough"},
        "example/chat-model":      {"provider": "example-gateway", "mode": "translate"},
    },
    # 内置 OpenAI 系模型不能因为接了三方就消失：原生说 Responses，按前缀透传到官方端点。
    # 没配 key 时明确报错，而不是悄悄换个模型回答。
    "prefix_routes": [
        {"prefixes": ["gpt-", "o1", "o3", "o4", "codex-", "chatgpt-"],
         "provider": "openai", "mode": "passthrough"},
    ],
}


def _load_routing():
    """优先读 ~/.codex/router-routes.json（配置台可编辑）；缺失或损坏则回退内置默认。

    返回 (ROUTES 扁平表, 前缀路由, 前缀元组, 生效的结构化表)。
    扁平表保持 {model: {base, key_env, mode}} 形状，resolve()/main() 等调用点零改动。
    """
    data, src = _DEFAULT_ROUTING, "builtin"
    if os.path.exists(ROUTES_PATH):
        try:
            with open(ROUTES_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded.get("providers") and loaded.get("models"):
                data, src = loaded, "file"
            else:
                print("[警告] router-routes.json 缺 providers/models，回退内置默认",
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[警告] router-routes.json 解析失败，回退内置默认：{e}",
                  file=sys.stderr, flush=True)

    providers = data["providers"]
    routes = {}
    for m, cfg in data["models"].items():
        p = providers.get(cfg.get("provider"))
        if not p:
            print(f"[警告] 模型 {m} 的 provider {cfg.get('provider')!r} 未定义，已跳过",
                  file=sys.stderr, flush=True)
            continue
        routes[m] = {"base": p["base"], "key_env": p["key_env"],
                     "mode": cfg.get("mode", "passthrough")}

    prefixes, prefix_route = (), None
    for pr in data.get("prefix_routes") or []:
        p = providers.get(pr.get("provider"))
        if p:
            prefixes = tuple(pr.get("prefixes") or [])
            prefix_route = {"base": p["base"], "key_env": p["key_env"],
                            "mode": pr.get("mode", "passthrough")}
            break

    effective = {"source": src, "providers": providers,
                 "models": data["models"], "prefix_routes": data.get("prefix_routes") or []}
    return routes, prefix_route, prefixes, effective


ROUTES, OPENAI_ROUTE, OPENAI_PREFIXES, EFFECTIVE_ROUTING = _load_routing()


def resolve(model):
    """返回路由配置，未知模型返回 None。

    绝不静默回落到另一个模型——那会让使用者以为在用 A、实际是 B 在回答，
    比直接报错危险得多。
    """
    if model in ROUTES:
        return ROUTES[model]
    if model and model.startswith(OPENAI_PREFIXES):
        return OPENAI_ROUTE
    return None

RETRYABLE = ("mpe-429", "限流", "rate limit", "too many requests", "平台运行错误")
MAX_RETRY = 4


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def map_usage(u):
    """chat 的 usage 命名换成 Responses 的命名。

    codex 对 response.completed 里的 usage 做严格解析，input_tokens /
    output_tokens / total_tokens 缺一个就报 failed to parse ResponseCompleted，
    所以上游没给 usage 时也必须补一个全零的。
    """
    u = u or {}
    inp = u.get("input_tokens", u.get("prompt_tokens", 0)) or 0
    out = u.get("output_tokens", u.get("completion_tokens", 0)) or 0
    total = u.get("total_tokens") or (inp + out)
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or (u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    reasoning = ((u.get("completion_tokens_details") or {}).get("reasoning_tokens")
                 or (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": reasoning},
    }


# ─────────────────────────── Responses -> Chat ───────────────────────────

def _ensure_json_args(args):
    """保证 tool_call 的 function.arguments 是合法 JSON 字符串，非法则退回 "{}"。

    很多 chat 网关（如 qwen 系）会校验请求历史里**每个** tool_call 的 arguments 必须
    是合法 JSON，否则整条请求 400（典型报错 `function.arguments ... must be in JSON
    format`）。当模型把超大内容（如一次性 `cat > file <<'EOF' …` 的 heredoc）塞进单个
    工具调用时，输出可能撞到上游生成上限被截断，arguments 变成未闭合的 JSON；客户端
    解析失败后仍把这条坏 args 存进历史，于是之后**每个**请求都被它拖垮、整条会话卡死。

    这里在把 Responses 历史翻译成 chat 请求时兜底：非法 JSON 一律置为 "{}"。那条调用
    本就失败、其输出已记为解析错误，置空不改变语义，模型照常自行重试；但请求重新合法、
    会话不再卡死。
    """
    if not isinstance(args, str) or not args.strip():
        return "{}"
    try:
        json.loads(args)
        return args
    except Exception:
        log("   [warn] tool_call arguments 非法 JSON（疑似上游截断），回传历史时置为 {}")
        return "{}"


def repair_tool_pairs(msgs):
    """保证 chat 协议不变式：带 tool_calls 的 assistant 消息后面必须紧跟每个
    tool_call_id 对应的 tool 消息。

    会话被中断时（模型已发出工具调用、输出还没回来用户就按了 Esc），Responses
    历史里会留下没有 function_call_output 的孤儿调用，上游一律以 400 拒绝整个
    请求。这里给孤儿补一条占位输出，并丢掉对不上任何调用的游离 tool 消息。
    """
    out, i = [], 0
    while i < len(msgs):
        m = msgs[i]
        out.append(m)
        i += 1
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        replies = {}
        while i < len(msgs) and msgs[i].get("role") == "tool":
            replies[msgs[i].get("tool_call_id")] = msgs[i]
            i += 1
        for tc in m["tool_calls"]:
            reply = replies.pop(tc["id"], None)
            if reply is None:
                log(f"   [warn] tool_call {tc['id']} 无输出，补占位")
                reply = {"role": "tool", "tool_call_id": tc["id"],
                         "content": "[no output: call was interrupted]"}
            out.append(reply)
        for orphan in replies:
            log(f"   [warn] 游离 tool 消息 tool_call_id={orphan}，已丢弃")
    return out


def resp_req_to_chat(body):
    """把 codex 的 Responses 请求体翻译成 chat/completions 请求体。"""
    msgs = []
    if body.get("instructions"):
        msgs.append({"role": "system", "content": body["instructions"]})

    for item in body.get("input") or []:
        if isinstance(item, str):
            msgs.append({"role": "user", "content": item})
            continue
        t = item.get("type")
        if t == "message" or (t is None and item.get("role")):
            role = item.get("role") or "user"
            # developer 角色 chat 协议不认，降级成 system
            if role == "developer":
                role = "system"
            parts = item.get("content")
            if isinstance(parts, str):
                text = parts
            else:
                text = "".join(p.get("text", "") for p in (parts or [])
                               if isinstance(p, dict))
            msgs.append({"role": role, "content": text})
        elif t == "function_call":
            tc = {
                "id": item.get("call_id") or item.get("id") or new_id("call"),
                "type": "function",
                "function": {"name": item.get("name", ""),
                             "arguments": _ensure_json_args(item.get("arguments", ""))},
            }
            # 并行工具调用在 Responses 里是连续多个 function_call item。chat 协议要求
            # 一条 assistant 消息带上全部 tool_calls，紧跟着才是各自的 tool 回复；
            # 每个 call 单独成一条 assistant 消息会让上游报 tool_call_id 无应答。
            if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
                msgs[-1]["tool_calls"].append(tc)
            else:
                msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
        elif t == "function_call_output":
            out = item.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool",
                         "tool_call_id": item.get("call_id") or "",
                         "content": out})
        elif t == "reasoning":
            continue  # 上游 chat 协议不接受回传推理内容
        else:
            log(f"   [warn] 未识别的 input item type={t}，已跳过")

    chat = {"model": body["model"], "messages": repair_tool_pairs(msgs),
            "stream": bool(body.get("stream")), "max_tokens": 8192}

    tools = []
    for tl in body.get("tools") or []:
        if tl.get("type") != "function":
            continue
        # Responses 里 tool 是扁平的；chat 里要包一层 function
        fn = tl.get("function") or tl
        tools.append({"type": "function", "function": {
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }})
    if tools:
        chat["tools"] = tools
        if body.get("tool_choice"):
            chat["tool_choice"] = body["tool_choice"]
        if body.get("parallel_tool_calls") is not None:
            chat["parallel_tool_calls"] = body["parallel_tool_calls"]
    return chat


# ─────────────────────────── Chat -> Responses SSE ───────────────────────────

class ResponsesEmitter:
    """按抓包到的真实格式合成 Responses 事件流。"""

    def __init__(self, wfile, req_body):
        self.w = wfile
        self.req = req_body
        self.seq = 0
        self.output_index = 0
        self.resp_id = new_id("resp")
        self.output = []          # 累积最终 output 数组
        self.open_kind = None     # None | reasoning | message | function_call
        self.finish_reason = None  # 上游最后一个 finish_reason（length 表示被截断）
        self.item_id = None
        self.buf = ""             # 当前 item 累积文本
        self.fc = None            # 当前 function_call 状态

    # --- 底层 ---
    def emit(self, etype, payload):
        payload = dict(payload)
        payload["type"] = etype
        payload["sequence_number"] = self.seq
        self.seq += 1
        blob = (f"event: {etype}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
        self.w.write(blob.encode())
        self.w.flush()

    def _response_obj(self, status, usage=None):
        o = {
            "id": self.resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.req.get("model"),
            "instructions": self.req.get("instructions"),
            "reasoning": self.req.get("reasoning") or {},
            "tools": self.req.get("tools") or [],
            "tool_choice": self.req.get("tool_choice", "auto"),
            "parallel_tool_calls": self.req.get("parallel_tool_calls", True),
            "store": self.req.get("store", False),
            "metadata": {},
            "output": self.output,
        }
        if usage is not None:
            o["usage"] = usage
        return o

    # --- 生命周期 ---
    def start(self):
        self.emit("response.created", {"response": self._response_obj("in_progress")})
        self.emit("response.in_progress", {"response": self._response_obj("in_progress")})

    def finish(self, usage=None):
        self.close_item()
        self.emit("response.completed",
                  {"response": self._response_obj("completed", map_usage(usage))})

    def fail(self, message, etype="server_error"):
        self.emit("error", {"error": {"message": str(message)[:500], "type": etype}})

    # --- item 开合 ---
    def open_item(self, kind, name=None, call_id=None):
        if self.open_kind == kind and kind != "function_call":
            return
        self.close_item()
        self.item_id = new_id("msg")
        self.open_kind = kind
        self.buf = ""
        if kind == "reasoning":
            item = {"type": "reasoning", "id": self.item_id, "summary": []}
        elif kind == "message":
            item = {"type": "message", "id": self.item_id,
                    "role": "assistant", "content": []}
        else:
            self.fc = {"name": name or "", "args": "",
                       "call_id": call_id or new_id("call")}
            item = {"type": "function_call", "id": self.item_id,
                    "name": self.fc["name"], "arguments": "",
                    "call_id": self.fc["call_id"]}
        self.emit("response.output_item.added",
                  {"item": item, "output_index": self.output_index})
        if kind == "message":
            self.emit("response.content_part.added", {
                "output_index": self.output_index, "content_index": 0,
                "item_id": self.item_id,
                "part": {"type": "output_text", "annotations": [], "text": ""}})

    def close_item(self):
        if self.open_kind is None:
            return
        k, iid = self.open_kind, self.item_id
        if k == "reasoning":
            self.emit("response.reasoning_text.done", {
                "output_index": self.output_index, "content_index": 0,
                "item_id": iid, "text": self.buf})
            item = {"type": "reasoning", "id": iid,
                    "summary": ([{"type": "summary_text", "text": self.buf}]
                                if self.buf.strip() else [])}
        elif k == "message":
            self.emit("response.output_text.done", {
                "output_index": self.output_index, "content_index": 0,
                "item_id": iid, "text": self.buf, "logprobs": []})
            self.emit("response.content_part.done", {
                "output_index": self.output_index, "content_index": 0,
                "item_id": iid,
                "part": {"type": "output_text", "annotations": [], "text": self.buf}})
            item = {"type": "message", "id": iid, "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "annotations": [],
                                 "text": self.buf}]}
        else:
            args = self.fc["args"] or "{}"
            if args.strip():
                try:
                    json.loads(args)
                except Exception:
                    log(f"   [warn] 上游 tool_call '{self.fc['name']}' arguments 截断/非法 JSON"
                        f"（finish_reason={self.finish_reason}，len={len(args)}）；原样下发，"
                        f"回传历史时会由 _ensure_json_args 兜底")
            self.emit("response.function_call_arguments.done", {
                "name": self.fc["name"], "arguments": args,
                "output_index": self.output_index, "item_id": iid})
            item = {"type": "function_call", "id": iid,
                    "name": self.fc["name"], "arguments": args,
                    "call_id": self.fc["call_id"], "status": "completed"}
        self.emit("response.output_item.done",
                  {"item": item, "output_index": self.output_index})
        self.output.append(item)
        self.output_index += 1
        self.open_kind = None
        self.item_id = None
        self.buf = ""
        self.fc = None

    # --- 增量 ---
    def reasoning_delta(self, text):
        self.open_item("reasoning")
        self.buf += text
        self.emit("response.reasoning_text.delta", {
            "delta": text, "output_index": self.output_index,
            "content_index": 0, "item_id": self.item_id})

    def text_delta(self, text):
        self.open_item("message")
        self.buf += text
        self.emit("response.output_text.delta", {
            "delta": text, "content_index": 0, "item_id": self.item_id,
            "output_index": self.output_index, "logprobs": []})

    def tool_delta(self, name, args, call_id, is_new):
        if is_new or self.open_kind != "function_call":
            self.open_item("function_call", name=name, call_id=call_id)
        if name and not self.fc["name"]:
            self.fc["name"] = name
        if args:
            self.fc["args"] += args
            self.emit("response.function_call_arguments.delta", {
                "delta": args, "output_index": self.output_index,
                "item_id": self.item_id})


def translate_stream(up, wfile, req_body):
    """消费上游 chat SSE，合成 Responses 事件流。"""
    em = ResponsesEmitter(wfile, req_body)
    em.start()
    usage = None
    seen_tool_idx = set()
    chunks = 0
    try:
        for raw in up:
            chunks += 1
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if not p or p == "[DONE]":
                continue
            try:
                o = json.loads(p)
            except Exception:
                continue
            if o.get("usage"):
                usage = o["usage"]
            for ch in o.get("choices") or []:
                fr = ch.get("finish_reason")
                if fr:
                    em.finish_reason = fr
                d = ch.get("delta") or {}
                if d.get("reasoning_content"):
                    em.reasoning_delta(d["reasoning_content"])
                if d.get("content"):
                    em.text_delta(d["content"])
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    em.tool_delta(fn.get("name"), fn.get("arguments"),
                                  tc.get("id"), idx not in seen_tool_idx)
                    seen_tool_idx.add(idx)
        log(f"   [usage] model={req_body.get('model')} chunks={chunks} "
            f"upstream={json.dumps(usage, ensure_ascii=False) if usage else None} "
            f"mapped={json.dumps(map_usage(usage), ensure_ascii=False)}")
        em.finish(usage)
    except (BrokenPipeError, ConnectionResetError):
        log("   客户端断开")
    except Exception as e:
        log(f"   翻译中断: {type(e).__name__}: {e}")
        try:
            em.fail(f"router translate error: {e}")
        except Exception:
            pass


# ─────────────────────────── HTTP ───────────────────────────

def call_upstream(route, path, payload, accept, timeout=900):
    key = os.environ.get(route["key_env"])
    if not key:
        raise RuntimeError(f"环境变量 {route['key_env']} 未设置")
    req = urllib.request.Request(
        route["base"] + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": accept or "*/*"},
        method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-model-router/2.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.rstrip("/")
        if path.endswith("/models"):
            self._json(200, {"object": "list",
                             "data": [{"id": m, "object": "model",
                                       "owned_by": "router"} for m in ROUTES]})
        elif path.endswith("/routes"):
            # 透明端点：返回生效的 provider/model/前缀路由，让配置台能展示
            # 「每个模型真实走哪个 provider」，把 router 这层黑盒掀开。
            self._json(200, EFFECTIVE_ROUTING)
        else:
            self._json(404, {"error": {"message": f"no route for {self.path}"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/responses"):
            self._json(404, {"error": {"message": f"no route for {self.path}"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode())
        except Exception as e:
            self._json(400, {"error": {"message": f"bad body: {e}"}})
            return

        model = body.get("model", "")
        route = resolve(model)
        if route is None:
            known = ", ".join(list(ROUTES) + ["gpt-* / o* / codex-*"])
            log(f"-> {model}  无路由，已拒绝")
            self._json(400, {"error": {
                "type": "invalid_request_error",
                "message": (f"router 未为模型 {model!r} 配置路由。"
                            f"已知：{known}。"
                            "如需新增，请在 ~/.codex/codex-model-router.py 的 ROUTES 里登记。")}})
            return
        mode = route["mode"]
        if not os.environ.get(route["key_env"]):
            log(f"-> {model}  缺少 {route['key_env']}")
            self._json(401, {"error": {
                "type": "invalid_request_error",
                "message": (f"模型 {model} 需要环境变量 {route['key_env']}，"
                            "当前未设置。请 export 后重启代理（用你自己的服务 label）："
                            "launchctl kickstart -k gui/$(id -u)/<你的-router-label>")}})
            return
        log(f"-> {model}  [{mode}]  tools={len(body.get('tools') or [])}")

        payload = body if mode == "passthrough" else resp_req_to_chat(body)
        path = "/responses" if mode == "passthrough" else "/chat/completions"

        up = None
        last = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                up = call_upstream(route, path, payload,
                                   self.headers.get("Accept"))
                break
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                last = (e.code, raw)
                if attempt < MAX_RETRY and any(t in raw.lower() for t in RETRYABLE):
                    log(f"   上游可重试错误，第 {attempt} 次退避重试")
                    time.sleep(1.5 * attempt)
                    continue
                break
            except Exception as e:
                last = (502, f"{type(e).__name__}: {e}")
                break

        if up is None:
            code, raw = last
            log(f"   失败 HTTP {code}: {str(raw)[:200]}")
            try:
                self._json(code if 400 <= code < 600 else 502, json.loads(raw))
            except Exception:
                self._json(code if 400 <= code < 600 else 502,
                           {"error": {"message": str(raw)[:500],
                                      "type": "upstream_error"}})
            return

        ctype = up.headers.get("Content-Type", "application/json")

        if mode == "passthrough":
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if "event-stream" in ctype:
                    for line in up:
                        self.wfile.write(line)
                        self.wfile.flush()
                else:
                    self.wfile.write(up.read())
            except (BrokenPipeError, ConnectionResetError):
                log("   客户端断开")
            return

        # translate
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if "event-stream" in ctype:
            translate_stream(up, self.wfile, body)
        else:
            # 上游没给流，把整包转成一次性事件序列
            data = json.loads(up.read().decode())
            em = ResponsesEmitter(self.wfile, body)
            em.start()
            ch = (data.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            if msg.get("reasoning_content"):
                em.reasoning_delta(msg["reasoning_content"])
            if msg.get("content"):
                em.text_delta(msg["content"])
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                em.tool_delta(fn.get("name"), fn.get("arguments"),
                              tc.get("id"), True)
            em.finish(data.get("usage"))


def main():
    missing = sorted({c["key_env"] for c in ROUTES.values()
                      if not os.environ.get(c["key_env"])})
    if missing:
        log(f"[警告] 环境变量未设置，相关模型会失败: {', '.join(missing)}")
    log(f"codex-model-router 监听 http://{HOST}:{PORT}/v1/responses")
    for m, c in ROUTES.items():
        log(f"  {m:20s} {c['mode']:12s} -> {c['base']}  (${c['key_env']})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
