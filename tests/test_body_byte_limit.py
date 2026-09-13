#!/usr/bin/env python3
r"""字节护栏的回归测试：计量口径 + TooLarge 卸载循环。纯 stdlib，不打真实上游。

为什么要有它：这两个 bug 都能确定性复现，而且都属于"日志看着在干活、实际没救回来"
的类型，靠肉眼看日志发现不了（见 ADR 0009）：

  1. 计量口径。上游按 ASCII 转义后字节计量，旧实现按发送字节（ensure_ascii=False）
     做预算，中文会话的体积被低估近一倍，预检形同虚设。
  2. 卸载循环。卸载轮数与重试次数共用一个计数器，最后一轮卸完 continue 直接掉出
     循环——那个已经够小的 body 从未发出，请求照样失败。

测试分两部分：
  Part 1 单元   直接导入路由模块，断言口径与限额计算（无网络）。
  Part 2 端到端 起一个"按转义字节限额"的模拟上游 + 真路由进程，验证三种场景。
                不消耗任何真实额度，可重复跑。

用法：
    python3 tests/test_body_byte_limit.py
退出码非 0 = 有断言失败。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTER = os.path.join(REPO, "codex-model-router.py")

FAILURES = []


def check(ok, label, detail=""):
    print(("  OK   " if ok else "  FAIL ") + label + ((" — " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


# ─────────────────────────── Part 1: 单元 ───────────────────────────

def load_router(home):
    """按路径导入路由模块（文件名带连字符，不能 import）。"""
    os.environ["CODEX_HOME"] = home
    spec = importlib.util.spec_from_file_location("router_under_test", ROUTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def part1():
    print("== Part 1: 计量口径与限额（单元）==")
    home = tempfile.mkdtemp(prefix="router-test-home-")
    try:
        r = load_router(home)

        # 1. _body_bytes 必须按 ASCII 转义后计量，与上游校验口径一致。
        ascii_body = {"messages": [{"role": "user", "content": "A" * 1000}]}
        cjk_body = {"messages": [{"role": "user", "content": "汉" * 1000}]}
        a_sent = len(json.dumps(ascii_body, ensure_ascii=False).encode())
        c_sent = len(json.dumps(cjk_body, ensure_ascii=False).encode())
        a_esc, c_esc = r._body_bytes(ascii_body), r._body_bytes(cjk_body)
        check(a_esc == a_sent, "纯 ASCII：转义字节 == 发送字节", f"{a_esc} vs {a_sent}")
        # 中文每字符 UTF-8 3B、转义 6B => 转义口径应约为发送口径的 2 倍
        check(1.9 < c_esc / c_sent < 2.1, "纯中文：转义字节约为发送字节的 2 倍",
              f"ratio={c_esc / c_sent:.2f} (esc={c_esc}, sent={c_sent})")
        check(c_esc > c_sent, "中文体积不再被低估",
              f"旧口径会把 {c_esc:,} B 当成 {c_sent:,} B")

        # 2. 默认硬顶是实测值，不是错误消息里的宣称值。
        check(r.UPSTREAM_BODY_BYTE_LIMIT == 4718592,
              "默认硬顶 = 实测 4,718,592 B（4.5 MiB）",
              f"实际 {r.UPSTREAM_BODY_BYTE_LIMIT}")

        # 3. 预算必须低于硬顶。旧默认 6291456*0.92 = 5,788,139 > 真实硬顶，
        #    等于预检永远放行、每次都靠烧重试轮卸载。
        lim = r._limits_for({"base": "x", "key_env": "Y", "mode": "translate"})
        check(lim["byte_budget"] < lim["byte_limit"], "预算 < 硬顶",
              f"budget={lim['byte_budget']:,} limit={lim['byte_limit']:,}")
        check(lim["byte_budget"] == int(r.UPSTREAM_BODY_BYTE_LIMIT *
                                       r.BODY_BYTE_BUDGET_RATIO),
              "预算 = 硬顶 × BODY_BYTE_BUDGET_RATIO")

        # 4. 路由级覆盖仍生效（不同网关硬顶不同）。
        o = r._limits_for({"base": "x", "key_env": "Y", "mode": "translate",
                           "body_byte_limit": 2000000})
        check(o["byte_limit"] == 2000000 and o["byte_budget"] == 1840000,
              "路由级 body_byte_limit 覆盖生效", str(o))

        # 5. 卸载轮数与重试次数是两个独立计数器。
        check(hasattr(r, "MAX_BODY_OFFLOAD") and r.MAX_BODY_OFFLOAD > r.MAX_RETRY,
              "MAX_BODY_OFFLOAD 独立于 MAX_RETRY 且更大",
              f"offload={getattr(r, 'MAX_BODY_OFFLOAD', None)} retry={r.MAX_RETRY}")

        # 6. 卸载链必须真的把体积压到预算内（含中文）。
        chat = {"messages": [{"role": "system", "content": "You are Codex."}]}
        filler = "这是用于撑大请求体的中文内容，验证转义口径下的卸载链路。"
        for i in range(60):
            chat["messages"].append({"role": "user", "content": f"[{i}] " + filler * 400})
            chat["messages"].append({"role": "assistant", "content": "好的。"})
        before = r._body_bytes(chat)
        budget = 400_000
        r._enforce_body_byte_limit(chat, budget, budget)
        after = r._body_bytes(chat)
        check(before > budget and after <= budget,
              "卸载链把中文 body 压进预算（转义口径）",
              f"{before:,} -> {after:,} (预算 {budget:,})")

        # 7. 卸载后不能留下孤儿 tool_call（上游会 400）。
        chat2 = {"messages": [
            {"role": "system", "content": "You are Codex."},
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "exec_command",
                              "arguments": json.dumps({"cmd": "echo " + "x" * 9000})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "y" * 400000},
            {"role": "user", "content": "thanks " + filler * 200},
        ]}
        r._enforce_body_byte_limit(chat2, 20000, 20000)
        ids_call = [tc["id"] for m in chat2["messages"]
                    for tc in (m.get("tool_calls") or [])]
        ids_out = [m.get("tool_call_id") for m in chat2["messages"]
                   if m.get("role") == "tool"]
        check(all(i in ids_out for i in ids_call),
              "卸载后无孤儿 tool_call", f"calls={ids_call} outputs={ids_out}")
        check(all(i in ids_call for i in ids_out),
              "卸载后无孤儿 tool 输出", f"calls={ids_call} outputs={ids_out}")
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ─────────────── Part 2: 端到端（模拟上游 + 真路由进程）───────────────

class MockUpstream(BaseHTTPRequestHandler):
    """按 **ASCII 转义后**字节限额的上游，复刻 TooLarge 的报文形态。"""
    protocol_version = "HTTP/1.1"
    limit = 1_500_000
    hits = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            obj = json.loads(raw.decode())
        except Exception:
            obj = None
        esc = len(json.dumps(obj, ensure_ascii=True)) if obj is not None else len(raw)
        with self.lock:
            self.hits.append((esc, len(raw)))
        if esc > self.limit:
            # 复刻真实网关：把错误塞进 delta.content，finish_reason=error_finish
            payload = ('{"choices":[{"index":0,"delta":{"role":"assistant",'
                       '"content":"data:{\\"code\\":\\"BadRequest.TooLarge\\",'
                       '\\"message\\":\\"Exceeded limit on max bytes to request body '
                       ': 6291456\\"}\\n\\n"},"finish_reason":"error_finish"}]}')
            self._send(400, payload.encode())
            return
        self._send(200, json.dumps({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": esc // 4000, "completion_tokens": 2,
                      "total_tokens": esc // 4000 + 2}}).encode())

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=25):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


TOOLS = [{"type": "function",
          "function": {"name": "exec_command", "description": "run a shell command",
                       "parameters": {"type": "object",
                                      "properties": {"cmd": {"type": "string"}},
                                      "required": ["cmd"]}}}]


def build_request(model, target_escaped, cjk):
    """构造一个带 tools 的 Responses 请求，escaped 体积约 target_escaped。

    带 tools 是必要的：不带 tools 会被当成"压缩请求"走字符裁剪路径，测不到字节护栏。
    """
    fill = "这是用于撑大请求体的中文内容，验证转义口径下的行为。" if cjk else "A" * 32
    msgs = []
    i = 0
    while True:
        body = {"model": model, "stream": False, "instructions": "You are Codex.",
                "tools": TOOLS,
                "input": msgs + [{"type": "message", "role": "user",
                                  "content": [{"type": "input_text",
                                               "text": "Reply with exactly: OK"}]}]}
        esc = len(json.dumps(body, ensure_ascii=True))
        if esc >= target_escaped or i > 800:
            return body, esc
        chars = max(1000, (target_escaped - esc) // 6)
        msgs.append({"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "[%d] " % i + fill * chars}]})
        msgs.append({"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "好的。" if cjk else "ok"}]})
        i += 1


def part2():
    print("\n== Part 2: 端到端（模拟上游按转义字节限额）==")
    up_port = free_port()
    rt_port = free_port()
    # 路由预算故意**高于**模拟上游真实限额：预检会放行，从而必然触发 TooLarge 卸载循环。
    # 这正是旧代码失败的路径（卸完的 body 从未重发）。
    declared = 2_000_000
    MockUpstream.limit = 1_500_000
    home = tempfile.mkdtemp(prefix="router-e2e-home-")
    routes = {"providers": {"mock": {"base": "http://127.0.0.1:%d/v1" % up_port,
                                     "key_env": "MOCK_TEST_KEY"}},
              "models": {"mock/chat": {"provider": "mock", "mode": "translate",
                                       "body_byte_limit": declared}},
              "prefix_routes": []}
    with open(os.path.join(home, "router-routes.json"), "w", encoding="utf-8") as f:
        json.dump(routes, f)
    log_path = os.path.join(home, "router.log")

    up = ThreadingHTTPServer(("127.0.0.1", up_port), MockUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    env = dict(os.environ, CODEX_HOME=home, CODEX_ROUTER_PORT=str(rt_port),
               MOCK_TEST_KEY="dummy", ROUTER_LOG_FILE=log_path,
               ROUTER_DUMP_PATH=os.path.join(home, "dump.json"))
    proc = subprocess.Popen([sys.executable, ROUTER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_port(rt_port):
            check(False, "路由进程启动", "端口 %d 未就绪" % rt_port)
            return
        check(True, "路由进程启动（预算 %s > 上游真实限额 %s，强制触发卸载）"
              % (format(int(declared * 0.92), ","), format(MockUpstream.limit, ",")))

        scenarios = [
            ("中文 2.4M escaped（旧预算会放行）", 2_400_000, True),
            ("中文 3.5M escaped（需多轮卸载）", 3_500_000, True),
            ("ASCII 2.4M escaped", 2_400_000, False),
        ]
        for label, target, cjk in scenarios:
            body, esc = build_request("mock/chat", target, cjk)
            sent = len(json.dumps(body, ensure_ascii=False).encode())
            with MockUpstream.lock:
                base = len(MockUpstream.hits)
            req = urllib.request.Request(
                "http://127.0.0.1:%d/v1/responses" % rt_port,
                data=json.dumps(body, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json",
                         "Accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    raw = resp.read().decode(errors="replace")
                    code = resp.status
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                code = e.code
            except Exception as e:
                raw = "%s: %s" % (type(e).__name__, e)
                code = 0
            with MockUpstream.lock:
                hits = MockUpstream.hits[base:]
            completed = "response.completed" in raw
            final_ok = bool(hits) and hits[-1][0] <= MockUpstream.limit
            all_ok = all(e_ <= MockUpstream.limit for e_, _ in hits[1:]) or len(hits) <= 1
            print("  -- %s: sent=%s escaped=%s" %
                  (label, format(sent, ","), format(esc, ",")))
            for i, (e_, s_) in enumerate(hits):
                print("     上游第 %d 次收到 escaped=%s sent=%s %s"
                      % (i + 1, format(e_, ","), format(s_, ","),
                         "超限" if e_ > MockUpstream.limit else "限额内"))
            check(code == 200 and completed, "  %s：请求最终成功" % label,
                  "HTTP %s completed=%s" % (code, completed))
            check(final_ok, "  %s：最终发出的 body 在限额内" % label,
                  "final=%s limit=%s" % (format(hits[-1][0], ",") if hits else "-",
                                         format(MockUpstream.limit, ",")))
            check(all_ok, "  %s：除首轮外没有重复超限" % label, str(hits))
            # 旧 bug 的判据：卸载到够小却因循环结束而从未发出 -> 上游一次都没收到
            check(len(hits) >= 1, "  %s：卸载后的 body 确实被重发了" % label,
                  "上游收到 %d 次" % len(hits))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        up.shutdown()
        print("\n  路由日志（最后 12 行）:")
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines()[-12:]:
                    print("    " + line)
        except OSError as e:
            print("    (读不到日志: %s)" % e)
        shutil.rmtree(home, ignore_errors=True)


def main():
    part1()
    part2()
    print()
    if FAILURES:
        print("FAILED (%d):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL BODY-BYTE-LIMIT TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
