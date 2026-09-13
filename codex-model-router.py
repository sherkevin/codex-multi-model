#!/usr/bin/env python3
"""codex-model-router

把多个上游聚合成单个 Responses API 端点，让 codex 只看到一个 provider，
从而能在 /model 列表里自由切换全部模型，并按模型走各自的 AK。

三种模式：
  passthrough  上游原生支持 Responses API，原样转发，payload 零改动
  translate    上游只有 /chat/completions，做 Responses <-> Chat 双向翻译

**mode 的判据（唯一标准）**：上游能不能吃下 Codex 的真实 Responses 请求。
  能 → passthrough（零改动，Codex 升级不受影响，桥接层不参与任何语义）
  不能 → translate（chat 桥，只保留协议翻译这一件事）
用 `python3 ~/.codex/probe-responses-support.py <provider> <model>` 实测，别靠猜。

为什么这个桥必须存在：codex 0.142.5 起 wire_api="chat" 已被官方删除（二进制
硬编码报错 "wire_api = \"chat\" is no longer supported"，WireApi 枚举只剩
responses），所以 Codex 自己**无法**对接任何只有 chat/completions 的网关。

而"上游声称支持 /responses"并不等于能用。实测有一类 OpenAI 兼容聚合网关，
/responses 对任何模型都无法回传 function_call_output（它内部把工具输出转成
role=tool，再被自己的校验器拒掉："tool must be one of user,assistant,system,
function"）；个别模型干脆没配 responses 通道，直接报网关侧错误码。agentic
会话每轮都要回传工具输出，所以这类上游只能走 chat 桥。别信文档，跑 probe。

事件格式对齐真实抓包（/tmp/rec_responses.jsonl）：命名 SSE，event: 行 + data: 行，
sequence_number 全局递增。

AK 只从环境变量读取，不落盘。
"""
import json
import os
import sys
import threading
import time
import base64
import hashlib
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEX_ROUTER_PORT", "8317"))

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
ROUTES_PATH = os.path.join(CODEX_HOME, "router-routes.json")

# ROUTER_DUMP_REQUESTS=1 时把每个请求体落盘，便于排查协议问题。
# 不硬编码 /tmp（Windows 没有）：默认走平台临时目录，可用 ROUTER_DUMP_PATH 覆盖。
DUMP_REQUEST_PATH = os.environ.get(
    "ROUTER_DUMP_PATH",
    os.path.join(tempfile.gettempdir(), "codex-router-last-request.json"))

# ── 原生能力提示：让第三方模型也知道 codex 的「延迟工具」协议，尽量恢复原生体验 ──
# codex 把大量原生工具（线程/多智能体/automations/app·MCP）设为 deferred：不在模型即时
# 工具表里，要模型先调用元工具 `tool_search` 把它们加载出来再用。GPT-5-codex 被专门调过、
# 会主动这么做；第三方模型常无视它，于是"看不到=以为没有"，转而用外部脚本凑。
# 这段附言只点破这一个高杠杆行为，一段话解锁全部延迟工具。仅 translate 路径注入（=自定义模式
# 经中转，天然只在自定义模式生效）；设 ROUTER_NATIVE_HINT=0 可关。
# 默认**开启**：它依赖 BRIDGE_DEFERRED 把 tool_search 真正下发给模型，而后者现已默认开
# （见 BRIDGE_DEFERRED 处的说明）。两者一起开才自洽——只开 hint 不开桥，等于叫模型去调
# 一个它根本收不到的工具。要关就两个一起关。
NATIVE_HINT = os.environ.get("ROUTER_NATIVE_HINT", "1") == "1"
NATIVE_HINT_TEXT = (
    "\n\n[Native Codex tools — concrete] You are Codex on a third-party model via a local router. "
    "Codex's native tools are available to you; some are active now, others are DEFERRED and must be "
    "loaded with the `tool_search` meta-tool before calling. When a native tool exists, use it — do NOT "
    "improvise with shell scripts or external one-shot LLM calls.\n"
    "Multi-agent (use ONLY when the user explicitly asks for subagents, delegation, or parallel/"
    "multi-angle work; 'be thorough' or 'investigate' alone is NOT permission to spawn):\n"
    "- `spawn_agent`: spawn a sub-agent for a concrete, bounded task that can run in parallel with your "
    "local work. Args: `task_name` (lowercase/digits/underscore), `message` or `items` (the task), "
    "`fork_turns` (true=inherit this thread's history, false=only the initial prompt). Sub-agents "
    "inherit your model by default — omit `model` unless the user asks. Returns the agent id + canonical "
    "task name; the spawned agent has your tools and can spawn its own.\n"
    "- `wait_agent`: wait for spawned agent(s) to reach a final status (pass several ids to wake on the "
    "first); use sparingly, only when your next step is blocked on the result.\n"
    "- `list_agents`: list live agents. `send_input`/`send_message`: message a running agent "
    "(`send_input` with interrupt=true redirects it immediately). `close_agent`: close agents no longer "
    "needed (they count toward the concurrency limit until closed).\n"
    "Other deferred natives — load via `tool_search`, then call per the returned schema: threads "
    "(`create_thread`, `fork_thread`, `read_thread`, `wait_threads`, `send_message_to_thread`, "
    "`handoff_thread`), automations (`automation_update`), MCP resources (`list_mcp_resources`, "
    "`read_mcp_resource`).\n"
    "If any tool you need is not in your active list, it is deferred: call `tool_search` "
    "(e.g. query=\"spawn_agent\") to load it, then call it. Never assume a Codex capability is missing "
    "just because its tool is not listed yet."
)

# ── 内置默认路由：没有 ~/.codex/router-routes.json 时的兜底 ──
# provider = 真实上游（base + 用哪个环境变量当 AK）；model 只引用 provider 名 + 模式。
# 这样「model 接哪个 provider」是数据、可由配置台编辑，而不是写死在代码里。
#
# 下面的 base/slug 全是**占位示例**，照抄跑不通，请换成你自己的上游：
#   base    上游 OpenAI 兼容端点（写到 /v1 为止，不含 /responses、/chat/completions）
#   key_env 存 AK 的环境变量名（AK 只从环境读，不写进任何文件）
#   mode    passthrough / translate —— 用 probe-responses-support.py 实测决定，别猜
# mode 写错成 passthrough 而上游其实吃不下 Responses，会在 router-routes.json
# 缺失/损坏回退到这里时，把请求打到必然失败的端点上。
_DEFAULT_ROUTING = {
    "providers": {
        "example-gateway": {"base": "https://api.example-gateway.com/v1",
                            "key_env": "EXAMPLE_GATEWAY_API_KEY"},
        "openai": {"base": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    },
    "models": {
        # 同一个网关上的两个模型：一个原生说 Responses，一个只有 chat/completions。
        # slug 必须与 custom-model-catalog.json 里的一致，才会出现在 /model 列表。
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


def _route_from(cfg, provider):
    """把一条 model/prefix 配置 + 它的 provider 合成扁平路由。

    可选字段（都按上游网关的真实上限填，不填就用全局默认）：
      body_byte_limit    请求体字节硬顶，超了上游回 TooLarge
      input_token_limit  输入 token 硬顶，超了上游回 Range of input length
    这两个值决定压缩请求能多大、以及一个 400 该当成"真超限"还是"瞬时抖动"，
    所以必须是**路由级**而不是全局级 —— 见 _limits_for。
    """
    r = {"base": provider["base"], "key_env": provider["key_env"],
         "mode": cfg.get("mode", "passthrough")}
    for k in ("body_byte_limit", "input_token_limit"):
        v = cfg.get(k, provider.get(k))
        if v:
            r[k] = int(v)
    return r


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
        routes[m] = _route_from(cfg, p)

    prefixes, prefix_route = (), None
    for pr in data.get("prefix_routes") or []:
        p = providers.get(pr.get("provider"))
        if p:
            prefixes = tuple(pr.get("prefixes") or [])
            prefix_route = _route_from(pr, p)
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

# ── 上游错误签名：按网关措辞配置，不写死 ────────────────────────────────
# 每个网关的报错文案都不一样，所以三组签名都做成环境变量可覆盖，默认值是实测中
# 较通用的措辞。换网关后如果"超限没触发压缩"或"明明能重试却直接报错"，先来这里改签名。
#   ROUTER_OVERLIMIT_SIG   token 超限（该唤醒 codex 压缩）
#   ROUTER_TOOLARGE_SIG    请求体字节超限（该在路由内卸载字节后重试）
#   ROUTER_RETRYABLE_SIG   瞬时抖动（该退避重试）；逗号分隔，可留空
#
# 关于 "range of input length" 同时出现在 overlimit 与 retryable 里：这不是笔误。
# 实测某些网关会在**远未超限**时（19k–116k token，上限 98 万）偶发(~0.67%)回这句，
# 同一请求重试即成功。所以用请求字符量区分二者：大的当超限去压缩，小的当抖动去重试
# （见 do_POST 的 overlimit_chars 判断）。
OVERLIMIT_SIG = os.environ.get("ROUTER_OVERLIMIT_SIG", "range of input length").lower()
# 请求体字节超限。**不能**当 token 超限处理：压缩降的是 token，图片字节纹丝不动，
# 翻译成 context_length_exceeded 会让 codex 反复压文本直到终态 "ran out of room"
# （实测某线程 token 仅 57% 却报满）。正确做法是路由内就地卸载字节后重试
# （见 _enforce_body_byte_limit 与 do_POST 的 TooLarge 分支，以及 ADR 0004）。
TOOLARGE_SIG = os.environ.get("ROUTER_TOOLARGE_SIG", "max bytes to request body").lower()
# 可重试的瞬时错误签名（命中即退避重试，对客户端透明）。
RETRYABLE = tuple(x.strip().lower() for x in os.environ.get(
    "ROUTER_RETRYABLE_SIG",
    "mpe-429,限流,rate limit,too many requests,平台运行错误,range of input length"
).split(",") if x.strip())
MAX_RETRY = int(os.environ.get("ROUTER_MAX_RETRY", "4"))
#
# ── 上游限流值：按路由配置，不写死 ──────────────────────────────────────
# 下面两个数都是**上游网关的属性**，不是 router 的属性：
#   body_byte_limit   请求体字节硬顶（超了回 TooLarge）
#   input_token_limit 输入 token 硬顶（超了回 Range of input length）
# 不同网关差别巨大（OpenAI 官方没有字节硬顶；某些兼容网关卡 6MB / 98 万 token），
# 写死在代码里会让别人复用时莫名其妙地丢图或死锁。所以做成每条路由可选字段，
# 缺省时用环境变量，再缺省用下面的保守默认值。
#
# 字节默认取 6MB 而不是"不限"：判错的代价不对称。给没有硬顶的网关设了 6MB，
# 只是多降一档图片质量；给有硬顶的网关没设，则 TooLarge → 会话死锁（见 ADR 0004）。
UPSTREAM_BODY_BYTE_LIMIT = int(os.environ.get("ROUTER_BODY_BYTE_LIMIT", "6291456"))
UPSTREAM_INPUT_LIMIT = int(os.environ.get("ROUTER_INPUT_LIMIT", "0"))  # 0 = 不设 token 护栏
# 压缩请求的可靠字符上限。按"任意内容 ≤ ~1.4 token/char"换算，与内容类型无关，
# 无需 token 估算器（_est_tokens 的比率随内容漂移、不可靠）。默认 90 万字符
# ⇒ ≤ ~98 万 token；接了 token 上限更小的网关时由 _limits_for() 自动收紧。
COMPACT_CHAR_CAP = int(os.environ.get("ROUTER_COMPACT_CHAR_CAP", "900000"))
# 400 路径拿不到 usage 时，用请求字符量区分"真超限"(大 → 该触发 codex 压缩) 与
# "瞬时抖动"(小 → 该重试)。有些网关会在**远未超限**时偶发报同一句错，实测同一请求
# 重试即成功，所以不能见错就压缩。配了 token 上限时按上限收紧（见 _limits_for）。
OVERLIMIT_CHAR_THRESHOLD = int(os.environ.get("ROUTER_OVERLIMIT_CHAR_THRESHOLD", "900000"))


def _limits_for(route):
    """算出某条路由生效的护栏值。路由字段 > 环境变量 > 保守默认。

    token 上限决定压缩请求的字符上限：90 万字符的默认值只对 ~98 万 token 的网关
    安全，接了 128k 上限的网关照用会把压缩请求自己撑爆（压不下去 → 死锁）。
    """
    route = route or {}
    byte_limit = int(route.get("body_byte_limit") or UPSTREAM_BODY_BYTE_LIMIT)
    budget = int(os.environ.get("ROUTER_BODY_BYTE_BUDGET", "0")) or int(byte_limit * 0.92)
    tok = int(route.get("input_token_limit") or UPSTREAM_INPUT_LIMIT or 0)
    if tok > 0:
        # 换算用**最坏比率** 1.4 token/char（本文件其它处一致采用），保证
        # cap × 1.4 ≤ tok，与内容是中英文还是 base64 无关。宁紧勿松：cap 偏小只是
        # 压缩请求多裁一点，偏大则是"压缩请求自己超限 → 死锁"。
        cap = min(COMPACT_CHAR_CAP, int(tok / 1.4))
        # "真超限 vs 瞬时抖动"的判据：请求字符量大到**有可能**撞上 token 上限，
        # 才当成真超限去唤醒压缩；否则按抖动重试（有些网关会在远未超限时偶发
        # 报同一句错，实测同一请求重试即成功）。
        over = min(OVERLIMIT_CHAR_THRESHOLD, int(tok / 1.4))
    else:
        cap = COMPACT_CHAR_CAP
        # token 上限未知：没法判断"大到足以撞限"，退回历史阈值。
        over = OVERLIMIT_CHAR_THRESHOLD
    return {"byte_limit": byte_limit, "byte_budget": budget,
            "token_limit": tok, "compact_char_cap": cap,
            "overlimit_chars": over}

# ── 图片：原生 image_url，而非 base64 文本 ──────────────────────────────
# 根因（实测）：codex 把截图放进 function_call_output 的 input_image part，而 chat 协议的
# tool 角色**不接受图片**（上游报 "Unexpected item"）。原翻译层对整个 output 做 json.dumps，
# 把图片拍平成 base64 纯文本 → 单张 1.06MB 截图在上游值 779,811 token（按 1.36 字符/token），
# 同一张图作为原生 image_url part 只值 1,759 token，**膨胀 443 倍**。
#   后果一：状态栏读 codex 侧 token（图片≈1.7k），上游实收 1.45M → "显示 23% 却超限"。
#   后果二：codex 的自动压缩阈值同样看不到真实体积，压缩永不触发（不是压缩机制坏了）。
# 修法：图片一律还原成原生 image_url part；tool 输出里的图片提升到紧随其后的 user 消息。
#   上游实测：4 张图 3.21MB 请求体，16.7s 返回、仅 6,963 token，且正确读懂内容。
# ROUTER_MAX_IMAGES：历史很长时（实测单线程 246 处 input_image）只保留最近的若干张，
#   更旧的降级成文字占位——每张仍值 ~1.7k token，不设限也会堆出几十万 token。
ROUTER_MAX_IMAGES = int(os.environ.get("ROUTER_MAX_IMAGES", "32"))
# 上游实测接受 max_tokens=65536；原硬编码 8192 会截断长 tool_call arguments，产生大量
# "非法 JSON"（已由 _ensure_json_args 兜底，但根因在此）。
MAX_OUTPUT_TOKENS = int(os.environ.get("ROUTER_MAX_OUTPUT_TOKENS", "32768"))


_tls = threading.local()

# 日志除了 stderr，还可以同时追加到文件。launchd/systemd 各自有办法接住 stderr，
# 但 Windows 计划任务没有 —— 不设这个变量的话，常驻跑起来就完全没日志可查。
# ROUTER_LOG_FILE 留空 = 只写 stderr（前台调试时的默认，最直观）。
_LOG_FILE = os.environ.get("ROUTER_LOG_FILE", "").strip()
_LOG_FH = None
if _LOG_FILE:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(os.path.expanduser(_LOG_FILE))),
                    exist_ok=True)
        _LOG_FH = open(os.path.expanduser(_LOG_FILE), "a", encoding="utf-8")
    except Exception:
        # 日志打不开绝不能拖垮路由本身，退回只写 stderr。
        _LOG_FH = None

def log(*a):
    rid = getattr(_tls, "rid", "")
    msg = " ".join(str(x) for x in a)
    line = f"[{rid}] {msg}" if rid else msg
    print(line, file=sys.stderr, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass


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

    上游 chat 网关会校验请求历史里**每个** tool_call 的 arguments 必须是合法 JSON，
    否则整条请求 400（实测见过网关报 `function.arguments ... must be in JSON format`）。
    模型把超大 heredoc（如 `cat > file <<'PY' …`）一次性塞进 exec_command 时，输出
    会撞到上游生成上限被截断，arguments 变成未闭合的 JSON；codex 解析失败后仍把这条
    坏 args 存进历史，于是之后**每个**请求都被它拖垮、整条会话彻底卡死。

    这里在把 Responses 历史翻译成 chat 请求时兜底：非法 JSON 一律置为 "{}"。那条调用
    本就失败、其 function_call_output 已记为 "failed to parse"，置空不改变语义，模型
    照常自行重试；但请求重新变成合法、会话不再卡死。
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


# ── 延迟工具桥接：让第三方模型也能用 codex 的原生工具 ──────────────────────
# codex 把大量原生工具（create_thread 等线程管理、multi_agent、automations、
# app/MCP namespace、apply_patch、tool_search）放在 Responses 的 namespace/custom/
# tool_search 类型里，而不是普通 function。早先的 translate 只认 type==function，
# 这些工具被整个丢掉 → 模型永远看不到它们，只能拿 shell 脚本硬凑（这正是用户最初
# 抱怨的"不能创建其他聊天窗口 / 不能 user input QA"）。
#
# 现在 _responses_tool_to_chat 已实现完整双向翻译（namespace 摊平、custom 包 input、
# tool_search 合成 function，上行再由 emitter 还原成带 namespace 的正确 item 形态），
# 实测把 Codex 真实请求的 15 个工具翻成 32 个 chat 工具、kind 映射全部正确，
# 故默认**开启**。设 ROUTER_BRIDGE_DEFERRED=0 可退回只转发普通 function 的旧行为。
BRIDGE_DEFERRED = os.environ.get("ROUTER_BRIDGE_DEFERRED", "1") == "1"


# custom（freeform）工具桥接时的参数说明：chat function 只接受 JSON 参数，
# 把自由格式正文装进 {"input": "..."}，emitter 上行时再拆出来填 custom_tool_call.input。
CUSTOM_TOOL_ARG_DESC = {
    "apply_patch": ("The complete patch text in codex apply_patch grammar, verbatim: "
                    "starts with '*** Begin Patch', ends with '*** End Patch', with "
                    "'*** Add File: <path>' / '*** Delete File: <path>' / "
                    "'*** Update File: <path>' sections and @@ context hunks; "
                    "new/changed lines prefixed with '+' or '-'."),
}
CUSTOM_TOOL_DESC = {
    "apply_patch": ("Apply a patch to files in the workspace using the codex "
                    "apply_patch freeform grammar."),
}


def _chat_fn(name, desc, params):
    if not name:
        return None
    return {"type": "function", "function": {
        "name": name, "description": desc or "",
        "parameters": params or {"type": "object", "properties": {}}}}


def _bridge_maps(tools):
    """一次性算出桥接所需的四张表，保证下行翻译与上行还原用的是同一套映射。"""
    plan = _namespace_plan(tools)
    canon, alias = _build_name_index(tools, plan)
    return plan, _build_tool_kind(tools, plan), canon, alias


def _responses_tool_to_chat(tl, plan=None):
    """把一个 Responses ToolSpec 转成 0..n 个 chat function 工具。

    codex 用 #[serde(tag="type")] 序列化工具：function / namespace / tool_search /
    web_search / custom。原 translate 只认 type==function，把 tool_search 和 namespace
    （spawn_agent 这类多智能体工具）全丢了 → 第三方模型永远够不到原生延迟工具。这里补全：
      function    -> 1 个 chat function
      namespace   -> 摊平其内嵌工具；名字由 plan 决定（默认裸名，重名时加 ns 前缀消歧）
      tool_search -> 合成一个 tool_search function（参数 query/limit），让模型能调它来加载延迟工具
      custom      -> apply_patch 这类自由格式工具：chat 侧包成 function(input: string)，
                     上行再由 emitter 还原成 custom_tool_call item（codex 只认 freeform 形态，
                     function 形态的 apply_patch 会解析失败，故必须在路由层桥接）
      web_search  -> 不桥接（丢弃）。它是 Responses 的**类型工具**而非 function，语义是
                     "上游服务端自己联网"；chat 网关不支持（实测 enable_search 无效，
                     模型只能答 NO_WEB）。丢掉比假装有更好：模型看不到就不会乱调。
                     联网搜索改走 MCP 搜索工具（它们本就是普通 function，天然可用）。
    """
    plan = plan or {}
    t = tl.get("type")
    out = []
    if t == "function":
        fn = tl.get("function") or tl
        out.append(_chat_fn(fn.get("name"), fn.get("description", ""), fn.get("parameters")))
    elif t == "namespace":
        ns = tl.get("name") or ""
        for sub in tl.get("tools") or []:
            sfn = sub.get("function") or sub
            bare = sfn.get("name")
            if not bare:
                continue
            name = plan.get((ns, bare), bare)
            out.append(_chat_fn(name, sfn.get("description", ""),
                                sfn.get("parameters")))
    elif t == "custom":
        nm = tl.get("name")
        if nm:
            # 注意：codex 自带描述说 "FREEFORM, do not wrap in JSON"，与 chat 侧
            # {"input": ...} 包装矛盾 → 已知工具用路由层描述覆盖，未知工具才回退原描述。
            out.append(_chat_fn(nm,
                                CUSTOM_TOOL_DESC.get(nm) or tl.get("description") or "",
                                {"type": "object",
                                 "properties": {"input": {"type": "string",
                                                          "description": CUSTOM_TOOL_ARG_DESC.get(nm, "Freeform tool input.")}},
                                 "required": ["input"]}))
    elif t == "tool_search":
        out.append(_chat_fn("tool_search",
                            tl.get("description", "Search for deferred tools by query."),
                            tl.get("parameters") or {
                                "type": "object",
                                "properties": {"query": {"type": "string"},
                                               "limit": {"type": "number"}},
                                "required": ["query"]}))
    return [x for x in out if x]


def _namespace_plan(tools):
    """决定 namespace 内嵌工具在 chat 侧叫什么名字。

    默认用裸名（与 Codex 原生下发的形态一致）。但 chat 协议**没有 namespace 字段**，
    两个 namespace 里的同名工具（实测存在：`mcp__node_repl.js` 与 `mcp__cua_repl.js`）
    摊平后会重名 —— 上游无法区分，kind 映射也会被后者覆盖。所以只在真冲突时，
    给冲突的那几个加 "<ns>__" 前缀消歧，不冲突的保持裸名不变。
    """
    owners = {}
    for tl in tools or []:
        if tl.get("type") == "namespace":
            ns = tl.get("name") or ""
            for sub in tl.get("tools") or []:
                sfn = sub.get("function") or sub
                nm = sfn.get("name")
                if nm:
                    owners.setdefault(nm, []).append(ns)
    plan = {}
    for nm, nss in owners.items():
        for ns in nss:
            plan[(ns, nm)] = f"{ns}__{nm}" if len(nss) > 1 else nm
    return plan


def _build_tool_kind(tools, plan=None):
    """从 Responses 请求的 tools 建 chat名→kind 映射，供上行还原正确的 item 形态：
      "tool_search"   -> 该名字是 tool_search（上行要发 tool_search_call）
      "<namespace>"   -> 该名字属于某命名空间（上行 function_call 要带 namespace）
      None            -> 普通 function（上行照常 function_call）
    """
    plan = plan if plan is not None else _namespace_plan(tools)
    kind = {}
    for tl in tools or []:
        t = tl.get("type")
        if t == "tool_search":
            kind["tool_search"] = "tool_search"
        elif t == "namespace":
            ns = tl.get("name")
            for sub in tl.get("tools") or []:
                sfn = sub.get("function") or sub
                if sfn.get("name"):
                    kind[plan.get((ns, sfn["name"]), sfn["name"])] = ns
        elif t == "custom":
            if tl.get("name"):
                kind.setdefault(tl["name"], "custom")
        elif t == "function":
            fn = tl.get("function") or tl
            if fn.get("name"):
                kind.setdefault(fn["name"], None)
    return kind


def _build_name_index(tools, plan=None):
    """建 (chat名 -> Codex 侧裸名) 与 (别名 -> chat名) 两张表。

    Codex 的 function_call 要的是**裸 name + 独立 namespace 字段**，而上游模型
    经常自己把命名空间拼进名字（实测有第三方模型会调 `mcp__codex_app__create_thread`，
    即使我们下发的是裸名 `create_thread`）。照原样回传，Codex 认不出这个工具，
    调用被静默丢弃 —— 表现就是"原生能力还是用不了"。所以这里做名字归一化：
    任何已知的带前缀写法都映射回下发给模型的那个 chat 名。
    """
    plan = plan if plan is not None else _namespace_plan(tools)
    canon, alias = {}, {}
    bare_owners = {}
    for (ns, bare), chat_name in plan.items():
        canon[chat_name] = bare
        alias[f"{ns}__{bare}"] = chat_name       # 模型自行加前缀的写法
        bare_owners.setdefault(bare, []).append(chat_name)
    # 消歧后的裸名只有在唯一时才敢映射回去：两个 namespace 都有 `js` 时，
    # 把裸 `js` 猜成其中一个 = 可能在错的 runtime 里执行代码。宁可让它走
    # 告警路径（模型下一轮自行改叫带前缀的全名），也不静默猜。
    for bare, chat_names in bare_owners.items():
        if len(chat_names) == 1 and chat_names[0] != bare:
            alias[bare] = chat_names[0]
    return canon, alias


def _est_tokens(s):
    """保守估算 token 数（宁可高估，确保裁完真实值仍在上限内）。
    CJK 约 1.3 字/token、其余约 3.5 字符/token。"""
    if not s:
        return 0
    cjk = 0
    for ch in s:
        o = ord(ch)
        if (0x4e00 <= o <= 0x9fff or 0x3400 <= o <= 0x4dbf or 0x3040 <= o <= 0x30ff
                or 0xac00 <= o <= 0xd7af or 0xf900 <= o <= 0xfaff):
            cjk += 1
    other = len(s) - cjk
    return int(cjk / 1.3 + other / 3.5)


# 一张原生 image_url part 在字符制估算里记多少"字符"。
# 依据：上游对原生图片按视觉 token 计费（实测 1,759 token/张），而字符制裁剪的换算率是
# ≤1.4 token/字符；要让裁剪器不低估图片消息，需给每张图记 ≥ 1759/1.4 ≈ 1257 字符。
# 取 1300 留一点余量。注意这是**估算权重**，不是真实字符数（真实 base64 有百万字符，
# 但走原生 part 后上游不再按文本计费，记真实值会让裁剪器过度反应、误删正文）。
IMAGE_CHAR_WEIGHT = 1300


def _msg_text(m):
    """取一条 chat 消息的可计长文本（含 tool_calls）。

    多模态消息里的每张 image_url part 额外记 IMAGE_CHAR_WEIGHT 字符：字符制裁剪
    （COMPACT_CHAR_CAP）按"任意内容 ≤1.4 token/字符"换算，图片若不记权重，一条只有
    84 字符占位文本却带 4 张图的消息会被当成 84 字符，裁剪器对真实体积失明。"""
    c = m.get("content")
    nimg = 0
    if isinstance(c, str):
        s = c
    elif isinstance(c, list):
        s = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        nimg = sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
    else:
        s = "" if c is None else json.dumps(c, ensure_ascii=False)
    if m.get("tool_calls"):
        s += json.dumps(m["tool_calls"], ensure_ascii=False)
    return s + "\x00" * (nimg * IMAGE_CHAR_WEIGHT)


def _truncate_msg_content(m, keep_chars):
    """把单条消息的文本内容截到 keep_chars（保首尾、丢中间），应对"单条超大消息"。"""
    c = m.get("content")
    if isinstance(c, str):
        s = c
    elif isinstance(c, list):
        s = "".join(p.get("text", "") for p in c if isinstance(p, dict))
    else:
        return False
    if len(s) <= keep_chars:
        return False
    mark = "\n…[router truncated middle to fit upstream input limit]…\n"
    h = max(0, (keep_chars - len(mark)) // 2)
    m["content"] = (s[:h] + mark + s[-h:]) if h > 0 else s[:max(0, keep_chars)]
    return True


def _trim_messages_to_fit(msgs, cap=None):
    """把请求裁到 COMPACT_CHAR_CAP 字符以内（只用于压缩请求），杜绝"压缩请求自身超限→死锁"。

    为什么按**字符**而非 token 估算：_est_tokens 的 char/token 比率随内容剧烈漂移
    （普通文本高估 ~5x、base64/高密度内容反而低估），曾让 1.45M token 的请求逃过截断。
    字符与 token 之间只有一个可靠不等式——任何内容 ≤ ~1.4 token/char，故把字符上限设成
    "上游 token 上限 ÷ 1.4" 即可保证不超，与内容类型无关、无需估算器。
    cap 由调用方按路由传入（见 _limits_for）；不传则用全局 COMPACT_CHAR_CAP。
    两步：① 丢最旧的非 system 消息（保 system+最近≥2）；② 仍超则截断最大的单条内容（保首尾）。
    裁完重跑 repair_tool_pairs 保工具配对。返回 (msgs, dropped)。只在超限时触发。"""
    cap = COMPACT_CHAR_CAP if cap is None else int(cap)
    sizes = [len(_msg_text(m)) for m in msgs]
    total = sum(sizes)
    n_sys = 0
    while n_sys < len(msgs) and msgs[n_sys].get("role") == "system":
        n_sys += 1
    head_sz = sum(sizes[:n_sys])
    if total > 200000:
        log(f"   [trim-diag] msgs={len(msgs)} n_sys={n_sys} total_chars={total} "
            f"head_chars={head_sz} body_chars={total - head_sz} cap={cap}")
    if total <= cap:
        return msgs, 0
    # ① 丢最旧的 body 消息
    body = list(range(n_sys, len(msgs)))
    body_sz = total - head_sz
    dropped = 0
    while body_sz > cap - head_sz and len(body) > 2:
        body_sz -= sizes[body[0]]
        body.pop(0)
        dropped += 1
    kept = msgs[:n_sys] + [msgs[i] for i in body]
    # ② 仍超 → 截断最大的单条消息（应对"整段历史在一条消息里"）
    cur = sum(len(_msg_text(m)) for m in kept)
    truncated = 0
    guard = 0
    while cur > cap and guard < 200:
        guard += 1
        idx = max(range(len(kept)), key=lambda i: len(_msg_text(kept[i])))
        if len(_msg_text(kept[idx])) < 2000:
            break
        textlen = len(_msg_text(kept[idx]))
        keep_chars = max(1500, int(textlen * cap / max(cur, 1)))
        if not _truncate_msg_content(kept[idx], keep_chars):
            break
        truncated += 1
        cur = sum(len(_msg_text(m)) for m in kept)
    if truncated:
        log(f"   [trim] 仍有单条超大消息，截断了 {truncated} 条内容（保首尾）→ 字符 {cur}")
    if not dropped and not truncated:
        return msgs, 0
    return repair_tool_pairs(kept), dropped


def _image_part(p):
    """Responses 的 input_image part -> chat 的 image_url part；不合法返回 None。"""
    url = p.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    part = {"type": "image_url", "image_url": {"url": url}}
    d = p.get("detail")
    if d in ("low", "high", "auto", "original"):
        part["image_url"]["detail"] = d
    return part


def _split_parts(parts):
    """把 Responses content 列表拆成 (纯文本, [image_url part])。"""
    texts, imgs = [], []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("input_image", "image_url"):
            ip = _image_part(p)
            if ip:
                imgs.append(ip)
        else:
            texts.append(p.get("text", "") or "")
    return "".join(texts), imgs


def _drop_images(m):
    """把一条多模态消息里的图片换成文字占位，返回丢弃张数。"""
    c = m.get("content")
    if not isinstance(c, list):
        return 0
    dropped = 0
    for p in c:
        if isinstance(p, dict) and p.get("type") == "image_url":
            p.clear()
            p.update({"type": "text",
                      "text": "[image dropped by router to stay within the upstream input limit]"})
            dropped += 1
    return dropped


def _cap_images(msgs, cap):
    """只保留最近 cap 张图，更旧的降级为文字占位。返回丢弃张数。"""
    locs = []  # (msg_index, part_index)
    for mi, m in enumerate(msgs):
        c = m.get("content")
        if isinstance(c, list):
            for pi, p in enumerate(c):
                if isinstance(p, dict) and p.get("type") == "image_url":
                    locs.append((mi, pi))
    if len(locs) <= cap:
        return 0
    for mi, pi in locs[:len(locs) - cap]:
        part = msgs[mi]["content"][pi]
        part.clear()
        part.update({"type": "text",
                     "text": "[older image dropped by router; only the most recent "
                             f"{cap} images are kept]"})
    return len(locs) - cap


def _image_bytes(part):
    """一张 image_url part 在请求体里占的字节（data URL 的 base64 长度；远程 URL 忽略）。"""
    url = ((part.get("image_url") or {}).get("url")) or ""
    return len(url) if url.startswith("data:") else 0


# ── 闭环字节护栏：根治 TooLarge ↔ 压缩 死锁 ─────────────────────────────
# 死锁机理（实测于一个含多张大图的长会话）：
#   上游 TooLarge 被翻译成 context_length_exceeded 以唤醒 codex 原生压缩，但**压缩降的
#   是 token，图片字节纹丝不动**（rollout 里 token 仅 57% 却仍报满）。于是：
#   TooLarge → 压缩 → 仍 TooLarge → 再压缩 → 重试耗尽 → codex 给出终态
#   "ran out of room in the model's context window"。而那个假信号让 codex 以为该压文本，
#   方向从一开始就错了。
# 为什么开环预算挡不住：旧实现只统计"图片 base64 合计 ≤ 4.5MB"，但 6MB 硬顶约束的是
#   **整个序列化 body**——tools 定义（实测 42KB）、instructions（21KB）、历史文本、
#   以及 JSON 转义全都不在统计内。预算检查永远看不见真实体积。
# 修法：在 body 完全构建好之后实测字节，与发出去的完全同一种序列化，超了就逐级卸载：
#   ① 丢最旧的图（字节大户，且 token 价值最低）→ ② 截最旧的文本。留 8% 余量吸收
#   序列化差异。这样请求在出门前就一定合法，上游再没机会回 TooLarge。
BODY_BYTE_BUDGET = int(os.environ.get(
    "ROUTER_BODY_BYTE_BUDGET", str(int(UPSTREAM_BODY_BYTE_LIMIT * 0.92))))


def _body_bytes(obj):
    """实测一个请求体的真实线上字节数——与 call_upstream 用完全相同的序列化方式。"""
    return len(json.dumps(obj, ensure_ascii=False).encode())


def _drop_oldest_image(chat):
    """把 messages 里最旧的一张 data URL 图片降级成文字占位。没有可丢的返回 False。"""
    for m in chat.get("messages") or []:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if isinstance(p, dict) and p.get("type") == "image_url" \
                    and _image_bytes(p) > 0:
                p.clear()
                p.update({"type": "text",
                          "text": "[image dropped by router to stay within the "
                                  "upstream request body byte limit]"})
                return True
    return False


def _shrink_oldest_text(chat):
    """把最长的单条文本消息砍到一半（保首尾）。砍不动返回 False。"""
    msgs = chat.get("messages") or []
    if not msgs:
        return False
    idx = max(range(len(msgs)), key=lambda i: len(_msg_text(msgs[i])))
    cur = len(_msg_text(msgs[idx]))
    if cur < 2000:
        return False
    return _truncate_msg_content(msgs[idx], max(1000, cur // 2))


def _enforce_body_byte_limit(chat, budget=None, hard_limit=None):
    """闭环保证 chat 请求体不超上游字节硬顶。就地修改 chat。

    budget/hard_limit 由调用方按路由传入（不同网关硬顶不同）；不传则用全局默认。
    """
    if budget is None:
        budget = BODY_BYTE_BUDGET
    if hard_limit is None:
        hard_limit = UPSTREAM_BODY_BYTE_LIMIT
    n = _body_bytes(chat)
    if n <= budget:
        return
    before, dropped_imgs, truncated, retiered = n, 0, 0, 0
    # ① 先降档：同样一批图全部按更低分辨率/质量重编码。实测降一档换来的容量
    #    远大于丢一张（1024px/q70 单张 81KB，可塞 71 张；丢图只腾出 333KB）。
    #    逐档往下试，直到进预算或档位用尽。
    for tier in range(1, len(IMAGE_TIERS)):
        if n <= budget:
            break
        changed = _retier_images(tier)
        if changed:
            retiered = tier
            n = _body_bytes(chat)
    # ② 降到最低档仍超 → 丢最旧的图（字节大户，token 价值最低）。
    while n > budget and _drop_oldest_image(chat):
        dropped_imgs += 1
        n = _body_bytes(chat)
    # ③ 图片丢光仍超 → 截最长的文本消息（保首尾）。
    while n > budget and _shrink_oldest_text(chat):
        truncated += 1
        n = _body_bytes(chat)
    if dropped_imgs or truncated or retiered:
        # 卸载会改动消息内容，重跑配对修复，避免产生孤儿 tool_call。
        chat["messages"] = repair_tool_pairs(chat["messages"])
        n = _body_bytes(chat)
        _tier_desc = ""
        if retiered:
            _d, _q = IMAGE_TIERS[retiered]
            _tier_desc = f" / 降档至 {_d}px q{_q}"
        log(f"   [body] 实测 {before:,} B 超预算({budget:,} B)，"
            f"丢图 {dropped_imgs} 张 / 截文本 {truncated} 条{_tier_desc} → {n:,} B"
            f"（硬顶 {hard_limit:,}）")
    if n > budget:
        # 理论上到不了：system 消息 + tools 定义就超预算。记下来让日志可查。
        log(f"   [body] 警告：卸载后仍 {n:,} B > 预算 {budget:,} B")


# ── 镜像原生 image_preparation：缩小重编码，而非丢弃 ──────────────────────
# 原生 codex-rs/utils/image/src/lib.rs（已拉源码核对）：data URL 一律解码 →
# resize 到 ≤2048px(high detail) → 重编码后回填 image_url；远程 http(s) URL 不支持
# 则 omit 并附文字占位。OpenAI 官方后端没有请求体字节硬顶，很多兼容网关有，所以"图片字节
# 管理"是桥接层的职责。这里照搬原生的"缩小重编码"思路：单张 base64 超过
# IMAGE_SHRINK_BYTES 时缩到 ≤2048px、转 JPEG q85 回填；缩不动（无可用后端 / 解码失败 /
# 没变小）才落到后续按字节预算丢最旧图。编码器：macOS 用自带 sips（零依赖），
# 其余平台用 Pillow（pip install pillow）——见 SHRINK_BACKEND。
# 实测：488KB PNG → 250KB JPEG，单次 ~30ms；同一图每轮重复出现，故按 sha1 缓存。
IMAGE_SHRINK_BYTES = int(os.environ.get("ROUTER_IMAGE_SHRINK_BYTES", "300000"))
IMAGE_SHRINK_ENABLED = os.environ.get("ROUTER_IMAGE_SHRINK", "1") == "1"
SIPS = shutil.which("sips")
try:
    from PIL import Image as _PILImage
    import io as _io
except Exception:
    _PILImage = None
    _io = None
# 重采样滤镜常量在 Pillow 10 起从 Image.LANCZOS 迁到 Image.Resampling.LANCZOS，
# 旧别名仍在但已弃用；用 getattr 兼容两端，避免某个版本上 AttributeError。
_PIL_LANCZOS = None
if _PILImage is not None:
    _PIL_LANCZOS = getattr(getattr(_PILImage, "Resampling", _PILImage), "LANCZOS",
                           getattr(_PILImage, "LANCZOS", None))


def _pick_backend():
    """选图片编码器。Pillow 优先，sips 次之，都没有则 none（不降档，只靠丢图兜底）。

    优先 Pillow 而不是 macOS 自带的 sips，是实测结论（同一张 488KB 真实截图）：
      档位        sips 输出    Pillow 输出   耗时
      2048/q85    333 KB       135 KB        sips 51ms / Pillow 11ms
      1024/q70     81 KB        51 KB
    同样尺寸同样质量下 Pillow 小 2.5 倍、快 4.5 倍。输出更小 = 字节预算能装下更多
    图 = 更少走到"丢图"那一步，对长会话保真度差别很大。
    sips 保留为后备：Pillow 没装时（纯 stdlib 环境）macOS 仍能降档。
    用 ROUTER_IMAGE_BACKEND=sips|pil|none 可强制指定。
    """
    want = os.environ.get("ROUTER_IMAGE_BACKEND", "").strip().lower()
    avail = {"pil": _PILImage is not None, "sips": bool(SIPS)}
    if want in avail:
        return want if avail[want] else "none"
    if want == "none":
        return "none"
    if avail["pil"]:
        return "pil"
    if avail["sips"]:
        return "sips"
    return "none"


SHRINK_BACKEND = _pick_backend()
_SHRINK_CACHE = {}  # sha1(原始字节) -> 缩小后的 data URL（或原 url 表示放弃缩小）


# 渐进降档表：(最长边 px, JPEG 质量)。第 0 档就是原生 high detail 的 2048/q85。
# 为什么要多档：请求体字节硬顶是部分兼容网关的约束（OpenAI 官方没有），原生 Codex
# 因此从不考虑"图太多要降质"。实测单张 488KB PNG 在各档下的 base64 体积：
#   2048/q85=333KB(塞17张) 1568/q80=172KB(33张) 1280/q75=124KB(46张)
#   1024/q70= 81KB(71张)   768/q60= 42KB(138张)
# 即"降一档"换来的容量远大于"丢一张"，所以降档必须排在丢图之前。
IMAGE_TIERS = ((2048, 85), (1568, 80), (1280, 75), (1024, 70), (768, 60))


def _reencode(raw, dim, qual):
    """把图片字节缩到最长边 ≤dim、以 JPEG 质量 qual 重编码，返回新字节（失败返回 None）。

    两个后端语义一致：sips（macOS 自带，无需装包）/ Pillow（全平台 pip 可装）。
    与原生 codex-rs/utils/image 的 high-detail 行为对齐：按最长边等比缩到 ≤2048px。
    """
    if SHRINK_BACKEND == "sips" and SIPS:
        try:
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "src.img")
                dst = os.path.join(d, "out.jpg")
                with open(src, "wb") as f:
                    f.write(raw)
                r = subprocess.run(
                    [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", str(qual),
                     "-Z", str(dim), src, "--out", dst],
                    capture_output=True, timeout=20)
                if r.returncode == 0 and os.path.exists(dst):
                    with open(dst, "rb") as f:
                        return f.read()
        except Exception:
            return None
        return None
    if SHRINK_BACKEND == "pil" and _PILImage is not None:
        try:
            im = _PILImage.open(_io.BytesIO(raw))
            im.load()
            if im.mode not in ("RGB", "L"):
                # 透明通道/调色板转 JPEG 会丢或报错，统一铺白底转 RGB
                bg = _PILImage.new("RGB", im.size, (255, 255, 255))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg.paste(im, mask=im.split()[-1])
                else:
                    bg.paste(im.convert("RGB"))
                im = bg
            elif im.mode == "L":
                im = im.convert("RGB")
            w, h = im.size
            scale = dim / float(max(w, h))
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                               _PIL_LANCZOS)
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=int(qual), optimize=True)
            return buf.getvalue()
        except Exception:
            return None
    return None


def _shrink_data_url(url, tier=0):
    """把一个 data URL 按档位缩小重编码（镜像原生 resize）。失败/没变小则返回原 url。"""
    if not url.startswith("data:") or SHRINK_BACKEND == "none":
        return url
    try:
        _head, b64 = url.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        return url
    tier = max(0, min(tier, len(IMAGE_TIERS) - 1))
    dim, qual = IMAGE_TIERS[tier]
    key = f"{hashlib.sha1(raw).hexdigest()}:{dim}:{qual}"
    if key in _SHRINK_CACHE:
        return _SHRINK_CACHE[key]
    result = url  # 默认放弃缩小
    # 只有确实变小才回填，避免把已经更优的图越压越大。
    out = _reencode(raw, dim, qual)
    if out and len(out) < len(raw):
        result = "data:image/jpeg;base64," + base64.b64encode(out).decode()
    _SHRINK_CACHE[key] = result
    return result


def _shrink_large_images(msgs):
    """对超过单张字节阈值的图做缩小重编码（镜像原生 resize）。返回成功缩小的张数。"""
    if not IMAGE_SHRINK_ENABLED or SHRINK_BACKEND == "none":
        return 0
    n = 0
    for m in msgs:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if not (isinstance(p, dict) and p.get("type") == "image_url"):
                continue
            iu = p.get("image_url")
            if not isinstance(iu, dict):
                continue
            url = iu.get("url") or ""
            if not url.startswith("data:"):
                continue
            # 登记**所有**本地图的原始 data URL：降档要从原始字节重新编码（不能在
            # JPEG 上再压），且小图虽不触发首轮缩小，但仍需参与后续降档——
            # 否则一堆小图撑爆预算时，降档无从下手只能丢图（信息丢失）。
            _remember_original(p, url)
            if len(url) > IMAGE_SHRINK_BYTES:
                new = _shrink_data_url(url)
                if new != url:
                    iu["url"] = new
                    n += 1
    return n


# ── 原图登记表：降档必须从**原始字节**重编码 ──────────────────────────────
# 在已经压过的 JPEG 上再压一档 = 两次有损压缩叠加，画质掉得比直接低档编码差。
# 所以第 0 档缩小后仍要留着原始 data URL，后续降档一律从它重新编码。
# 用线程本地：ThreadingHTTPServer 每请求一线程，登记表天然不跨请求串味；
# value 里存 part 本身的强引用，避免 part 被 GC 后 id() 复用导致错配。
def _orig_registry():
    reg = getattr(_tls, "orig_imgs", None)
    if reg is None:
        reg = {"tier": 0, "parts": {}}
        _tls.orig_imgs = reg
    return reg


def _reset_orig_registry():
    """每个请求开始时清空登记表。

    必须清：ThreadingHTTPServer 复用线程，不清会让上一个请求的图片 part 一直被
    强引用（内存泄漏），还可能被本请求的降档误改。
    """
    _tls.orig_imgs = {"tier": 0, "parts": {}}


def _remember_original(part, url):
    reg = _orig_registry()
    reg["parts"].setdefault(id(part), (part, url))


def _retier_images(tier):
    """把所有登记过的图按指定档位从原始字节重新编码。返回实际改动的张数。"""
    reg = _orig_registry()
    if tier <= reg["tier"] or not reg["parts"]:
        return 0
    n = 0
    for _pid, (part, orig_url) in reg["parts"].items():
        iu = part.get("image_url")
        if not isinstance(iu, dict):
            continue
        new = _shrink_data_url(orig_url, tier)
        if new != orig_url and new != iu.get("url"):
            iu["url"] = new
            n += 1
    if n:
        reg["tier"] = tier
    return n


def resp_req_to_chat(body, limits=None):
    """把 codex 的 Responses 请求体翻译成 chat/completions 请求体。

    limits 来自 _limits_for(route)：上游字节硬顶、token 硬顶都是**按路由**的，
    不传则退回全局默认（保守值）。
    """
    _lim = limits or {}
    msgs = []
    # namespace 命名方案：下行摊平与历史回放必须用同一张表，名字才一致。
    _replay_plan = _namespace_plan(body.get("tools") or []) if BRIDGE_DEFERRED else {}
    # 来自 tool 输出的图片暂存在这里：chat 的 tool 角色不能带图，要等这一串 tool 回复排完，
    # 再作为一条 user 消息插进去。提前插会把 assistant 的 tool_calls 和它的 tool 回复拆开，
    # 触发 repair_tool_pairs 补占位 / 上游报 tool_call_id 无应答。
    pending_imgs = []

    def flush():
        if not pending_imgs:
            return
        msgs.append({"role": "user", "content": [
            {"type": "text",
             "text": ("[image(s) from the preceding tool output, carried natively: "
                      "the chat protocol cannot put images in a tool message]")}
        ] + pending_imgs[:]})
        pending_imgs.clear()

    sys_text = body.get("instructions") or ""
    if NATIVE_HINT:
        sys_text += NATIVE_HINT_TEXT
    if sys_text:
        msgs.append({"role": "system", "content": sys_text})

    for item in body.get("input") or []:
        if isinstance(item, str):
            flush()
            msgs.append({"role": "user", "content": item})
            continue
        t = item.get("type")
        # 只有 tool 回复能紧跟 tool 回复；其余 item 之前先把暂存图片排出去
        if t not in ("function_call_output", "tool_search_output"):
            flush()
        if t == "message" or (t is None and item.get("role")):
            role = item.get("role") or "user"
            # developer 角色 chat 协议不认，降级成 system
            if role == "developer":
                role = "system"
            parts = item.get("content")
            if isinstance(parts, str):
                msgs.append({"role": role, "content": parts})
            else:
                text, imgs = _split_parts(parts)
                if imgs and role == "user":
                    # 原翻译只拼 text，用户粘贴/拖入的截图被**静默丢弃**（模型完全看不到）。
                    # chat 的 user 角色支持多模态 content，按 text/image 原顺序还原。
                    content = []
                    for pp in parts:
                        if not isinstance(pp, dict):
                            continue
                        if pp.get("type") in ("input_image", "image_url"):
                            ip = _image_part(pp)
                            if ip:
                                content.append(ip)
                        elif pp.get("text"):
                            content.append({"type": "text", "text": pp["text"]})
                    msgs.append({"role": role, "content": content or text})
                else:
                    if imgs:
                        log(f"   [warn] {role} 角色消息含 {len(imgs)} 张图片，chat 协议不支持，已丢弃")
                    msgs.append({"role": role, "content": text})
        elif t == "function_call":
            # 历史里的 namespaced 调用（裸 name + namespace 字段）要还原成**下行时
            # 实际发出去的那个 chat 名**，否则上游收到的工具名与它当时下发的不一致，
            # 轻则模型困惑，重则部分严格校验的网关直接 400。
            _bare = item.get("name", "")
            _ns = item.get("namespace")
            if BRIDGE_DEFERRED and _ns:
                _bare = _replay_plan.get((_ns, _bare), _bare)
            tc = {
                "id": item.get("call_id") or item.get("id") or new_id("call"),
                "type": "function",
                "function": {"name": _bare,
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
            cid = item.get("call_id") or ""
            if isinstance(out, list):
                text, imgs = _split_parts(out)
                if imgs:
                    # tool 角色不能带图：这里留文字占位，图片本体提升到随后的 user 消息。
                    stub = (f"[{len(imgs)} image(s) in this tool output are attached "
                            "natively in the following user message]")
                    body_text = f"{text}\n{stub}".strip() if text.strip() else stub
                    msgs.append({"role": "tool", "tool_call_id": cid, "content": body_text})
                    pending_imgs.extend(imgs)
                    continue
                out = text if text.strip() else json.dumps(out, ensure_ascii=False)
            elif not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
        elif t == "custom_tool_call":
            # 历史里的 freeform 工具调用（apply_patch）：还原成 chat assistant tool_calls，
            # 参数包回 {"input": ...} 与下行桥接形态对称。
            inp = item.get("input", "")
            if not isinstance(inp, str):
                inp = json.dumps(inp, ensure_ascii=False)
            tc = {"id": item.get("call_id") or item.get("id") or new_id("call"),
                  "type": "function",
                  "function": {"name": item.get("name", ""),
                               "arguments": json.dumps({"input": inp}, ensure_ascii=False)}}
            if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
                msgs[-1]["tool_calls"].append(tc)
            else:
                msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
        elif t == "custom_tool_call_output":
            out = item.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool",
                         "tool_call_id": item.get("call_id") or "",
                         "content": out})
        elif t == "tool_search_call":
            a = item.get("arguments")
            if not isinstance(a, str):
                a = json.dumps(a or {}, ensure_ascii=False)
            tc = {"id": item.get("call_id") or item.get("id") or new_id("call"),
                  "type": "function",
                  "function": {"name": "tool_search", "arguments": _ensure_json_args(a)}}
            if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
                msgs[-1]["tool_calls"].append(tc)
            else:
                msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
        elif t == "tool_search_output":
            out = item.get("tools")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool",
                         "tool_call_id": item.get("call_id") or "",
                         "content": out})
        elif t == "reasoning":
            continue  # 上游 chat 协议不接受回传推理内容
        else:
            log(f"   [warn] 未识别的 input item type={t}，已跳过")

    flush()
    # 1) 先镜像原生 resize：把单张过大的图缩小重编码（不丢内容），这步先把字节降下来。
    _shrunk = _shrink_large_images(msgs)
    if _shrunk:
        log(f"   [img] 缩小重编码 {_shrunk} 张大图（≤2048px JPEG q85，镜像原生 image_preparation）")
    # 2) 再按张数上限丢最旧的（历史很长时仍值 ~1.7k token/张）。
    _capped = _cap_images(msgs, ROUTER_MAX_IMAGES)
    if _capped:
        log(f"   [img] 历史含图片超上限，丢弃最旧 {_capped} 张（保留最近 {ROUTER_MAX_IMAGES} 张）")

    chat = {"model": body["model"], "messages": repair_tool_pairs(msgs),
            "stream": bool(body.get("stream")), "max_tokens": MAX_OUTPUT_TOKENS}
    _ni = sum(1 for m in chat["messages"] if isinstance(m.get("content"), list)
              for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")
    if _ni:
        log(f"   [img] 原生携带 {_ni} 张图片（image_url part），"
            f"字符量={sum(len(_msg_text(m)) for m in chat['messages'])}")

    if not (body.get("tools") or []):
        # 只对**压缩请求**截断：保证"压缩请求自身"发得出去、一次成功。
        # 普通轮不截断——让它能溢出，从而触发 codex 的被动压缩（见 do_POST 的错误翻译）。
        chat["messages"], _dropped = _trim_messages_to_fit(
            chat["messages"], (_lim or {}).get("compact_char_cap"))
        if _dropped:
            log(f"   [trim] 压缩请求超字符上限"
                f"({(_lim or {}).get('compact_char_cap', COMPACT_CHAR_CAP)})，"
                f"裁掉最旧 {_dropped} 条消息"
                f"（保留 system+最近）；防压缩请求自身超限→死锁")

    tools = []
    _rt_tools = body.get("tools") or []
    # 映射表是 tools 的纯函数，emitter 侧用同一函数重算即可保持一致，无需共享状态。
    _plan = _namespace_plan(_rt_tools) if BRIDGE_DEFERRED else {}
    for tl in _rt_tools:
        if BRIDGE_DEFERRED:
            tools.extend(_responses_tool_to_chat(tl, _plan))
        elif tl.get("type") == "function":
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

    # 3) 闭环字节护栏：在 body 完全构建好（含 tools/instructions/转义）之后，测**真实
    #    序列化字节**——和发出去的一模一样，不靠图片 base64 开环估算。超预算就逐级卸载
    #    （先丢最旧图，再截最旧文本）。这是根治 TooLarge→压缩→仍 TooLarge 死锁的关键：
    #    之前的开环预算只数图片字节，漏掉 tools 定义、文本、JSON 转义，导致真实 body
    #    冲破 6MB 硬顶，而压缩降的是 token、图片字节不动 → 死循环到"ran out of room"。
    _enforce_body_byte_limit(chat, _lim.get("byte_budget"), _lim.get("byte_limit"))
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
        # 桥接：上行把工具调用还原成 codex 认的 item 形态，需要三样东西
        #   tool_kind  chat名 -> kind（tool_search / 所属 namespace / custom / None）
        #   tool_canon chat名 -> codex 侧裸名（回传 function_call.name 用裸名 + namespace 字段）
        #   tool_alias 别名 -> chat名（模型自行加 "<ns>__" 前缀时纠回来）
        if BRIDGE_DEFERRED:
            _rt = req_body.get("tools") or []
            _p = _namespace_plan(_rt)
            self.tool_kind = _build_tool_kind(_rt, _p)
            self.tool_canon, self.tool_alias = _build_name_index(_rt, _p)
        else:
            self.tool_kind, self.tool_canon, self.tool_alias = {}, {}, {}

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

    def fail_context_window(self):
        """把上游"输入超上下文上限"翻译成 codex 认得的 context_length_exceeded。

        codex 只把 response.failed 里 error.code=="context_length_exceeded" 认作
        ContextWindowExceeded（sse_responses.rs），进而触发**原生自动压缩**；压缩请求自身
        超限时还会 remove_first_item 逐条退让重试（compact.rs）。而网关自己那句
        "Range of input length" 之类的文案不在 codex 的识别范围内——若不翻译，codex 既不
        压缩、又把错误 JSON 当正文追加，上下文只增不减 → 死锁。这里补上翻译，唤醒原生压缩。
        """
        self.close_item()
        self.emit("response.failed", {"response": {
            "id": self.resp_id, "object": "response",
            "created_at": int(time.time()), "status": "failed",
            "model": self.req.get("model"),
            "error": {"code": "context_length_exceeded",
                      "message": ("upstream input exceeds the model context window "
                                  "(upstream rejected the input as too long); "
                                  "compact the context and retry")},
            "usage": None, "metadata": {}, "output": self.output}})

    def _resolve_name(self, name):
        """把上游返回的工具名归一化成 (chat名, codex侧名字)。

        实测第三方模型即使收到裸名也会自己把命名空间拼进去（调
        `mcp__codex_app__create_thread` 而不是 `create_thread`）。Codex 的
        function_call 要的是**裸 name + 独立 namespace 字段**，带前缀的名字它
        认不出 → 工具调用被静默丢弃。这里纠回来。
        """
        if not name:
            return name, name
        chat = self.tool_alias.get(name, name)
        return chat, self.tool_canon.get(chat, chat)

    # --- item 开合 ---
    def open_item(self, kind, name=None, call_id=None, emit_name=None):
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
            nm = name or ""
            # 回传给 Codex 的名字（裸名；namespace 走独立字段）。added/done 两个事件
            # 必须用同一个名字，否则 Codex 侧 item 对不上。
            enm = emit_name or nm
            if BRIDGE_DEFERRED and nm and nm not in self.tool_kind:
                log(f"   [warn] 上游调用了未下发的工具 {nm!r}（可能是模型自行拼名），"
                    f"已按原名回传，Codex 可能不认")
            self.fc = {"name": enm, "args": "",
                       "call_id": call_id or new_id("call"),
                       "kind": self.tool_kind.get(nm)}
            k = self.fc["kind"]
            if k == "tool_search":
                item = {"type": "tool_search_call", "id": self.item_id,
                        "call_id": self.fc["call_id"], "execution": "client",
                        "status": "in_progress", "arguments": {}}
            elif k == "custom":
                item = {"type": "custom_tool_call", "id": self.item_id,
                        "call_id": self.fc["call_id"], "name": enm,
                        "status": "in_progress", "input": ""}
            else:
                item = {"type": "function_call", "id": self.item_id,
                        "name": enm, "arguments": "", "call_id": self.fc["call_id"]}
                if k:
                    item["namespace"] = k
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
            kind = self.fc.get("kind")
            if args.strip():
                try:
                    json.loads(args)
                except Exception:
                    log(f"   [warn] 上游 tool_call '{self.fc['name']}' arguments 截断/非法 JSON"
                        f"（finish_reason={self.finish_reason}，len={len(args)}）；原样下发，"
                        f"回传历史时会由 _ensure_json_args 兜底")
            if kind == "custom":
                # chat 侧参数是 {"input": "<freeform>"}；codex 的 custom_tool_call 要裸字符串
                try:
                    aobj = json.loads(args) if args.strip() else {}
                    inp = aobj.get("input", "") if isinstance(aobj, dict) else args
                except Exception:
                    inp = args
                if not isinstance(inp, str):
                    inp = json.dumps(inp, ensure_ascii=False)
                item = {"type": "custom_tool_call", "id": iid,
                        "call_id": self.fc["call_id"], "name": self.fc["name"],
                        "status": "completed", "input": inp}
            elif kind == "tool_search":
                # tool_search_call 的 arguments 是对象（不是 JSON 字符串），且不发 function_call_arguments.done
                try:
                    aobj = json.loads(args) if args.strip() else {}
                except Exception:
                    aobj = {}
                item = {"type": "tool_search_call", "id": iid,
                        "call_id": self.fc["call_id"], "execution": "client",
                        "status": "completed", "arguments": aobj}
            else:
                self.emit("response.function_call_arguments.done", {
                    "name": self.fc["name"], "arguments": args,
                    "output_index": self.output_index, "item_id": iid})
                item = {"type": "function_call", "id": iid,
                        "name": self.fc["name"], "arguments": args,
                        "call_id": self.fc["call_id"], "status": "completed"}
                if kind:
                    item["namespace"] = kind
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
        # 名字归一化：上游可能回带命名空间前缀的名字，也可能只认裸名。
        chat_name, codex_name = self._resolve_name(name)
        if is_new or self.open_kind != "function_call":
            self.open_item("function_call", name=chat_name, call_id=call_id,
                           emit_name=codex_name)
        if codex_name and not self.fc["name"]:
            self.fc["name"] = codex_name
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
                _dtext = (d.get("content") or "").lower()
                if fr == "error_finish" and OVERLIMIT_SIG in _dtext:
                    # 上游把"输入超限"伪装成 200 SSE 的正常助手文本(error_finish)。若照原样透传，
                    # codex 会当普通消息追加、永不压缩 → 死锁。这里翻译成 context_length_exceeded。
                    # 注意：TooLarge（字节超限）**不在此列**——压缩降 token 救不了字节，
                    # 翻译过去等于骗 codex 压文本，实测会循环到终态 "ran out of room"。
                    log(f"   [ctx] 上游 error_finish 报输入超限"
                        f"（prompt_tokens={(usage or {}).get('prompt_tokens')}），"
                        f"转 context_length_exceeded 触发 codex 原生压缩")
                    em.fail_context_window()
                    return
                if fr == "error_finish" and TOOLARGE_SIG in _dtext:
                    # 流已经开了才报字节超限：无法就地重试（请求体早发出去了）。
                    # 闭环护栏 _enforce_body_byte_limit 已在出门前保证字节合法，
                    # 走到这里说明护栏被绕过（如 passthrough）或上游限额变了 —— 如实报错，
                    # 不伪装成 context_length_exceeded，免得 codex 去压根本不需要压的文本。
                    log("   [body] 上游 error_finish 报请求体字节超限（护栏未拦住），如实报错")
                    em.fail("upstream rejected the request body as too large "
                            "(byte limit, not a token/context limit)",
                            etype="invalid_request_error")
                    return
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
        # ensure_ascii=False：默认 True 会把中文转义成 \uXXXX（每字 6 字节），
        # 实测同样内容 body 字节翻倍。上游接受 UTF-8（已实测中文往返无损），
        # 故按 UTF-8 直接编码，body 立减一半——这是 6MB 硬顶下最便宜的一击。
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json; charset=utf-8",
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
        _tls.rid = uuid.uuid4().hex[:6]
        _reset_orig_registry()  # 清空上个请求的原图登记表（线程会被复用）
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
                            "如需新增，请编辑 ~/.codex/router-routes.json"
                            "（或用 codex-console 的可视化配置台）。")}})
            return
        mode = route["mode"]
        if not os.environ.get(route["key_env"]):
            log(f"-> {model}  缺少 {route['key_env']}")
            self._json(401, {"error": {
                "type": "invalid_request_error",
                "message": (f"模型 {model} 需要环境变量 {route['key_env']}，"
                            "当前未设置。请把它 export 到启动 router 的环境里，"
                            "然后重启 router 进程（重启方式见 README 的"
                            "「常驻运行」：macOS launchd / Linux systemd / Windows 计划任务）。")}})
            return
        _tn = [(t.get("name") or (t.get("function") or {}).get("name")
                or f"<{t.get('type')}>") for t in (body.get("tools") or [])]
        log(f"-> {model}  [{mode}]  tools={len(_tn)}: {','.join(str(x) for x in _tn)}")
        if os.environ.get("ROUTER_DUMP_REQUESTS") == "1":
            try:
                # 路径不硬编码 /tmp（Windows 没有）；默认平台临时目录，可用
                # ROUTER_DUMP_PATH 指定。启动时会把这个路径打进日志，便于定位。
                with open(DUMP_REQUEST_PATH, "w", encoding="utf-8") as _f:
                    json.dump(body, _f, ensure_ascii=False)
            except Exception:
                pass

        # 上游的字节/token 硬顶是**按路由**的属性（不同网关差很多），先算出来，
        # 翻译与错误分类都用同一份。
        limits = _limits_for(route)
        payload = body if mode == "passthrough" else resp_req_to_chat(body, limits)
        path = "/responses" if mode == "passthrough" else "/chat/completions"

        _payload_chars = 0
        if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            _payload_chars = sum(len(_msg_text(m)) for m in payload["messages"])
        up = None
        last = None
        overlimit_large = False
        for attempt in range(1, MAX_RETRY + 1):
            try:
                up = call_upstream(route, path, payload,
                                   self.headers.get("Accept"))
                break
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                last = (e.code, raw)
                _low = raw.lower()
                # TooLarge（请求体字节超硬顶）**不能**翻译成 context_length_exceeded：
                # 那会让 codex 去压缩文本，而字节是图片驱动的，压完照样 TooLarge，
                # 循环到重试耗尽 → 用户看到 "ran out of room"（实测 token 才 57%）。
                # 正确做法是在路由内就地卸载（丢最旧图 / 截最长文本）后重试同一请求。
                if TOOLARGE_SIG in _low and mode != "passthrough":
                    before = _body_bytes(payload)
                    if _drop_oldest_image(payload) or _shrink_oldest_text(payload):
                        payload["messages"] = repair_tool_pairs(payload["messages"])
                        log(f"   [body] 上游 TooLarge：就地卸载 {before:,} B → "
                            f"{_body_bytes(payload):,} B，重试第 {attempt} 次")
                        continue
                    log("   [body] 上游 TooLarge 但已无可卸载内容，放弃")
                    break
                # 真超限(token 维度、请求确实很大)→ 不重试，转 context_length_exceeded
                # 唤醒 codex 原生压缩；小请求的同签名错误 = 上游瞬时抖动 → 走下面的重试。
                if OVERLIMIT_SIG in _low and _payload_chars > limits["overlimit_chars"]:
                    overlimit_large = True
                    break
                _hit = next((t for t in RETRYABLE if t in _low), None)
                if attempt < MAX_RETRY and _hit:
                    log(f"   上游可重试错误（命中 {_hit!r}），第 {attempt}/{MAX_RETRY - 1} 次退避重试")
                    time.sleep(1.5 * attempt)
                    continue
                break
            except Exception as e:
                last = (502, f"{type(e).__name__}: {e}")
                break

        if up is None:
            code, raw = last
            if overlimit_large:
                # 输入真超限：翻译成 codex 认得的 context_length_exceeded（SSE response.failed），
                # 触发其原生自动压缩；压缩请求自身超限时 codex 会 remove_first_item 退让重试。
                log(f"   [ctx] HTTP {code} 输入超限（chars={_payload_chars}），"
                    f"转 context_length_exceeded 触发 codex 原生压缩")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                em = ResponsesEmitter(self.wfile, body)
                em.start()
                em.fail_context_window()
                return
            _hint = ""
            if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
                _m = payload["messages"]
                _hint = (f" msgs={len(_m)}"
                         f" chars={sum(len(_msg_text(x)) for x in _m)}")
            log(f"   失败 HTTP {code}{_hint}: {str(raw)[:200]}")
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
    # 把生效的关键开关打进日志：排查"原生工具不见了/图片没降档"时一眼能看到原因。
    log(f"  bridge_deferred={BRIDGE_DEFERRED} (原生延迟工具/tool_search 桥接)  "
        f"native_hint={NATIVE_HINT}")
    log(f"  image_shrink={IMAGE_SHRINK_ENABLED} backend={SHRINK_BACKEND} "
        f"(none 表示既无 sips 也无 Pillow，图片只按字节预算丢弃、不降档)")
    if os.environ.get("ROUTER_DUMP_REQUESTS") == "1":
        log(f"  dump_requests -> {DUMP_REQUEST_PATH}")
    for m, c in ROUTES.items():
        _l = _limits_for(c)
        _tok = f"{_l['token_limit']:,}tok" if _l["token_limit"] else "unbounded"
        log(f"  {m:24s} {c['mode']:12s} -> {c['base']}  (${c['key_env']})"
            f"  body<={_l['byte_limit']:,}B  input<{_tok}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
