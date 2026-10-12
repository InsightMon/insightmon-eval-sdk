# 平台契约（SDK 视角）

本文从 InsightMon AI Eval 平台的**代码**中整理出 SDK 依赖的全部约定：端点、属性、日志事件、内容采集、
traceparent 回执和 `evaluate()` 流程。平台仓库是 `InsightMon/insightmon-ai-eval`（main 分支加上
`fix/sdk-platform-gaps` 分支的改动，2026-10-12 读取）。下文中的 `file:line` 都相对于平台仓库根目录：标明“新平台”
的内容引用 `fix/sdk-platform-gaps` 的行号，其余引用 main 分支的行号。

**新平台与旧平台**：“新平台”指带数据库迁移 `0009_experiment_idempotency` 的平台（`fix/sdk-platform-gaps`）：
创建实验的幂等键（§6.1）、`GET /api/v1/score-configs`（§5.1）、`items:batch` 的 `trial_index` 与
`invalid_scores` 以及客户端分数 metadata 检查（§6.4）、按 `insightmon.experiment_id` 识别实验 trace（§8）、
第三方内容属性清单（§10.2）、回放先提交再调用 Target（§11）都随它发布。“旧平台”指更早的版本。SDK 不查询平台
版本，而是看平台的回答：带幂等键的创建答 422 时去掉幂等键重发（§6.1），`GET /api/v1/score-configs` 答 404 时
不预先检查分数名（§5.1），`score_invalid` 没有 `invalid_scores` 时按旧的方式逐条处理（§12 第 5 步）。

**代码优先**：平台文档（`docs/dev/m*.md`、`docs/architecture.md`）与代码不一致时以代码为准，
不一致之处列在 [§13 差异](#13-差异)。平台缺口（SDK 只能绕开、需要平台修的问题）列在
[§14](#14-平台缺口)。

---

## 0. 通用约定

| 项目 | 约定 | 代码 |
|---|---|---|
| Base URL | 平台根地址，例如 `https://eval.example.com`；OTLP 路由挂在根上（`/otlp/v1/...`），REST 路由在 `/api/v1` 下 | `backend/app/api/routes/otlp.py:64`、`:84` |
| 认证头 | `Authorization: Bearer imk_...`。scheme 不区分大小写；Key 必须以 `imk_` 开头且未吊销，否则 401 `not_authenticated`。Key 请求忽略 cookie | `backend/app/platform/authz/dependency.py:53-60`、`:95-99` |
| 项目 | SDK 用到的路由全部是 key-only 路由：URL 中没有 `project_id`，项目由 Key 决定 | `docs/dev/m1.md` §5.1 |
| 浏览器会话 | 访问 key-only 路由 → 403 `permission_denied {required}` | `authz/dependency.py` |
| Scope 不足 | 403 `permission_denied {required: "a\|b"}` | `authz/dependency.py` |
| 项目已归档 | 409 `project_archived` | `authz/dependency.py` |
| 错误体 | `{"code": str, "params": object, "message": str}`；422 时 `params = {"errors": [{"loc": [...], "type": str, "msg": str}]}` | `backend/app/platform/errors.py` |
| 未知路由 / 方法 | 404 `not_found {resource: "route"}` / 405 `method_not_allowed` | `platform/errors.py` |
| 服务器异常 | 500 `internal_error` | `platform/errors.py` |
| 请求体上限 | 全局 `BodySizeLimitMiddleware`：POST/PUT/PATCH/DELETE 的请求体（**传输时的字节数**）> 8 MiB → 413 `payload_too_large {max_bytes}`，在认证之前执行 | `backend/app/api/middleware.py:33`、`:84-96` |
| 非有限数 | JSON 请求体里的 `NaN` / `Infinity` / `-Infinity` → 422 `validation_error`（`type = json_invalid`） | `api/middleware.py:135-151` |
| 分页 | `limit` 默认 50、最大 200；`cursor` 为不透明字符串（外部实验的 examples 用偏移游标，坏游标 → 422） | `backend/app/platform/pagination.py` |
| 限流 | 429 `rate_limited {retry_after_s}`，并带 `Retry-After` 头 | `platform/errors.py:246` |

Key 的 scope（`backend/app/platform/api_keys.py` `API_KEY_SCOPES`）：
`trace_ingest`、`ingest`、`score_write`、`experiment_write`、`release_write`、`ci`。
SDK 只用其中四个：`trace_ingest`（OTLP traces / logs）、`score_write`（`scores:batch`）、
`experiment_write`（外部实验）以及任意 scope 都能调的 `GET /api/v1/api-key`。

### 0.1 SDK 的重试分类

| 情况 | 是否重试 | 说明 |
|---|---|---|
| 网络错误、连接超时、读超时 | 幂等请求重试（带幂等键的 `POST /experiments` 也是）；旧平台上不带幂等键的 `POST /experiments` 只在**连接**失败时重试 | 那时读超时说明服务器可能已经创建了实验，见 §6.1 |
| 429 `rate_limited` | 重试，等待 `Retry-After` / `params.retry_after_s` | |
| 429 `judge_budget_exhausted`、`translation_budget_exhausted` | **不重试** | 预算按 UTC 自然日计算，当天重试没有意义 |
| 500 `internal_error`、502、503、504 | 幂等请求重试 | 不带幂等键的 `POST /experiments`（旧平台）不重试；重试用完或不重试时，502、504 与没有平台错误体的 5xx（`unexpected_response`，多半来自网关）说明请求已经到达，错误消息提示实验可能已创建，带幂等键时 `params.idempotency_key` 给出可以重用的键 |
| 400、401、403、404、409、413、415、422 | 不重试 | 409 `invalid_state` 在 `:finalize` 重试时有特殊含义，见 §6.5 |
| 3xx 重定向 | 不跟随，报 `redirect` | 防止 Key 被带到别的主机 |

“幂等请求”：所有 GET、带 `idempotency_key` 的 `POST /experiments`（新平台，§6.1）、`items:batch`（按报告
内容去重，§6.4）、带 `idempotency_key` 的 `scores:batch`（§5）、`:finalize`（§6.5）、OTLP 导出（按 span id /
记录摘要去重，§2、§3）。

重试次数与时间：

| 请求 | 重试 | 退避 |
|---|---|---|
| REST 一般请求 | 最多 `Client(max_retries=3)` 次 | 全抖动，上限从 0.5 秒翻倍到 8 秒；`Retry-After` 优先（最多 60 秒） |
| `items:batch`、`:finalize` | 最多 `Client(report_retries=8)` 次：长时间运行的 `evaluate()` 不能因为平台短暂不可用而停下 | 同上，退避上限合计约 40 秒 |
| OTLP 导出（traces、logs） | 429、500、502、503、504 与网络错误，在每次导出 10 秒的期限内重试；`shutdown()` 的期限过后不再发送 | 全抖动，上限从 0.5 秒翻倍到 4 秒；`Retry-After` 优先。3xx 不跟随：每个地址记一次 WARNING，计入 `rejections().redirects`，导出失败 |

OTLP 导出用 SDK 自己的 httpx 导出器，不用 OpenTelemetry 的 OTLP/HTTP 导出器：后者在 1.35 到 1.45 之间重试的
状态码各不相同，会跟随或静默接受重定向，并把通用的 `OTEL_EXPORTER_OTLP_*HEADERS` 合并进请求（§14）。SDK 的
导出器只发送 `Authorization`、`Content-Type`、`Content-Encoding`、`User-Agent` 四个请求头。

---

## 1. 端点总表

| 方法 | 路径 | Scope | 成功响应 | 用途 |
|---|---|---|---|---|
| POST | `/otlp/v1/traces` | `trace_ingest` | 200 OTLP `ExportTraceServiceResponse` | span 上报 |
| POST | `/otlp/v1/logs` | `trace_ingest` | 200 OTLP `ExportLogsServiceResponse` | 评估结果、消息内容事件 |
| GET | `/api/v1/api-key` | 任意 | 200 `ApiKeyInfo` | 自检：Key 所属项目与 scope |
| POST | `/api/v1/scores:batch` | `score_write` | 200 `{items: [...]}` | 直接写分数（终端用户反馈、业务结果、导入） |
| GET | `/api/v1/score-configs` | `score_write`、`experiment_write` 或 `trace_ingest` | 200 `{items: [ScoreConfig]}` | 已发布的评分定义（新平台，§5.1）：预先检查分数名 |
| POST | `/api/v1/experiments` | `experiment_write` | 201 `{experiment, examples}` | 创建外部实验 |
| GET | `/api/v1/experiments/{id}` | `experiment_write` | 200 `Experiment` | 轮询状态与汇总 |
| GET | `/api/v1/experiments/{id}/examples` | `experiment_write` | 200 `{items, next_cursor}` | 读取冻结的样例 |
| POST | `/api/v1/experiments/{id}/items:batch` | `experiment_write` | 200 `{accepted, duplicates, item_ids}` | 上报客户端输出 |
| POST | `/api/v1/experiments/{id}:finalize` | `experiment_write` | 202 `{experiment, job_id}` | 结束上报并触发服务端评估 |

路由代码：`backend/app/api/routes/otlp.py`、`api_key_info.py`、`ingest.py:92`、`key_score_configs.py:41-58`、
`external_experiments.py:79-160`。

---

## 2. POST /otlp/v1/traces

- **请求**：OTLP `ExportTraceServiceRequest`。
  - `Content-Type`：`application/x-protobuf`、`application/protobuf`（二者等价）或 `application/json`
    （OTLP/JSON，trace / span id 为 hex）；其他 → 415 `otlp_content_type_unsupported {content_type}`
    （`backend/app/telemetry/otlp.py:57-58`）。
  - `Content-Encoding`：可省略，或 `gzip`、`x-gzip`、`deflate`；其他编码 → 400
    （`backend/app/api/body_limits.py:49`）。
- **上限**：
  - 压缩后的请求体 > 8 MiB → 413（全局中间件）；
  - 解压后的请求体 > 8 MiB → 413 `payload_too_large {max_bytes: 8388608}`（`body_limits.py:34`、`otlp.py:56`）；
  - OTLP/JSON 嵌套深度 > 128 → 400 `otlp_payload_invalid {reason}`（`otlp.py:236`）；
  - 无法解码 → 400 `otlp_payload_invalid {reason}`。
- **响应**：200；全部接受时为空消息，否则为 `partial_success`
  （`rejectedSpans` + `errorMessage`；protobuf 请求得到 protobuf 响应，JSON 请求得到 JSON 响应）。
- **幂等**：按 `(project, otel trace id, otel span id)` 去重；重复的 span 计为**接受**
  （`backend/app/telemetry/ingest.py:688-690`）。导出器重试是安全的。
- **拒绝**：已导入（`threads:import`）的 trace 不再接受新 span，以 partial success 返回。
- **SDK 注意**：OpenTelemetry 自带的 Python OTLP 导出器在收到 partial success 时不报错，被拒绝的 span 无人察觉；
  SDK 自己的导出器解析 partial success，每种原因记一条 WARNING 并计入 `rejections()`（§0.1、§14）。

---

## 3. POST /otlp/v1/logs

- 请求格式、`Content-Type`、`Content-Encoding`、上限与 §2 相同（`backend/app/api/routes/otlp.py:84-110`）。
- 一个请求最多处理 `MAX_LOG_RECORDS = 10000` 条记录，多出的计为拒绝（`too_many_records`）
  （`backend/app/telemetry/otlp_logs.py:112`）。
- trace id 不是 16 字节（或为空）、span id 不是 8 字节（或为空）的记录单独拒绝（`otlp_logs.py:208-213`）。
- **响应**：200；`partialSuccess {rejectedLogRecords, errorMessage}`，`errorMessage` 是前 5 个不同的拒绝原因，
  用 `; ` 连接（`otlp_logs.py:262-286`）。
- 事件内容见 [§9](#9-logs-事件契约)。

---

## 4. GET /api/v1/api-key

- 任意 scope 都可以调用（`backend/app/api/routes/api_key_info.py:20`）。
- 响应：`{"id": str, "name": str, "prefix": str, "project_id": str, "scopes": [str]}`。
- SDK 用它做 `Client.api_key_info()`，以及在 `evaluate()` 出错时给出“缺少 scope”的提示。

---

## 5. POST /api/v1/scores:batch

Scope `score_write`（`backend/app/api/routes/ingest.py:35`、`:92`）。

- **请求**：`{"scores": [ScoreInput, ...]}`，1–500 条；超过 500 → 400 `batch_too_large {max: 500}`；
  空数组 → 422（`backend/app/api/schemas/scores.py:53`、`backend/app/eval/scores.py:82`）。
- **ScoreInput**（`extra = forbid`，未知字段 → 422；`eval/scores.py:271-289`）：

| 字段 | 类型 | 约束 |
|---|---|---|
| `idempotency_key` | str \| null | 1–128；缺省时服务器生成（**此时重试会重复写入**，SDK 必须自己填） |
| `name` | str | 1–64；必须是本项目已发布的 ScoreConfig 名称 |
| `subject` | Subject | 见下表 |
| `source` | `"end_user"` \| `"outcome"` \| `"import"` | Key 只能写这三种，否则 400 `source_not_allowed_for_key` |
| `status` | `"scored"` \| `"unscored"` \| `"not_applicable"` | 默认 `scored` |
| `unscored_reason` | `"evaluator_error"` \| `"missing_input"` \| `"judge_unknown"` \| `"timeout"` \| null | |
| `value` | JSON | 按 config 的 `data_type`：boolean → bool；numeric → 有限数且在 min/max 内；categorical → ≤ 200 的字符串且在 labels 中；text → str / dict / list |
| `explanation` | str \| null | ≤ 20000 |
| `agent_version` | str \| null | ≤ 200 |
| `environment` | str \| null | ≤ 100 |
| `metadata` | object | 保留键 → 400 `metadata_reserved` |
| `supersedes_id` | str \| null | ≤ 36 |
| `edit_reason` | str \| null | ≤ 2000 |

- **Subject**（`eval/scores.py:119-133`，id 为 1–64 字符，hex id 大小写均可）：

| `type` | 形状 A（内部 id） | 形状 B（外部 id） |
|---|---|---|
| `thread` | `{thread_id}` | `{thread_key}`（≤ 200） |
| `trace` | `{trace_id, thread_id?}` | `{otel_trace_id}`（32 hex） |
| `span` | `{span_id, trace_id?, thread_id?}` | `{otel_trace_id, otel_span_id}`（32 + 16 hex） |
| `experiment_item` | `{experiment_item_id, trace_id?}` | — |

- **响应**：`{"items": [{"index": int, "id": str, "idempotency_key": str, "created": bool}]}`。
- **原子性**：任何一条无效，整批拒绝，不写入任何数据。逐条校验错误 → 422
  （`loc = ["body", "scores", index, ...]`，`backend/app/api/routes/ingest.py:69-90`）；
  业务校验错误 → 400，`params.index` 指向出错的条目。
- **幂等**：`idempotency_key` 已存在时返回已有行，`created = false`。
- **错误码**：`score_config_not_found` 400、`score_config_not_published` 409、`subject_level_not_allowed` 400、
  `score_value_invalid {name, reason, index}` 400、`source_not_allowed_for_key` 400、`adjudicated_reserved` 400、
  `metadata_reserved` 400、`subject_not_found` 400、`batch_too_large` 400、`validation_error` 422。
- **保留的 metadata 键**（`backend/app/eval/scores.py:87-96`、`:431-441`）：`adjudicated`（→ `adjudicated_reserved`）、
  `accepted_by`（→ `source_not_allowed_for_key`）、`mt_assisted`、`self_review`、`verification_id`
  （→ `metadata_reserved {key}`）。
- **SDK 规则**（`Client.scores_batch`）：
  - 没有 `idempotency_key` 的分数由 SDK 生成（随机 UUID），同一请求的重试发送同一个键；
  - 保留的 metadata 键在本地报 `ValueError`，不发请求；
  - 超过 500 条时按顺序分批；某一批失败时，之前的批已经写入（每批各自原子），抛出的 `InsightMonError` 的
    `params` 另带 `written`（之前写入的条数）和 `idempotency_keys`（每条分数的键，按输入顺序），平台给的
    `index` 改写为在整个输入中的位置。调用方把这些键填回分数后重新调用，已写入的分数返回 `created = false`。

### 5.1 GET /api/v1/score-configs（新平台）

Key-only，scope `score_write`、`experiment_write` 或 `trace_ingest` 之一（写分数的三条路径，
`backend/app/api/routes/key_score_configs.py:25-58`）。

- **响应**：`{"items": [{"name": str, "data_type": "boolean" | "categorical" | "numeric" | "text",
  "labels": [str] | null, "min": float | null, "max": float | null, "subject_levels": [str],
  "allow_unknown": bool}]}`：项目里**已发布**的评分定义，按名称排序（草稿与已归档的不列出）。
  `subject_levels` 取自 `span`、`trace`、`thread`、`experiment_item`。
- 旧平台没有这个路由 → 404 `not_found {resource: "route"}`；Key 没有上面的 scope → 403 `permission_denied`。
- **SDK 规则**（`Client.score_configs()`，`published_score_names()`）：只用来提前发现名称错误，从不因此失败：
  任何错误（404、403、5xx、意外的响应体）都当作“不知道”，名称照常交给平台检查。
  - `evaluate()` 有客户端评估器时每次运行读一次，只保留允许 `experiment_item` 层级的名称（§12 第 3 步）；
  - `log_evaluation()` 在 SDK 导出到平台时，每个运行时在后台线程读一次（调用从不等待它），对不允许 `span`、
    `trace`、`thread` 任一层级的名称各记一条 WARNING；记录照常发送（§9.1）。

---

## 6. 外部实验（`executor = external`）

全部路由都要求 scope `experiment_write`（`backend/app/api/routes/external_experiments.py:36`）。
Key 只能看到自己创建的实验；访问别的实验 → 404 `not_found {resource: "experiment"}`。

### 6.1 POST /api/v1/experiments → 201

请求 `ExternalExperimentIn`（`extra = forbid`，`backend/app/eval/external.py:83-97`）：

| 字段 | 类型 | 约束 / 默认 |
|---|---|---|
| `name` | str | 1–200 |
| `dataset_id` | str | ≤ 36 |
| `dataset_version` | int \| null | ≥ 1；缺省为当前版本；不存在 → 400 `plan_invalid {field: "dataset_version", reason: "not_found"}` |
| `example_keys` | [str] \| null | ≤ 5000；缺省 = 该版本所有 `ready` 且 `split = dev` 的样例 |
| `trials` | int | 1–10，默认 1 |
| `requested` | `{agent_version?: str ≤ 200, components: [{type ≤ 64, name ≤ 200, version? ≤ 200}] ≤ 100}` | 组件**不接受 `content_hash`**（`extra = forbid` → 422）；type 必须是项目配置的组件类型，否则 `plan_invalid component_type_unknown`；重复 → `plan_invalid duplicate` |
| `evaluators` | `[{evaluator_version_id: str 1–36, role: "gating" \| "diagnostic" = "gating"}]` | ≤ 20；未发布 → 409 `evaluator_not_published` |
| `triggered_by` | `"sdk"` \| `"ci"` | 默认 `sdk` |
| `metadata` | object \| null | |
| `idempotency_key` | str \| null | 1–128（新平台；旧平台 → 422，见下文“幂等”） |

响应：`{"experiment": Experiment, "examples": "/api/v1/experiments/{id}/examples"}`，实验立即为 `running`，
每个 (example, trial) 一条 `pending` 条目。

错误：
- 未知 key → 400 `examples_not_found {example_keys}`；
- 包含 holdout → 400 `plan_invalid {field: "example_keys", reason: "holdout_requires_final"}`；
- 未就绪或选中为空 → 409 `examples_not_ready {example_keys}`；
- 限流 → 429 `rate_limited`：每个调用方、每个项目每分钟 `rescore_calls_per_minute`（默认 20）次，
  **每次新建都计数**，幂等重放不计（`external_experiments.py:84-95`）；
- 评审调用预算 → 429 `judge_budget_exhausted {calls, remaining, max_calls}`：按项目、按 UTC 日计算，
  默认 50000（`backend/app/api/judge_budget.py`），不可重试；幂等重放同样不计；
- 同一个幂等键配不同的内容，或项目里另一个 Key 用过这个键 → 409 `idempotency_key_conflict {idempotency_key}`。

**幂等**（新平台，`external.py:193-224`、`:282-293`，迁移 `0009_experiment_idempotency`）：带 `idempotency_key`
的请求，同一个 Key 再次发送同一个键与同样的内容（`request_hash`：去掉幂等键，`example_keys` 按集合比较）→
201，返回第一次创建的实验；并发的同键请求由唯一索引保证只建一个。不带幂等键时每次请求都新建一个实验。
旧平台没有这个字段：`extra = forbid` 使它答 422 `validation_error`，`errors` 中一项
`{loc: ["body", "idempotency_key"], type: "extra_forbidden"}`，什么也不创建。

**SDK 规则**（`Client.create_experiment`）：每次调用带一个幂等键（不给时随机生成；`evaluate()` 每次运行生成
一个），按幂等请求重试（§0.1）。收到上面那种 422 时去掉幂等键重发一次，并记住这个平台不认幂等键：此后同一个
`Client` 不再带键，只在连接失败与 429 `rate_limited` 时重试；读超时、网关 502 / 504 与没有平台错误体的 5xx
说明请求已经到达，错误消息提示实验可能已创建。带键的请求重试用完仍是这些情况时，错误的
`params.idempotency_key` 给出发送过的键：用同一个键和同样的参数再调用一次就能拿到那个实验。

### 6.2 GET /api/v1/experiments/{id}

响应 `Experiment`（`backend/app/api/schemas/eval_assets.py:352-397`）：
`id, project_id, name, source_kind, dataset_id, dataset_version, example_keys, examples_hash, source_ref,
executor, prefix_policy, trials, target_id, requested, environment_id, evaluators, config_snapshot,
metadata, triggered_by, verification_id, status, summary, started_at, finished_at, created_by, created_at,
updated_at, source_experiment_id, api_key_id, finalized_at`。

- 对 Key 隐藏评估器配置：`config_snapshot.evaluators[*]` 不含 `config`，带 `config_withheld: true`。
- `status`：`pending`、`running`、`completed`、`failed`、`cancelled`。`failed` 表示所有主条目都以
  `infra_error` 结束。
- `summary`（`backend/app/eval/evaluation.py:152-163` 及其后）：
  `counts {ok, task_error, timeout, infra_error}`、`n_examples`、`n_trials`、`conclusions`、`pass_rate`、
  `by_evaluator`、`cost`、`latency_ms {p50, p95}`、`ignored_verifier_scores`、`evaluated`，
  以及 `trials`、`client_scores {n, names}`。
- 实验页面 URL：`<base>/projects/<project_id>/experiments/<id>`（前端路由）。

### 6.3 GET /api/v1/experiments/{id}/examples

- 查询参数：`limit`（1–200，默认 50）、`cursor`（偏移游标）。
- 响应：`{"items": [Example], "next_cursor": str | null}`（`backend/app/eval/external.py:277-308`）。
- `Example`：`{example_key: str, example_id: str, input: JSON, initial_state: object, reference: JSON | null,
  language: str, metadata: object, trials: int}`。
- 样例来自创建时冻结的数据集版本；行已不存在的 key 会被跳过。

### 6.4 POST /api/v1/experiments/{id}/items:batch

请求 `{"items": [ReportedItem]}`，1–200 条（`external.py:99-160`）：

| 字段 | 类型 | 约束 |
|---|---|---|
| `example_key` | str | 1–100，必须属于本实验 |
| `trial_index` | int | 0 ≤ i < `trials` |
| `status` | `"ok"` \| `"task_error"` \| `"timeout"` | 默认 `ok` |
| `output` | JSON | `ok` 时必填；规范 JSON 字节数 > 256 KiB → **422**（字段校验） |
| `error` | `{code: str 1–64, message: str ≤ 4000}` \| null | |
| `latency_ms` | int ≥ 0 \| null | |
| `tokens` | `{prompt?: int ≥ 0, completion?: int ≥ 0}` \| null | |
| `cost` | float ≥ 0 \| null | |
| `otel_trace_id` | str \| null | `^[0-9a-f]{32}$`（只接受小写） |
| `receipt` | `{agent_version?: str ≤ 200, components: [{type, name, version?}] ≤ 100}` \| null | 不接受 `content_hash` |
| `scores` | `[{name: str ≤ 200, value: JSON, explanation?: str ≤ 20000, metadata?: object}]` | ≤ 20；`name` 必须**完全等于**一个已发布、允许 `experiment_item` 层级的 ScoreConfig 名称，同一条目内不重复，且值合法；`metadata` 的保留键见下文 |

- **响应**：`{"accepted": int, "duplicates": int, "item_ids": [str]}`。`item_ids` 先列新接受的，再列重复的，
  **与请求顺序不对应**。
- **原子性**：整批先校验；第一个问题 → 400 `external_item_invalid {example_key, trial_index, reason}`，
  不写入任何数据（新平台，`external.py:174-183`）。旧平台的 `params` 只有 `{example_key, reason}`：`trials` > 1 时
  同一样例的几个条目无法区分。
  `reason`：`unknown_example`、`trial_out_of_range`、`duplicate_in_batch`、`already_reported`、
  `output_missing`、`score_invalid`。
- **`score_invalid`**（新平台，`external.py:441-491`）：`params.invalid_scores = [{example_key, trial_index,
  name}]` 列出本批所有**未上报**条目中每个无效的分数（同名重复、没有已发布且允许 `experiment_item` 层级的
  config、值不合法）；已上报的重复条目不在其中。客户端去掉这些分数后重发一次即可。
- **客户端分数的 metadata**（新平台，`external.py:563-573`，与 `scores:batch` 相同的规则，§5）：`adjudicated` →
  400 `adjudicated_reserved`，`accepted_by` → 400 `source_not_allowed_for_key {source: "import"}`，
  `mt_assisted`、`self_review`、`verification_id` → 400 `metadata_reserved {key}`；`params` 都另带
  `example_key, trial_index, name`。旧平台原样写入这些键。SDK 在上报前去掉它们（§12 第 4 步）。
- **幂等**：对已上报条目，按报告的规范哈希比较（`report_hash`，`model_dump(exclude_none)` 的内容哈希）：
  相同 → 计为 `duplicates`；不同 → `already_reported`。**重试时必须发送与第一次完全相同的内容**。
- 实验不再开放（已 finalize 或状态不是 running）→ 409 `invalid_state {resource: "experiment",
  status: "finalized" | <status>, action: "report"}`。
- `otel_trace_id`：不存在 → 平台预建 `origin = experiment` 的 trace，之后到达的 span 并入其中；已存在 →
  关联到条目（`external.py:494-516`）。新平台上两种到达顺序结果相同：span 先到且带着本实验的
  `insightmon.experiment_id` 时，入库时已把 trace 记为本实验的流量（§8），`items:batch` 认领它；先到的 span
  不带该属性（生产 trace）时照原样关联，带属性的 span 之后到达时入库再挂上条目。`items:batch` 关联 trace 前
  先取项目的入库锁（`lock_project_ingest`，`external.py:582`），`create_experiment_trace` 在保存点中处理并发的
  同一 trace 写入（`backend/app/telemetry/ingest.py:564-612`），不会因此 500。
- 客户端分数以 `source = import`、`metadata.client = true` 写入，只作诊断，不参与结论；
  幂等键 `client:{item_id}:{name}`。
- **上限**：**8 MiB**（全局中间件）。旧平台的路由另声明了约 58 MiB 的上限，实际同样是 8 MiB；新平台去掉了它。

### 6.5 POST /api/v1/experiments/{id}:finalize → 202

- 请求体可省略（或 `{}`）。
- 响应 `{"experiment": Experiment, "job_id": str}`。
- 效果：仍为 `pending` 的条目 → `infra_error`（`termination_reason = "not_reported"`）；设置 `finalized_at`；
  排入一个 `evaluate` 任务，任务运行服务端评估器、写汇总并把实验置为 `completed`（或 `failed`）。
- 再次调用 → 409 `invalid_state {status: "finalized", action: "finalize"}`。SDK 在**重试**时收到它，
  说明第一次已经成功，视为完成。
- 自动结束：创建 24 小时后仍未 finalize 的实验由 sweep 自动结束（`external.py:66`）。

---

## 7. 错误码总表

| code | HTTP | params | SDK 是否重试 |
|---|---|---|---|
| `not_authenticated` | 401 | — | 否 |
| `permission_denied` | 403 | `required` | 否 |
| `not_found` | 404 | `resource` | 否 |
| `method_not_allowed` | 405 | — | 否 |
| `project_archived` | 409 | — | 否 |
| `invalid_state` | 409 | `resource, status, action` | 否（`:finalize` 重试时见 §6.5） |
| `payload_too_large` | 413 | `max_bytes` | 否（SDK 先拆分请求） |
| `otlp_content_type_unsupported` | 415 | `content_type` | 否 |
| `otlp_payload_invalid` | 400 | `reason` | 否 |
| `validation_error` | 422 | `errors` | 否（旧平台对 `POST /experiments` 的 `idempotency_key` 答 422：SDK 去掉幂等键重发，§6.1） |
| `batch_too_large` | 400 | `max` | 否（SDK 先拆分请求） |
| `score_config_not_found` | 400 | `name` | 否 |
| `score_config_not_published` | 409 | `name` | 否 |
| `subject_level_not_allowed` | 400 | | 否 |
| `score_value_invalid` | 400 | `name, reason, index` | 否 |
| `source_not_allowed_for_key` | 400 | `source`；`items:batch` 另带 `example_key, trial_index, name` | 否 |
| `adjudicated_reserved` | 400 | `items:batch`：`example_key, trial_index, name` | 否 |
| `metadata_reserved` | 400 | `key`；`items:batch` 另带 `example_key, trial_index, name` | 否 |
| `subject_not_found` | 400 | | 否 |
| `plan_invalid` | 400 | `field, reason` | 否 |
| `examples_not_found` | 400 | `example_keys` | 否 |
| `examples_not_ready` | 409 | `example_keys` | 否 |
| `evaluator_not_published` | 409 | | 否 |
| `external_item_invalid` | 400 | `example_key, trial_index, reason`；`score_invalid` 另有 `invalid_scores`（旧平台：只有 `example_key, reason`） | 否（`score_invalid` 时 SDK 去掉列出的客户端分数后重发，旧平台上去掉被拒条目的全部客户端分数，见 §12 第 5 步） |
| `idempotency_key_conflict` | 409 | `idempotency_key` | 否（新平台：同一个幂等键配了不同的内容，§6.1） |
| `rate_limited` | 429 | `retry_after_s` | **是**（按 `Retry-After`） |
| `judge_budget_exhausted` | 429 | `calls, remaining, max_calls` | 否 |
| `translation_budget_exhausted` | 429 | `max_chars` | 否 |
| `internal_error` | 500 | — | 幂等请求是（包括带幂等键的 `POST /experiments`） |

SDK 自己的错误码（`InsightMonError.code`，`status = None`）：`network_error`、`timeout`、`redirect`、
`unexpected_response`（非 JSON 或缺少字段）、`config_error`（缺少 base URL / Key、Key 格式不对）。

---

## 8. Span 属性表

平台把 **resource 属性与 span 属性合并**（span 优先，下称“合并属性”）后再解析
（`backend/app/telemetry/otlp.py:509-606`）。“自身属性”指 span 自己的属性，不含 resource。

| 属性 | 值 | 类型 | 放在哪里 | 平台用途 / 代码 |
|---|---|---|---|---|
| `gen_ai.conversation.id` | 会话键 ≤ 200 | string | 本轮 trace 的任一 span（SDK：每个 span） | 会话键第一优先；> 200 被忽略（`otlp.py:563-567`） |
| `session.id` | 会话键 ≤ 200 | string | 同上 | 会话键第二优先 |
| `insightmon.thread_id` | 会话键 ≤ 200 | string | 同上 | 会话键第三优先 |
| `insightmon.thread.end` | `true` | bool（也接受 `"true"`/`"1"`/`"yes"`、整数 1） | 最后一轮的任一 span（合并属性，**不要放在 resource**） | 关闭会话，`close_reason = explicit`（`otlp.py:394-402`、`:603`） |
| `insightmon.eval_root` | `true` | bool（同上） | 一个 span（取最早的） | trace 的评估根（`otlp.py:604`） |
| `gen_ai.agent.name` | ≤ 200 | string | 根 span（SDK：每个 span） | 顺序：根 → `invoke_agent` span → 其他 span → resource `service.name`（`ingest.py:233-243`） |
| `gen_ai.agent.version` | ≤ 200 | string | 同上 | 回执 `trace_attr` 的 agent 版本；缺失时退回 resource `service.version` |
| `insightmon.component.<type>.<name>.version` | ≤ 200 | string | 本地根 span 与轮次 span | 组件版本；`<type>` 是第一个 `.` 之前的部分，`<name>` 可以含 `.`（`otlp.py:441-461`）；按 (type, name) 取第一个，根优先 |
| `insightmon.component.<type>.<name>.content_hash` | ≤ 200 | string | 同上 | 组件内容哈希；参与回执冲突检测 |
| `insightmon.language` | `zh`/`en`/`ja`/`mixed`/`und` | string | 任一 span | 声明语言，去空格并转小写后必须在集合中，否则忽略（`language.py:84-90`） |
| `insightmon.metadata.<k>` | 任意 | any | 任一 span | 会话 `metadata[k]`，按 span 开始时间后写覆盖 |
| `enduser.id` | ≤ 128 | string | 任一 span | 会话 `user_ref`（只写一次） |
| `deployment.environment.name` | ≤ 100 | string | resource（推荐） | 环境；其次 `deployment.environment` |
| `gen_ai.operation.name` | `chat`、`text_completion`、`generate_content`、`embeddings`、`execute_tool`、`invoke_agent`、`create_agent` | string | 每个 span | span 类型；其他值视为空（`otlp.py:60-70`） |
| `gen_ai.request.model` / `gen_ai.response.model` | ≤ 200 | string | 模型调用 span | `spans.model` |
| `gen_ai.tool.name` / `gen_ai.tool.call.id` | ≤ 200 | string | 工具 span | 工具名 / 调用 id |
| `gen_ai.usage.input_tokens` / `output_tokens` | ≥ 0 | int | 模型调用 span | token；退回 `prompt_tokens` / `completion_tokens`（`otlp.py:532-538`） |
| `insightmon.cost` | 有限数 | double | 任一 span | 成本 |
| `error.type` | ≤ 200 | string | 出错的 span | 错误类型；缺失时取 `exception` 事件的 `exception.type` |
| `gen_ai.response.id` | ≤ 200 | string | 产生该响应的 span（**自身属性**，resource 上的不算） | 日志按 response id 关联（`otlp.py:586`） |
| `gen_ai.system_instructions` | JSON 字符串或数组 | string / any | 模型调用 span | 内容 → `input.system` |
| `gen_ai.input.messages` | JSON 消息数组 | string / any | 模型调用 / 轮次 span | 内容 → `input.messages` |
| `gen_ai.output.messages` | JSON 消息数组 | string / any | 同上 | 内容 → `output.messages` |
| `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` | JSON | string / any | 工具 span | 内容 → `input.arguments` / `output.result` |

消息形状：`{role, content}`，或 OTel 形状 `{role, parts: [{type: "text", content}]}`
（`backend/app/telemetry/language.py`）。内容属性也可以放在 span 事件
`gen_ai.client.inference.operation.details` 的属性上（`otlp.py:498-506`）。

**服务器写入、SDK 不得设置的属性**：`insightmon.redaction`、`insightmon.dropped_attributes`、
`insightmon.content_events`。

**SDK 写入的实验属性**：`insightmon.experiment_id`、`insightmon.example_key`、`insightmon.trial_index`
（`evaluate()` 每次运行的根 span）。后两个只作为普通属性保存，便于在 UI 中筛选。新平台读取
`insightmon.experiment_id`（`backend/app/telemetry/ingest.py:780-810`、`backend/app/eval/external.py:339-370`）：
trace 已入库的 span（根优先）带着一个仍在运行、由**导入这些 span 的同一个 Key** 创建的外部实验的 id，而且 trace
仍是 `origin = otlp`、没有加入会话、在线规则还没派发时，trace 变为 `origin = experiment`（已有条目关联它时同时
挂上条目，否则等 `items:batch`，§6.4）。这只看已经到达的 span：带属性的 span 在第一批里，或者客户端在条目上报前
暂存 span（SDK 的做法，§12 第 4、6 步）时，trace 才不会先作为生产流量出现。其他 id（未知、别的 Key 的、已
finalize 的、回放实验的）被忽略，属性只是普通属性，发送方无从得知 id 是否存在。旧平台不解析这三个属性。

**会话归属规则**（`backend/app/telemetry/ingest.py:192-200`、`:699-730`）：
- 会话键按**属性优先级**在 trace 的所有 span 中查找（先在所有 span 中找 `gen_ai.conversation.id`，
  再找 `session.id`……），span 按“根优先、再按开始时间”排序；
- 没有会话键的 trace 单独存在，不会凭空生成会话；
- `origin = experiment` 的 trace 永远不加入会话（`ingest.py:684-685`）；
- 已关闭的会话不会重新打开，之后到达的 trace 标记 `arrived_after_eval`。

**存储规则**：
- span 的 `attributes` = 合并属性去掉映射到列的键（`SPAN_MAPPED`，`otlp.py:91-105`、`:578`）。
- `traces.attributes` = 根 span 的属性再去掉 trace 级映射键与组件键（`TRACE_MAPPED`，`otlp.py:108-121`）。
- span 事件不保存（只用来取内容和 `exception.type`）。
- 单个 span 的 `attributes` > 64 KiB 时，从最大的键开始丢弃，丢弃的键名列在 `insightmon.dropped_attributes`
  （`backend/app/telemetry/content.py`）。

---

## 9. Logs 事件契约

事件名取 LogRecord 的 `event_name` 字段，没有时取 `event.name` 属性（`otlp_logs.py:216-221`）。
SDK 两处都写，兼容只认属性的转发链路。

### 9.1 `gen_ai.evaluation.result`（评估结果 → 分数）

| 属性 | 类型 | 规则（`backend/app/eval/otlp_scores.py`） |
|---|---|---|
| `gen_ai.evaluation.name` | string | 转小写，`[^a-z0-9_]` → `_`，去掉首尾 `_`，截到 64；必须是已发布的 ScoreConfig，否则拒绝 `score_config_not_found`（SDK 在新平台上预先检查，§5.1） |
| `gen_ai.evaluation.score.value` | double | numeric：只看 value；boolean：没有 label 时 value 必须是 0 或 1 |
| `gen_ai.evaluation.score.label` | string | boolean：`true/pass/passed/yes/1` 或 `false/fail/failed/no/0`（不区分大小写，**label 优先于 value**）；categorical / text：取 label |
| `gen_ai.evaluation.explanation` | string | 截到 4000 字符 |
| `error.type` | string | 有值时写成 `unscored`、`evaluator_error`，`explanation = error.type` |
| `gen_ai.response.id` | string | 没有 span 上下文时用它关联；写入 `metadata.otel.response_id` |

- **关联**（`otlp_logs.py:294-344`）：
  1. 记录带 span 上下文（trace id + span id）→ 找这个 span；
  2. 否则用 `gen_ai.response.id` 找 `spans.response_id`（最新的 span）；
  3. 评估结果的 span 上下文指向一个不存在的 span（例如在评估器自己的 trace 里上报）→ 也退回 response id；
  4. 两者都没有 → 拒绝 `no_correlation`。
- **晚到**：关联的 span 还没入库时，记录在 30 s、120 s、600 s 后重试，之后丢弃；一个请求里待重试的记录
  规范 JSON 总量 > 2 MiB → 拒绝 `pending_overflow`。
- **主体**：config 允许 span 级时为该 span，否则为其 trace，再否则为会话（都不允许 → `subject_level_not_allowed`）。
- **幂等键**：`otlp_log:` + sha256（规范 JSON `{trace_id, span_id, time_unix_nano, event_name,
  记录自身属性}`）的前 64 位 hex。导出器重发同一条记录不会重复写分数。
- **SDK 规则**：
  - 只给 `response_id` 时，SDK 用**空上下文**发出记录（不能沾上当前 span，否则平台按 span 关联）；
  - value 为 bool 时发送 `1.0` / `0.0`；
  - value、label、error_type 至少有一个，否则 SDK 直接报错（平台会以 `value_missing` 拒绝）。

### 9.2 消息内容事件

`gen_ai.system.message`（body `{content}`）、`gen_ai.user.message` / `gen_ai.assistant.message` /
`gen_ai.tool.message`（body `{content, role?, tool_calls?, id?}`）、`gen_ai.choice`
（body `{index, finish_reason, message}`）、`gen_ai.client.inference.operation.details`（属性与 span 内容属性相同）。

- 带 span 上下文的内容事件只按 span 上下文关联，不退回 response id。
- span 的内容已来自属性（`content_source = attributes`），或项目为 `metadata_only` 时，事件被忽略，
  但仍计为接受。
- 按（span、事件名、时间、内容）摘要去重；晚到的事件按时间插入。

### 9.3 其他事件

其他事件名一律忽略，计为接受。SDK 的日志处理器只转发 `gen_ai.evaluation.result`，开启内容采集时
再转发 §9.2 的事件；应用自己的普通日志不会发到平台。

---

## 10. 内容采集

### 10.1 SDK 选项

`init(capture_content=False)`（默认）：
- SDK 的导出器在导出前从 span 和 span 事件上删除 `semconv.is_content_attribute()` 认得的属性：五个标准内容属性
  （`gen_ai.system_instructions`、`gen_ai.input.messages`、`gen_ai.output.messages`、`gen_ai.tool.call.arguments`、
  `gen_ai.tool.call.result`），以及常见第三方插桩的内容属性（`input.value`、`output.value`、
  `llm.prompt_template.variables`、`traceloop.entity.input/output`、`langfuse.observation.input/output`、
  `langfuse.trace.input/output`、`mlflow.spanInputs/spanOutputs`、`mlflow.chat.messages`、`all_messages_events`、
  Vercel AI SDK 的 `ai.prompt`、`ai.prompt.messages`、`ai.response.text/object/toolCalls`、`ai.toolCall.args/result`、
  `ai.value(s)`、`ai.embedding(s)`，pydantic-ai 与 Logfire 的 `pydantic_ai.all_messages`、`final_result`、
  `tool_arguments`、`tool_response`、`request_data`、`response_data`，Google ADK 的
  `gcp.vertex.agent.llm_request/llm_response/tool_call_args/tool_response`，前缀 `gen_ai.prompt`、
  `gen_ai.completion`、`llm.input_messages`、`llm.output_messages`、`llm.prompts`、`retrieval.documents.`、
  `reranker.input_documents.`、`reranker.output_documents.`、`embedding.embeddings.`；与新平台的内容属性清单
  相同，§10.2；完整列表见 [conventions.md](conventions.md#内容)），再加上 `init(content_attributes=[...])` 给出的
  属性名与 `*` 结尾的前缀；
  并删除 `gen_ai.client.inference.operation.details` 等内容事件；
- 日志处理器不转发 §9.2 的内容事件；
- `record_io()` 什么也不做并返回 `False`。

这份列表不可能覆盖所有插桩：列表之外的属性照常导出，平台在 `metadata_only` 下也会保存它们（§10.2）。
内容开关也不管下面这些数据：
- `evaluate()` 上报的任务输出、`error.message`（任务异常的消息）、客户端分数的 `explanation` 与 `metadata`
  （平台评估需要它们）；
- span 的 `exception` 事件（`turn()` 与 OpenTelemetry 的 `start_as_current_span()` 默认记录）里的
  `exception.message`、`exception.stacktrace`；
- 会话键、`user_ref`、`insightmon.metadata.*`、评估日志的 `explanation`。

只有 SDK 自己的导出器会删除内容属性：经 Collector 或应用自己的导出器发送时，内容由那条管道负责。

`init(capture_content=True)`：保留上述内容；`record_io()` 写入 OTel 形状的消息 JSON；
单个字段先在客户端压缩到 256 KiB 以内，整个 span 的属性控制在 64 KiB 以内由平台处理。插桩以日志事件
发出的内容（§9.2）只有经过 SDK 的日志处理器才会转发：SDK 新建的 `LoggerProvider` 是私有的（只供
`log_evaluation()` 使用），插桩的内容事件要求把插桩所用的 provider 传给 `init(logger_provider=...)`。

### 10.2 平台在 `metadata_only` 下丢弃什么

项目设置 `content_capture.mode = metadata_only`（`backend/app/platform/project_settings.py:159`）时：
- span 的 `input` / `output` 为 null，没有内容哈希、没有截断文件（`content.py:146-147`）；
- 五个标准内容属性本来就映射到列，不出现在 `attributes` 中，因此也被丢弃；
- 消息内容日志事件被忽略（`otlp_logs.py:518`）；
- 常见第三方插桩的内容属性（新平台的 `CONTENT_ATTRIBUTE_DENYLIST`，`backend/app/telemetry/otlp.py:101-156`，
  与 SDK 的列表相同，§10.1）也被丢弃（`backend/app/telemetry/content.py:9`）；
- **不丢弃**的：清单之外的属性（包括 SDK 的 `init(content_attributes=...)` 补充的）仍保存在 span 的 `attributes`
  中；旧平台连清单中的属性也保存（见 §14）。因此 SDK 在 `capture_content=False` 时自己删除这些属性，不依赖平台。

`full` 模式下的平台处理（`content.py:136-170`）：按入库脱敏规则脱敏（新平台对清单中的第三方内容属性同样
脱敏）→ 单字段 > 256 KiB 时保形压缩（`truncated = true`，完整内容另存文件）→ 计算内容哈希。预览取前 2000 字符。

---

## 11. traceparent 传播与回放回执

平台的回放执行器（`executor = replay`）调用被测服务时：

1. 先预建 `origin = experiment` 的 trace（`backend/app/telemetry/ingest.py:501-538`），永不加入会话。
2. Target 配置 `propagate_traceparent` 为 true（默认）时，请求头带
   `traceparent: 00-<otel_trace_id>-<16 位随机 hex>-01`（`backend/app/eval/targets.py:513-514`、`:660-661`）。
   `traceparent` / `tracestate` 是保留头名，不能被 `version_param` / `component_params` 占用。
3. 默认请求体（`targets.py:67-76`）包含 `agent_version`、`components`、`case`（example key）、`trial_index`、
   `phase`、`timeout_ms`。
4. 响应映射默认读取 `output`、`receipt`、`env_receipt`（`targets.py:77-81`）。`receipt` 形状：
   `{agent: {name, version}, components: [{type, name, version, content_hash?}]}`（`targets.py:565-592`）。
5. 条目完成后等待 `receipt_wait_s`（0–600，默认 120）秒，再从预建 trace 读取 `trace_attr` 回执：
   trace 的 `agent_name`、`agent_version`、`components`（至少有一个 span 入库才算，`executor.py:231-250`）。
   `finalize` 只在 `receipt_wait_s > 0` 时延后。
6. 合并（`executor.py:151-228`）：响应字段（R）优先，trace 属性（S）补缺；agent 版本、组件版本或
   `content_hash` 不一致都记为 `receipt_conflicts`。`version_source` 为 `both`、`response_field`、`trace_attr`
   或 `none`。

**SDK 侧规则**：
- 服务端用 `extract_context(headers)`，或 `InsightMonASGIMiddleware` / `InsightMonWSGIMiddleware`，
  按 W3C Trace Context 取出远程父上下文；请求中创建的 span 成为它的子 span，trace id 与预建 trace 相同。
- 远程父上下文下的第一个本地 span 是“本地根”，SDK 在它上面写 agent 与组件属性，`trace_attr` 回执因此可用。
- `receipt()` 返回当前设置对应的 `{agent, components}`，服务可以把它放进响应体的 `receipt` 字段
  （R 回执，比 S 回执可靠，不依赖 span 是否及时到达）。
- span 必须在 `receipt_wait_s` 内到达：批处理默认每 5 秒导出一次，默认 120 秒足够；
  `receipt_wait_s = 0` 时用中间件的 `flush_on_traceparent=True`，在服务器拿到最后一块响应之前导出，每次最多等
  `flush_timeout_s`（默认 5 秒）。它对每个带合法 `traceparent` 的请求生效，而调用方传播 W3C 上下文时几乎每个请求
  都带：只在专门承接回放的部署里使用，或者用 `flush_when=`（参数是 ASGI scope / WSGI environ）挑出回放请求。
  平台不发送标记回放的请求头，可以让 Target 通过 `headers_ref` 带一个。
- SDK 写入的 agent 名称与版本、环境、组件的类型 / 名称 / 版本 / 哈希都去掉首尾空白：平台读取 trace 属性（S）时
  去掉空白，响应字段回执（R）若带着空白就与 S 不一致，记为 `receipt_conflicts`。
- **旧平台使用 SQLite 时不能在响应前导出**（`flush_on_traceparent` / `flush_when`）：旧平台的 SQLite 分支在作业
  自己的会话里预建 trace，随后在同一个未提交的写事务中调用 Target；OTLP 入库需要同一把写锁，于是响应前的导出
  一直阻塞到调用超时：条目记为 `timeout`、span 丢失、回执为 `none`。那样的平台上保持后台导出，并把
  `receipt_wait_s` 设为至少几秒。新平台预建 trace 后先提交作业的事务，再调用 Target，调用期间不持有任何事务
  （`backend/app/eval/executor.py:11-16`），SQLite 与 PostgreSQL 上都可以在响应前导出（§14）。
- 不要在 resource 上设置 `service.version`，除非它就是 agent 版本：缺少 `gen_ai.agent.version` 时平台会
  拿它当 agent 版本，可能造成回执冲突（§14）。

---

## 12. evaluate() 流程

`evaluate(task, dataset=...)` 在客户端运行任务，平台运行 gating 评估器并给出结论。逐步：

1. **准备**：解析 `base_url` / `api_key`（参数 → `init()` 的设置 → 环境变量 `INSIGHTMON_BASE_URL` /
   `INSIGHTMON_API_KEY`）。Key 需要 `experiment_write`；要上报 span 还需要 `trace_ingest`。
2. **创建**：`POST /api/v1/experiments`，`triggered_by = "sdk"`，带本次运行生成的 `idempotency_key`；
   `requested` 缺省时取 `init()` 的 `agent_version` 与组件（去掉 `content_hash`）。新平台上读超时、网络错误、
   429 `rate_limited`（按 `Retry-After`）与 5xx 都按幂等请求重试：平台对同一个键返回第一次创建的实验，不会建出
   第二个。旧平台答 422（§6.1）后去掉幂等键重发，只在连接失败与 429 时重试；读超时**不重试**，直接报错；读超时、
   网关 502 / 504 与没有平台错误体的 5xx 说明请求已经到达，消息中提示实验可能已创建（先到实验页面确认再重跑）。
3. **取样例**：`GET .../examples?limit=200`，跟随 `next_cursor` 取完。有客户端评估器时再读一次
   `GET /api/v1/score-configs`（§5.1），记下允许 `experiment_item` 层级的已发布名称；读不到（旧平台 404、
   Key 无权 403 或其他失败）时不做本地检查。
4. **运行**：对每个 (example, trial)，最多 `max_concurrency` 个并发（先取得一个名额，见第 3 小步）：
   1. 在 `experiment_scope(experiment_id, example_key, trial_index)` 中用 `tracing.turn("evaluate")` 开启
      一条新 trace 的根 span（轮次 span：`gen_ai.operation.name = invoke_agent`，带 agent 版本与组件版本），
      属性 `insightmon.experiment_id`、`insightmon.example_key`、`insightmon.trial_index`；作用域内 SDK
      **不写会话键**（实验流量不进入会话），任务里调用的 `set_component()` 只作用于这一次运行；
      该 trace 的 span 先**暂存在 SDK 内**（按实验 id 登记），等条目上报后再导出（见第 6 步）；
   2. 调用任务。参数按签名决定：第二个位置参数名为 `initial_state` → `task(input, initial_state)`（样例
      `initial_state` 的深拷贝；对应 `docs/dev/m4.md` §5.5 “输入为样例的 input（与 initial_state）”）；
      第二个位置参数必填或名为 `example`（或任务接受 `*args`）→ `task(input, example)`（`Example`）；否则
      `task(input)`。样例带非空的
      `initial_state` 而任务只接收 `input` 时记一条 WARNING。
      输出的形状与平台对 HTTP Target 输出的处理相同（`normalize_output`，`backend/app/eval/targets.py:557-562`）：
      str → `{"text": ...}`，Mapping → 对象，None → `{}`，其他 JSON 值 → `{"json": value}`（`json_schema` 等
      内置检查读 `output.json`）。NaN / Infinity 转成 null（顶层 NaN 因此是 `{"json": null}`，不会出现
      `status = ok` 而没有输出的条目），孤立的 UTF-16 代理字符换成 U+FFFD；
   3. 异常 → `status = task_error`，`error = {code: "exception", message: "<类型>: <消息>"}`（同样清洗代理字符，
      ≤ 4000 字符）；不能编码成 JSON 的输出（任意对象、引用自身、嵌套超过 100 层）→ `task_error`，
      `code = "output_not_json"`；输出 > 256 KiB → `task_error`，`code = "output_too_large"`；
      超过 `timeout_s` → `status = timeout`。线程无法被杀死：超时的任务在返回之前继续占着 `max_concurrency`
      中的名额，挂起的下游服务同时最多收到 `max_concurrency` 个调用；所有名额都被这样的任务占着、再过
      `timeout_s` 仍没有空出时，运行停止（按第 10 步处理，不 finalize），抛出写明实验 id 的 `RuntimeError`；
   4. 轮次 span 结束后（不在条目的 trace 里，当前上下文为空）运行客户端评估器
      `evaluator(input, output, reference)`，`output` 是上报的输出；返回 Mapping、Mapping 列表、标量
      （名称取函数 `__name__`）或 None；同名分数只保留第一个；评估器异常只记日志，不影响条目。评估器产生的
      span 开始各自的 trace，在单独的暂存所有者（`<实验 id>/evaluators/<example_key>/<trial>`）下暂存后丢弃：
      它们不会混进平台评估器读取的 agent 轨迹，也不会成为生产 trace。分数的名称与 `explanation` 清洗代理
      字符；`metadata` 中平台保留的键（`adjudicated`、`accepted_by`、`mt_assisted`、`self_review`、
      `verification_id`，§5）被去掉，每个分数名记一次 WARNING（新平台的 `items:batch` 会拒绝这些键，§6.4；
      旧平台原样写入）。名称不在第 3 步读到的列表中的分数不上报，每个名称每次运行记一次 WARNING（不再让
      平台逐批拒绝）；
   5. 记录 `latency_ms`、`otel_trace_id`（根 span 的 trace id，32 位小写 hex；没有真实 tracer 时省略）、
      `receipt`（`init()` 的 agent 版本与组件，去掉 `content_hash`）。
5. **上报**：条目进入批处理器，每 50 条或 5 秒发送一次 `items:batch`（同时不超过 200 条、约 7 MB）。
   - 同一批的重试发送**同一份**请求体（保证 `report_hash` 相同，重复计为 `duplicates`）；`items:batch` 与
     `:finalize` 最多重试 `report_retries`（默认 8）次（§0.1）；
   - 400 `external_item_invalid {reason: "score_invalid"}`：没有写入任何数据。新平台在 `invalid_scores` 中列出
     本批每个无效的分数（§6.4）：SDK 按 `(example_key, trial_index, name)` 只去掉这些分数，重发一次。旧平台
     每次只指出一个条目，而且只给 `example_key`：请求里只有一个带客户端分数的条目属于该样例时，SDK 去掉它的
     全部客户端分数后重发；有几个（`trials` > 1）时不猜：其中一个随请求其余部分重发，其余的各自单独发送，让
     每次拒绝都恰好指向一个条目，一个 trial 的无效分数不会连累其他 trial 的分数（`params` 带 `trial_index` 时
     SDK 直接按它匹配）；
   - 同一请求被拒 3 次后，仍带客户端分数的条目逐个发送（每次拒绝只重发一个小请求，不再重发整批）；同一个
     分数名（平台不列名称时：同一组分数名）被拒 3 次、且平台从未接受过它时，本次运行其余条目在上报前直接去掉
     它。每个名称（每组名称）第一次被拒记 WARNING，开始直接去掉时再记一次；
   - 其他 4xx：停止运行，**不 finalize**，抛出 `InsightMonError`。
6. **释放 span**：每批上报成功后，释放这些条目的暂存 span，交给导出器。此时平台已预建
   `origin = experiment` 的 trace，span 并入其中。新平台不暂存也能识别实验 trace（§8），但只对同一个 Key 创建的
   实验、只在带属性的根 span 到达之后（根 span 最后结束，先导出的子 span 会先建出生产 trace），而且 trace 不能
   已加入会话或已派发在线规则；旧平台完全不识别。所以 SDK 仍然暂存（`docs/design.md` §4）。
7. **收尾**：全部条目上报后，释放**本实验**仍暂存的 span（同一进程中并行的其他 `evaluate()` 不受影响），
   `flush()`（traces 再 logs）；`POST :finalize`（重试时 409 `invalid_state status = finalized` 视为成功）。
8. **等待**：`wait=True` 时轮询 `GET /experiments/{id}`，间隔从 1 秒退避到 10 秒，直到 `completed`、
   `failed` 或 `cancelled`，最长 `wait_timeout_s`（默认 1800 秒）；超时返回当前状态，不抛错。
9. **结果**：`ExperimentResult(experiment_id, url, status, summary, items, experiment)`，
   `url = <base>/projects/<project_id>/experiments/<id>`。
10. **SDK 自身失败**（网络中断、不可重试的错误）：不调用 `:finalize`，平台 24 小时后自动结束实验；
    异常信息中带上实验 id。未上报条目的暂存 span 被**丢弃**（导出后会成为 `origin = otlp` 的生产 trace），
    之后才结束的 span 也一并丢弃。
11. **中断**（Ctrl-C、取消 `aevaluate()` 所在的任务），包括第 7 步上报最后一批条目、等待 429 的时候：停止调度
    新的运行，上报已完成的条目，丢弃其余条目的 span，**不 finalize**；收尾期间再次中断，丢弃 span 与 WARNING
    照样执行。WARNING 日志写明实验 id 与已上报条目数，然后重新抛出 `KeyboardInterrupt` / `CancelledError`。
    之后可以 `Client().finalize_experiment(id)` 收尾，否则平台 24 小时后自动结束（未上报的条目保持 `pending`）。

`aevaluate()` 是异步版本：异步任务直接 await（`timeout_s` 到时取消）；同步任务、同步评估器和 HTTP 调用（同一个
同步 `Client`）在专用的守护线程中运行，结果通过 `call_soon_threadsafe` 交回事件循环，从不使用事件循环的默认
线程池（它与应用共用，挂起的任务占满它会卡住 SDK，排队时间也会被算进超时）。超时从任务开始运行时计时。
被取消时，`aevaluate()` 等收尾（第 7 步或第 10 步）结束后才关闭 `Client`。

---

## 13. 差异

平台文档与代码曾经不一致的地方（以代码为准）。新平台（`fix/sdk-platform-gaps`）已把这 17 处全部同步到平台文档
（`docs/dev/m0.md`、`m1.md`、`m3.md`、`m4.md` 与 `backend/app/api/judge_budget.py` 的模块说明），状态列写明；
表格保留，供对照旧平台的文档：

| # | 文档 | 代码 | 差异 | 状态 |
|---|---|---|---|---|
| 1 | `docs/dev/m1.md:1055` | `backend/app/telemetry/ingest.py:233-243` | agent 名称 / 版本的查找顺序在 `invoke_agent` span 之后、resource 之前还会看“任意其他 span” | 已同步 |
| 2 | `docs/dev/m1.md:1054` | `backend/app/telemetry/ingest.py:192-200`、`backend/app/telemetry/otlp.py:563-567` | 文档写“先根 span、再其他 span”；代码是**先按属性优先级**在所有 span 中查找，再按 span 顺序。长度 > 200 的会话键被忽略，文档未提 | 已同步 |
| 3 | `docs/dev/m1.md:1045` | `backend/app/api/body_limits.py:49`、`backend/app/telemetry/otlp.py:57` | 除 `gzip` 外还接受 `x-gzip`、`deflate`；除 `application/x-protobuf` 外还接受 `application/protobuf` | 已同步 |
| 4 | `docs/dev/m1.md:1045` | `backend/app/api/middleware.py:33`、`:84-96` | 8 MiB 上限也作用于**压缩后**的请求体（全局中间件，先于认证），不只是解压后的大小 | 已同步 |
| 5 | `docs/dev/m1.md:1057` | `backend/app/telemetry/language.py:89` | `insightmon.language` 先去空格、转小写再比较（`"ZH"` 有效） | 已同步 |
| 6 | `docs/dev/m3.md:265` | `backend/app/eval/otlp_scores.py:103-108` | 评估名称还会截到 64 字符 | 已同步 |
| 7 | `docs/dev/m3.md:266-267` | `backend/app/eval/otlp_scores.py:139-162` | 布尔型：有 label 时 label 优先，value 被忽略；label 不在列表中 → `score_value_invalid`。类别型 label 是否在类别中由批量写入器检查 | 已同步 |
| 8 | `docs/dev/m4.md:563` | `backend/app/eval/external.py:143-149` | 输出 > 256 KiB 是 pydantic 字段校验错误，返回 **422 `validation_error`**，不是 `external_item_invalid` | 已同步 |
| 9 | `docs/dev/m4.md` §5.3 | `backend/app/api/routes/external_experiments.py:40`、`backend/app/api/middleware.py:33` | 路由声明的上限约 58 MiB，但全局中间件已把请求体限制在 8 MiB，实际上限是 8 MiB | 已同步；新平台去掉了路由自己的上限，两者都是 8 MiB |
| 10 | `docs/dev/m4.md` §5.3 | `backend/app/eval/external.py:113-117`、`backend/app/eval/experiments.py:63-68` | `receipt.components` 与 `requested.components` 不接受 `content_hash`（`extra = forbid` → 422） | 已同步 |
| 11 | `backend/app/api/judge_budget.py` 模块说明 | `backend/app/api/routes/external_experiments.py:85` | 模块说明称只限制“会运行评审的启动”，路由对每次创建都计数（`docs/dev/m4.md:976` 与代码一致） | 已同步；新平台对幂等重放不计数 |
| 12 | `docs/dev/m1.md:1065` | `backend/app/telemetry/otlp.py:532-538` | token 还接受旧属性 `gen_ai.usage.prompt_tokens` / `completion_tokens` | 已同步 |
| 13 | `docs/dev/m0.md:624` | `backend/app/eval/scores.py:119-133` | span 主体的 `trace_id` 是可选的；另有外部 id 形状 `{otel_trace_id, otel_span_id}`、`{otel_trace_id}`、`{thread_key}` | 已同步 |
| 14 | `docs/dev/m1.md`（实验汇总键） | `backend/app/eval/evaluation.py:152-163` | 汇总还包含 `ignored_verifier_scores`、`evaluated`，以及 M4 的 `trials`、`client_scores` | 已同步 |
| 15 | `docs/dev/m4.md` §5.5 | 实验状态集合 | 文档只列出 `completed` / `failed` 两个终态；实验还可能是 `cancelled` | 已同步 |
| 16 | `docs/dev/m3.md:285` | `backend/app/telemetry/otlp.py:603-604` | `insightmon.thread.end` / `insightmon.eval_root` 从合并属性读取：放在 resource 上会作用于该进程的每个 span | 已同步 |
| 17 | `docs/dev/m4.md` §5.3 | `backend/app/eval/external.py:448-452` | `item_ids` 先列新接受的条目，再列重复条目，不与请求顺序对应 | 已同步 |

---

## 14. 平台缺口

SDK 只能绕开、建议平台修复的问题（同样汇总在 SDK 设计文档中）。新平台（`fix/sdk-platform-gaps`，迁移 0009）已经
修复的列在前一张表：SDK 在新平台上用新行为，在旧平台上保留原来的绕开方式（见本文开头）。

**新平台已修复**：

| 原来的问题 | 平台的修复 | SDK 的处理 |
|---|---|---|
| 创建外部实验没有幂等键，读超时后重试会建出第二个实验 | 可选 `idempotency_key`：同一个 Key、同一个键、同样内容返回第一次创建的实验，内容不同 → 409 `idempotency_key_conflict`；幂等重放不计入创建限流与评审预算（`backend/app/eval/external.py:193-224`、`backend/app/api/routes/external_experiments.py:79-97`，迁移 `0009_experiment_idempotency`） | 每次运行带一个幂等键，按幂等请求重试；旧平台答 422 时去掉幂等键，退回只在连接失败与 429 时重试并提示实验可能已创建（§6.1） |
| 没有 Key 可读的 ScoreConfig 列表，SDK 无法预先检查客户端分数名 | `GET /api/v1/score-configs`（`backend/app/api/routes/key_score_configs.py`） | `evaluate()` 每次运行读一次、`log_evaluation()` 每个运行时后台读一次，未发布的名称记 WARNING（`evaluate()` 不上报它们）；读不到时不检查（§5.1） |
| `items:batch` 的 `score_invalid` 每次只指出第一个出错的条目，一批里多个条目的分数名都无效时要逐条重试 | `params.invalid_scores` 列出本批每个无效的分数（`external.py:441-491`） | 去掉列出的分数后重发一次（§12 第 5 步）；旧平台上保留逐条处理与“同一请求被拒 3 次后逐个发送、同一组名称被拒 3 次后直接去掉” |
| `external_item_invalid` 的 `params` 没有 `trial_index`：`trials` > 1 时无法知道被拒的是哪个 trial | `params` 带 `trial_index`（`external.py:174-183`） | 按 `trial_index` 匹配；旧平台上同一样例的几个带分数的条目分开发送 |
| `items:batch` 原样写入客户端分数的 `metadata`，不检查 `scores:batch` 拒绝的保留键 | 按 `scores:batch` 的规则检查（`external.py:563-573`） | 仍在上报前去掉这些键并记 WARNING（两种平台都不会因此拒绝整批） |
| span 先于 `items:batch` 到达时 trace 为 `origin = otlp`：出现在生产 trace 列表、可能触发在线规则；平台不读 `insightmon.experiment_id` | 入库识别 `insightmon.experiment_id`：同一个 Key 创建的、仍在运行的外部实验 → `origin = experiment`（`backend/app/telemetry/ingest.py:780-810`），`items:batch` 在两种到达顺序下都能关联（§6.4、§8） | 仍然暂存实验 span、条目上报后再导出：识别只对同一个 Key、只在带属性的根 span 到达后生效，旧平台不识别（理由见 `docs/design.md` §4）；集成测试关掉暂存验证了平台的识别 |
| `create_experiment_trace` 没有处理 `IntegrityError`：同一 trace 的 OTLP 写入并发时可能 500 | 在保存点中插入，冲突时认领同一实验的、还没有条目的实验 trace（`ingest.py:564-612`）；`items:batch` 关联 trace 前先取项目入库锁（`external.py:582`） | 仍对 `items:batch` 的 5xx 重试（按内容去重，安全） |
| `metadata_only` 只丢弃五个标准内容属性，非标准内容属性仍保存在 `attributes` 中 | 常见第三方插桩的内容属性清单（`backend/app/telemetry/otlp.py:101-156`）在 `metadata_only` 下丢弃，`full` 下脱敏 | 内置列表与平台清单同步（§10.1）；`capture_content=False` 时仍在发送前删除（清单之外的属性平台照样保存） |
| SQLite 上回放的预建 trace 写在作业的事务里，调用 Target 期间持有写锁；服务在响应前导出 span 会阻塞到超时 | 预建 trace 后先提交，再调用 Target（`backend/app/eval/executor.py:11-16`） | 文档只对旧平台保留“SQLite 上不用 `flush_on_traceparent`”的提醒（§11） |
| `items:batch` 路由声明的上限（约 58 MiB）与全局 8 MiB 不一致，200 条 × 256 KiB 的合法批次会被 413 拒绝 | 去掉路由自己的上限，实际上限明确为 8 MiB | 仍按序列化大小拆批（约 7 MB） |
| 17 处平台文档与代码的差异（§13）；`docs/dev/m4.md` §5.5 没有说明 `initial_state` 怎样传给任务 | 平台文档已同步，并写明 SDK 的 `task(input, initial_state)` 约定与 `cancelled` 终态 | 无需处理 |

**仍然存在**：

| 位置 | 问题 | SDK 的处理 |
|---|---|---|
| `backend/app/telemetry/ingest.py:242-244` | 缺少 `gen_ai.agent.version` 时用 resource `service.version` 作为 agent 版本，可能与响应回执冲突 | SDK 在每个 span 上写 `gen_ai.agent.version`；文档提醒 |
| `backend/app/api/routes/otlp.py` | OTLP partial success 中的拒绝原因（如 `score_config_not_found`）对 Python 导出器不可见，原因里也没有被拒的名称 | SDK 自己的导出器解析响应：每种原因记一条 WARNING，计入 `rejections()`；`log_evaluation` 名称在本地按平台规则规范化，并在新平台上按 `GET /api/v1/score-configs` 预先检查（§5.1） |
| — | 线程、trace、分数、实验条目只能用浏览器会话读取，没有 Key 可读的查询接口 | 集成测试用项目管理员账号读回结果 |
| （依赖）OTel Python 导出器 | 超过 `max_request_size` 的批次被整批丢弃，不会拆分；把通用的 `OTEL_EXPORTER_OTLP_*HEADERS`（可能带着别家服务的凭据）合并进发往平台的请求；1.35 到 1.45 之间重试的状态码各不相同；跟随或静默接受重定向（数据丢失而 `flush()` 仍返回成功）；不遵守 flush / shutdown 的期限 | SDK 用自己的 httpx 导出器（§0.1）：只发四个请求头、固定的重试集合、不跟随重定向（WARNING + `rejections().redirects`）、按期限中止；包装器按序列化大小拆分批次 |
| （依赖）OTel Python `BatchSpanProcessor` | 队列默认 2048，满了静默丢弃 span；一批条目上报后一次释放的暂存 span 可能远超这个数 | SDK 自己的批处理器队列容得下整个暂存缓冲（10000 + 2048）；交给应用的 `export_processor` 时按 512 个一组释放，组间等待导出 |

---

## 15. 本文与纯净性检查

SDK 的纯净性测试（`tests/test_purity.py`）也扫描本文。平台中个别标识符含有被禁用的领域词，
本文一律用中性描述代替（例如平台自带的领域包写作“平台自带的领域包”），不加入白名单。
