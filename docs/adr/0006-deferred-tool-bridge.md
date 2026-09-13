# 0006. 延迟工具桥接：translate 模式必须还原 Codex 的全部原生工具形态

- 状态：Accepted
- 日期：2026-09-13
- 相关文件：`codex-model-router.py`（`_responses_tool_to_chat` / `_build_name_index` / `ResponsesEmitter`）
- 关联：[0001](0001-router-mode-criterion.md)（为什么必须走 chat 桥）

## 背景

用户最初的核心抱怨：接了自定义 API 之后，Codex 的原生能力没了 —— 不能创建其他聊天
窗口，不能弹 user input 的 QA 选择。

根因不在鉴权或端点，而在**协议翻译只认一种工具形态**。Codex 用
`#[serde(tag="type")]` 序列化工具，实际有五种：

| 类型 | 承载什么 |
|---|---|
| `function` | 普通工具（`exec_command` 等） |
| `namespace` | `create_thread`、`spawn_agent`、各 MCP 命名空间 |
| `custom` | `apply_patch`（freeform，参数不是 JSON） |
| `tool_search` | 加载延迟工具的元工具 |
| `web_search` | 上游侧联网搜索 |

而早先的 translate 路径写着 `if tl.get("type") != "function": continue`，
后四种被整个丢弃。拿真实 Codex 请求实测：15 个工具只下发了 10 个，
`create_thread` / `apply_patch` / `tool_search` 全部消失。模型不是不肯用原生能力，
是**根本没收到过**。

更隐蔽的是第二层：Codex 把大量原生工具设为 deferred（不在即时工具表里，要模型先调
`tool_search` 把它们捞出来）。`gpt-5-codex` 被专门调过、会主动这么做；第三方模型没见过
这个协议，于是"看不到 = 以为没有"，转而写 shell 脚本硬凑 —— 表现出来就像原生能力坏了。

## 决策

桥接层做**完整双向翻译**，而不是把工具挑一部分下发。默认开启（`ROUTER_BRIDGE_DEFERRED=1`）。

下行（Responses → chat）：

- `namespace` 摊平成扁平 chat function
- `custom` 包成 `function(input: string)`
- `tool_search` 合成一个可调用的 function（参数 `query`/`limit`）
- `web_search` 丢弃（见下）

上行（chat → Responses）：每个调用还原成 Codex 认得的 item 形态 ——
`tool_search_call` 的 arguments 是**对象**、`custom_tool_call` 的 input 是**裸字符串**、
`function_call` 带**独立的 `namespace` 字段**。三者任一处形态不对，Codex 都会静默丢弃
该调用。

同时注入一段 `NATIVE_HINT`（默认开），只点破"延迟工具要用 tool_search 加载"这一个高杠杆
行为，让第三方模型也知道该协议存在。

## 三个明确的边界

**命名空间重名不能猜。** chat 协议没有 namespace 字段，摊平后两个命名空间里的同名工具
（真实存在：`mcp__node_repl.js` 与 `mcp__cua_repl.js`）会撞车。撞车的那几个加
`<ns>__` 前缀消歧；不撞车的保持裸名（与 Codex 原生下发形态一致）。**裸名在歧义时
不做任何猜测映射** —— 猜错意味着在错误的 runtime 里执行代码，代价远高于一次明确的失败。

**模型自行拼的名字要纠回来。** 实测第三方模型即使收到裸名 `create_thread`，也会回传
`mcp__codex_app__create_thread`。Codex 认不出带前缀的名字，调用被静默丢弃，表现就是
"桥接做了但原生能力还是用不了"。所以 `_build_name_index` 维护别名表做归一化，
且下行的 `added` 与 `done` 两个事件必须用同一个名字。

**`web_search` 不桥接。** 它不是 function，语义是"上游服务端自己联网"，chat 网关普遍
不提供。实测 `enable_search` / `search_options` 参数被上游接受但无效，模型只能答
"我无法联网"。假装桥接只会让模型去调一个必然失败的工具，所以直接丢弃 —— 模型看不到
就不会乱调。联网搜索改走 MCP 搜索工具，那些本就是普通 function，天然可用。
另注：`config.toml` 里 `web_search_mode = "disabled"` **不会**移除该工具声明，
只把 `external_web_access` 置 false（已实测）。

## 验证

- 真实抓包的 Codex 请求：15 个 Responses 工具 → 32 个 chat 工具，kind 映射全对。
- 真实上游往返：正确产出 `tool_search_call`、带 namespace 的 `create_thread`、
  freeform `apply_patch`，`response.completed` 完整。
- 回放一致性：历史里所有 tool_call 的名字都必须在下发工具表内，否则上游会 400
  或模型看到不存在的工具；重名的两个 `js` 回放后仍可区分。
- 关掉开关（`=0`）退回"只转发普通 function"的旧行为，作为逃生通道保留。
