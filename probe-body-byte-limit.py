#!/usr/bin/env python3
r"""实测上游 /chat/completions 的请求体字节硬顶与计量口径，给出可直接抄进配置的数。

为什么需要它（踩过的坑，见 ADR 0009）：

上游报错 "Exceeded limit on max bytes to request body : 6291456" 里那个数字不可信。
实测同一网关的真实阈值是 4,718,592 B（4.5 MiB），比它宣称的小 25%；而且它量的不是
我们发出的 UTF-8 字节，而是 ASCII 转义后的字节——中文每字符 UTF-8 占 3 B，转义成
\uXXXX 占 6 B。两者叠加的后果：按错误消息和发送字节去设预算，会把可用空间高估到
真实值的 1.2 倍以上，中文会话尤其严重。

本脚本做两组实验，各自独立给结论：

  A. 口径判定  用纯 ASCII 与纯中文两组填充分别二分。若两组阈值显著不同（中文那组
               约为一半），则上游按转义字节计量；若相同，则按发送字节计量。
  B. 阈值      在 A 的口径下二分收敛，输出硬顶与推荐预算（硬顶 x 0.92）。

判据是"上游是否回字节超限类错误"，所以签名要能匹配你的网关（可用 --sig 追加）。

用法：

    export EXAMPLE_GATEWAY_API_KEY=...
    python3 probe-body-byte-limit.py BASE MODEL --key-env EXAMPLE_GATEWAY_API_KEY
    python3 probe-body-byte-limit.py BASE MODEL --key-env K --declared 6291456

注意：会真实调用上游、消耗额度。默认两组各约 8 轮二分，共约 20 次请求。
AK 只从环境变量读，不落盘、不打印。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 上游"请求体字节超限"的签名。不同网关措辞不同，用 --sig 追加。
DEFAULT_SIGS = ("max bytes to request body", "toolarge", "body size",
                "payload too large", "413")
# 上游"输入 token 超限"的签名。撞到这个说明请求在字节维度已经过了，阈值判定必须把它
# 和字节超限区分开，否则会把 token 上限误当成字节上限。
TOKEN_SIGS = ("range of input length", "maximum context length",
              "context_length_exceeded", "too many tokens")


def send(base, key, model, msgs, timeout):
    """发一次非流式 chat/completions。返回 (ok, sent_bytes, err_text, token_limited)。"""
    body = {"model": model, "stream": False, "max_tokens": 4,
            "messages": msgs + [{"role": "user", "content": "Reply with exactly: OK"}]}
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=data,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json; charset=utf-8",
                 "Accept": "*/*"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, len(data), "", False
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        low = txt.lower()
        return False, len(data), txt[:300], any(s in low for s in TOKEN_SIGS)
    except Exception as e:  # 超时/连接类：不是限额结论，如实返回便于排查
        return False, len(data), type(e).__name__ + ": " + str(e), False


def build(target_sent, fill):
    """构造序列化后 sent 字节约为 target_sent 的 messages（fill 决定字符密度）。

    逐条追加、每步按剩余差额反算字符数，所以两三步就逼近目标（早期实现固定加 1KB，
    几百步也到不了 4MB，二分区间直接失真）。
    """
    msgs = [{"role": "system", "content": "You are a terse assistant."}]
    while True:
        body = {"model": "m", "stream": False, "max_tokens": 4,
                "messages": msgs + [{"role": "user",
                                     "content": "Reply with exactly: OK"}]}
        n = len(json.dumps(body, ensure_ascii=False).encode())
        if n >= target_sent or len(msgs) > 4000:
            return msgs
        need = target_sent - n
        per_char = len(fill.encode())
        chars = max(64, (need - 300) // per_char)
        msgs.append({"role": "assistant", "content": "noted"})
        msgs.append({"role": "user", "content": fill * chars})


def bisect(base, key, model, fill, lo, hi, sigs, timeout, rounds, label):
    """二分收敛 sent 阈值。返回 (lo, hi)；无法判定时返回 (None, None)。"""
    print("\n-- %s 填充（每字符 UTF-8 %d B）" % (label, len(fill.encode())), flush=True)
    for _ in range(rounds):
        mid = (lo + hi) // 2
        msgs = build(mid, fill)
        ok, sent, err, tok = send(base, key, model, msgs, timeout)
        low = err.lower()
        byte_limited = (not ok) and any(s in low for s in sigs) and not tok
        if tok:
            print("   sent=%10d  命中 token 上限（非字节）—— 说明字节维度已过，"
                  "请用 --hi 调低，或先解决 token 上限" % sent, flush=True)
            hi = sent
        elif ok or not byte_limited:
            if not ok:
                print("   sent=%10d  非限额错误: %s" % (sent, err[:200]))
                print("   => 无法据此判定字节阈值。用 --sig 补上你的网关措辞后重试。")
                return None, None
            lo = sent
            print("   sent=%10d  通过" % sent, flush=True)
        else:
            hi = sent
            print("   sent=%10d  字节超限" % sent, flush=True)
        if hi - lo < 120_000:
            break
        time.sleep(0.2)
    return lo, hi


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", help="上游 base，写到 /v1 为止")
    ap.add_argument("model", help="模型 slug")
    ap.add_argument("--key-env", default="OPENAI_API_KEY", help="存 AK 的环境变量名")
    ap.add_argument("--lo", type=int, default=1_500_000, help="二分下界（sent 字节）")
    ap.add_argument("--hi", type=int, default=8_000_000, help="二分上界（sent 字节）")
    ap.add_argument("--rounds", type=int, default=8, help="每组二分轮数")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--sig", action="append", default=[],
                    help="追加字节超限签名（可多次）")
    ap.add_argument("--declared", type=int, default=0,
                    help="上游错误消息里宣称的硬顶，用于对照（可选）")
    a = ap.parse_args()

    key = os.environ.get(a.key_env)
    if not key:
        print("环境变量 %s 未设置。" % a.key_env, file=sys.stderr)
        return 2
    sigs = tuple(x.lower() for x in list(DEFAULT_SIGS) + a.sig)

    print("上游   %s  模型 %s" % (a.base, a.model))
    print("AK     $%s（已设置，len=%d）" % (a.key_env, len(key)))
    print("签名   %s" % (sigs,))

    # 先确认两端行为，避免二分在错误区间里空转
    print("\n== 预检 ==", flush=True)
    ok_lo, _, _, tok_lo = send(a.base, key, a.model, build(a.lo, "A"), a.timeout)
    ok_hi, _, err_hi, tok_hi = send(a.base, key, a.model, build(a.hi, "A"), a.timeout)
    print("   lo=%d -> ok=%s %s" % (a.lo, ok_lo, "(token 限)" if tok_lo else ""))
    print("   hi=%d -> ok=%s %s %s" % (a.hi, ok_hi,
                                       "(token 限)" if tok_hi else "", err_hi[:140]))
    if not ok_lo:
        print("\n下界 %d 就失败了，请用 --lo 调低再试。" % a.lo)
        return 1
    if ok_hi and not tok_hi:
        print("\n上界 %d 仍通过：该上游可能没有字节硬顶，或用 --hi 调高再试。" % a.hi)
        print("没有硬顶是好事——配置里可以不设 body_byte_limit。")
        return 0

    print("\n== A. 计量口径判定（ASCII 对比 CJK）==", flush=True)
    a_lo, a_hi = bisect(a.base, key, a.model, "A", a.lo, a.hi,
                        sigs, a.timeout, a.rounds, "ASCII")
    if a_lo is None:
        return 1
    c_lo, c_hi = bisect(a.base, key, a.model, "汉",
                        max(a.lo // 3, 200_000), max(a.hi // 2, 1_500_000),
                        sigs, a.timeout, a.rounds, "CJK")
    if c_lo is None:
        return 1

    ascii_th = (a_lo + a_hi) // 2
    cjk_th = (c_lo + c_hi) // 2
    ratio = cjk_th / max(1, ascii_th)
    print("\n   ASCII 阈值(sent) 约 %d" % ascii_th)
    print("   CJK   阈值(sent) 约 %d   比值 %.3f" % (cjk_th, ratio))
    if ratio < 0.75:
        measure = "escaped"
        print("   => 上游按 ASCII 转义后字节计量（CJK 阈值约为 ASCII 的一半）。")
        print("      中文每字符 UTF-8 3B / 转义 6B，所以预算必须按转义口径算。")
    else:
        measure = "sent"
        print("   => 上游按发送字节计量，预算与内容语种无关。")

    # ASCII 填充下两种口径同值，所以 ascii_th 同时就是转义口径下的阈值。
    lim = ascii_th
    budget = int(lim * 0.92)
    print("\n== B. 推荐配置 ==")
    print("   计量口径        : %s（%s）"
          % (measure, "转义后字节" if measure == "escaped" else "发送字节"))
    print("   实测硬顶        : %d B" % lim)
    if a.declared:
        d = (lim - a.declared) / a.declared * 100
        print("   上游宣称        : %d B  -> 实测偏差 %+.1f%%" % (a.declared, d))
        if abs(d) > 5:
            print("   注意：宣称值与实测差得明显，以实测为准（见 ADR 0009）")
    print("   推荐预算(x0.92) : %d B" % budget)
    print("\n   router-routes.json:")
    print('     "%s": {"provider": "...", "mode": "translate", "body_byte_limit": %d}'
          % (a.model, lim))
    if measure == "sent":
        print("\n   注意：你的上游按发送字节计量，而 router 默认按转义字节计量（更保守）。")
        print("         保守方向是安全的，只是中文会话会更早开始卸载图片。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
