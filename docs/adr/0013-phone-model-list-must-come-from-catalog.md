# 0013. 手机的模型清单必须来自 `model/list`，不是 App 内置那份 GPT 列表

- 状态：Accepted
- 日期：2026-09-14
- 相关文件：`tools/happy-patch.py`（补丁 3 `model-catalog-to-phone`）、
  `tools/happy-codex-shim/codex`（`readThreadModel` / `warnIfModelChangeIgnored`）、
  `tools/happy-codex-shim/test-model-meta.mjs`、
  `tools/happy-codex-shim/fixtures/webapp-model-picker.mjs`
- 关联：[0001](0001-router-mode-criterion.md)（router 只认自己路由表里的模型）、
  [0003](0003-phone-remote-control-via-happy.md)（手机远控走 Happy）、
  [0008](0008-mirror-mode-for-live-session-takeover.md)（镜像模式的能力边界）

## 背景

手机配对成功、对话能看能发之后，用户提出：手机的模型选择器里只有 GPT 系列，
看不到我们自己接的 `qwen3.8-max` / `bailian/glm-5.2` / `moonshot/kimi-k3` /
`MiniMax-M3`，问会不会有影响。

两条独立的事实凑成了这个故障：

1. **手机端读的是会话 metadata，不是 `model/list`。** 线上 App bundle 里
   `getAvailableModels('codex', metadata)` 走的是
   `mapMetadataOptions(metadata.models)`，把 `{code, value, description}`
   映射成 `{key, name, description}`；`metadata.models` 为空就
   `return includeConfiguredModel(...)` —— 回落到 App **内置硬编码**的 GPT 清单。
   「当前模型」角标读 `metadata.currentModelCode`，经 `resolveCurrentOption`
   在选项里找；找不到就显示成 `default`。
2. **Happy 的 codex 后端从不填这两个字段。** 上游只有 ACP 后端填
   `models` / `currentModelCode`。于是 codex 会话的 metadata 里根本没有这两个键，
   手机永远显示内置清单——不报错、不告警。

而 codex app-server **有**官方 `model/list`，且 happy 建连时已经声明了
`capabilities: { experimentalApi: true }`，所以它本来就能调到。实测返回 9 个
模型（`~/.codex/custom-model-catalog.json` 的 10 个里，`visibility: hide` 的
`codex-auto-review` 被 app-server 自己滤掉），第一个就是 `qwen3.8-max`，
带 `displayName: "Qwen3.8-Max (ideaLAB)"`。清单一直都在，只是没人把它报上去。

## 影响评估（用户问的「会不会有影响」）

不是纯粹的显示问题，但**没有破坏任何数据或配置**：

- **不会写坏配置**：Happy 全程不调 `settings/update`，也不写
  `config.toml` / `custom-model-catalog.json`。手机端选模型只影响它自己发的
  `turn/start` 参数，改不了电脑上任何文件。
- **会真的失败**：在 Happy 自己新开的会话里，手机选一个 GPT 模型 → `turn/start`
  带 `model: gpt-5.5` → 打到 router → **401**。因为 router 的
  `gpt-` 前缀路由指向 `api.openai.com`，而它的 `key_env` 是 `OPENAI_API_KEY`，
  启动 router 的环境里没有这个变量。实测：
  `POST /v1/responses {"model":"gpt-5.5"}` → `401 模型 gpt-5.5 需要环境变量
  OPENAI_API_KEY，当前未设置`。也就是说手机上那份 GPT 清单里**一个都点不动**。
- **镜像模式下是静默无效**：`thread/queue/add`（镜像注入走的就是它）会
  **忽略** `model` 字段——实测带 `model` 与带一个乱编字段的响应完全一样
  （`ThreadQueueAddParams` 的 schema 只有 `threadId` / `clientUserMessageId` /
  `input`，多余键被丢掉而不报错）。用户在手机上换了模型，什么都没发生，
  还以为换成功了。这比报错更糟。
- **选 `default` 是安全的**：App 对 codex flavor 会自动补一个 `default model`
  项，它映射成 `model: null`，即用 `config.toml` 里配的模型（当前是
  `qwen3.8-max`）。所以「什么都不动、用默认」这条路一直是好的。

还有一条反直觉的：官方**确实**有能改模型的 RPC——`thread/settings/update`，
schema 里 `model` 字段的说明就是 "Override the model for subsequent turns"。
但它在镜像模式下够不着，实测返回 `thread not found`：这个 RPC 要求线程已在
**这条连接**里加载，而加载只能靠 `thread/resume`，resume 恰恰被 writer lock
挡住（`already has an active writer`）。所以「第二个连接能改模型」这条路是关的，
不是我们没试。三条边界的可复现探针：

```bash
node tools/happy-codex-shim/probe-mirror-model-boundary.mjs
# A thread/start: 01a09e66-... model=qwen3.8-max
# A 持有 writer lock: true
# [1] B thread/resume: error -32600: ... already has an active writer
# [2] B thread/settings/update: error -32600: thread not found: ...
# [3] queue/add 带 model vs 带乱编字段是否同形: 完全相同 -> model 被静默丢弃
```

它在临时 `CODEX_HOME` 里自建线程跑，不碰生产会话，也不抢任何 writer lock。

## 决策

**模型清单的唯一来源是 `model/list`，即 `custom-model-catalog.json`。**
不维护第二份清单，不在补丁里硬编码任何模型名。以后加模型只改 catalog，
手机自动跟着变。

**上报形状对齐 App 的解析器，不对齐我们的直觉。** 必须是
`{code, value, description}`：`code` 是给 `turn/start` 用的模型 id，
`value` 是显示名（取 `displayName`），`description` 是副标题。写成
`key`/`name` 一样是静默失败。

**当前模型只在它真的在清单里时才写。** 否则宁可留空让 App 显示 `default`，
也不要写一个解析不出来的值——那会让角标显示成一个点不动的假名字。

**`model/list` 失败时静默降级，绝不让会话起不来。** 这条上报是增强功能，
不是主链路：拿不到清单就保持上游行为（手机显示内置清单），
所有异常吞掉只记 debug 日志。

**镜像模式下如实告知「换不动」，但只在真的换了时才说。**
垫片去 `state_5.sqlite` 读线程真实的 `model` / `reasoning_effort`，与手机请求里的
值比对，不一致才弹一条说明。为什么必须比对：因为 config-model-default 补丁
会让 happy **恒定**带上 `model`，无条件提醒就是每条消息都弹一次「你选的模型
不会生效」——而用户根本没在换模型，纯噪音。

**真实模型只能从 sqlite 读，不能从 `thread/read` 读。** 实测 `thread/read`
返回的 thread 对象里**没有** `model` 键（只有 `modelProvider`），`model` 只
出现在 `thread/resume` 的结果顶层——而那条恰恰是真 codex 拒绝、由垫片代为
应答的。顺带一个坑：sqlite 要用 `-cmd '.timeout 5000'`，不能用
`PRAGMA busy_timeout=5000`，后者会把返回值 `5000` 当成一行结果回显，
解析出来就是 `model=5000`（实测踩过）。

## 实现

`tools/happy-patch.py` 的补丁 3 `model-catalog-to-phone`，注入
`syncCodexModelMetadata({client, session, currentModel})`，在三个时机各调一次：
建连后（只报清单，不报当前模型）、resume 后（顺带接住上游丢掉的
`resumedThread.model`）、新开会话后。结果缓存在
`globalThis.__codexModelCatalog`，避免每条消息都发一次 RPC。
kill-switch：`HAPPY_CODEX_MODEL_META=0`。

垫片侧新增 `readThreadModel(threadId)`（只读连接 + `.timeout`）与
`warnIfModelChangeIgnored(threadId, params)`。

## 验证

`node tools/happy-codex-shim/test-model-meta.mjs`（40 项断言，两份 bundle 都验；
`--live` 改用真 `model/list` 而非 fixture）：

- 从**打过补丁的 bundle** 里抠出真实的 `syncCodexModelMetadata` 跑，
  不是手抄一份逻辑——手抄的那份永远是对的，没有意义。
- 用从**线上 App bundle 逐字抄来**的 `getAvailableModels` /
  `resolveCurrentOption` / `mapMetadataOptions`
  （`fixtures/webapp-model-picker.mjs`）去解析上一步产物，断言自定义模型可见、
  显示名取 `displayName`、当前角标命中、隐藏模型不泄漏、失败时静默降级。
- 一组**对照断言**：`.orig-bak` 里确实没有这个函数、上游确实从不填
  `metadata.models`、缺 `models` 时 App 确实回落。没有对照组，一片 PASS
  说明不了任何事。

真实运行证据：`~/.happy/logs/2026-09-14-11-13-26-pid-21810.log` 里
`[CODEX MODEL META] reported 9 models, current=unreported`（建连那次）与
`current=qwen3.8-max`（resume 那次）；relay 接受了 metadata 版本 1→2→3，
没有版本冲突。三个镜像刷新后各自日志里都能看到这两条。

## 影响

- 手机模型选择器从「9 个点不动的 GPT」变成「default + 9 个真实可用项」，
  自定义模型排在最前。
- 当前模型角标显示真实值（`Qwen3.8-Max (ideaLAB)`），不再恒为 `default`。
- 镜像模式下换模型会得到一条明确说明而不是静默无效。要在镜像的会话里换模型，
  仍然只能去那个桌面/CLI 窗口里换——这是
  [ADR 0008](0008-mirror-mode-for-live-session-takeover.md) 的能力边界，
  不是本条能解决的。
- Happy 升级后需要重跑 `--check` 确认三个补丁还在，并重跑
  `test-model-meta.mjs`；App 端若改了 metadata 解析，
  `fixtures/webapp-model-picker.mjs` 需要重新核对（这是抄来的，不是同步来的）。
