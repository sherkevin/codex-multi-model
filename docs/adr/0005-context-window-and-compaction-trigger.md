# 0005. 上下文窗口与压缩触发点必须等于上游真实上限

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`regen-model-catalog.py`、`~/.codex/custom-model-catalog.json`
- 关联：[0004](0004-byte-limit-vs-token-limit.md)（字节超限不是 token 超限）

## 背景

用户报的现象：界面右上角显示上下文只用了 **23%**，但请求已经被上游 400 拒绝，
接着报 "Codex ran out of room in the model's context window"，会话卡死。

第一反应是"压缩机制没触发"，怀疑 Codex 的 compaction 坏了。实测下来这个判断是错的：
**压缩机制完好，它只是从来没被通知该干活。**

真正的原因有两层：

1. **界面百分比分母是假的。** `custom-model-catalog.json` 里自定义模型的
   `context_window` 被随手填成了内置模型的量级（例如 100 万），而上游实际只收
   30 万。于是上游已经超限时，界面算出来还是 23%。
2. **压缩触发点也是同一个假数字。** Codex 的自动压缩按
   `auto_compact_token_limit` 触发，这个值同样来自目录。窗口填大了，触发点跟着
   跑到上游上限之外，压缩永远不会自己启动。

这两层叠加，结果是 Codex 完全看不见真实体积，只能等上游报错。而报错之后，Codex
本地账本里 `total_token_usage` 会被钉死在 window 值上，之后每次「继续」都在本地
直接判定失败、请求根本发不出去 —— 与 [0004](0004-byte-limit-vs-token-limit.md)
描述的死锁是同一个终态，但**起因完全不同**，修法也不同。

## 决策

`context_window` 一律填**上游真实输入上限**，不许估、不许照抄内置模型的值。
判据是实测：用探针把输入逐步加大，打到上游开始报 input-length 类错误为止。

在此基础上，`auto_compact_token_limit` 必须**显著低于** `context_window`
（生成器默认取 85%）。理由是压缩请求自身要把整段历史发上去做摘要：触发点贴着上限
设，压缩请求自己就先超限了，压不下去 → 死锁。留余量是让压缩有机会成功。

这两个值由 `regen-model-catalog.py` 生成，不允许手改目录文件 —— 手改会在下次
重新生成时被覆盖，而重新生成是每次 Codex 升级后都必须做的动作。

## 影响

- 生成器现在输出 `context_window` / `max_context_window` /
  `effective_context_window_percent` / `auto_compact_token_limit` 四个字段，
  并在结束时把自定义模型的窗口配置打印出来供核对。
- 显式给了 `auto_compact_token_limit` 就用给的值，否则按 `AUTO_COMPACT_RATIO`
  （默认 0.85）自动算。
- 界面百分比从此可信：它和上游真实上限同分母，"显示 23% 却报满"不再出现。
- 与 0004 的分工：**窗口配置**解决"token 维度的压缩何时触发"，
  **路由字节护栏**解决"字节维度的请求怎么瘦身"。两者是独立故障，不能互相替代 ——
  把窗口配小并不能阻止图片把请求体撑爆字节上限。
