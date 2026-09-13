# 0001. Router mode 判据：上游能否吃下 Codex 的真实 Responses 请求

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`codex-model-router.py`、`~/.codex/router-routes.json`、`~/.codex/probe-responses-support.py`

## 背景

Codex 桌面端只允许配置一个 provider。要在 `/model` 面板里同时用多家模型，
必须在本机起一个回环代理冒充那个唯一 provider，由它按模型名分发到真实上游。

代理对每个模型有两种转发模式：

- `passthrough`：请求体零改动，原样转给上游的 `/responses`。
- `translate`：把 Responses 协议翻译成上游的 `/chat/completions`，再把响应翻译回来。

问题是如何判定某个模型该用哪种模式。此前靠人工猜测并写死在配置里，
结果 `_DEFAULT_ROUTING` 把某个模型标成了 `passthrough`——而它走原生
`/responses` 必然失败（网关直接回内部错误码，根本没进模型）。日常没事，只因
`router-routes.json` 优先于内置默认；一旦那个文件丢失或损坏，回退路径就是坏的。

## 决策

判据只有一条：**上游能不能吃下 Codex 真实发出的 Responses 请求**。能则
`passthrough`，不能则 `translate`。不允许按厂商、按模型名或按文档推测。

判定必须实测，用 `probe-responses-support.py` 跑四级递进：

1. 端点可用性（最小文本请求能否 200）
2. 流式完整性（`stream=true` 是否发 `response.completed`，Codex 靠它收尾）
3. 工具调用（能否返回 `function_call`）
4. **工具输出回传**（能否吃下 `function_call_output`）

第 4 级是决定性的：agentic 会话每一轮都要把工具输出回传给模型，回传不了
就等于不可用，前三级全过也没意义。

探针另设 `inconclusive` 三态：额度耗尽、鉴权失败（401/403）等临时问题
不算"协议不支持"，不得据此改 mode，解决后重跑。

## 2026-09-13 实测结论

对某个 OpenAI 兼容聚合网关（下称「网关 A」）实测，得到两类失败模式：

| 失败模式 | 判定 | 表现 |
|---|---|---|
| 没配 responses 通道 | translate | 前三级就失败，`/responses` 直接报网关侧错误码（如 `PRE-006` 加一句网关内部的 urlFormat 为空），根本没进模型 |
| responses 通道有缺陷 | translate | 前三级全过，**只在第 4 级失败** |
| 额度耗尽 | inconclusive | 没测到协议层，不能据此判定 mode（充值后结论会变） |

第二类是关键反例，也是最容易误判的一类：它在原生 `/responses` 上流式、工具调用
都正常，**唯独回传 `function_call_output` 时报**
`Value error, tool must be one of user,assistant,system,function`。
经核对 Codex 实际只发送 `user`/`assistant`/`developer` 三种 role，
那个 `role=tool` 是网关内部把工具输出转成 chat 消息时自己产出的，
又被它自己的校验器拒掉。所以这是上游缺陷，不是我们的请求形状不对——
**光看前三级会误判成 passthrough**，这正是探针必须做满四级的原因。

## 为什么不能干脆去掉代理

两个独立约束叠加，使代理成为必需：

1. codex-cli 0.142.5 起 `wire_api = "chat"` 已被官方删除，二进制硬编码报错
   `wire_api = "chat" is no longer supported`，`WireApi` 枚举只剩 `responses`。
   Codex 自己无法对接 chat 端点。
2. 某些网关无法在 `/responses` 上回传工具输出（见上表）。

于是"只在某网关的 chat 端点提供"的模型，必须经代理翻译才能用。
这也是我们保留 translate 桥的理由——桥接后的模型效果更好，值得为它养这座桥。

## 影响

- 能原生 passthrough 的模型，代理不参与任何语义，Codex 升级不受影响。
  这是首选路径；上游一旦补齐 responses 支持，重跑探针即可切过去。
- 必须 translate 的模型，桥接层要承担上游特有限制的适配。已知的两例：
  网关的请求体字节硬顶（OpenAI 官方后端没有此限制，故原生 Codex 不做图片
  字节管理），以及网关把"输入超限"伪装成 200 SSE 的正常助手文本。
  这些属于桥接层职责，不是重做原生逻辑，但每加一条都要能指回上游的具体约束。
- 新增模型或 Codex/上游升级后，跑一次探针再定 mode，不要手工猜。
