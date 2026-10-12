"""Exporters: request splitting, content stripping, wiring, and the OTLP wire format against a
local HTTP endpoint (127.0.0.1, no external network)."""

from __future__ import annotations

import gzip
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest
from opentelemetry import context as otel_context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN, set_span_in_context

import insightmon
from insightmon import _export, semconv
from insightmon._config import resolve_settings
from insightmon._errors import InsightMonError
from insightmon._export import (
    ContentStrippingSpanExporter,
    ExportControl,
    PlatformLogExporter,
    PlatformSpanExporter,
    SplittingLogExporter,
    SplittingSpanExporter,
    build_log_exporter,
    build_span_exporter,
    strip_span,
)
from insightmon.logs import emit_event

from .collector import Collector, any_value
from .conftest import API_KEY, InMemoryLogExporter


class Recorder(SpanExporter):
    def __init__(self, result: SpanExportResult = SpanExportResult.SUCCESS) -> None:
        self.batches: list[list[ReadableSpan]] = []
        self.result = result

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.batches.append(list(spans))
        return self.result


class LogRecorder:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []
        self.shut_down = False

    def export(self, batch: Sequence[Any]) -> Any:
        self.batches.append(list(batch))
        return _export.LogResult.SUCCESS

    def shutdown(self) -> None:
        self.shut_down = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def make_spans(count: int, payload: int = 400, limits: SpanLimits | None = None) -> list[Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=limits) if limits else TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("app")
    for index in range(count):
        with tracer.start_as_current_span(f"s{index}", attributes={"blob": "x" * payload}):
            pass
    return list(exporter.get_finished_spans())


def make_logs(count: int, payload: int = 400) -> list[Any]:
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = provider.get_logger("app")
    context = set_span_in_context(INVALID_SPAN)
    for index in range(count):
        emit_event(logger, context, "gen_ai.evaluation.result", {"n": index, "blob": "x" * payload})
    return list(exporter.get_finished_logs())


# --------------------------------------------------------------------------- splitting


def test_span_batches_are_split_by_serialized_size() -> None:
    spans = make_spans(10)
    max_bytes = 1500
    inner = Recorder()
    assert (
        SplittingSpanExporter(inner, max_bytes=max_bytes).export(spans) is SpanExportResult.SUCCESS
    )
    assert len(inner.batches) > 1
    assert [s.name for batch in inner.batches for s in batch] == [s.name for s in spans]
    for batch in inner.batches:
        assert encode_spans(batch).ByteSize() <= max_bytes


def test_small_span_batches_go_out_whole() -> None:
    spans = make_spans(3)
    inner = Recorder()
    SplittingSpanExporter(inner).export(spans)
    assert len(inner.batches) == 1
    assert SplittingSpanExporter(inner).export([]) is SpanExportResult.SUCCESS
    assert len(inner.batches) == 1


def test_a_single_oversized_span_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    spans = [*make_spans(1, payload=50), *make_spans(1, payload=5000), *make_spans(1, payload=50)]
    inner = Recorder()
    with caplog.at_level(logging.WARNING, logger="insightmon._export"):
        result = SplittingSpanExporter(inner, max_bytes=2000).export(spans)
    assert result is SpanExportResult.FAILURE
    assert sum(len(batch) for batch in inner.batches) == 2
    assert any("dropping one span" in record.getMessage() for record in caplog.records)


def test_inner_failures_are_reported() -> None:
    inner = Recorder(SpanExportResult.FAILURE)
    assert SplittingSpanExporter(inner).export(make_spans(2)) is SpanExportResult.FAILURE


def test_log_batches_are_split_by_count_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semconv, "OTLP_MAX_LOG_RECORDS", 3)
    records = make_logs(7, payload=10)
    inner = LogRecorder()
    exporter = SplittingLogExporter(inner)
    assert exporter.export(records) is _export.LogResult.SUCCESS
    assert [len(batch) for batch in inner.batches] == [3, 3, 1]
    inner.batches.clear()
    monkeypatch.setattr(semconv, "OTLP_MAX_LOG_RECORDS", 10_000)
    big = make_logs(6, payload=500)
    SplittingLogExporter(inner, max_bytes=1500).export(big)
    assert len(inner.batches) > 1
    for batch in inner.batches:
        assert encode_logs(batch).ByteSize() <= 1500
    assert exporter.export([]) is _export.LogResult.SUCCESS
    assert exporter.force_flush() is True
    exporter.shutdown()
    assert inner.shut_down


def test_an_oversized_log_record_is_dropped() -> None:
    inner = LogRecorder()
    result = SplittingLogExporter(inner, max_bytes=300).export(make_logs(1, payload=1000))
    assert result is _export.LogResult.FAILURE
    assert inner.batches == []


# --------------------------------------------------------------------------- stripping


def content_span(limits: SpanLimits | None = None) -> ReadableSpan:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=limits) if limits else TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("app").start_as_current_span("chat model") as span:
        span.set_attributes(
            {
                semconv.INPUT_MESSAGES: "[]",
                semconv.SYSTEM_INSTRUCTIONS: "[]",
                "gen_ai.completion.0.content": "secret",
                semconv.REQUEST_MODEL: "small-model",
                semconv.OPERATION_NAME: "chat",
            }
        )
        span.add_event(semconv.DETAILS_EVENT, {semconv.OUTPUT_MESSAGES: "[]"})
        span.add_event(semconv.USER_MESSAGE_EVENT, {"content": "hi"})
        span.add_event("gen_ai.content.prompt", {"gen_ai.prompt": "hi", "kept": 1})
        span.add_event("retry", {"attempt": 2})
    (finished,) = exporter.get_finished_spans()
    return finished


def test_strip_span_removes_content_attributes_and_events() -> None:
    original = content_span()
    stripped = strip_span(original)
    assert stripped is not original
    assert dict(stripped.attributes or {}) == {
        semconv.REQUEST_MODEL: "small-model",
        semconv.OPERATION_NAME: "chat",
    }
    assert [(e.name, dict(e.attributes or {})) for e in stripped.events] == [
        ("gen_ai.content.prompt", {"kept": 1}),
        ("retry", {"attempt": 2}),
    ]
    assert stripped.context == original.context
    assert stripped.parent == original.parent
    assert stripped.start_time == original.start_time
    assert stripped.end_time == original.end_time
    assert stripped.resource == original.resource
    assert stripped.instrumentation_scope == original.instrumentation_scope
    assert encode_spans([stripped]).ByteSize() > 0  # still encodable


def test_strip_span_keeps_dropped_counts() -> None:
    limited = content_span(SpanLimits(max_span_attributes=3, max_events=2))
    assert limited.dropped_attributes > 0
    assert limited.dropped_events > 0
    stripped = strip_span(limited)
    assert stripped.dropped_attributes == limited.dropped_attributes
    assert stripped.dropped_events == limited.dropped_events


def test_spans_without_content_pass_through_unchanged() -> None:
    (span,) = make_spans(1)
    assert strip_span(span) is span
    inner = Recorder()
    ContentStrippingSpanExporter(inner).export([span, content_span()])
    (batch,) = inner.batches
    assert batch[0] is span
    assert not any(semconv.is_content_attribute(k) for k in batch[1].attributes or {})


#: Content attributes of common instrumentations: each must be stripped with capture off.
THIRD_PARTY_CONTENT = [
    "input.value",
    "output.value",
    "traceloop.entity.input",
    "traceloop.entity.output",
    "llm.prompt_template.variables",
    "llm.input_messages.0.message.content",
    "llm.output_messages.0.message.content",
    "llm.prompts.0",
    "gen_ai.prompt.0.content",
    "gen_ai.completion.0.content",
    "retrieval.documents.0.document.content",
    "reranker.input_documents.1.document.content",
    "reranker.output_documents.0.document.content",
    "embedding.embeddings.0.embedding.text",
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.trace.input",
    "langfuse.trace.output",
    "mlflow.spanInputs",
    "mlflow.spanOutputs",
    "mlflow.chat.messages",
    "all_messages_events",
    "ai.prompt.messages",
    "ai.response.text",
    "ai.toolCall.args",
    "ai.embeddings",
    "pydantic_ai.all_messages",
    "final_result",
    "tool_arguments",
    "request_data",
    "response_data",
    "gcp.vertex.agent.llm_request",
    "gcp.vertex.agent.tool_response",
]
#: Look-alikes that carry no message content and must be kept.
NOT_CONTENT = [
    "retrieval.documents_count",
    "embedding.model_name",
    "llm.model_name",
    "traceloop.entity.name",
    "langfuse.observation.type",
    "mlflow.spanType",
    "ai.model.id",
    "ai.usage.promptTokens",
    "gcp.vertex.agent.invocation_id",
    semconv.REQUEST_MODEL,
]


@pytest.mark.parametrize("key", THIRD_PARTY_CONTENT)
def test_third_party_content_attributes_are_stripped(key: str) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    attributes = {key: "private text", **dict.fromkeys(NOT_CONTENT, "kept")}
    with provider.get_tracer("app").start_as_current_span("step", attributes=attributes) as span:
        span.add_event("details", {key: "private text", "attempt": 1})
    (finished,) = exporter.get_finished_spans()
    stripped = strip_span(finished)
    assert dict(stripped.attributes or {}) == dict.fromkeys(NOT_CONTENT, "kept")
    assert [dict(e.attributes or {}) for e in stripped.events] == [{"attempt": 1}]


def test_content_attributes_of_your_own_are_stripped_too() -> None:
    settings = resolve_settings(
        base_url="https://eval.example.test",
        api_key=API_KEY,
        content_attributes=["acme.prompt", " acme.documents.* "],
        env={},
    )
    assert settings.content_attributes == ("acme.prompt", "acme.documents.*")
    exporter = build_span_exporter(settings)
    assert isinstance(exporter, ContentStrippingSpanExporter)
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    attributes = {
        "acme.prompt": "private",
        "acme.documents.0.text": "private",
        "acme.prompt_version": "kept",
        "input.value": "private",
    }
    with provider.get_tracer("app").start_as_current_span("step", attributes=attributes):
        pass
    inner = Recorder()
    ContentStrippingSpanExporter(inner, is_content=exporter._is_content).export(
        memory.get_finished_spans()
    )
    ((span,),) = inner.batches
    assert dict(span.attributes or {}) == {"acme.prompt_version": "kept"}


@pytest.mark.parametrize("patterns", ["acme.prompt", ["*"], [""], [3]])
def test_content_attributes_are_checked(patterns: Any) -> None:
    with pytest.raises(ValueError, match="content_attributes"):
        resolve_settings(content_attributes=patterns, env={})


# --------------------------------------------------------------------------- building


def test_build_exporters_need_export_settings() -> None:
    with pytest.raises(InsightMonError, match="base URL"):
        build_span_exporter(resolve_settings(env={}))
    with pytest.raises(InsightMonError, match="base URL"):
        build_log_exporter(resolve_settings(env={}))


def test_build_span_exporter_layers() -> None:
    settings = resolve_settings(base_url="https://eval.example.test", api_key=API_KEY, env={})
    control = ExportControl()
    stripping = build_span_exporter(settings, control)
    assert isinstance(stripping, ContentStrippingSpanExporter)
    assert isinstance(stripping._inner, SplittingSpanExporter)
    platform = stripping._inner._inner
    assert isinstance(platform, PlatformSpanExporter)
    assert platform.endpoint == "https://eval.example.test/otlp/v1/traces"
    assert platform.control is control
    assert API_KEY not in repr(platform)
    capturing = build_span_exporter(
        resolve_settings(
            base_url="https://eval.example.test", api_key=API_KEY, capture_content=True, env={}
        )
    )
    assert isinstance(capturing, SplittingSpanExporter)
    assert capturing.force_flush() is True
    capturing.shutdown()
    logs = build_log_exporter(settings, control)
    assert isinstance(logs, SplittingLogExporter)
    assert isinstance(logs._inner, PlatformLogExporter)
    assert logs._inner.endpoint == "https://eval.example.test/otlp/v1/logs"
    assert logs._inner.control is control


def test_oversized_items_count_as_failures() -> None:
    control = ExportControl()
    SplittingSpanExporter(Recorder(), max_bytes=2000, control=control).export(
        make_spans(1, payload=5000)
    )
    SplittingLogExporter(LogRecorder(), max_bytes=300, control=control).export(
        make_logs(1, payload=1000)
    )
    assert control.failures == 2
    assert (control.dropped("spans"), control.dropped("log records")) == (1, 1)


# --------------------------------------------------------------------------- platform exporter

Handler = Callable[[httpx.Request], httpx.Response]


def platform_exporter(
    *answers: httpx.Response | BaseException | Handler,
    timeout_s: float = 10.0,
    control: ExportControl | None = None,
) -> tuple[PlatformSpanExporter, list[httpx.Request]]:
    """A span exporter whose requests get `answers` in turn (the last one repeats)."""
    seen: list[httpx.Request] = []
    queue = list(answers)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return answer(request)

    exporter = PlatformSpanExporter(
        "https://eval.example.test/otlp/v1/traces",
        API_KEY,
        control=control,
        timeout_s=timeout_s,
        transport=httpx.MockTransport(handle),
    )
    return exporter, seen


@pytest.fixture
def no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_export, "_jitter", lambda: 0.0)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried(status: int, no_jitter: None) -> None:
    exporter, seen = platform_exporter(httpx.Response(status), httpx.Response(200))
    assert exporter.export(make_spans(2)) is SpanExportResult.SUCCESS
    assert len(seen) == 2
    assert exporter.control.failures == 0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_other_statuses_fail_at_once(
    status: int, no_jitter: None, caplog: pytest.LogCaptureFixture
) -> None:
    exporter, seen = platform_exporter(
        httpx.Response(status, json={"code": "some_code", "params": {}, "message": ""})
    )
    with caplog.at_level(logging.WARNING, logger="insightmon._export"):
        assert exporter.export(make_spans(3)) is SpanExportResult.FAILURE
    assert len(seen) == 1
    assert exporter.control.failures == 1
    assert exporter.control.dropped("spans") == 3
    (record,) = [r for r in caplog.records if "could not export" in r.getMessage()]
    assert f"HTTP {status} some_code" in record.getMessage()
    assert API_KEY not in record.getMessage()


def test_network_errors_are_retried(no_jitter: None) -> None:
    exporter, seen = platform_exporter(httpx.ConnectError("refused"), httpx.Response(200))
    assert exporter.export(make_spans(1)) is SpanExportResult.SUCCESS
    assert len(seen) == 2


def test_retries_stop_at_the_export_deadline() -> None:
    exporter, seen = platform_exporter(
        httpx.Response(503, headers={"Retry-After": "30"}), timeout_s=0.5
    )
    started = time.monotonic()
    assert exporter.export(make_spans(1)) is SpanExportResult.FAILURE
    assert time.monotonic() - started < 0.5
    assert len(seen) == 1  # waiting 30 s would pass the deadline: give up at once


def test_abort_ends_backoff_waits_and_later_exports() -> None:
    control = ExportControl()
    exporter, seen = platform_exporter(
        httpx.Response(503, headers={"Retry-After": "5"}), control=control
    )
    results: list[SpanExportResult] = []
    worker = threading.Thread(target=lambda: results.append(exporter.export(make_spans(1))))
    started = time.monotonic()
    worker.start()
    time.sleep(0.2)
    control.abort()
    worker.join(5)
    assert results == [SpanExportResult.FAILURE]
    assert time.monotonic() - started < 2.0
    assert exporter.export(make_spans(1)) is SpanExportResult.FAILURE
    assert len(seen) == 1  # nothing is sent once aborted


def test_control_deadline_caps_exports() -> None:
    control = ExportControl()
    control.set_deadline(time.monotonic() - 1)
    exporter, seen = platform_exporter(httpx.Response(200), control=control)
    assert exporter.export(make_spans(1)) is SpanExportResult.FAILURE
    assert seen == []


def test_exports_run_with_instrumentation_suppressed() -> None:
    suppressed: list[Any] = []

    def handle(request: httpx.Request) -> httpx.Response:
        suppressed.append(otel_context.get_value(_SUPPRESS_INSTRUMENTATION_KEY))
        return httpx.Response(200)

    exporter, _ = platform_exporter(handle)
    exporter.export(make_spans(1))
    assert suppressed == [True]
    assert otel_context.get_value(_SUPPRESS_INSTRUMENTATION_KEY) is None


def test_exporter_after_shutdown_fails() -> None:
    exporter, seen = platform_exporter(httpx.Response(200))
    exporter.shutdown()
    assert exporter.export(make_spans(1)) is SpanExportResult.FAILURE
    assert seen == []


def test_log_exporter_posts_gzip_protobuf() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    exporter = PlatformLogExporter(
        "https://eval.example.test/otlp/v1/logs", API_KEY, transport=httpx.MockTransport(handle)
    )
    assert exporter.export(make_logs(2)) is _export.LogResult.SUCCESS
    assert exporter.export([]) is _export.LogResult.SUCCESS
    (request,) = seen
    assert request.headers["content-encoding"] == "gzip"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    decoded = ExportLogsServiceRequest()
    decoded.ParseFromString(gzip.decompress(request.content))
    assert sum(len(sl.log_records) for rl in decoded.resource_logs for sl in rl.scope_logs) == 2
    assert exporter.force_flush() is True
    exporter.shutdown()


# --------------------------------------------------------------------------- wire format


def test_otlp_wire_format(collector: Collector) -> None:
    insightmon.init(
        base_url=collector.base_url,
        api_key=API_KEY,
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=[{"type": "prompt", "name": "system", "version": "v7"}],
        set_global=False,
    )
    with insightmon.thread("chat-42"), insightmon.turn() as turn_span:
        turn_span.set_attribute("input.value", "Will it rain?")  # third-party style content
    insightmon.log_evaluation("helpfulness", value=0.75, span=turn_span)
    assert insightmon.flush(10.0) is True

    assert [r["path"] for r in collector.requests] == ["/otlp/v1/traces", "/otlp/v1/logs"]
    for request in collector.requests:
        assert request["headers"]["authorization"] == f"Bearer {API_KEY}"
        assert request["headers"]["content-type"] == "application/x-protobuf"
        assert request["headers"]["content-encoding"] == "gzip"

    traces = ExportTraceServiceRequest()
    traces.ParseFromString(collector.body(0))
    (resource_spans,) = traces.resource_spans
    resource = {kv.key: any_value(kv.value) for kv in resource_spans.resource.attributes}
    assert resource["service.name"] == "weather-assistant"
    assert "service.version" not in resource
    (span,) = [s for scope in resource_spans.scope_spans for s in scope.spans]
    attributes = {kv.key: any_value(kv.value) for kv in span.attributes}
    assert attributes[semconv.GEN_AI_CONVERSATION_ID] == "chat-42"
    assert attributes[semconv.AGENT_VERSION] == "1.4.0"
    assert attributes[semconv.component_attribute("prompt", "system")] == "v7"
    assert attributes[semconv.OPERATION_NAME] == "invoke_agent"
    assert "input.value" not in attributes  # capture_content=False

    logs = ExportLogsServiceRequest()
    logs.ParseFromString(collector.body(1))
    (record,) = [r for rl in logs.resource_logs for sl in rl.scope_logs for r in sl.log_records]
    assert record.event_name == "gen_ai.evaluation.result"
    record_attributes = {kv.key: any_value(kv.value) for kv in record.attributes}
    assert record_attributes["event.name"] == "gen_ai.evaluation.result"
    assert record_attributes["gen_ai.evaluation.name"] == "helpfulness"
    assert record_attributes["gen_ai.evaluation.score.value"] == 0.75
    assert record.trace_id == span.trace_id
    assert record.span_id == span.span_id


def test_response_id_records_carry_no_span_context_on_the_wire(collector: Collector) -> None:
    insightmon.init(base_url=collector.base_url, api_key=API_KEY, set_global=False)
    with insightmon.turn():
        insightmon.set_response_id("resp-1")
        insightmon.log_evaluation("helpfulness", label="pass", response_id="resp-1")
    insightmon.flush(10.0)
    logs = ExportLogsServiceRequest()
    logs.ParseFromString(collector.body(1))
    (record,) = [r for rl in logs.resource_logs for sl in rl.scope_logs for r in sl.log_records]
    # The platform treats empty or all-zero ids as "no span context" (otlp_logs.py).
    assert not any(record.trace_id)
    assert not any(record.span_id)
    assert len(record.trace_id) in (0, 16)
    assert len(record.span_id) in (0, 8)


#: Headers any HTTP client adds; everything else must be one the SDK chose.
TRANSPORT_HEADERS = {"host", "content-length", "accept", "accept-encoding", "connection"}


def test_generic_otlp_header_variables_never_reach_the_platform(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    ):
        monkeypatch.setenv(name, "x-vendor-key=vendor-secret,authorization=Bearer vendor-token")
    insightmon.init(base_url=collector.base_url, api_key=API_KEY, set_global=False)
    with insightmon.turn() as span:
        pass
    insightmon.log_evaluation("helpfulness", value=1.0, span=span)
    assert insightmon.flush(10.0) is True
    assert collector.paths() == ["/otlp/v1/traces", "/otlp/v1/logs"]
    for request in collector.requests:
        headers = request["headers"]
        assert set(headers) - TRANSPORT_HEADERS == {
            "authorization",
            "content-type",
            "content-encoding",
            "user-agent",
        }
        assert headers["authorization"] == f"Bearer {API_KEY}"
        assert headers["user-agent"].startswith("insightmon-eval-sdk/")
        assert "vendor" not in str(headers)


def test_redirects_are_not_followed_and_reported(
    collector: Collector, caplog: pytest.LogCaptureFixture
) -> None:
    location = "https://eval.example.test/otlp/v1/traces"
    collector.statuses["/otlp/v1/traces"] = 301
    collector.redirects["/otlp/v1/traces"] = location
    insightmon.init(base_url=collector.base_url, api_key=API_KEY, set_global=False)
    with caplog.at_level(logging.DEBUG, logger="insightmon._export"):
        for _ in range(2):
            with insightmon.turn():
                pass
            assert insightmon.flush(10.0) is False
    assert collector.paths() == ["/otlp/v1/traces", "/otlp/v1/traces"]  # never followed
    assert _export.rejections().redirects == 2
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "redirect" in r.getMessage()
    ]
    assert len(warnings) == 1  # once per location
    assert location in warnings[0].getMessage()
    assert "base URL" in warnings[0].getMessage()


# --------------------------------------------------------------------------- partial success


def test_partial_success_is_logged_once_per_reason_and_counted(
    collector: Collector, caplog: pytest.LogCaptureFixture
) -> None:
    refused = ExportLogsServiceResponse()
    refused.partial_success.rejected_log_records = 1
    refused.partial_success.error_message = "score_config_not_found"
    collector.answers["/otlp/v1/logs"] = ("application/x-protobuf", refused.SerializeToString())
    insightmon.init(base_url=collector.base_url, api_key=API_KEY, set_global=False)
    caplog.set_level(logging.DEBUG, logger="insightmon._export")
    for _ in range(2):
        with insightmon.turn() as span:
            pass
        insightmon.log_evaluation("unknown_metric", value=1.0, span=span)
        assert insightmon.flush(10.0) is True
    found = _export.rejections()
    assert (found.spans, found.log_records) == (0, 2)
    assert found.reasons == ("score_config_not_found",)
    messages = [r for r in caplog.records if "refused" in r.getMessage()]
    assert [r.levelno for r in messages] == [logging.WARNING, logging.DEBUG]
    assert "score_config_not_found" in messages[0].getMessage()


class _Answer:
    def __init__(self, content_type: str, content: bytes, status_code: int = 200) -> None:
        self.headers = {"content-type": content_type}
        self.content = content
        self.status_code = status_code


def test_partial_success_hook_reads_protobuf_and_json() -> None:
    spans = ExportTraceServiceResponse()
    spans.partial_success.rejected_spans = 3
    spans.partial_success.error_message = "imported_trace"
    _export.partial_success_hook("spans")(
        _Answer("application/x-protobuf", spans.SerializeToString())
    )
    _export.partial_success_hook("log records")(
        _Answer(
            "application/json",
            b'{"partialSuccess": {"rejectedLogRecords": 2, "errorMessage": "no_correlation"}}',
        )
    )
    _export.partial_success_hook("spans")(_Answer("application/x-protobuf", b""))  # all accepted
    _export.partial_success_hook("spans")(_Answer("text/plain", b"not json"))  # unreadable
    _export.partial_success_hook("spans")(_Answer("application/json", b"{}", status_code=503))
    found = _export.rejections()
    assert (found.spans, found.log_records) == (3, 2)
    assert found.reasons == ("imported_trace", "no_correlation")


def test_rejection_reasons_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_export, "MAX_REASONS", 2)
    hook = _export.partial_success_hook("log records")
    for reason in ("a", "b", "c"):
        body = f'{{"partialSuccess": {{"rejectedLogRecords": 1, "errorMessage": "{reason}"}}}}'
        hook(_Answer("application/json", body.encode()))
    assert _export.rejections().reasons == ("b", "c")
    assert _export.rejections().log_records == 3
