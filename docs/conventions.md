# 属性与事件约定

SDK 发给平台的每个属性名和事件名都在这里，和 `src/insightmon/semconv.py` 一一对应：
`tests/test_conventions.py` 检查 `semconv.py` 里的每个名称都出现在本文中，并且本文属性表的第一列只出现
`semconv.py` 里有的名称（或它们的模式），上限与 `semconv.py` 的常量一致。平台怎样解析这些属性（代码位置、
优先级、存储规则）见 [contract.md](contract.md) §8 至 §10。

用法：

- 只用 SDK 的函数时不需要手写这些属性，“谁来写”一列说明由哪个函数写入；
- 用其他语言、其他 SDK 或 OpenTelemetry Collector 直接上报时，按本文设置属性，平台的解析结果相同；
- 名称区分大小写；类型 `bool` 也接受字符串 `"true"` / `"1"` / `"yes"` 和整数 1。

“放在哪里”里的 **span** 指 span 属性，**resource** 指 resource 属性。平台把 resource 属性与 span 属性合并
（span 优先）后再解析，所以除特别说明外两处都可以；SDK 一律写在 span 上。

## 会话

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `gen_ai.conversation.id` | string | 会话键，≤ 200 字符 | `thread(key)`：块内的每个 span | 会话键（第一优先）；超长时被忽略 |
| `session.id` | string | ≤ 200 字符 | SDK 不写（第三方插桩可能写） | 会话键（第二优先） |
| `insightmon.thread_id` | string | ≤ 200 字符 | SDK 不写 | 会话键（第三优先） |
| `insightmon.thread.end` | bool | `true` | `end_thread()`、`turn(end_thread=True)` | 关闭会话，`close_reason = explicit`；不要放在 resource 上 |
| `insightmon.eval_root` | bool | `true` | `mark_eval_root()`、`turn(eval_root=True)` | trace 的评估根（取最早的一个） |
| `insightmon.language` | string | 见[语言](#语言) | `init(language=)`、`thread(language=)` | 声明语言，会话的语言来源记为 `declared` |
| `insightmon.metadata.<key>` | string / bool / int / double | — | `thread(metadata=)` | 会话 `metadata[key]`（按 span 开始时间，后写覆盖） |
| `enduser.id` | string | ≤ 128 字符 | `thread(user_ref=)` | 会话的 `user_ref`（只写一次）；不要放个人信息 |

会话键按上表的优先级在整条 trace 的所有 span 中查找；没有会话键的 trace 不加入任何会话，`origin = experiment`
的 trace（`evaluate()` 与平台回放）也永远不加入会话。

## Agent 与组件

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `gen_ai.agent.name` | string | ≤ 200 字符 | `init(agent_name=)`：每个 span | trace 与会话的 agent 名 |
| `gen_ai.agent.version` | string | ≤ 200 字符 | `init(agent_version=)`：每个 span | agent 版本；回放回执（`trace_attr`）的 agent 版本 |
| `insightmon.component.<type>.<name>.version` | string | ≤ 200 字符 | `init(components=)`、`set_component()`：本地根 span 与轮次 span | 组件版本；按 (type, name) 取第一个，根 span 优先 |
| `insightmon.component.<type>.<name>.content_hash` | string | ≤ 200 字符 | 同上（`content_hash=`） | 组件内容哈希；参与回执冲突检测 |

`<type>` 是第一个 `.` 之前的部分，不能含 `.`；`<name>` 可以含 `.`。type ≤ 64 字符，name ≤ 200 字符，每个 span
最多 100 个组件。项目没有配置组件类型时，平台的默认类型是 `agent`、`prompt`、`skill`、`knowledge`、`tool`、
`model`。

## Resource

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `service.name` | string | — | `init(service_name=)`（默认取 `agent_name`），只在 SDK 新建 provider 时 | 缺少 `gen_ai.agent.name` 时作为 agent 名 |
| `service.version` | string | — | SDK 不写 | 缺少 `gen_ai.agent.version` 时作为 agent 版本：不是 agent 版本就不要设置 |
| `deployment.environment.name` | string | ≤ 100 字符 | `init(environment=)` | 平台的环境 |
| `deployment.environment` | string | ≤ 100 字符 | SDK 不写 | 旧名称，`deployment.environment.name` 缺失时使用 |

## Span 事实

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `gen_ai.operation.name` | string | 见[操作](#操作) | `turn(operation=)`（默认 `invoke_agent`）；工具、模型 span 由你或插桩写 | span 类型；其他值视为空 |
| `gen_ai.request.model` | string | ≤ 200 字符 | 你或插桩 | span 的模型 |
| `gen_ai.response.model` | string | ≤ 200 字符 | 你或插桩 | span 的模型（优先） |
| `gen_ai.response.id` | string | ≤ 200 字符 | `set_response_id()` | 评估日志按它关联 span；必须是 span 自己的属性 |
| `gen_ai.tool.name` | string | ≤ 200 字符 | 你或插桩 | 工具名 |
| `gen_ai.tool.call.id` | string | ≤ 200 字符 | 你或插桩 | 工具调用 id |
| `gen_ai.usage.input_tokens` | int | ≥ 0 | 你或插桩 | 输入 token |
| `gen_ai.usage.output_tokens` | int | ≥ 0 | 你或插桩 | 输出 token |
| `insightmon.cost` | double | 有限数 | 你 | 成本 |
| `error.type` | string | ≤ 200 字符 | `turn()` 块内抛出异常时（异常类名）；`evaluate()` 的超时与任务错误 | 错误类型；缺失时取 `exception` 事件的 `exception.type` |

## 内容

这些属性带着消息内容，**只在 `init(capture_content=True)` 时发送**；关闭时 SDK 的导出器在发送前从 span 和 span
事件上删除它们（见 README 的“内容采集与隐私”）。

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `gen_ai.system_instructions` | string（JSON） | 单个字段 ≤ 256 KiB | `record_io(system_instructions=)` | span 的 `input.system` |
| `gen_ai.input.messages` | string（JSON 消息数组） | 单个字段 ≤ 256 KiB | `record_io(input_messages=)` | span 的 `input.messages` |
| `gen_ai.output.messages` | string（JSON 消息数组） | 单个字段 ≤ 256 KiB | `record_io(output_messages=)` | span 的 `output.messages` |
| `gen_ai.tool.call.arguments` | string（JSON） | 单个字段 ≤ 256 KiB | 你或插桩（工具 span） | span 的 `input.arguments` |
| `gen_ai.tool.call.result` | string（JSON） | 单个字段 ≤ 256 KiB | 你或插桩（工具 span） | span 的 `output.result` |
| `input.value` | string | — | 第三方插桩 | 平台不映射到 span 输入输出；`metadata_only` 下丢弃，否则入库脱敏（旧平台原样保存） |
| `output.value` | string | — | 第三方插桩 | 同上 |
| `llm.prompt_template.variables` | string | — | 第三方插桩（提示模板变量） | 同上 |
| `traceloop.entity.input` | string | — | 第三方插桩（工作流 / 任务 span） | 同上 |
| `traceloop.entity.output` | string | — | 第三方插桩（工作流 / 任务 span） | 同上 |
| `langfuse.observation.input` | string | — | 第三方插桩 | 同上 |
| `langfuse.observation.output` | string | — | 第三方插桩 | 同上 |
| `langfuse.trace.input` | string | — | 第三方插桩 | 同上 |
| `langfuse.trace.output` | string | — | 第三方插桩 | 同上 |
| `mlflow.spanInputs` | string | — | 第三方插桩 | 同上 |
| `mlflow.spanOutputs` | string | — | 第三方插桩 | 同上 |
| `mlflow.chat.messages` | string | — | 第三方插桩 | 同上 |
| `all_messages_events` | string | — | 第三方插桩 | 同上 |
| `ai.prompt` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.prompt.messages` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.response.text` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.response.object` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.response.toolCalls` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.toolCall.args` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.toolCall.result` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.value` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.values` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.embedding` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `ai.embeddings` | string | — | 第三方插桩（Vercel AI SDK） | 同上 |
| `pydantic_ai.all_messages` | string | — | 第三方插桩（pydantic-ai） | 同上 |
| `final_result` | string | — | 第三方插桩（pydantic-ai） | 同上 |
| `tool_arguments` | string | — | 第三方插桩（pydantic-ai） | 同上 |
| `tool_response` | string | — | 第三方插桩（pydantic-ai） | 同上 |
| `request_data` | string | — | 第三方插桩（Logfire 的 OpenAI 集成） | 同上 |
| `response_data` | string | — | 第三方插桩（Logfire 的 OpenAI 集成） | 同上 |
| `gcp.vertex.agent.llm_request` | string | — | 第三方插桩（Google ADK） | 同上 |
| `gcp.vertex.agent.llm_response` | string | — | 第三方插桩（Google ADK） | 同上 |
| `gcp.vertex.agent.tool_call_args` | string | — | 第三方插桩（Google ADK） | 同上 |
| `gcp.vertex.agent.tool_response` | string | — | 第三方插桩（Google ADK） | 同上 |
| `gen_ai.prompt*` | any | — | 第三方插桩（旧的 GenAI 约定） | 同上 |
| `gen_ai.completion*` | any | — | 第三方插桩（旧的 GenAI 约定） | 同上 |
| `llm.input_messages*` | any | — | 第三方插桩 | 同上 |
| `llm.output_messages*` | any | — | 第三方插桩 | 同上 |
| `llm.prompts*` | any | — | 第三方插桩 | 同上 |
| `retrieval.documents.*` | any | — | 第三方插桩（检索到的文档） | 同上 |
| `reranker.input_documents.*` | any | — | 第三方插桩（重排前的文档） | 同上 |
| `reranker.output_documents.*` | any | — | 第三方插桩（重排后的文档） | 同上 |
| `embedding.embeddings.*` | any | — | 第三方插桩（被向量化的文本） | 同上 |

- 消息的形状是 `{role, content}` 或 OTel 形状 `{role, parts: [{type: "text", content}]}`；`record_io()` 写 OTel 形状。
- 标准内容属性也可以放在 span 事件 `gen_ai.client.inference.operation.details` 的属性上。
- 第三方的内容类属性平台不映射到 span 的输入输出。表里的第三方属性就是平台的内容属性清单：平台在项目的
  `metadata_only` 模式下丢弃它们，其他模式下按入库脱敏规则处理（旧版平台原样保存）。SDK 在
  `capture_content=False` 时在发送前自己删除它们（`*` 表示以此开头的所有属性），不依赖平台。这张表不可能列全：
  请检查你用的插桩写了哪些带内容的属性，表里没有的用 `init(content_attributes=[...])` 补上（属性名，或以 `*`
  结尾的前缀；平台不认识它们，`metadata_only` 也不会丢弃）。
- 整个 span 的属性超过 64 KiB 时，平台从最大的键开始丢弃。

## 日志事件

日志的事件名写在 LogRecord 的 `event_name` 字段，同时写在 `event.name` 属性上（兼容只认属性的转发链路）。

| 事件 | 谁来发 | 平台用途 |
|---|---|---|
| `gen_ai.evaluation.result` | `log_evaluation()`（总是转发） | 写成 `source = import` 的分数 |
| `gen_ai.system.message` | 你或插桩（只在开启内容采集时转发） | span 的 `input.system` |
| `gen_ai.user.message` | 同上 | span 的 `input.messages` |
| `gen_ai.assistant.message` | 同上 | span 的 `input.messages` |
| `gen_ai.tool.message` | 同上 | span 的 `input.messages` |
| `gen_ai.choice` | 同上 | span 的 `output.messages` |
| `gen_ai.client.inference.operation.details` | 同上（也可以是 span 事件） | 与 span 的内容属性相同 |

其他事件名平台一律忽略；SDK 的日志处理器也不转发它们，应用自己的普通日志不会发到平台。

`gen_ai.evaluation.result` 的属性：

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `event.name` | string | `gen_ai.evaluation.result` | `log_evaluation()` | 事件名（`event_name` 字段缺失时） |
| `gen_ai.evaluation.name` | string | ≤ 64 字符，已发布的评分定义名 | `log_evaluation(name)` | 评分定义；平台转小写、`[^a-z0-9_]` 换成 `_`、去掉首尾 `_`；未知名称拒绝（`score_config_not_found`） |
| `gen_ai.evaluation.score.value` | double | bool 发送 `1.0` / `0.0` | `log_evaluation(value=)` | numeric 的值；boolean 没有 label 时必须是 0 或 1 |
| `gen_ai.evaluation.score.label` | string | 布尔标签见[布尔标签](#布尔标签) | `log_evaluation(label=)` | boolean（label 优先于 value）、categorical、text 的值 |
| `gen_ai.evaluation.explanation` | string | ≤ 4000 字符 | `log_evaluation(explanation=)` | 分数说明 |
| `error.type` | string | ≤ 200 字符 | `log_evaluation(error_type=)` | 评估器失败：写成 unscored 分数 |
| `gen_ai.response.id` | string | ≤ 200 字符 | `log_evaluation(response_id=)` | 没有 span 上下文时按它关联 span |

## 平台写入的属性

| 属性 | 平台用途 |
|---|---|
| `insightmon.redaction` | 入库脱敏改动了内容时的记录 |
| `insightmon.dropped_attributes` | 因超过 64 KiB 被丢弃的属性名 |
| `insightmon.content_events` | 已合并到 span 内容中的日志内容事件 |

这些属性由平台写入，SDK 从不设置，你也不要设置。

## 实验属性

`evaluate()` 在每次运行的根 span（`evaluate`）上写下面三个属性。实验条目与 trace 的对应由条目上报的 trace id
决定；平台另外读 `insightmon.experiment_id`：trace 的 span 带着一个仍在运行、由导入这些 span 的同一个 key 创建的
客户端实验的 id 时，这个 trace 在条目上报之前就记为实验流量（不会先成为生产 trace，见 contract.md §8）。旧版平台
只把它作为普通属性保存。另外两个属性始终是普通 span 属性（便于筛选）。

| 属性 | 类型 | 取值 / 上限 | 谁来写 | 平台用途 |
|---|---|---|---|---|
| `insightmon.experiment_id` | string | 平台的实验 id | `evaluate()` / `aevaluate()` | 把 trace 记为该实验的流量（旧版平台：普通属性） |
| `insightmon.example_key` | string | ≤ 100 字符 | 同上 | 普通属性 |
| `insightmon.trial_index` | int | 从 0 开始 | 同上 | 普通属性 |

## 取值

### 操作

`gen_ai.operation.name` 的取值：`chat`、`text_completion`、`generate_content`、`embeddings`、`execute_tool`、
`invoke_agent`、`create_agent`。

### 语言

`insightmon.language` 的取值：`zh`、`en`、`ja`、`mixed`、`und`（平台去掉空格并转小写后检查，其他值忽略）。

### 布尔标签

boolean 评分定义接受的 label（不区分大小写）：真 `true`、`pass`、`passed`、`yes`、`1`；
假 `false`、`fail`、`failed`、`no`、`0`。
