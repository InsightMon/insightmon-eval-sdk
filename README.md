# insightmon-eval-sdk

InsightMon AI Eval 的 Python SDK。它做三件事：

- **上报会话**：把 Agent 的每一轮对话作为 OpenTelemetry trace 发给平台，按平台的属性约定打上会话键、
  agent 版本、组件版本和语言，平台据此组装会话（thread）、做评估和回归分析；
- **上报分数**：评估结果（OTLP 日志事件）以及用户反馈、业务结果（REST）；
- **客户端实验**：`insightmon.evaluate()` 在你的机器上跑平台测评集里的样例，平台运行 gating 评估器并给出结论。

它比同类 SDK 更轻：运行时只依赖 OpenTelemetry（API、SDK、OTLP/HTTP 导出器）和 httpx，不依赖任何 Web 框架或
模型厂商的库；会话数据就是普通的 OTel span，已有的 OpenTelemetry 配置和第三方插桩可以直接接入。

平台本体：[InsightMon/insightmon-ai-eval](https://github.com/InsightMon/insightmon-ai-eval)。
English: [README.en.md](README.en.md)。

> **状态**：0.1.0。公共 API 全部实现，并已对真实平台实例做过端到端验证（`scripts/integration.sh`，见
> [开发](#开发)）。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

目录：[安装](#安装) · [5 分钟快速开始](#5-分钟快速开始) · [会话与轮次](#会话与轮次) ·
[接入已有的 OpenTelemetry](#接入已有的-opentelemetry) · [内容采集与隐私](#内容采集与隐私) ·
[评估日志](#评估日志) · [分数](#分数) · [evaluate()](#evaluate) · [回放回执](#回放回执) ·
[配置](#配置) · [故障排查](#故障排查) · [兼容性](#兼容性) · [开发](#开发)

## 安装

SDK 目前从 git 安装（Python ≥ 3.10）：

```bash
pip install "insightmon-eval-sdk @ git+https://github.com/InsightMon/insightmon-eval-sdk.git@main"
# 或者
uv add "insightmon-eval-sdk @ git+https://github.com/InsightMon/insightmon-eval-sdk.git@main"
```

生产环境请把 `@main` 换成某个发布 tag 或 commit，保证每次安装的是同一份代码。导入名是 `insightmon`。

## 5 分钟快速开始

1. **建 API Key**：在平台上打开项目，侧栏「API Key」（需要项目管理员），新建一个带 `trace_ingest` 权限的 Key。
   密钥（`imk_` 开头）只显示一次，不要写进代码或提交到仓库。
2. **设置环境变量**：

   ```bash
   export INSIGHTMON_BASE_URL=https://eval.example.com   # 平台根地址
   export INSIGHTMON_API_KEY=imk_...
   ```

3. **上报一段两轮的对话**（即 [examples/quickstart.py](examples/quickstart.py)，`python examples/quickstart.py`）：

```python
import uuid

import insightmon


def forecast(city: str) -> str:
    """A tool call: its own span inside the turn."""
    with insightmon.get_tracer().start_as_current_span(
        "forecast",
        attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "forecast"},
    ):
        return f"Light rain is expected in {city} tomorrow."


def reply(question: str, *, last: bool = False) -> str:
    """One turn of the assistant (one trace on the platform); the last one closes the thread."""
    with insightmon.turn(end_thread=last):
        if "rain" in question.lower():
            return forecast("Oslo")
        return "You're welcome!"


def main() -> None:
    insightmon.init(
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=[{"type": "prompt", "name": "system", "version": "p7"}],
        language="en",
    )
    chat_id = f"chat-{uuid.uuid4().hex[:8]}"  # your own conversation id
    with insightmon.thread(chat_id, user_ref="user-42"):
        print(reply("Will it rain in Oslo tomorrow?"))
        print(reply("Thanks!", last=True))
    insightmon.shutdown()  # export what is left before the process exits
    print(f"reported thread {chat_id}")


if __name__ == "__main__":
    main()
```

4. **在平台上查看**：侧栏「会话与 Trace」，按会话键（`chat-...`）查找。你会看到一个会话、两个 trace（每轮一个，
   第一轮里有 `forecast` 工具 span），agent 版本 1.4.0、组件 `prompt/system = p7`、声明语言 `en`；最后一轮带了
   `end_thread`，会话已关闭（关闭原因 `explicit`），平台随即开始评估。

没设置这两个环境变量时 SDK 只在本地打属性、不发送任何数据，可以先空跑。

## 会话与轮次

| 函数 | 作用 |
|---|---|
| `thread(key, *, language=None, metadata=None, user_ref=None)` | 块内开始的每个 span 都带上会话键 `gen_ai.conversation.id`；同一会话的各轮用同一个 key（例如你的聊天会话 id，1–200 字符） |
| `turn(name="turn", *, eval_root=False, end_thread=False, operation="invoke_agent", attributes=None)` | 一轮对话：一个 `invoke_agent` span；当前没有 span 时它就是 trace 的根，一轮 = 一个 trace |
| `end_thread(span=None)` | 在（当前）span 上标记 `insightmon.thread.end`：平台关闭会话（`close_reason = explicit`），之后不再重开 |
| `mark_eval_root(span=None)` | 标记评估根 span（默认是 trace 的根 span） |
| `set_component(type, name, *, version=None, content_hash=None)` | 声明正在使用的组件版本（作用范围见下文） |
| `set_response_id(response_id, span=None)` | 写 `gen_ai.response.id`，评估日志可以按它关联 span |
| `current_thread()` | 当前的会话作用域（没有时为 None） |
| `get_tracer()` | SDK 的 tracer，用来创建工具调用、模型调用等子 span |

```python
import insightmon

with insightmon.thread("chat-42", language="en", metadata={"channel": "web"}, user_ref="user-42"):
    with insightmon.turn() as span:  # one turn = one trace
        insightmon.set_component("prompt", "system", version="p8")  # this thread only
        insightmon.set_response_id("resp-0001")  # evaluation logs can find this span by it
        insightmon.mark_eval_root(span)
    with insightmon.turn():
        insightmon.end_thread()  # the same as turn(end_thread=True)
```

要点：

- **不在 `thread()` 里的 span 没有会话键**，平台不会为它们建会话（SDK 从不凭空生成会话键）。
- `language` 取 `zh` / `en` / `ja` / `mixed` / `und`；会话上的声明优先于 `init(language=...)`，平台把会话的语言
  来源记为 `declared`。`metadata` 写入会话 metadata；`user_ref` 是不透明的用户引用（≤ 128 字符），**不要放个人信息**。
- `set_component()` 的作用范围：在 `evaluate()` 的任务里只影响这一次运行；在 `thread()` 里只影响这个会话；
  其他地方影响整个进程。组件只写在本地根 span 和轮次 span 上。
- 嵌套 `thread()` 时最内层生效。`evaluate()` 内的 span 永远不带会话键（实验流量不进入会话）。
- 已关闭的会话不会重开，之后到达的同 key trace 记为晚到；没有 `end_thread` 的会话由平台按空闲时间关闭。

### asyncio、线程池与多进程

会话作用域和当前 span 一样放在 `contextvars` 里，跟着上下文走。

**asyncio**：任务创建时复制上下文，`asyncio.gather()` / `create_task()` 自动留在会话里。

```python
import asyncio

import insightmon


async def lookup(city: str) -> str:
    with insightmon.get_tracer().start_as_current_span("lookup"):
        await asyncio.sleep(0)  # e.g. an async HTTP call
        return f"forecast for {city}"


async def answer() -> list[str]:
    with insightmon.thread("chat-43"), insightmon.turn():
        return list(await asyncio.gather(lookup("Oslo"), lookup("Lima")))


asyncio.run(answer())
```

**线程池**：新线程和 `ThreadPoolExecutor` 的工作线程从**空上下文**开始，在里面创建的 span 会变成没有会话键的
独立 trace。每次提交时复制一份调用方的上下文，用 `ctx.run` 执行：`contextvars.copy_context()` +
`executor.submit(ctx.run, fn, arg)`。一个上下文同一时间只能被一个线程进入，所以**每次提交各复制一份**。

```python
import contextvars
from concurrent.futures import ThreadPoolExecutor

import insightmon


def lookup(city: str) -> str:
    with insightmon.get_tracer().start_as_current_span("lookup"):
        return f"forecast for {city}"


with (
    insightmon.thread("chat-44"),
    insightmon.turn(),
    ThreadPoolExecutor(max_workers=4) as executor,
):
    futures = []
    for city in ("Oslo", "Lima"):
        ctx = contextvars.copy_context()  # one copy per submitted call
        futures.append(executor.submit(ctx.run, lookup, city))
    forecasts = [future.result() for future in futures]
```

**多进程（fork）**：gunicorn、uWSGI、`multiprocessing` 的子进程，请**在 fork 之后、在每个 worker 里**调用
`init()`，例如 gunicorn 的 `post_fork` 钩子。OpenTelemetry 的批处理器会在子进程里重启导出线程，SDK 也会在子进程
里重建自己的全部锁、忘掉父进程暂存的 span，所以在 fork 之前初始化（例如 gunicorn 的 `preload_app`）通常也能工作；
但每个 worker 自己初始化最可预测，主进程不调用 `init()` 时也只能这样。两处只选一处：已经初始化后再调用 `init()` 只记 warning 并返回原来的句柄。`spawn`
方式启动的子进程不继承任何东西，同样要各自 `init()`。

```python
# gunicorn.conf.py
import insightmon


def post_fork(server, worker):
    insightmon.init(agent_name="weather-assistant", agent_version="1.4.0")
```

## 接入已有的 OpenTelemetry

`init()` 按以下顺序选择 `TracerProvider`：

1. 参数 `tracer_provider=`；
2. 否则，全局 provider 已经是 OpenTelemetry SDK 的 `TracerProvider`（例如 `opentelemetry-instrument` 或你自己的
   初始化代码设置的）时，**接入它**：只追加 SDK 的处理器和导出器，不替换、也不关闭它；
3. 否则新建一个 provider（`service.name` 取 `service_name` 或 `agent_name`），并设为全局 provider。

接入后，第三方插桩（HTTP 客户端、Web 框架、模型 SDK 的插桩）产生的 span 同样会被打上会话键、agent 版本和语言。
SDK 自己的 HTTP 请求（OTLP 导出、REST 调用）在关闭插桩的上下文中发送，不会被 HTTP 客户端插桩记成 agent 的 span。

provider 要用默认的（同步）span 处理器：`ConcurrentMultiSpanProcessor` 在线程池里调用 `on_start`，SDK 在那里
读不到调用方的会话、轮次和实验作用域，什么都打不上，`evaluate()` 的 span 也无法暂存（`init()` 遇到它时记一条
warning）。

```python
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

import insightmon

provider = TracerProvider(resource=Resource.create({"service.name": "weather-api"}))
# ... your own span processors and exporters stay as they are ...
insightmon.init(tracer_provider=provider, agent_name="weather-assistant", agent_version="1.4.0")
```

**经 OpenTelemetry Collector 转发**：不给 `init()` 传 base URL 和 Key 时，SDK 只打属性、不导出；由你自己的导出器
发给 Collector，Collector 再转发到平台的 OTLP/HTTP 端点。把你的导出处理器交给 `init(export_processor=...)`，
**不要**自己 `provider.add_span_processor(...)`：这样 `evaluate()` 运行的 span 在这条路径上同样先暂存、条目上报后
才导出；否则它们先到平台，成为生产 trace（`evaluate()` 发现 span 无法暂存时记一条 warning）。

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import insightmon

# No base URL / key: the SDK stamps attributes, your exporter sends them to the collector.
insightmon.init(
    agent_name="weather-assistant",
    agent_version="1.4.0",
    export_processor=BatchSpanProcessor(OTLPSpanExporter()),  # not provider.add_span_processor()
)
```

```yaml
exporters:
  otlphttp/insightmon:
    traces_endpoint: https://eval.example.com/otlp/v1/traces
    logs_endpoint: https://eval.example.com/otlp/v1/logs
    headers:
      Authorization: "Bearer ${env:INSIGHTMON_API_KEY}"
```

注意：

- 只有 SDK 自己的导出器会在 `capture_content=False` 时删除内容属性；走自己的导出器时，内容由你的管道负责
  （见[内容采集与隐私](#内容采集与隐私)）。
- 不要在 resource 上设置 `service.version`，除非它就是 agent 版本：缺少 `gen_ai.agent.version` 时平台会拿它当
  agent 版本。SDK 在每个 span 上写 `gen_ai.agent.version`。
- 不调用 `init()` 也可以只用处理器：
  `provider.add_span_processor(insightmon.InsightMonSpanProcessor(BatchSpanProcessor(exporter)))`（导出处理器同样
  交给它，而不是直接加到 provider 上）。
- 平台识别的全部属性见 [docs/conventions.md](docs/conventions.md)。

## 内容采集与隐私

**默认不发送消息内容**（`capture_content=False`）：

- SDK 的导出器在发送前从 span 和 span 事件中删除它认得的内容属性（`semconv.is_content_attribute()`）：标准的
  `gen_ai.input.messages`、`gen_ai.output.messages`、`gen_ai.system_instructions`、`gen_ai.tool.call.arguments`、
  `gen_ai.tool.call.result`，以及常见第三方插桩的 `input.value`、`output.value`、`llm.input_messages*`、
  `traceloop.entity.input`、`langfuse.observation.input`、`retrieval.documents.*` 等，完整列表见
  [docs/conventions.md](docs/conventions.md#内容)；
- 日志处理器只转发评估结果事件，不转发消息内容事件；
- `record_io()` 什么也不做，返回 `False`。

**列表之外的属性照常发送**，平台在 `metadata_only` 模式下也会保存这些非标准属性。请检查你用的插桩写了哪些带内容的
属性，表里没有的用 `content_attributes` 补上（属性名，或以 `*` 结尾的前缀）：

```python
import insightmon

insightmon.init(
    agent_name="weather-assistant",
    agent_version="1.4.0",
    content_attributes=["notes.body", "search.results.*"],  # stripped too while capture is off
)
```

**内容开关管不到的数据**：

- `evaluate()` 总是上报任务输出、任务异常的消息，以及客户端评估器给出的分数说明（`explanation`）和 metadata：
  平台要靠它们给出结论；
- span 出错时，`turn()`（以及 OpenTelemetry 的 `start_as_current_span()`）记录的 `exception` 事件带着异常消息和堆栈
  （`exception.message`、`exception.stacktrace`）：异常消息里含有用户输入时，它会随 span 一起发出；
- 会话键、`user_ref`、metadata 和 `log_evaluation()` 的 `explanation`：不要在其中放个人信息。

需要在平台上看到对话内容时显式开启：

```python
import insightmon

insightmon.init(agent_name="weather-assistant", agent_version="1.4.0", capture_content=True)

with insightmon.thread("chat-45"), insightmon.turn():
    insightmon.record_io(
        system_instructions="You are a concise weather assistant.",
        input_messages="Will it rain in Oslo tomorrow?",
        output_messages="Light rain is expected in Oslo tomorrow.",
    )
```

- `record_io()` 接受字符串、`{role, content}` 或 OTel 形状 `{role, parts}` 的消息（或它们的列表），写成 OTel GenAI
  约定的 JSON；单个字段超过 256 KiB 时先在客户端保形截断。
- 插桩以**日志事件**发出的内容（`gen_ai.user.message` 等）只有经过 SDK 的日志处理器才会转发：把插桩所用的
  `LoggerProvider` 传给 `init(logger_provider=...)`。SDK 自己新建的 `LoggerProvider` 只供 `log_evaluation()` 使用，
  插桩不会用到它。
- 平台侧还有一道开关：项目设置的内容采集模式为 `metadata_only` 时，平台不保存标准的消息内容。平台按入库规则做脱敏。

## 评估日志

自己的评估器（规则、模型打分、标注工具）的结果用 `log_evaluation()` 发给平台，作为 `gen_ai.evaluation.result`
日志事件；平台把它写成 `source = import` 的分数：

```python
import insightmon

with insightmon.thread("chat-46"), insightmon.turn() as span:
    answer = "Light rain is expected in Oslo tomorrow."

insightmon.log_evaluation("helpfulness", value=0.9, explanation="answers the question", span=span)
insightmon.log_evaluation("mentions_city", label="pass", span=span)
```

- **名称必须是项目里已发布的评分定义**（侧栏「评分定义」）。平台把名称转小写、非 `[a-z0-9_]` 字符换成 `_`。
  未知名称会被平台拒绝：SDK 读取 OTLP 的 partial success 响应，对每种原因记一条 warning（例如
  `score_config_not_found`），并计入 `insightmon.rejections()`。
- `value`、`label`、`error_type` 三者给且只给一个；`error_type` 表示评估器本身失败（写成 unscored 分数）。
- 关联：给 `span=`（或在被评估的 span 里调用）；拿不到 span 时给 `response_id=`（先用 `set_response_id()` 写在 span 上）。
- 分数落在哪一层：评分定义允许 span 级时落在该 span，否则落在它的 trace，再否则落在会话。

## 分数

用户反馈（`end_user`）、业务结果（`outcome`）和外部导入（`import`）的分数直接写 REST（Key 需要 `score_write`）：

```python
import insightmon

with insightmon.Client() as client:  # INSIGHTMON_BASE_URL / INSIGHTMON_API_KEY
    client.scores_batch(
        [
            {
                "name": "thumbs_up",
                "source": "end_user",
                "subject": {"type": "thread", "thread_key": "chat-42"},
                "value": True,
            },
            {
                "name": "task_completed",
                "source": "outcome",
                "subject": {"type": "trace", "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"},
                "value": True,
                "explanation": "the user saved the forecast",
            },
        ]
    )
```

- 主体（`subject`）可以用外部 id：会话 `{"type": "thread", "thread_key": ...}`、trace
  `{"type": "trace", "otel_trace_id": ...}`、span `{"type": "span", "otel_trace_id": ..., "otel_span_id": ...}`；
  也可以用平台内部 id。名称同样必须是已发布的评分定义。
- 每条分数都有幂等键（不填时 SDK 生成），请求的重试不会重复写入；同一批中任何一条无效，整批拒绝（抛出
  `InsightMonError`，例如 `score_config_not_found`）。每批最多 500 条，SDK 自动分批。
- 分成多批时，某一批失败会抛出 `InsightMonError`，它之前的批已经写入；错误的 `params` 带 `written`（已写入的条数）
  和 `idempotency_keys`（每条分数的幂等键，按顺序）。重新调用时把这些键填回各条分数的 `idempotency_key`（或者
  一开始就自己填），已写入的分数不会重复。
- `metadata` 不能用平台保留的键（`adjudicated`、`accepted_by`、`mt_assisted`、`self_review`、`verification_id`），
  SDK 在发送前报 `ValueError`。

## evaluate()

`evaluate()` 在客户端对测评集的每个（样例，trial）运行你的任务，把输出上报给平台；平台运行 gating 评估器并给出
结论。Key 需要 `experiment_write`（同时上报 span 还需要 `trace_ingest`）。可运行的示例：
[examples/evaluate_example.py](examples/evaluate_example.py)。

```python
import insightmon

insightmon.init(agent_name="weather-assistant", agent_version="1.4.0")


def weather_assistant(input):
    return "Light rain is expected in Oslo tomorrow."  # a str becomes {"text": ...}


def mentions_rain(input, output, reference):
    return {"name": "mentions_rain", "value": 1.0 if "rain" in output["text"].lower() else 0.0}


result = insightmon.evaluate(
    weather_assistant,
    dataset="<dataset id>",
    evaluators=[mentions_rain],  # client side: diagnostic only
    server_evaluators=["<evaluator version id>"],  # gating: decides the verdict
    trials=2,
    max_concurrency=4,
    timeout_s=60,
)
print(result.status, result.pass_rate, result.url)
```

- **任务**：`task(input)`；需要样例的初始状态时，把第二个位置参数命名为 `initial_state`（`task(input, initial_state)`，
  得到样例 `initial_state` 的一份副本）；需要整个样例（`reference`、`metadata` 等）时写 `task(input, example)`。
  样例带着非空的 `initial_state` 而任务只接收 `input` 时，SDK 记一条 warning。
- **输出**：与平台处理 HTTP Target 输出的方式相同：字符串 → `{"text": ...}`，dict 原样，None → `{}`，其他 JSON 值
  （列表、数字、布尔）→ `{"json": ...}`。NaN / Infinity 变成 null，孤立的 UTF-16 代理字符变成 U+FFFD；不能编码成 JSON
  的输出（任意对象、引用自身、嵌套超过 100 层）记为 `task_error`（`output_not_json`），超过 256 KiB 的记为
  `task_error`（`output_too_large`）。任务抛异常记为 `task_error`，超时记为 `timeout`；这些都不会让 `evaluate()` 抛错。
- **样例**：默认是数据集当前版本中 ready 的 dev 样例；`example_keys=` 取子集，`dataset_version=` 固定版本，
  `trials=` 每个样例跑 1–10 次。
- **客户端评估器**只作诊断，不影响结论。它们在条目的轮次结束后、在条目的 trace 之外运行：评估器自己产生的 span
  （例如插桩记录的模型调用）被丢弃，不会混进平台评估器读取的 agent 轨迹，也不会成为生产 trace。它们的**名称必须是
  已发布的评分定义**、值必须符合定义，否则平台拒绝这些分数，SDK 不带这些分数重新上报该条目并记 warning（同一组
  名称被拒 3 次、且平台从未接受过这组名称时，本次运行的其余条目直接不带它们）；`trials` > 1 时，一个 trial 的无效
  分数不会连累其他 trial 的分数。分数 metadata 中平台保留的键（`adjudicated`、`accepted_by` 等）被去掉并记 warning。
- **服务端评估器**是平台上发布的评估器版本 id（gating），也可以写 `{"evaluator_version_id": ..., "role": "diagnostic"}`。
- **超时与并发**：`timeout_s` 到时，这次运行记为 `timeout`；但线程无法被杀死，任务返回之前仍占着 `max_concurrency`
  中的一个名额，所以挂起的下游服务同时最多只会收到 `max_concurrency` 个调用。所有名额都被超时仍未返回的任务占着、
  再过 `timeout_s` 仍没有空出时，运行停止（不 finalize），抛出写明实验 id 的 `RuntimeError`。
- 每次运行都有自己的 trace（根 span `evaluate`），任务里创建的 span 都在其中；SDK 等条目上报后才导出这些 span，
  平台把它们并入实验 trace（`origin = experiment`），不会出现在生产会话里。经 Collector 转发时要用
  `init(export_processor=...)`（见[接入已有的 OpenTelemetry](#接入已有的-opentelemetry)），否则 span 无法暂存，
  `evaluate()` 记一条 warning。
- **上报重试**：`items:batch` 与 `:finalize` 是幂等的，SDK 对 429、5xx 和网络错误最多重试 8 次（退避合计约 40 秒，
  `Retry-After` 另计；`Client(report_retries=...)` 可调），平台短暂不可用不会让运行停下。创建实验没有幂等键，
  只在确定请求没有送达时重试；网关答 502 / 504、读超时等情况下实验可能已经建好，错误消息会提示先到实验页面确认。
- `result.url` 是平台上的实验页面；`result.items` 是本地记录的每次运行（状态、输出、延迟、trace id、客户端分数）；
  `wait=False` 时不等平台评估完成。
- 异步版本 `aevaluate()`：异步任务直接 await（`timeout_s` 到时取消）；同步任务、同步评估器和 SDK 的 REST 调用在
  专用的守护线程中运行，不占用事件循环的默认线程池；超时从任务开始运行时计时。

```python
import asyncio

import insightmon


async def weather_assistant(input):
    await asyncio.sleep(0)  # e.g. an async HTTP call
    return "Light rain is expected tomorrow."


async def main():
    return await insightmon.aevaluate(weather_assistant, dataset="<dataset id>", max_concurrency=8)


result = asyncio.run(main())
```

### 中断（Ctrl-C / 取消）

按下 Ctrl-C（或取消 `aevaluate()` 所在的任务）时，包括正在上报最后一批条目时：停止调度新的运行；**已完成的条目
照常上报**，已上报条目的 span 照常导出，其余条目的 span 被丢弃（导出后会成为生产 trace），收尾时再按一次 Ctrl-C
也一样；**不调用 finalize**，实验停在未完成状态；warning 日志写明实验 id 和已上报的条目数，然后重新抛出
`KeyboardInterrupt` / `CancelledError`。之后可以手动收尾（未上报的条目保持 pending），也可以不管它，平台在实验
创建 24 小时后自动结束：

```python
import insightmon

with insightmon.Client() as client:
    client.finalize_experiment("<experiment id>")
```

上报失败等 SDK 自身的错误同样不 finalize，`InsightMonError` 的消息里带实验 id。

## 回放回执

平台也可以反过来调用你的服务（HTTP Target）回放测评集。每次请求都带
`traceparent: 00-<otel_trace_id>-<随机 id>-01`，平台预先建好这个 trace id 的实验 trace。你的服务在处理请求时
创建的 span 只要接上这个上下文，就会并入平台的 trace，平台再从这些 span 读出**被测的 agent 版本和组件版本**
（回执，`version_source = trace_attr`）。

```python
import insightmon

insightmon.init(agent_name="weather-agent", agent_version="2.1.0")


async def app(scope, receive, send):
    """Your ASGI application (FastAPI, Starlette, ...)."""


app = insightmon.InsightMonASGIMiddleware(app)
```

- `InsightMonASGIMiddleware` / `InsightMonWSGIMiddleware` 把请求里的 `traceparent` 接成当前上下文，不创建服务端
  span（需要时用框架自己的插桩）；WSGI 中间件在消费整个响应期间（包括生成器的每一步）保持同一个上下文，在生成器里
  开始的 span 照常嵌套。Starlette / FastAPI 也可以写 `app.add_middleware(insightmon.InsightMonASGIMiddleware)`，
  Flask 写 `app.wsgi_app = insightmon.InsightMonWSGIMiddleware(app.wsgi_app)`；不用中间件时可以调用
  `insightmon.extract_context(headers)`。
- 处理请求的代码照常用 `insightmon.turn()`：它成为平台 trace 中的本地根 span，SDK 在上面写 agent 与组件版本。
- 还可以在响应体里带上 `"receipt": insightmon.receipt()`（响应字段回执，优先于 span 回执，不依赖 span 是否及时
  到达；两者都有时 `version_source = both`，不一致记为冲突）。
- 完整可运行的服务：[examples/http_agent.py](examples/http_agent.py)（需要 `pip install uvicorn`），可以直接注册为
  平台的 HTTP Target（URL 指向 `/v1/respond`，使用平台默认的请求模板）。

**时间**：平台在条目完成后等待 Target 配置的 `receipt_wait_s`（默认 120 秒）再读 span。SDK 默认每 5 秒批量导出
一次，默认值足够。

**`receipt_wait_s = 0`**：平台不等待，span 必须在响应之前送达。为此使用
`InsightMonASGIMiddleware(app, flush_on_traceparent=True)`（WSGI 中间件同名参数）：带 `traceparent` 的请求在发出
最后一块响应之前先导出 span，每次最多等 `flush_timeout_s`（默认 5 秒）。调用方普遍传播 W3C 上下文时，几乎每个请求
都带 `traceparent`，于是每个请求都要同步导出一次：只在专门承接回放的部署里这样用，或者用 `flush_when=` 只对回放
请求导出。平台本身不发送标记回放的请求头，可以让回放 Target 通过 `headers_ref` 带上一个：

```python
import insightmon


async def app(scope, receive, send):
    """Your ASGI application, serving replays and live traffic alike."""


def is_replay(scope) -> bool:
    """A header the replay target sends (configured in its headers_ref)."""
    return (b"x-replay", b"1") in scope.get("headers", [])


app = insightmon.InsightMonASGIMiddleware(app, flush_when=is_replay, flush_timeout_s=3)
```

`flush_when` 收到 ASGI 的 scope（WSGI 中间件收到 environ），只对带 `traceparent` 的请求调用。WSGI 中间件在服务器
拿到最后一块响应之前导出：应用返回列表时立即导出，流式响应则提前读一块（响应因此晚一块发出）。

**平台使用 SQLite 时不要在响应前导出**：平台在等待响应期间持有数据库写锁，导出会一直阻塞到超时，条目记为
`timeout`、span 丢失。SQLite 上不要开 `flush_on_traceparent` / `flush_when`，并把 `receipt_wait_s` 设为至少几秒；
PostgreSQL 上两种方式都可以。

## 配置

`init()` 的参数（都可省略）：

| 参数 | 环境变量 | 说明 |
|---|---|---|
| `base_url` | `INSIGHTMON_BASE_URL` | 平台根地址；与 Key 同时给出才导出，都不给时只打属性 |
| `api_key` | `INSIGHTMON_API_KEY` | `imk_` 开头的 Key |
| `service_name` | `OTEL_SERVICE_NAME` | SDK 新建 provider 时的 `service.name`（默认取 `agent_name`） |
| `agent_name` / `agent_version` | — | 每个 span 上的 `gen_ai.agent.name` / `gen_ai.agent.version` |
| `components` | — | `Component` 或 `{type, name, version?, content_hash?}` 列表 |
| `language` | — | 默认声明语言：`zh` / `en` / `ja` / `mixed` / `und` |
| `capture_content` | — | 是否发送消息内容（默认 `False`） |
| `content_attributes` | — | 关闭内容采集时额外删除的属性（属性名，或以 `*` 结尾的前缀），见[内容采集与隐私](#内容采集与隐私) |
| `environment` | — | 平台的环境（`deployment.environment.name`，≤ 100 字符） |
| `tracer_provider` / `logger_provider` | — | 接入指定的 provider；插桩的内容日志事件要经过 `logger_provider` 才会转发 |
| `set_global` | — | SDK 新建的 provider 是否设为全局（默认 `True`） |
| `export_processor` | — | 你自己的 span 导出处理器（例如发往 Collector 的 `BatchSpanProcessor`）：由 SDK 转交 span，`evaluate()` 的 span 在这条路径上也先暂存；不要再把它加到 provider 上 |

`agent_name`、`agent_version`、`environment` 与组件的名称、版本去掉首尾空白（平台读取时同样去掉，否则回执会记为
冲突）。base URL 要写最终地址：SDK 不跟随重定向（Key 不能被带到别的主机），也不接受带用户名或密码的 URL；配置错误
的消息不复述你给的值，即使 Key 和 URL 填反了，Key 也不会出现在错误信息里。

`Client(base_url=None, api_key=None, *, timeout=30.0, max_retries=3, report_retries=8)` 与
`evaluate(..., base_url=None, api_key=None)` 依次取参数、`init()` 的设置、环境变量。`max_retries` 是一般请求的重试次数，
`report_retries` 是 `items:batch` 与 `:finalize` 的重试次数。

SDK 的 OTLP 导出器只发送 `Authorization`、`Content-Type`、`Content-Encoding`、`User-Agent` 这几个请求头：通用的
`OTEL_EXPORTER_OTLP_HEADERS`、`OTEL_EXPORTER_OTLP_TRACES_HEADERS`、`OTEL_EXPORTER_OTLP_LOGS_HEADERS`（可能带着
别家服务的凭据）对它不起作用。每次导出对 429、5xx 和网络错误按 `Retry-After` 或退避重试，最多 10 秒（平台按
span id 和记录摘要去重，重试不会重复写入）。导出批处理可以用标准的 OpenTelemetry 环境变量调节（例如
`OTEL_BSP_SCHEDULE_DELAY`，毫秒）。

Key 的权限：

| 权限 | 用途 |
|---|---|
| `trace_ingest` | 上报 trace 和评估日志（`init()`） |
| `score_write` | `Client.scores_batch()` |
| `experiment_write` | `evaluate()` / `aevaluate()` 与 `Client` 的实验方法 |

`flush(timeout_s=10)` 立即导出，最多等 `timeout_s` 秒；超时或期间有导出失败时返回 `False`。
`shutdown(timeout_s=10)` 导出剩余数据并关闭，同样在期限内返回（平台不可达也不会卡住进程退出），没能导出的数据丢弃
并记一条 warning；进程退出时自动调用（期限 5 秒）。两者都不会在持有 SDK 的锁时访问网络，其他线程照常开始新的
span。`init()` 返回的句柄也可以用作上下文管理器。

## 故障排查

- **平台上什么都没有**：确认 `INSIGHTMON_BASE_URL` 和 `INSIGHTMON_API_KEY` 都已设置（只设一个会报
  `config_error`）；短命脚本结束前调用 `insightmon.shutdown()` 或 `flush()`（`flush()` 返回 `False` 说明导出失败或
  超时，原因在 warning 里）；打开日志 `logging.getLogger("insightmon").setLevel(logging.DEBUG)` 与
  `logging.getLogger("opentelemetry").setLevel(logging.DEBUG)`。
- **warning `the platform answered an export of spans with HTTP 301 (redirect to ...)`**：base URL 被重定向了（例如
  http → https，或者多了、少了路径前缀）。SDK 不跟随重定向，这些数据已经丢失：把 base URL 改成 warning 里的最终
  地址；`insightmon.rejections().redirects` 有累计数。REST 调用遇到重定向时报 `redirect` 错误。
- **401 / 403**：Key 被吊销、属于别的项目或缺少权限；`insightmon.Client().api_key_info()` 显示 Key 的项目和权限。
- **trace 没进会话**：span 不在 `thread()` 里，或者在线程池里丢了上下文（见[线程池](#asyncio线程池与多进程)）。
- **会话没关闭**：最后一轮没有 `end_thread`；平台会按空闲时间关闭。
- **warning `the platform refused N log records: score_config_not_found`**：评估名称没有对应的已发布评分定义；
  `insightmon.rejections()` 有累计数。
- **`evaluate()` 的客户端分数不见了**：同上，名称必须是已发布的评分定义；warning 里有被拒的名称。
- **`evaluate()` 被中断后实验一直未完成**：见[中断](#中断ctrl-c--取消)，用 `Client().finalize_experiment(id)` 收尾。
- **`evaluate()` 抛出 `RuntimeError`，说超时的任务仍在运行**：下游服务挂住了，`max_concurrency` 的名额全被超时未返回
  的任务占着。查明挂起的原因，或者调大 `timeout_s`；实验没有 finalize，处理同上。
- **warning `the spans of evaluate() are exported as they end, not held`**：span 由你自己加到 provider 上的处理器导出，
  SDK 无法暂存；改用 `init(export_processor=...)`（见[接入已有的 OpenTelemetry](#接入已有的-opentelemetry)）。
- **创建实验报错，提示 `the experiment may have been created`**：请求已经到达服务器（网关答 502 / 504、读超时等），
  先到平台的实验页面确认，再决定是否重新运行。
- **开启 `capture_content` 后看不到插桩以日志事件发出的内容**：把插桩所用的 `LoggerProvider` 传给
  `init(logger_provider=...)`，见[内容采集与隐私](#内容采集与隐私)。
- **回放条目全部 `timeout`、回执 `none`**：平台用 SQLite 且服务开了 `flush_on_traceparent`，见[回放回执](#回放回执)。
- **回执 `version_source = none`**：span 没在 `receipt_wait_s` 内到达，或服务没接上 `traceparent`（没加中间件，
  或在线程池里丢了上下文）。
- **代理**：OTLP 导出与 REST 请求都遵循 `HTTPS_PROXY` / `NO_PROXY`。

## 兼容性

- Python 3.10、3.11、3.12、3.13（CI 全部运行）。
- OpenTelemetry Python ≥ 1.35（`opentelemetry-api`、`opentelemetry-sdk`、`opentelemetry-exporter-otlp-proto-http`），
  已在 1.35 到 1.45 上测试；httpx ≥ 0.27。
- 平台：对接 [InsightMon/insightmon-ai-eval](https://github.com/InsightMon/insightmon-ai-eval) 当前 `main` 的
  OTLP/HTTP 端点（`/otlp/v1/traces`、`/otlp/v1/logs`）和 Key 接口（`/api/v1`），契约见
  [docs/contract.md](docs/contract.md)。平台用 SQLite 时注意[回放回执](#回放回执)中的限制。
- Web 框架：ASGI / WSGI 中间件不依赖任何框架。

## 开发

```bash
make install            # uv sync
make lint               # ruff check、ruff format --check、mypy
make test               # 单元测试（不含集成测试）
make test-integration   # 集成测试：scripts/integration.sh
make build              # uv build
```

集成测试对真实平台运行：`scripts/integration.sh` 从 `INSIGHTMON_PLATFORM_DIR`（默认 `../insightmon-ai-eval`，
需要先在那里 `make build` 构建前端）启动一个临时平台实例（SQLite、演示数据、只监听本机），创建 API Key，运行
`tests/integration/`，最后关闭平台。对已有平台运行时设置 `INSIGHTMON_IT_BASE_URL` 和项目管理员账号
（`INSIGHTMON_IT_ADMIN_EMAIL` / `INSIGHTMON_IT_ADMIN_PASSWORD`），详见
[tests/integration/conftest.py](tests/integration/conftest.py)。脚本自己创建的 Key 在结束时吊销（失败或 Ctrl-C 时
也一样），共享的平台上不会留下可用的 Key。

文档：

- [docs/conventions.md](docs/conventions.md)：属性与事件名一览（测试对照 `semconv.py` 检查）。
- [docs/contract.md](docs/contract.md)：从平台代码整理的契约（端点、属性、日志事件、回放、evaluate 流程、差异与缺口）。
- [docs/design.md](docs/design.md)：公共 API 与模块结构。
