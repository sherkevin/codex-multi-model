# 0004. 字节超限与 token 超限是两类故障，不得混用同一个信号

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`codex-model-router.py`
- 关联：[0001](0001-router-mode-criterion.md)（为什么必须走 chat 桥）

## 背景

某个 OpenAI 兼容聚合网关（下称「网关 A」，与 [0001](0001-router-mode-criterion.md)
同一个）对 `/chat/completions` 有两个**互相独立**的输入上限：

- token 维度：`Range of input length should be [1, 983616]`
- 字节维度：`BadRequest.TooLarge: Exceeded limit on max bytes to request body : 6291456`

OpenAI 官方后端只有前者，没有 6MB 请求体硬顶。所以字节维度是网关 A 特有的约束，
原生 Codex 从不需要考虑它，桥接层必须自己处理。

此前的做法把两者当成同一类故障，统一翻译成 Codex 认得的
`context_length_exceeded`，指望唤醒原生压缩自愈。

## 事故：某长会话的 "ran out of room" 死锁

实测时间线（北京时间）：

| 时刻 | 事件 |
|---|---|
| 09:03:50 | 请求发出，上游回 TooLarge（字节超限） |
| 09:03:57 | 路由翻译成 `context_length_exceeded`，Codex 压缩文本后重试 → 仍 TooLarge |
| 09:08:42 | 用户发「继续」→ 立刻报满，未发出任何请求 |
| 09:22:12 | 再次「继续」→ 同样报满 |

死锁机理：压缩降的是 **token**，而超限的是**字节**，且字节由图片主导——压缩根本
碰不到它。于是 TooLarge → 压缩 → 仍 TooLarge → 再压缩 → 重试耗尽 → Codex 给出
终态消息 "Codex ran out of room in the model's context window"。

关键反证：rollout 里失败瞬间 `last_token_usage = 163400`，窗口 285000，
**token 只用了 57%**，却报上下文满。而且更早的记录里
`total_token_usage.total_tokens = 30209708` 远超窗口值——Codex 发现 total 超过
window 后会把它钉死在 window 上，于是账本永远显示 100% 满。一旦进入这个状态，
每次「继续」都在本地被直接判定失败，请求根本发不出去。

## 决策

两类超限分开处理，不再共用一个信号：

1. **token 超限**（`OVERLIMIT_SIG`）→ 翻译成 `context_length_exceeded`，
   唤醒 Codex 原生压缩。这是正确的，因为压缩确实能降 token。
2. **字节超限**（`TOOLARGE_SIG`）→ **绝不**翻译成 `context_length_exceeded`。
   路由内就地卸载字节后重试同一请求；流已打开无法重试时才如实报错。
3. 字节护栏必须是**闭环**的：在请求体完全构建好之后实测序列化字节
   （`_enforce_body_byte_limit`），而不是按「图片 base64 合计」开环估算。

第 3 点是本次事故的直接技术原因。旧的开环预算只统计图片 base64 ≤ 4.5MB，
但 6MB 硬顶约束的是整个 body——tools 定义（实测 42KB）、instructions（21KB）、
历史文本、JSON 转义开销全都不在统计范围内，预算检查看不见真实体积。

## 卸载顺序：降档优先于丢图

实测单张 488KB PNG（1824×1358，Codex 截图的真实规格）在各档下的 base64 体积，
以及预算内能容纳的张数：

| 档位 | 单张 base64 | 可容纳张数 | 相对 token |
|---|---|---|---|
| 2048px q85 | 333 KB | 17 | 101% |
| 1568px q80 | 172 KB | 33 | 75% |
| 1280px q75 | 124 KB | 46 | 49% |
| 1024px q70 | 81 KB | 71 | 32% |
| 768px q60 | 42 KB | 138 | 18% |

降一档换来的容量远大于丢一张，所以顺序必须是：

1. 缩小重编码到 2048px/q85（镜像原生 `image_preparation` 的 high detail）
2. 仍超预算 → 逐档降低分辨率/质量，**一张都不丢**
3. 降到最低档仍超 → 才丢最旧的图
4. 图丢光仍超 → 截最长的文本消息（保首尾）

降档必须从**原始字节**重新编码（`_remember_original` 登记表），不能在已压过的
JPEG 上再压——那会叠加两次有损压缩，画质比直接低档编码更差。

降档不损可读性已实测：把用户那张含小字的截图降到 1568px 和 1024px，模型仍能
逐字读出 `Range of input length should be [1, 983616]` 中的 `983616`。
768px 曾两次返回空，复测确认是上游 curl 超时（exit 28），非画质问题。

## 顺带修掉的纯粹浪费

`call_upstream` 原用 `json.dumps(payload).encode()`，默认 `ensure_ascii=True`
会把中文转义成 `\uXXXX`，每字 6 字节。实测同样内容：

```
ensure_ascii=True  : 720,068 B
ensure_ascii=False : 360,068 B   （正好一半）
```

用户会话大量中文，这等于把所有中文内容翻倍计入 6MB 硬顶。改为
`ensure_ascii=False` + `Content-Type: application/json; charset=utf-8`，
上游实测接受且中文往返无损。这是最便宜的一击。

## 影响

- 预算留 8% 余量（`BODY_BYTE_BUDGET = 硬顶 × 0.92`）吸收序列化差异。
- 卸载会改动消息内容，卸载后必须重跑 `repair_tool_pairs`，否则产生孤儿
  `tool_call` 被上游 400 拒绝。
- 端到端实测（真实 `input_image` schema）：14 张图 9.1MB → shrink 后 4.68MB
  全保留；22 张 14.3MB → 降档至 1568px → 3.80MB 全保留，HTTP 200 正常收尾，
  无 TooLarge、无 context_length_exceeded。
- **已卡死的线程不会自动恢复**。Codex 本地账本里 `total_token_usage` 已被钉死在
  window 值上，每次「继续」都在本地被判定失败、请求发不出去。必须开新线程，
  或清空该线程历史。这一点是 Codex 客户端行为，路由侧无法修复。
