# SDK 设计

本文说明 `insightmon-eval-sdk`（导入名 `insightmon`）的公共 API、运行时结构、模块划分与测试约定，
以及相对默认设计的调整。平台约定见 [contract.md](contract.md)，属性一览见 [conventions.md](conventions.md)，
用法见 [README](../README.md)。

## 1. 原则

- **比同类 SDK 更轻**：运行时只依赖 `opentelemetry-api`、`opentelemetry-sdk`、
  `opentelemetry-exporter-otlp-proto-http`（≥ 1.35，第一个在 LogRecord 上带 `event_name` 的版本）和
  `httpx`（≥ 0.27）。不依赖任何 Web 框架或模型厂商的库。
- **OpenTelemetry 原生**：会话数据就是普通的 OTel span 和日志事件。应用已有的 `TracerProvider`
  可以直接接入，第三方插桩产生的 span 也会被打上平台属性。
- **通用**：代码、测试、示例、文档里不出现任何业务领域词汇，示例用“天气助手”“笔记助手”这类中性场景
  （`tests/test_purity.py` 检查）。
- **默认不发内容**：`capture_content=False` 时，SDK 的导出器删除 `semconv.is_content_attribute()` 列出的内容属性
  （以及 `init(content_attributes=...)` 补充的），日志处理器不转发内容事件。列表之外的属性照常发送，接入方要检查
  自己的插桩；`evaluate()` 上报的输出、错误消息与客户端分数说明，以及 span 的 `exception` 事件，不受这个开关控制
  （README“内容采集与隐私”）。
- Python ≥ 3.10，全部模块 `from __future__ import annotations`，mypy strict。

## 2. 公共 API

| 名称 | 签名 | 模块 |
|---|---|---|
| `init` | `init(*, base_url=None, api_key=None, service_name=None, agent_name=None, agent_version=None, components=None, language=None, capture_content=False, environment=None, content_attributes=None, tracer_provider=None, logger_provider=None, set_global=True, export_processor=None) -> InsightMonHandle` | `_setup` |
| `flush` | `flush(timeout_s=10.0) -> bool`：在 `timeout_s` 内返回；超时或期间有导出失败时为 `False` | `_setup` |
| `shutdown` | `shutdown(timeout_s=10.0) -> None`：在 `timeout_s`（加 0.5 秒）内返回，没导出的数据丢弃并记 warning | `_setup` |
| `InsightMonHandle` | `.settings`、`.tracer_provider`、`.logger_provider`、`.flush()`、`.shutdown()`，可用作上下文管理器 | `_setup` |
| `InsightMonSpanProcessor` | `InsightMonSpanProcessor(export_processor=None, *, max_held_spans=10000, release_chunk=512)` | `processor` |
| `thread` | `thread(key, *, language=None, metadata=None, user_ref=None) -> AbstractContextManager[ThreadScope]` | `tracing` |
| `current_thread` | `current_thread() -> ThreadScope \| None` | `tracing` |
| `turn` | `turn(name="turn", *, eval_root=False, end_thread=False, operation="invoke_agent", attributes=None) -> AbstractContextManager[Span]` | `tracing` |
| `mark_eval_root` | `mark_eval_root(span=None) -> None` | `tracing` |
| `end_thread` | `end_thread(span=None) -> None` | `tracing` |
| `set_component` | `set_component(type, name, *, version=None, content_hash=None) -> None` | `tracing` |
| `set_response_id` | `set_response_id(response_id, span=None) -> None` | `tracing` |
| `get_tracer` | `get_tracer() -> Tracer` | `tracing` |
| `record_io` | `record_io(*, input_messages=None, output_messages=None, system_instructions=None, span=None) -> bool` | `content` |
| `log_evaluation` | `log_evaluation(name, *, value=None, label=None, explanation=None, error_type=None, span=None, response_id=None) -> None` | `logs` |
| `extract_context` | `extract_context(headers) -> Context` | `propagation` |
| `receipt` | `receipt() -> dict` | `propagation` |
| `InsightMonASGIMiddleware` | `InsightMonASGIMiddleware(app, *, flush_on_traceparent=False, flush_when=None, flush_timeout_s=5.0)` | `propagation` |
| `InsightMonWSGIMiddleware` | `InsightMonWSGIMiddleware(app, *, flush_on_traceparent=False, flush_when=None, flush_timeout_s=5.0)` | `propagation` |
| `Client` | `Client(base_url=None, api_key=None, *, timeout=30.0, max_retries=3, report_retries=8, transport=None)`；方法 `api_key_info`、`scores_batch`、`score_configs`、`create_experiment(..., idempotency_key=None)`、`get_experiment`、`list_examples`、`iter_examples`、`report_items`、`finalize_experiment`、`experiment_url`、`close` | `client` |
| `ApiKeyInfo` / `Example` / `ExamplesPage` / `ReportResult` / `ScoreConfig` | 不可变数据类 | `client` |
| `evaluate` | `evaluate(task, *, dataset, dataset_version=None, example_keys=None, trials=1, evaluators=(), server_evaluators=(), name=None, requested=None, metadata=None, max_concurrency=4, timeout_s=None, api_key=None, base_url=None, wait=True, wait_timeout_s=1800.0, client=None) -> ExperimentResult` | `evaluate` |
| `aevaluate` | 与 `evaluate` 相同，`async` | `evaluate` |
| `ExperimentResult` / `ItemResult` | 数据类 | `evaluate` |
| `InsightMonError` | `InsightMonError(code, status=None, message="", params=None, *, retry_after_s=None)`；`.retryable`、`.delivered`、`from_response()`、`from_transport_error()` | `_errors` |
| `rejections` / `Rejections` | `rejections() -> Rejections`：平台在 OTLP partial success 中拒绝的 span 数、日志记录数与原因，以及被重定向的导出次数（`redirects`，进程内累计） | `_export` |
| `Component` / `Settings` / `ThreadScope` | 数据类 | `_config` / `_context` |
| `semconv` | 属性名、事件名、上限、`component_attribute()`、`metadata_attribute()`、`is_content_attribute()`、`content_matcher()`、`normalize_score_name()` | `semconv` |

## 3. 运行时结构

```
应用代码 ── thread()/turn()/set_component() ──▶ contextvars（_context）
                                             │
TracerProvider ── InsightMonSpanProcessor ───┤ on_start：打会话键、agent、组件、语言、实验属性
                     │                          on_end：暂存实验 trace 的 span，其余交给导出
                     ├─ BatchSpanProcessor（队列 12048）─ ContentStrippingSpanExporter（关闭内容时）
                     │                      └─ SplittingSpanExporter ─ PlatformSpanExporter（httpx）─▶ /otlp/v1/traces
                     └─ export_processor（应用自己的，例如发往 Collector；可选）
LoggerProvider（SDK 自己的或 logger_provider=）── EventFilterProcessor ─ BatchLogRecordProcessor
                                                 └─ SplittingLogExporter ─ PlatformLogExporter（httpx）─▶ /otlp/v1/logs
Platform*Exporter ── ExportControl（失败计数、shutdown 期限、中止）；响应体 partial success / 3xx ─▶ warning + rejections()
flush() / shutdown() ── _FlushGate：每个处理器在辅助线程里 force_flush，按期限 join
Client（httpx）── HttpTransport（重试、错误映射、关闭插桩）─▶ /api/v1/...
evaluate() ── Client + ItemBatcher + _TaskSlots + experiment_scope / release_traces / discard_owner（_context）
```

- 未调用 `init()` 时：`get_tracer()` 取全局 provider（默认是 no-op），`log_evaluation` 无处可发，
  `Client` 与 `evaluate` 只依赖参数或环境变量，照常工作。
- 没有 base URL / Key 时 `init()` 只打属性、不导出，适合经 Collector 转发或测试。
- 平台对 OTLP 请求的部分拒绝（例如未知评分定义 `score_config_not_found`）在 HTTP 200 的响应体里返回，
  OpenTelemetry 导出器不看它。SDK 的导出器解析 `partial_success`，每种原因第一次记 WARNING、之后记 DEBUG，
  并累计到 `rejections()`。
- OTLP 导出用 SDK 自己的 httpx 导出器（`PlatformSpanExporter` / `PlatformLogExporter`），而不是 OpenTelemetry 的
  OTLP/HTTP 导出器：后者把通用的 `OTEL_EXPORTER_OTLP_*HEADERS` 合并进请求（可能把别家的凭据发给平台），各版本
  重试的状态码不同，跟随或静默接受重定向，也不遵守 flush 期限。SDK 的导出器只发四个请求头，重试 429 / 5xx /
  网络错误（每次导出 10 秒内），不跟随 3xx（记 WARNING、计数、导出失败），由 `ExportControl` 统计失败、在
  `shutdown()` 的期限到时中止。OpenTelemetry 的 OTLP 包仍是依赖：SDK 用它的 protobuf 编码器。
- `flush()` / `shutdown()` 在期限内返回：每个处理器的 `force_flush()` 在辅助线程里运行（同一处理器同时只有一个，
  并发的调用者加入它），按剩余时间 join；`shutdown()` 只在 SDK 的锁内换出运行时状态，网络 I/O 都在锁外，
  `global_components()` 读不可变快照、不加锁。
- SDK 自己的 HTTP 请求（导出与 REST）在 `_SUPPRESS_INSTRUMENTATION_KEY` 的上下文中发送，HTTP 客户端插桩不会把
  它们记成 agent 的 span。
- fork 之后，子进程重建 SDK 的全部锁（`_context`、`_config`、`_export`、`logs`、`processor`、`_setup`），并忘掉
  父进程暂存的 trace。
- provider 的活动处理器是 `ConcurrentMultiSpanProcessor` 时 `init()` 记 WARNING：`on_start` 在线程池里运行，读不到
  调用方的 contextvars，打属性与暂存都不起作用。

## 4. 相对默认设计的调整

默认设计列出了 `init`、span 处理器、`thread`、`turn`、`mark_eval_root`、`end_thread`、`set_component`、
`record_io`、`set_response_id`、`log_evaluation`、传播（`extract_context` 与 ASGI/WSGI 中间件）、`flush`、
`shutdown`、`Client`、`InsightMonError`、`evaluate`/`aevaluate`/`ExperimentResult`、`semconv`。
以下调整都是契约要求的：

| 调整 | 原因（契约） |
|---|---|
| `init()` 增加 `environment` | 平台环境来自 resource `deployment.environment.name`（≤ 100）（contract §8） |
| `thread()` 增加 `metadata`、`user_ref` | 平台把 `insightmon.metadata.<k>` 写入会话 metadata，把 `enduser.id`（≤ 128）写入 `user_ref` |
| `turn()` 增加 `eval_root`、`end_thread` | 两个标志都要落在具体 span 上；`thread.end` 放在 resource 上会结束进程内所有会话（contract §13 #16） |
| 每个 span 都写 `gen_ai.agent.name/version` | 缺少时平台退回 resource `service.version`，可能造成回放回执冲突（contract §14） |
| 组件属性只写在本地根 span 与轮次 span | 平台把“自身属性中恰好声明一个组件”的 span 记为该组件的 span，到处都写会污染 `spans.component` |
| 新增 `receipt()` | 回放时响应体里的 `receipt` 优先于 trace 属性，且不依赖 span 是否及时到达（contract §11） |
| 中间件不创建服务端 span，增加 `flush_on_traceparent`、`flush_when`、`flush_timeout_s` | 保持轻量；`receipt_wait_s = 0` 时 span 必须在响应前送达，而几乎每个请求都带 `traceparent`，需要挑出回放请求并限制等待（contract §11） |
| `log_evaluation(span=..., response_id=...)`；只给 `response_id` 时用空上下文发出 | 平台优先按 span 上下文关联；沾上当前 span 会关联错对象（contract §9.1） |
| `InsightMonError` 增加 SDK 码 `connect_error` 与 `.delivered` | 旧平台的 `POST /experiments` 没有幂等键，只能在“请求确定未送达”时重试（contract §6.1） |
| `create_experiment()` 总是带 `idempotency_key`（`evaluate()` 每次运行一个），按幂等请求重试；平台对这个字段答 422 `extra_forbidden` 时去掉它重发，此后同一个 `Client` 退回只在连接失败与 429 时重试 | 新平台按幂等键返回已创建的实验；旧平台拒绝整个请求、什么也不建，所以带键的首次请求在任何平台上都可以安全重试，答复本身就说明平台是否支持（contract §6.1） |
| 新增 `Client.score_configs()` 与 `published_score_names()`；`evaluate()` 每次运行、`log_evaluation()` 每个运行时（后台线程）各读一次，读不到时不检查 | 提前发现未发布的分数名，不再靠平台逐批拒绝；检查只是诊断，绝不能让调用失败（contract §5.1） |
| `Client.report_items` 重试时发送完全相同的字节 | 平台按报告内容哈希去重，内容不同即 `already_reported`（contract §6.4） |
| `Client.finalize_experiment` 重试遇到 409 `invalid_state status=finalized` 视为成功 | `:finalize` 第二次调用返回 409（contract §6.5） |
| `evaluate()` 内不写会话键，暂存实验 trace 的 span，条目上报后再导出 | 先到的 span 会建出 `origin = otlp` 的生产 trace（contract §12）。新平台读 `insightmon.experiment_id`（contract §8），但仍保留暂存，理由见下文 |
| 请求按序列化大小拆分（OTLP 与 `items:batch`） | 全局 8 MiB 上限；OTel 导出器超限时整批丢弃（contract §14） |
| `capture_content=False` 还删除第三方内容属性；`init()` 增加 `content_attributes` | 旧平台在 `metadata_only` 下仍保存非标准内容属性，新平台只丢弃它自己的清单（与 SDK 的内置列表相同，contract §10.2）；内容不应离开进程，内置列表也不可能覆盖所有插桩 |
| `init()` 增加 `export_processor` | 经 Collector 转发时，实验 span 也要先暂存、条目上报后再导出，否则成为生产 trace（contract §14） |
| 自己的 OTLP 导出器（httpx），不用 OpenTelemetry 的 OTLP/HTTP 导出器 | 通用 `OTEL_EXPORTER_OTLP_*HEADERS` 会把别家凭据发给平台；各版本重试集合不同；重定向被静默吞掉；不遵守期限（contract §0.1、§14） |
| `flush()` / `shutdown()` 有真实期限，`flush()` 在导出失败时返回 `False` | 平台不可达时不能卡住进程退出、`evaluate()` 收尾和回放响应 |
| `Client` 增加 `report_retries`（默认 8） | `items:batch` 与 `:finalize` 幂等，短暂的 502 / 503 不应让长时间的运行停下（contract §0.1） |
| `scores_batch()` 失败时在 `params` 中给出 `written` 与 `idempotency_keys` | 分批写入时，调用方要能不重复地重试（contract §5） |
| `evaluate()` 的任务可以写成 `task(input, initial_state)` | 平台契约要求任务拿到样例的 `initial_state`（`docs/dev/m4.md` §5.5，contract §12） |
| `evaluate()` 的输出按平台的 `normalize_output` 包装（非 dict 值 → `{"json": ...}`） | 与回放 Target 的输出形状一致，平台内置检查读 `output.json`（contract §12） |
| 客户端评估器在条目的 trace 之外运行，其 span 丢弃 | 评估器的模型调用不能混进平台评估器读取的 agent 轨迹（contract §12） |
| 超时的任务继续占用 `max_concurrency` 名额，名额耗尽时停止运行 | 线程无法被杀死；否则挂起的下游服务会收到越来越多的调用 |
| `aevaluate()` 不用事件循环的默认线程池 | 默认线程池与应用共用，挂起的同步任务占满它会卡住 SDK，排队时间也会算进超时 |
| 永远不复用全局 `LoggerProvider` | 全局 provider 可能挂着应用日志；SDK 只转发评估事件 |
| `thread()` / `turn()` 的返回类型写作 `AbstractContextManager[...]` | 实现细节（生成器）不进入公共签名 |
| 新增 `rejections()` | 平台的部分拒绝只出现在响应体里，标准导出器看不到（contract §14） |
| 导出 `Component`、`Settings`、`ThreadScope`、`InsightMonHandle` 与 `client` 数据类 | 出现在公共签名中 |

**为什么在新平台上仍然暂存实验 span**：新平台的入库会把带 `insightmon.experiment_id` 的 trace 记为实验流量，
条目先报、span 后到也能关联（contract §6.4、§8）。集成测试 `test_spans_before_the_report_become_experiment_traffic`
关掉暂存验证了这一点。但不暂存只在下面几条同时成立时才不出错，SDK 无法保证：
- 平台是新平台（旧平台不读这个属性，先到的 span 一律成为生产 trace）；
- 导入 span 的 Key 就是创建实验的 Key（`init()` 用一个 Key、`evaluate(api_key=...)` 用另一个，或经 Collector
  用 Collector 的 Key 导出时，平台忽略这个属性）；
- 带属性的根 span 在 trace 的第一批里：SDK 只在根 span 上写这个属性，而根 span 最后结束，批处理器先导出的
  子 span 会先建出一条生产 trace；那时 trace 一旦被在线规则派发（或加入会话），就不再转为实验流量。

平台文档（`docs/dev/m4.md` §5.5）也把暂存写成 SDK 的默认做法。暂存的代价是条目上报前 span 留在内存里（有上限，
contract §14），没有发现别的害处，所以保留。

## 5. 模块

| 模块 | 内容 |
|---|---|
| `semconv` | 属性名、事件名、取值集合、上限与名称辅助函数（无依赖；[conventions.md](conventions.md) 由测试对照它检查） |
| `_config` | `Settings`（参数与环境变量解析、校验）、`Component`、运行时状态 `RuntimeState`、进程级组件覆盖 |
| `_context` | contextvars：会话作用域 `ThreadScope`、实验作用域 `ExperimentScope`（含本次运行的组件覆盖）；暂存 trace 登记（按实验 id 归属，`hold_trace` / `release_traces` / `discard_traces`、释放监听） |
| `tracing` | `thread()`、`turn()`、`end_thread()`、`mark_eval_root()`、`set_component()`、`set_response_id()`、`get_tracer()` |
| `processor` | `InsightMonSpanProcessor`：on_start 打属性；实验 trace 的 span 先暂存，释放时交给导出处理器，丢弃时（含之后晚到的 span）不导出；fork 后重置锁 |
| `content` | `record_io()`、OTel 消息形状转换、保形压缩、内容属性判断 |
| `logs` | `log_evaluation()`（导出到平台时在后台检查一次名称）、`EventFilterProcessor`（只转发评估事件，开启内容采集时再加内容事件） |
| `propagation` | `extract_context()`、`receipt()`、ASGI / WSGI 中间件（`flush_on_traceparent`） |
| `_export` | SDK 自己的 OTLP/HTTP 导出器（httpx、四个请求头、重试、期限、重定向处理）、`ExportControl`、按请求大小拆分、内容删除、partial success 解析与 `rejections()` |
| `_setup` | `init()` / `flush()` / `shutdown()`（带期限的 `_FlushGate`）、`InsightMonHandle`、进程退出时的 `shutdown()` |
| `_errors` | `InsightMonError` 与错误码、重试分类 |
| `_http` | JSON 编码与清洗（非有限数、孤立代理字符、深度上限）、`HttpTransport`（重试、`Retry-After`、错误映射、关闭插桩） |
| `client` | `Client` 与数据类；`published_score_names()`（读不到评分定义时返回 None） |
| `_batch` | `ItemBatcher`：条目分批上报；平台拒绝客户端分数时去掉 `invalid_scores` 列出的分数重报（旧平台：去掉被拒条目的全部分数，同一样例的多个 trial 分开发送），同一请求被拒 3 次后逐个发送，同一个名称（旧平台：同一组名称）被拒 3 次（且从未被接受）后不再随后续条目发送 |
| `evaluate` | `evaluate()` / `aevaluate()`、结果类型、任务名额（`_TaskSlots`）、专用线程 |

### 模块之间的接缝

- `evaluate` 只通过四个函数接触 tracing：`_item_span()`（在 `_context.experiment_scope()` 中用
  `tracing.turn("evaluate")` 开一条新 trace）、`_evaluator_scope()`（轮次结束后在空上下文中、以单独的暂存所有者
  运行评估器，结束时 `discard_owner()`）、`_release_traces()`（条目上报后 `_context.release_traces()`）、
  `_flush_telemetry()`（运行结束时释放或丢弃本实验仍暂存的 trace 并关闭这个所有者，再 `_setup.flush()`）。
  `tests/test_evaluate.py` 的大部分测试 monkeypatch 这些函数；另有测试走真实的处理器。
- `InsightMonSpanProcessor` 在实验作用域内：不写会话键；在本地根 span 上写三个实验属性与本次运行的组件版本；
  作用域 `hold=True` 时对新 trace 调用 `_context.hold_trace(trace_id, owner=experiment_id)`，并通过
  `add_release_listener()` 在释放时导出、在丢弃时清掉。
- `evaluate` 与 `Client` 读取 `_config.get_settings()` 作为默认 base URL、Key 与 `requested`（agent 与组件版本）。

## 6. 测试约定

- `tests/conftest.py` 的自动夹具在每个测试前后清掉 `INSIGHTMON_BASE_URL`、`INSIGHTMON_API_KEY`、`OTEL_SERVICE_NAME`
  环境变量，并重置运行时状态、暂存登记与 `rejections()` 计数。
- tracing 测试把 `tracer_provider` / `logger_provider` 夹具（内存导出器）传给 `init()`；全局 provider 每个进程
  只能设置一次，测试不依赖它。
- REST 测试用 `mock_api` 夹具（`httpx.MockTransport`），把 `mock_api.transport` 传给 `Client(transport=...)`；
  `tests/fake_platform.py` 是带平台校验与去重的外部实验路由。
- 导出测试用 `collector` 夹具（`tests/collector.py`，本机 OTLP/HTTP 端点）检查线上的请求格式。
- 文档与示例：`tests/test_readme.py` 运行两个 README 中的每个 Python 代码块（两份 README 的代码块必须相同，
  快速开始必须等于 `examples/quickstart.py`）；`tests/test_examples.py` 运行三个示例；`tests/test_conventions.py`
  对照 `semconv` 检查 [conventions.md](conventions.md)；`tests/test_purity.py` 检查中性用词。
- 单元测试不访问网络（只连 127.0.0.1）：自动夹具把 `log_evaluation()` 后台读取评分定义的函数
  （`logs._fetch_score_names`）换成返回 None 的桩，集成测试除外。
- 集成测试在 `tests/integration/`，标记 `integration`，没有 `INSIGHTMON_IT_*` 环境变量时跳过；`make test` 不运行它们。
  `scripts/integration.sh` 启动临时平台实例、创建 Key、运行它们并关闭平台；结果从平台的 REST API 读回
  （会话、trace、span、分数、实验条目与回执）。脚本创建的 Key 在结束时吊销（`tests/test_provision.py` 检查）。

## 7. 兼容性备注

- OpenTelemetry 1.35 与 1.45 的日志 API 不同：`LogRecordProcessor.on_emit` 收到 `LogData` 或
  `ReadWriteLogRecord`（二者都有 `.log_record`）；内存日志导出器改名为 `InMemoryLogRecordExporter`；
  日志导出器基类改名为 `LogRecordExporter`。SDK 代码对这几处做鸭子类型处理。
- CI 在 Python 3.10、3.11、3.12、3.13 上运行单元测试。
