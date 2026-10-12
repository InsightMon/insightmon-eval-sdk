# insightmon-eval-sdk

The Python SDK for InsightMon AI Eval. It does three things:

- **Report sessions**: every turn of your agent becomes an OpenTelemetry trace, tagged with the
  platform's attributes (thread key, agent version, component versions, language), from which the
  platform builds threads, runs evaluations and analyses regressions;
- **Report scores**: evaluation results (OTLP log events) as well as user feedback and business
  outcomes (REST);
- **Client-side experiments**: `insightmon.evaluate()` runs the examples of a platform dataset on
  your machine; the platform runs the gating evaluators and gives the verdict.

It is lighter than comparable SDKs: at runtime it depends only on OpenTelemetry (API, SDK, OTLP/HTTP
exporter) and httpx, with no web framework or model vendor library. Session data are plain OTel spans,
so existing OpenTelemetry setups and third-party instrumentations plug straight in.

The platform: [InsightMon/insightmon-ai-eval](https://github.com/InsightMon/insightmon-ai-eval).
中文：[README.md](README.md).

> **Status**: 0.1.0. The whole public API is implemented and verified end to end against a real
> platform instance (`scripts/integration.sh`, see [Development](#development)). Changes:
> [CHANGELOG.md](CHANGELOG.md).

Contents: [Install](#install) · [5-minute quick start](#5-minute-quick-start) ·
[Threads and turns](#threads-and-turns) · [Existing OpenTelemetry setups](#existing-opentelemetry-setups) ·
[Content capture and privacy](#content-capture-and-privacy) · [Evaluation logs](#evaluation-logs) ·
[Scores](#scores) · [evaluate()](#evaluate) · [Replay receipts](#replay-receipts) ·
[Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Compatibility](#compatibility) ·
[Development](#development)

## Install

The SDK is installed from git for now (Python 3.10 or later):

```bash
pip install "insightmon-eval-sdk @ git+https://github.com/InsightMon/insightmon-eval-sdk.git@main"
# or
uv add "insightmon-eval-sdk @ git+https://github.com/InsightMon/insightmon-eval-sdk.git@main"
```

In production, pin `@main` to a release tag or a commit so that every install gets the same code. The
import name is `insightmon`.

## 5-minute quick start

1. **Create an API key**: open the project on the platform, sidebar "API keys" (project admins), and
   create a key with the `trace_ingest` scope. The secret (`imk_...`) is shown once; keep it out of
   code and repositories.
2. **Set the environment**:

   ```bash
   export INSIGHTMON_BASE_URL=https://eval.example.com   # the platform's root URL
   export INSIGHTMON_API_KEY=imk_...
   ```

3. **Report a two-turn chat** (this is [examples/quickstart.py](examples/quickstart.py);
   `python examples/quickstart.py`):

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

4. **Look at it on the platform**: sidebar "Sessions & traces", search for the thread key
   (`chat-...`). You see one thread with two traces (one per turn; the first one has a `forecast`
   tool span), agent version 1.4.0, component `prompt/system = p7` and declared language `en`. The last
   turn carried `end_thread`, so the thread is closed (close reason `explicit`) and the platform starts
   evaluating it.

Without the two variables the SDK only sets attributes locally and sends nothing, so a dry run is safe.

## Threads and turns

| Function | What it does |
|---|---|
| `thread(key, *, language=None, metadata=None, user_ref=None)` | every span started in the block carries the thread key `gen_ai.conversation.id`; use the same key for all turns of a conversation (e.g. your chat session id, 1–200 characters) |
| `turn(name="turn", *, eval_root=False, end_thread=False, operation="invoke_agent", attributes=None)` | one turn: an `invoke_agent` span; with no current span it is the trace root, so one turn = one trace |
| `end_thread(span=None)` | set `insightmon.thread.end` on the (current) span: the platform closes the thread (`close_reason = explicit`) and never reopens it |
| `mark_eval_root(span=None)` | mark the evaluation root span (default: the trace's root span) |
| `set_component(type, name, *, version=None, content_hash=None)` | declare a component version in use (see the scope below) |
| `set_response_id(response_id, span=None)` | set `gen_ai.response.id`; evaluation logs can find the span by it |
| `current_thread()` | the current thread scope (None outside one) |
| `get_tracer()` | the SDK's tracer, for child spans such as tool and model calls |

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

Notes:

- **Spans outside `thread()` carry no thread key** and the platform creates no thread for them (the
  SDK never makes up a thread key).
- `language` is one of `zh` / `en` / `ja` / `mixed` / `und`; the thread's declaration wins over
  `init(language=...)`, and the platform records the thread's language source as `declared`.
  `metadata` becomes the thread's metadata; `user_ref` is an opaque user reference (at most 128
  characters): **no personal data**.
- The scope of `set_component()`: inside an `evaluate()` task it affects that run only; inside
  `thread()` that thread only; elsewhere the whole process. Components are written on local root spans
  and turn spans only.
- With nested `thread()` blocks the innermost wins. Spans inside `evaluate()` never carry a thread key
  (experiment traffic never joins a thread).
- A closed thread is never reopened: later traces with the same key are counted as late. A thread
  without `end_thread` is closed by the platform when it has been idle long enough.

### asyncio, thread pools and processes

The thread scope lives in `contextvars`, like the current span, and follows the context.

**asyncio**: tasks copy the context when they are created, so `asyncio.gather()` / `create_task()`
stay in the thread automatically.

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

**Thread pools**: new threads and `ThreadPoolExecutor` workers start with an **empty context**, so
spans created there become separate traces without a thread key. Copy the caller's context for each
submission and run the call in it: `contextvars.copy_context()` + `executor.submit(ctx.run, fn, arg)`.
A context can be entered by one thread at a time, so **make one copy per submission**.

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

**Processes (fork)**: with gunicorn, uWSGI or `multiprocessing` children, call `init()` **after the
fork, in every worker**, e.g. in gunicorn's `post_fork` hook. OpenTelemetry's batch processors
restart their export thread in the child, and the SDK recreates all its locks there and forgets the
spans the parent was holding, so initializing before the fork (e.g. with gunicorn's `preload_app`)
usually works too; but a per-worker `init()` is the most
predictable setup, and the only one when the parent never calls `init()`. Pick one place: a second
`init()` while one is active only logs a warning and returns the existing handle. Children started
with `spawn` inherit nothing and need their own `init()` as well.

```python
# gunicorn.conf.py
import insightmon


def post_fork(server, worker):
    insightmon.init(agent_name="weather-assistant", agent_version="1.4.0")
```

## Existing OpenTelemetry setups

`init()` picks the `TracerProvider` in this order:

1. the `tracer_provider=` argument;
2. otherwise, when the global provider already is an OpenTelemetry SDK `TracerProvider` (set by
   `opentelemetry-instrument` or your own setup code), it **joins it**: it only adds the SDK's
   processors and exporters and never replaces or shuts it down;
3. otherwise it creates a provider (`service.name` from `service_name` or `agent_name`) and sets it
   as the global provider.

Once joined, spans from third-party instrumentations (HTTP clients, web frameworks, model SDKs) get the
thread key, agent version and language as well. The SDK's own HTTP requests (OTLP exports, REST calls)
are sent with instrumentation suppressed, so an HTTP client instrumentation never records them as agent
spans.

The provider needs the default (synchronous) span processor: a `ConcurrentMultiSpanProcessor` calls
`on_start` in a thread pool, where the SDK cannot see the caller's thread, turn and experiment scopes,
so nothing is stamped and the spans of `evaluate()` cannot be held (`init()` logs a warning when it
finds one).

```python
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

import insightmon

provider = TracerProvider(resource=Resource.create({"service.name": "weather-api"}))
# ... your own span processors and exporters stay as they are ...
insightmon.init(tracer_provider=provider, agent_name="weather-assistant", agent_version="1.4.0")
```

**Through an OpenTelemetry Collector**: without a base URL and key, the SDK sets attributes and does
not export; your own exporter sends to the Collector, which forwards to the platform's OTLP/HTTP
endpoints. Hand your export processor to `init(export_processor=...)` and do **not** call
`provider.add_span_processor(...)` yourself: the spans of `evaluate()` runs are then held on that path
as well and exported once their items are reported. Otherwise they reach the platform first and
become production traces (`evaluate()` logs a warning when it cannot hold them).

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

Notes:

- Only the SDK's own exporter removes content attributes when `capture_content=False`; with your own
  exporter, content is up to your pipeline (see [Content capture and privacy](#content-capture-and-privacy)).
- Do not set `service.version` on the resource unless it is the agent version: without
  `gen_ai.agent.version` the platform takes it as the agent version. The SDK writes
  `gen_ai.agent.version` on every span.
- The processor works without `init()` too:
  `provider.add_span_processor(insightmon.InsightMonSpanProcessor(BatchSpanProcessor(exporter)))`
  (the export processor goes to it as well, not straight to the provider).
- Every attribute the platform reads is listed in [docs/conventions.md](docs/conventions.md).

## Content capture and privacy

**Message content is not sent by default** (`capture_content=False`):

- the SDK's exporter removes the content attributes it knows (`semconv.is_content_attribute()`) from
  spans and span events before sending: the standard `gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.call.arguments`,
  `gen_ai.tool.call.result`, and the ones common third-party instrumentations set, such as
  `input.value`, `output.value`, `llm.input_messages*`, `traceloop.entity.input`,
  `langfuse.observation.input` and `retrieval.documents.*`; the full list is in
  [docs/conventions.md](docs/conventions.md);
- the log processor forwards evaluation result events only, never message content events;
- `record_io()` does nothing and returns `False`.

**Attributes outside that list are sent as they are**, and the platform keeps such non-standard
attributes even in `metadata_only` mode. Check which content attributes your instrumentations write,
and add the missing ones with `content_attributes` (attribute names, or prefixes ending in `*`):

```python
import insightmon

insightmon.init(
    agent_name="weather-assistant",
    agent_version="1.4.0",
    content_attributes=["notes.body", "search.results.*"],  # stripped too while capture is off
)
```

**What the content switch does not cover**:

- `evaluate()` always reports task outputs, the messages of task exceptions, and the explanations
  and metadata of client evaluator scores: the platform needs them to reach its verdict;
- when a span fails, the `exception` event that `turn()` (and OpenTelemetry's
  `start_as_current_span()`) records carries the exception message and stack trace
  (`exception.message`, `exception.stacktrace`): when the message contains user input, it leaves
  with the span;
- thread keys, `user_ref`, metadata and the `explanation` of `log_evaluation()`: keep personal data
  out of them.

Turn content capture on explicitly to see conversations on the platform:

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

- `record_io()` takes strings, `{role, content}` messages or OTel-shaped `{role, parts}` messages (or
  lists of them) and writes the OTel GenAI JSON; a field over 256 KiB is shrunk on the client first,
  keeping its shape.
- Content that instrumentations emit as **log events** (`gen_ai.user.message` and so on) is forwarded
  only when it goes through the SDK's log processor: pass the `LoggerProvider` your instrumentations
  use to `init(logger_provider=...)`. A `LoggerProvider` the SDK creates itself serves
  `log_evaluation()` only; instrumentations never see it.
- The platform has its own switch: with the project's content capture mode `metadata_only` it stores
  no standard message content. The platform applies its own redaction on ingest.

## Evaluation logs

Send the results of your own evaluators (rules, model graders, labeling tools) with
`log_evaluation()`, as `gen_ai.evaluation.result` log events; the platform stores them as scores with
`source = import`:

```python
import insightmon

with insightmon.thread("chat-46"), insightmon.turn() as span:
    answer = "Light rain is expected in Oslo tomorrow."

insightmon.log_evaluation("helpfulness", value=0.9, explanation="answers the question", span=span)
insightmon.log_evaluation("mentions_city", label="pass", span=span)
```

- **The name must be a published score config of the project** (sidebar "Score configs"). The
  platform lowercases names and replaces characters outside `[a-z0-9_]` with `_`. Unknown names are
  refused: the SDK reads the OTLP partial success response, logs one warning per reason (e.g.
  `score_config_not_found`) and counts it in `insightmon.rejections()`.
- Give exactly one of `value`, `label` and `error_type`; `error_type` means the evaluator itself
  failed (stored as an unscored score).
- Linking: pass `span=` (or call it inside the evaluated span); without the span, pass
  `response_id=` (set on the span with `set_response_id()` first).
- Where the score lands: on the span when the score config allows the span level, otherwise on its
  trace, otherwise on the thread.

## Scores

User feedback (`end_user`), business outcomes (`outcome`) and external imports (`import`) go straight
to REST (the key needs `score_write`):

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

- Subjects can use external ids: a thread `{"type": "thread", "thread_key": ...}`, a trace
  `{"type": "trace", "otel_trace_id": ...}`, a span
  `{"type": "span", "otel_trace_id": ..., "otel_span_id": ...}`, or the platform's internal ids.
  Names must be published score configs here as well.
- Every score has an idempotency key (generated by the SDK when missing), so request retries never
  write twice; one invalid score refuses the whole batch (an `InsightMonError`, e.g.
  `score_config_not_found`). At most 500 per request; the SDK splits larger lists.
- When a list is split and one request fails, an `InsightMonError` is raised and the requests before
  it stay written; the error's `params` carry `written` (how many scores were written) and
  `idempotency_keys` (the key of every score, in order). When you call again, put these keys back
  into the scores' `idempotency_key` (or set your own from the start) and nothing is written twice.
- `metadata` may not use the keys the platform reserves (`adjudicated`, `accepted_by`,
  `mt_assisted`, `self_review`, `verification_id`); the SDK raises `ValueError` before sending.

## evaluate()

`evaluate()` runs your task on the client for every (example, trial) of a dataset and reports the
outputs; the platform runs the gating evaluators and gives the verdict. The key needs
`experiment_write` (plus `trace_ingest` to report spans). A runnable example:
[examples/evaluate_example.py](examples/evaluate_example.py).

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

- **Task**: `task(input)`; when the task needs the example's initial state, name its second positional
  parameter `initial_state` (`task(input, initial_state)` gets a copy of the example's
  `initial_state`); when it needs the whole example (`reference`, `metadata`, ...), write
  `task(input, example)`. When examples carry a non-empty `initial_state` and the task takes only
  `input`, the SDK logs a warning.
- **Output**: shaped the way the platform shapes HTTP target outputs: a string becomes
  `{"text": ...}`, a dict stays as it is, None becomes `{}`, and any other JSON value (a list, a number,
  a boolean) becomes `{"json": ...}`. NaN / Infinity become null and lone UTF-16 surrogates become
  U+FFFD; an output that cannot be encoded as JSON (an arbitrary object, a value containing itself,
  nesting deeper than 100 levels) is a `task_error` (`output_not_json`), and so is an output over
  256 KiB (`output_too_large`). A task exception is recorded as `task_error` and a timeout as
  `timeout`; none of these makes `evaluate()` raise.
- **Examples**: by default the ready dev examples of the dataset's current version; `example_keys=`
  picks a subset, `dataset_version=` pins a version, `trials=` runs each example 1–10 times.
- **Client evaluators** are diagnostic and never change the verdict. They run after the item's turn
  has ended, outside the item's trace: spans they start (e.g. an instrumented model call) are
  dropped, so they never mix into the agent trajectory the platform's evaluators read and never
  become production traces. **Their names must be published score configs** and their values must
  fit them, otherwise the platform refuses those scores and the SDK reports the item again without
  them and logs a warning (after the same set of names is refused 3 times and the platform never
  accepted it, the rest of the run's items are reported without them right away); with `trials` > 1,
  an invalid score of one trial never costs another trial its scores. Score metadata keys the
  platform reserves (`adjudicated`, `accepted_by`, ...) are dropped with a warning.
- **Server evaluators** are evaluator version ids published on the platform (gating), or
  `{"evaluator_version_id": ..., "role": "diagnostic"}`.
- **Timeouts and concurrency**: when `timeout_s` passes, the run is recorded as `timeout`; but a thread
  cannot be killed, so the task keeps one of the `max_concurrency` places until it returns, and a hung
  downstream service never gets more than `max_concurrency` calls at once. When every place is held
  by a task that timed out and still runs, and none frees up within another `timeout_s`, the run
  stops (not finalized) with a `RuntimeError` naming the experiment id.
- Each run has its own trace (root span `evaluate`) containing the spans your task creates; the SDK
  exports them after the item is reported, the platform merges them into the experiment trace
  (`origin = experiment`), and they never show up in production threads. Through a Collector this
  needs `init(export_processor=...)` (see [Existing OpenTelemetry setups](#existing-opentelemetry-setups)),
  otherwise the spans cannot be held and `evaluate()` logs a warning.
- **Report retries**: `items:batch` and `:finalize` are idempotent, so the SDK retries them up to 8
  times on 429, 5xx and network errors (about 40 seconds of backoff in all, plus any `Retry-After`;
  set it with `Client(report_retries=...)`), and a short platform outage does not stop the run.
  Creating the experiment has no idempotency key and is retried only when the request surely did not
  arrive; after a gateway 502 / 504, a read timeout and the like, the experiment may exist, and the
  error message says to check the experiments page first.
- `result.url` is the experiment page on the platform; `result.items` holds every run as recorded
  locally (status, output, latency, trace id, client scores); with `wait=False` it does not wait for
  the platform's evaluation.
- The async version `aevaluate()` awaits async tasks (cancelled at `timeout_s`); sync tasks, sync
  evaluators and the SDK's REST calls run in dedicated daemon threads, never in the event loop's
  default executor; the timeout clock starts when the task starts.

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

### Interruption (Ctrl-C / cancel)

On Ctrl-C (or when the task running `aevaluate()` is cancelled), also while the last items are being
reported: no new runs are scheduled; **the finished items are reported** and the spans of reported
items are exported, while the spans of the other items are discarded (exported, they would become
production traces), even on a second Ctrl-C during that cleanup; **finalize is not called**, so the
experiment stays open; a warning names the experiment id and how many items were
reported, and `KeyboardInterrupt` / `CancelledError` is raised again. Finish it later yourself (the
unreported items stay pending), or leave it: the platform finishes it 24 hours after it was created.

```python
import insightmon

with insightmon.Client() as client:
    client.finalize_experiment("<experiment id>")
```

The SDK's own errors (e.g. a failed report) do not finalize either; the `InsightMonError` message
names the experiment id.

## Replay receipts

The platform can also call your service (an HTTP target) to replay a dataset. Every request carries
`traceparent: 00-<otel_trace_id>-<random id>-01`, and the platform pre-creates the experiment trace
with that trace id. When the spans your service creates while handling the request join that context,
they are merged into the platform's trace, and the platform reads **the agent version and component
versions under test** from them (the receipt, `version_source = trace_attr`).

```python
import insightmon

insightmon.init(agent_name="weather-agent", agent_version="2.1.0")


async def app(scope, receive, send):
    """Your ASGI application (FastAPI, Starlette, ...)."""


app = insightmon.InsightMonASGIMiddleware(app)
```

- `InsightMonASGIMiddleware` / `InsightMonWSGIMiddleware` make the request's `traceparent` the current
  context; they create no server span (use your framework's instrumentation for that). The WSGI
  middleware keeps one context while the whole response is consumed (every step of a generator
  included), so spans started in a generator nest as usual. With Starlette /
  FastAPI you can write `app.add_middleware(insightmon.InsightMonASGIMiddleware)`, with Flask
  `app.wsgi_app = insightmon.InsightMonWSGIMiddleware(app.wsgi_app)`; without a middleware, call
  `insightmon.extract_context(headers)`.
- The request handler uses `insightmon.turn()` as usual: it becomes the local root span in the
  platform's trace, and the SDK writes the agent and component versions on it.
- The response body may also carry `"receipt": insightmon.receipt()` (a response field receipt: it
  wins over the span receipt and does not depend on spans arriving in time; with both,
  `version_source = both` and disagreements are recorded as conflicts).
- A complete runnable service: [examples/http_agent.py](examples/http_agent.py) (needs
  `pip install uvicorn`); register it as an HTTP target on the platform (URL pointing at
  `/v1/respond`, the platform's default request template).

**Timing**: after an item finishes, the platform waits the target's `receipt_wait_s` (default 120
seconds) before reading the spans. The SDK exports in batches every 5 seconds by default, well within
that.

**`receipt_wait_s = 0`**: the platform does not wait, so the spans must arrive before the response.
Use `InsightMonASGIMiddleware(app, flush_on_traceparent=True)` (the WSGI middleware takes the same
argument): for requests with a `traceparent` it exports the spans before sending the last chunk of the
response, waiting at most `flush_timeout_s` (default 5 seconds). When your callers propagate W3C
context, nearly every request carries a `traceparent`, and every one of them would wait for an
export: use it in a deployment that only serves replays, or pass `flush_when=` to flush for replay
requests only. The platform sends no header that marks a replay by itself; let the replay target add
one through its `headers_ref`:

```python
import insightmon


async def app(scope, receive, send):
    """Your ASGI application, serving replays and live traffic alike."""


def is_replay(scope) -> bool:
    """A header the replay target sends (configured in its headers_ref)."""
    return (b"x-replay", b"1") in scope.get("headers", [])


app = insightmon.InsightMonASGIMiddleware(app, flush_when=is_replay, flush_timeout_s=3)
```

`flush_when` gets the ASGI scope (the WSGI middleware passes the environ) and is called only for
requests with a `traceparent`. The WSGI middleware exports before the server gets the last chunk of
the response: right after the application returns a list, or by reading one chunk ahead for a
streamed response (which is then sent one chunk later).

**Do not export before the response when the platform runs on SQLite**: the platform holds the
database write lock while it waits for the response, so the export blocks until it times out, the
item is recorded as `timeout` and the spans are lost. On SQLite leave `flush_on_traceparent` /
`flush_when` off and set `receipt_wait_s` to at least a few seconds; on PostgreSQL both work.

## Configuration

`init()` arguments (all optional):

| Argument | Environment variable | Meaning |
|---|---|---|
| `base_url` | `INSIGHTMON_BASE_URL` | the platform's root URL; the SDK exports only with both URL and key, and with neither it only sets attributes |
| `api_key` | `INSIGHTMON_API_KEY` | an `imk_...` key |
| `service_name` | `OTEL_SERVICE_NAME` | `service.name` of a provider the SDK creates (default: `agent_name`) |
| `agent_name` / `agent_version` | — | `gen_ai.agent.name` / `gen_ai.agent.version` on every span |
| `components` | — | a list of `Component` or `{type, name, version?, content_hash?}` |
| `language` | — | the default declared language: `zh` / `en` / `ja` / `mixed` / `und` |
| `capture_content` | — | send message content (default `False`) |
| `content_attributes` | — | more attributes to strip while content capture is off (names, or prefixes ending in `*`); see [Content capture and privacy](#content-capture-and-privacy) |
| `environment` | — | the platform environment (`deployment.environment.name`, at most 100 characters) |
| `tracer_provider` / `logger_provider` | — | join the given providers; content log events of instrumentations are forwarded only through `logger_provider` |
| `set_global` | — | make a provider the SDK creates the global one (default `True`) |
| `export_processor` | — | your own span export processor (e.g. a `BatchSpanProcessor` to a Collector): the SDK hands it the spans, and the spans of `evaluate()` are held on that path too; do not also add it to the provider |

`agent_name`, `agent_version`, `environment` and the names and versions of components are stripped of
surrounding whitespace (the platform strips them when it reads them back; a difference would be
recorded as a receipt conflict). Give the final address as the base URL: the SDK never follows
redirects (the key must not travel to another host), and it refuses URLs with a user name or
password. Configuration errors never repeat the value you gave, so even with the key and the URL
swapped, the key does not show up in the error.

`Client(base_url=None, api_key=None, *, timeout=30.0, max_retries=3, report_retries=8)` and
`evaluate(..., base_url=None, api_key=None)` take the arguments, then the `init()` settings, then the
environment. `max_retries` counts the retries of ordinary requests, `report_retries` those of
`items:batch` and `:finalize`.

The SDK's OTLP exporters send only the `Authorization`, `Content-Type`, `Content-Encoding` and
`User-Agent` headers: the generic `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TRACES_HEADERS`
and `OTEL_EXPORTER_OTLP_LOGS_HEADERS` (which may carry another vendor's credentials) do not apply to
them. Each export retries 429, 5xx and network errors, honoring `Retry-After` or backing off, for at
most 10 seconds (the platform deduplicates by span id and record digest, so a retry never writes
twice). Tune export batching with the standard OpenTelemetry variables (e.g.
`OTEL_BSP_SCHEDULE_DELAY`, in milliseconds).

Key scopes:

| Scope | For |
|---|---|
| `trace_ingest` | traces and evaluation logs (`init()`) |
| `score_write` | `Client.scores_batch()` |
| `experiment_write` | `evaluate()` / `aevaluate()` and the experiment methods of `Client` |

`flush(timeout_s=10)` exports now and waits at most `timeout_s` seconds; it returns `False` on timeout
or when an export failed meanwhile. `shutdown(timeout_s=10)` exports what is left and closes, also
within its deadline (an unreachable platform never holds up process exit); what could not be
exported is dropped with a warning. It runs automatically at process exit (with a 5-second
deadline). Neither does network I/O while holding the SDK's lock, so other threads keep starting
spans. The handle `init()` returns also works as a context manager.

## Troubleshooting

- **Nothing shows up on the platform**: check that both `INSIGHTMON_BASE_URL` and
  `INSIGHTMON_API_KEY` are set (only one raises `config_error`); call `insightmon.shutdown()` or
  `flush()` before a short script ends (`flush()` returning `False` means an export failed or timed
  out; the warnings say why); turn on logging with
  `logging.getLogger("insightmon").setLevel(logging.DEBUG)` and
  `logging.getLogger("opentelemetry").setLevel(logging.DEBUG)`.
- **Warning `the platform answered an export of spans with HTTP 301 (redirect to ...)`**: the base URL
  is redirected (e.g. http to https, or a path prefix too many or too few). The SDK never follows
  redirects, so these data are lost: set the base URL to the final address the warning names;
  `insightmon.rejections().redirects` keeps the count. REST calls fail with a `redirect` error.
- **401 / 403**: the key is revoked, belongs to another project or lacks a scope;
  `insightmon.Client().api_key_info()` shows its project and scopes.
- **Traces do not join a thread**: the spans are outside `thread()`, or the context was lost in a
  thread pool (see [thread pools](#asyncio-thread-pools-and-processes)).
- **The thread does not close**: the last turn has no `end_thread`; the platform closes idle threads.
- **Warning `the platform refused N log records: score_config_not_found`**: an evaluation name has no
  published score config; `insightmon.rejections()` keeps the counts.
- **Client scores of `evaluate()` are missing**: likewise, names must be published score configs; the
  warning names the refused scores.
- **An interrupted `evaluate()` leaves the experiment open**: see
  [Interruption](#interruption-ctrl-c--cancel) and finish it with `Client().finalize_experiment(id)`.
- **`evaluate()` raises a `RuntimeError` about timed-out tasks that still run**: a downstream service
  hangs, and every place of `max_concurrency` is held by a task that timed out and has not returned.
  Find out why it hangs, or raise `timeout_s`; the experiment is not finalized, as above.
- **Warning `the spans of evaluate() are exported as they end, not held`**: the spans are exported by
  a processor you added to the provider yourself, so the SDK cannot hold them; use
  `init(export_processor=...)` instead (see [Existing OpenTelemetry setups](#existing-opentelemetry-setups)).
- **Creating the experiment fails with `the experiment may have been created`**: the request reached
  the server (a gateway 502 / 504, a read timeout, ...); check the experiments page on the platform
  before running again.
- **With `capture_content`, content that instrumentations emit as log events is missing**: pass the
  `LoggerProvider` they use to `init(logger_provider=...)`; see
  [Content capture and privacy](#content-capture-and-privacy).
- **Replay items all `timeout`, receipt `none`**: the platform runs on SQLite and the service uses
  `flush_on_traceparent`; see [Replay receipts](#replay-receipts).
- **Receipt `version_source = none`**: the spans did not arrive within `receipt_wait_s`, or the service
  did not join the `traceparent` (no middleware, or the context was lost in a thread pool).
- **Proxies**: OTLP export and REST calls both honor `HTTPS_PROXY` / `NO_PROXY`.

## Compatibility

- Python 3.10, 3.11, 3.12 and 3.13 (all run in CI).
- OpenTelemetry Python 1.35 or later (`opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`), tested on 1.35 through 1.45; httpx 0.27 or later.
- Platform: the OTLP/HTTP endpoints (`/otlp/v1/traces`, `/otlp/v1/logs`) and key endpoints
  (`/api/v1`) of the current `main` of
  [InsightMon/insightmon-ai-eval](https://github.com/InsightMon/insightmon-ai-eval); the contract is in
  [docs/contract.md](docs/contract.md). On a platform backed by SQLite mind the limit in
  [Replay receipts](#replay-receipts).
- Web frameworks: the ASGI / WSGI middlewares depend on no framework.

## Development

```bash
make install            # uv sync
make lint               # ruff check, ruff format --check, mypy
make test               # unit tests (no integration tests)
make test-integration   # integration tests: scripts/integration.sh
make build              # uv build
```

The integration tests run against a real platform: `scripts/integration.sh` boots a throwaway
platform instance (SQLite, demo data, local only) from `INSIGHTMON_PLATFORM_DIR` (default
`../insightmon-ai-eval`; build its frontend there first with `make build`), creates an API key, runs
`tests/integration/` and shuts the platform down. To run them against an existing platform, set
`INSIGHTMON_IT_BASE_URL` and a project admin account (`INSIGHTMON_IT_ADMIN_EMAIL` /
`INSIGHTMON_IT_ADMIN_PASSWORD`); see [tests/integration/conftest.py](tests/integration/conftest.py).
A key the script created is revoked when it ends (also on failure or Ctrl-C), so no usable key is left
behind on a shared platform.

Docs:

- [docs/conventions.md](docs/conventions.md): every attribute and event name (a test checks it against
  `semconv.py`).
- [docs/contract.md](docs/contract.md): the contract as read from the platform code (endpoints,
  attributes, log events, replay, the evaluate flow, differences and gaps).
- [docs/design.md](docs/design.md): the public API and the module layout.
