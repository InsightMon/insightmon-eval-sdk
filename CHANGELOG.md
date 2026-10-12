# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。1.0 之前，次版本号的变化可能包含不兼容的改动，会在这里写明。

## [未发布]

对接平台的 `fix/sdk-platform-gaps` 改动（数据库迁移 0009，docs/contract.md 开头的“新平台”）；在更早的平台上
自动退回原来的做法。

### 新增

- `Client.create_experiment(..., idempotency_key=None)`：总是带一个幂等键（不给时随机生成，`evaluate()` 每次
  运行生成一个），读超时、网络错误与 5xx 也重试，平台对同一个键返回已经建好的实验。平台不认这个字段（422）时
  去掉它重发，此后同一个 `Client` 照旧只在请求确定没有送达时重试。重试用完仍不确定时，错误的
  `params.idempotency_key` 给出可以重用的键。
- `Client.score_configs()` 与数据类 `ScoreConfig`（`GET /api/v1/score-configs`）。`evaluate()` 有客户端评估器时
  每次运行读一次，名称不是已发布、允许实验条目层级的评分定义的分数不上报，每个名称记一条 warning；
  `log_evaluation()` 在导出到平台时于后台读一次，对未知名称各记一条 warning（记录照常发送）。平台没有这个接口或
  Key 无权读取时都不检查。

### 变更

- `items:batch` 拒绝客户端分数（`score_invalid`）时，按平台列出的 `invalid_scores` 只去掉无效的分数，重发一次；
  按 `trial_index` 匹配被拒的条目。平台不列出时保留原来的逐条处理。
- 内置的第三方内容属性列表与平台的清单同步：新增 `mlflow.chat.messages`、Vercel AI SDK 的 `ai.*`
  内容属性、pydantic-ai / Logfire 的 `pydantic_ai.all_messages`、`final_result`、`tool_arguments`、
  `tool_response`、`request_data`、`response_data`，以及 Google ADK 的 `gcp.vertex.agent.*` 内容属性。
- 文档：契约（docs/contract.md）§13 的差异标为已同步，§14 分成“新平台已修复”与“仍然存在”；README 中 SQLite
  回放的限制只针对旧平台。`evaluate()` 仍然暂存实验 span（理由见 docs/design.md §4）。

## [0.1.0] - 2026-10-12

第一个版本。对接 [InsightMon/insightmon-ai-eval](https://github.com/InsightMon/insightmon-ai-eval) 当前
`main` 的 OTLP/HTTP 与 `/api/v1` 接口（契约见 [docs/contract.md](docs/contract.md)）。

### 新增

- **初始化**：`init()` / `flush()` / `shutdown()`；新建或接入 OpenTelemetry `TracerProvider`
  （参数、已有的全局 SDK provider、或新建）；`export_processor=` 接入应用自己的导出处理器（例如经 Collector
  转发）。SDK 自己的 OTLP/HTTP 导出器（httpx，protobuf + gzip）发往 `/otlp/v1/traces` 与 `/otlp/v1/logs`：只发
  `Authorization`、`Content-Type`、`Content-Encoding`、`User-Agent` 四个请求头，对 429 / 5xx / 网络错误在每次导出
  10 秒内重试，不跟随重定向，按平台的 8 MiB 请求上限拆分批次。`flush()` / `shutdown()` 在期限内返回，导出失败时
  `flush()` 返回 `False`；进程退出时自动 `shutdown()`（期限 5 秒）；fork 后子进程重建全部内部锁。
- **会话与轮次**：`thread()`（会话键、声明语言、metadata、`user_ref`）、`turn()`、`end_thread()`、
  `mark_eval_root()`、`set_component()`（作用于当前实验条目、会话或整个进程）、`set_response_id()`、
  `current_thread()`、`get_tracer()`；`InsightMonSpanProcessor` 给每个 span（包括第三方插桩的 span）
  打上会话键、agent 名称与版本、语言，在本地根 span 与轮次 span 上写组件版本。
- **内容采集**：默认关闭；关闭时导出前删除标准与常见第三方插桩的内容属性（列表见
  [docs/conventions.md](docs/conventions.md)）和内容事件，`init(content_attributes=[...])` 补充其他属性；
  `record_io()` 写 OTel GenAI 形状的消息 JSON，单个字段在客户端保形压缩到 256 KiB。
- **评估日志**：`log_evaluation()` 发出 `gen_ai.evaluation.result` 事件（按 span 或 `response_id` 关联）。
- **拒绝可见**：读取 OTLP partial success 响应，每种原因记一条 warning，`rejections()` 返回累计数；被重定向的
  导出记 warning 并计入 `rejections().redirects`。
- **REST**：`Client`（`api_key_info()`、`scores_batch()`、外部实验的创建 / 样例 / 上报 / finalize / 查询），
  带重试（幂等请求与带幂等键的写入；`items:batch` 与 `:finalize` 默认重试 8 次，`report_retries=` 可调）、
  `Retry-After`、错误码映射到 `InsightMonError`；`scores_batch()` 中途失败时错误带 `written` 与
  `idempotency_keys`，可以不重复地重试。
- **客户端实验**：`evaluate()` / `aevaluate()`：`task(input)`、`task(input, initial_state)` 或
  `task(input, example)`；输出按平台对 Target 输出的规则包装（非 dict 值 → `{"json": ...}`）；并发、超时（超时的
  任务继续占用并发名额）、trials、客户端评估器（诊断，在条目的 trace 之外运行）与服务端评估器（gating）、每次
  运行一条 trace（根 span `evaluate`，在条目上报后才导出并入平台的实验 trace）、分批上报、等待结论；Ctrl-C /
  取消时（包括上报最后一批时）只上报已完成的条目、不 finalize；平台拒绝客户端分数时不带分数重新上报，同一样例
  的多个 trial 分开发送，请求数有上限；`aevaluate()` 不使用事件循环的默认线程池。
- **回放回执**：`InsightMonASGIMiddleware` / `InsightMonWSGIMiddleware`（可选 `flush_on_traceparent`、
  `flush_when`、`flush_timeout_s`）、`extract_context()`、`receipt()`。
- **示例**：`examples/quickstart.py`、`examples/http_agent.py`（ASGI 服务，可注册为平台的 HTTP Target）、
  `examples/evaluate_example.py`。
- **文档**：中文 README 与英文 README.en.md、[docs/conventions.md](docs/conventions.md)（属性与事件一览，测试对照
  `semconv.py`）、[docs/contract.md](docs/contract.md)、[docs/design.md](docs/design.md)。
- **测试**：单元测试（不访问网络）；README 代码块与示例都在测试中运行；`tests/integration/` 与
  `scripts/integration.sh` 对真实平台实例做端到端验证（会话、内容采集、评估日志、分数、evaluate、回放回执），
  脚本创建的 Key 在结束时吊销。

### 发布前修复

发布前的审查发现并修复了以下问题（每项都有对应的单元测试）：

- **导出**：通用的 `OTEL_EXPORTER_OTLP_*HEADERS`（可能带着别家服务的凭据）不再发给平台；`flush()` /
  `shutdown()` 有真实期限，平台不可达时不再卡住进程退出、`evaluate()` 和回放响应；导出全部失败时 `flush()`
  返回 `False`；重定向的导出不再算成功；各 OpenTelemetry 版本的重试行为统一；`shutdown()` 不在持锁时访问网络；
  一次释放大量暂存 span 时不再被批处理队列丢弃；`discard` 与 `on_end` 竞争时不再导出已丢弃 trace 的 span；
  fork 后重建全部内部锁；SDK 自己的 HTTP 请求不再被 HTTP 客户端插桩记成 agent 的 span；
  `ConcurrentMultiSpanProcessor` 会记 warning（它让打属性与暂存失效）。
- **内容与隐私**：内容属性列表补充了常见插桩（`traceloop.*`、`langfuse.*`、`mlflow.*`、`retrieval.documents.*`
  等），可以用 `content_attributes` 补充；文档写明内容开关管不到的数据（`evaluate()` 的输出、错误消息与分数说明，
  span 的 `exception` 事件），以及插桩的内容日志事件需要 `logger_provider`；配置错误不再回显 base URL（Key 与
  URL 填反时不泄露 Key），拒绝带用户名或密码的 URL。
- **中间件**：WSGI 中间件在服务器拿到最后一块响应之前导出，并在生成器的各步之间保持同一个上下文；
  `flush_when` / `flush_timeout_s` 让响应前的导出只针对回放请求、且有等待上限。
- **evaluate()**：顶层 NaN 与非 dict 的输出按平台规则包装，不再中断运行；含孤立代理字符、引用自身或嵌套过深的
  输出记为 `task_error`；`agent_version` 等版本值去掉首尾空白，避免回执冲突；客户端分数中平台保留的 metadata
  键被去掉；`initial_state` 可以传给任务；客户端评估器的 span 不再混进条目的 trace；超时的任务继续占用
  `max_concurrency` 名额；`aevaluate()` 的超时不再计入排队时间，也不会因默认线程池被占满而挂起；在上报最后一批
  或收尾时再次中断，未上报条目的 span 也不会漏出去；`trials` > 1 时一个 trial 的无效分数不再连累其他 trial；
  重复被拒的分数不再引发整批重发风暴；`items:batch` 与 `:finalize` 的重试预算加大；网关 502 / 504 后提示实验
  可能已创建。
- **集成测试**：脚本创建的 Key 在结束时吊销。

### 已知限制

- 平台使用 SQLite 时，回放回执不能依赖 `flush_on_traceparent`（平台在等待响应期间持有数据库写锁），
  见 README 的“回放回执”（新平台已修复）。
- 平台目前不解析 `insightmon.experiment_id` / `example_key` / `trial_index`，只作为普通属性保存（新平台读取
  `insightmon.experiment_id`）。
- 线程池中需要手动传递上下文（`contextvars.copy_context()`），见 README。
- 平台的 `external_item_invalid` 不带 `trial_index`：`trials` > 1 时 SDK 把同一样例的几个 trial 分开上报以区分
  被拒的条目，多花几个请求（docs/contract.md §14；新平台已修复）。
- span 处理器在线程池中运行（`ConcurrentMultiSpanProcessor`）时，SDK 无法打属性和暂存 span。
- 插桩以日志事件发出的内容只有经 `init(logger_provider=...)` 传入的 provider 才会转发。
- 超时的同步任务无法被终止：它在返回之前一直占用一个并发名额。
