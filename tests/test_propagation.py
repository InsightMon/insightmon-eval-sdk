"""extract_context(), receipt() and the ASGI / WSGI middlewares."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import insightmon
from insightmon import _setup, propagation, semconv
from insightmon.propagation import InsightMonASGIMiddleware, InsightMonWSGIMiddleware

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"
COMPONENT_KEY = semconv.component_attribute("prompt", "system")


@pytest.fixture
def sdk(tracer_provider: TracerProvider) -> insightmon.InsightMonHandle:
    return insightmon.init(
        tracer_provider=tracer_provider,
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=[{"type": "prompt", "name": "system", "version": "v7", "content_hash": "h7"}],
    )


@pytest.fixture
def flushes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_flush(timeout_s: float = 10.0) -> bool:
        calls.append("flush")
        return True

    monkeypatch.setattr(_setup, "flush", fake_flush)
    return calls


def only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    (span,) = exporter.get_finished_spans()
    return span


def assert_continues_the_platform_trace(span: ReadableSpan) -> None:
    assert span.context.trace_id == int(TRACE_ID, 16)
    assert span.parent is not None
    assert span.parent.span_id == int(PARENT_ID, 16)
    assert span.parent.is_remote


# --------------------------------------------------------------------------- extract_context


@pytest.mark.parametrize(
    "headers",
    [
        {"traceparent": TRACEPARENT},
        {"TraceParent": TRACEPARENT, "tracestate": "vendor=1"},
        [(b"traceparent", TRACEPARENT.encode()), (b"tracestate", b"a=1"), (b"tracestate", b"b=2")],
        [("Traceparent", TRACEPARENT)],
    ],
)
def test_extract_context(headers: Any) -> None:
    context = insightmon.extract_context(headers)
    span_context = trace.get_current_span(context).get_span_context()
    assert span_context.trace_id == int(TRACE_ID, 16)
    assert span_context.span_id == int(PARENT_ID, 16)
    assert span_context.is_remote


def test_extract_context_joins_tracestate() -> None:
    context = insightmon.extract_context(
        [(b"traceparent", TRACEPARENT.encode()), (b"tracestate", b"a=1"), (b"tracestate", b"b=2")]
    )
    state = trace.get_current_span(context).get_span_context().trace_state
    assert dict(state) == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    "headers", [{}, {"traceparent": "garbage"}, {"traceparent": f"00-{'0' * 32}-{PARENT_ID}-01"}]
)
def test_extract_context_without_a_valid_traceparent_is_empty(headers: Any) -> None:
    context = insightmon.extract_context(headers)
    assert not trace.get_current_span(context).get_span_context().is_valid
    assert dict(context) == {}


def test_spans_started_in_an_extracted_context_join_the_trace(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    context = insightmon.extract_context({"traceparent": TRACEPARENT})
    with insightmon.get_tracer().start_as_current_span("handle", context=context):
        pass
    span = only_span(span_exporter)
    assert_continues_the_platform_trace(span)
    assert span.attributes is not None
    assert span.attributes[COMPONENT_KEY] == "v7"  # a local root: the trace_attr receipt


# --------------------------------------------------------------------------- receipt


def test_receipt_before_init() -> None:
    assert insightmon.receipt() == {"agent": {"name": None, "version": None}, "components": []}


def test_receipt_follows_settings_and_overrides(sdk: insightmon.InsightMonHandle) -> None:
    base = insightmon.receipt()
    assert base == {
        "agent": {"name": "weather-assistant", "version": "1.4.0"},
        "components": [{"type": "prompt", "name": "system", "version": "v7", "content_hash": "h7"}],
    }
    with insightmon.thread("chat-1"):
        insightmon.set_component("tool", "geo", version="2")
        inside = insightmon.receipt()
    assert inside["components"] == [
        {"type": "prompt", "name": "system", "version": "v7", "content_hash": "h7"},
        {"type": "tool", "name": "geo", "version": "2"},
    ]
    assert insightmon.receipt() == base


# --------------------------------------------------------------------------- ASGI


class ASGIHarness:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: Any) -> None:
        self.sent.append(dict(message))


def http_scope(headers: list[tuple[bytes, bytes]], kind: str = "http") -> dict[str, Any]:
    return {"type": kind, "method": "POST", "path": "/replay", "headers": headers}


async def replay_app(scope: Any, receive: Any, send: Any) -> None:
    with insightmon.get_tracer().start_as_current_span("handler"), insightmon.turn():
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"part", "more_body": True})
    await send({"type": "http.response.body", "body": b"end"})


async def test_asgi_continues_the_incoming_trace(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    app = InsightMonASGIMiddleware(replay_app)
    harness = ASGIHarness()
    await app(http_scope([(b"traceparent", TRACEPARENT.encode())]), harness.receive, harness.send)
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    assert_continues_the_platform_trace(spans["handler"])
    assert spans["turn"].context.trace_id == int(TRACE_ID, 16)
    assert spans["handler"].attributes is not None
    assert spans["handler"].attributes[COMPONENT_KEY] == "v7"
    assert spans["handler"].attributes[semconv.AGENT_VERSION] == "1.4.0"
    assert not trace.get_current_span().get_span_context().is_valid  # detached afterwards


async def test_asgi_without_traceparent_starts_a_new_trace(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter, flushes: list[str]
) -> None:
    app = InsightMonASGIMiddleware(replay_app, flush_on_traceparent=True)
    harness = ASGIHarness()
    await app(http_scope([]), harness.receive, harness.send)
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    assert spans["handler"].parent is None
    assert flushes == []


async def test_asgi_flushes_before_the_last_body_message(
    sdk: insightmon.InsightMonHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    class Recorder(ASGIHarness):
        async def send(self, message: Any) -> None:
            order.append(f"{message['type']}:{message.get('more_body', False)}")

    def recording_flush(timeout_s: float = 10.0) -> bool:
        order.append("flush")
        return True

    monkeypatch.setattr(_setup, "flush", recording_flush)
    app = InsightMonASGIMiddleware(replay_app, flush_on_traceparent=True)
    harness = Recorder()
    await app(http_scope([(b"traceparent", TRACEPARENT.encode())]), harness.receive, harness.send)
    assert order == [
        "http.response.start:False",
        "http.response.body:True",
        "flush",
        "http.response.body:False",
    ]


async def test_asgi_keeps_an_outer_span_of_the_same_trace(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    app = InsightMonASGIMiddleware(replay_app)
    harness = ASGIHarness()
    server = tracer_provider.get_tracer("web.framework")
    context = insightmon.extract_context({"traceparent": TRACEPARENT})
    with server.start_as_current_span("POST /replay", context=context) as outer:
        await app(
            http_scope([(b"traceparent", TRACEPARENT.encode())]), harness.receive, harness.send
        )
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    assert spans["handler"].parent is not None
    assert spans["handler"].parent.span_id == outer.get_span_context().span_id


async def test_asgi_passes_other_scopes_through(sdk: insightmon.InsightMonHandle) -> None:
    seen: list[str] = []

    async def lifespan_app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    app = InsightMonASGIMiddleware(lifespan_app, flush_on_traceparent=True)
    harness = ASGIHarness()
    await app({"type": "lifespan"}, harness.receive, harness.send)
    await app(
        http_scope([(b"traceparent", TRACEPARENT.encode())], kind="websocket"),
        harness.receive,
        harness.send,
    )
    assert seen == ["lifespan", "websocket"]


async def test_asgi_detaches_on_errors(sdk: insightmon.InsightMonHandle) -> None:
    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        assert trace.get_current_span().get_span_context().trace_id == int(TRACE_ID, 16)
        raise RuntimeError("boom")

    app = InsightMonASGIMiddleware(failing_app)
    harness = ASGIHarness()
    with pytest.raises(RuntimeError):
        await app(
            http_scope([(b"traceparent", TRACEPARENT.encode())]), harness.receive, harness.send
        )
    assert not trace.get_current_span().get_span_context().is_valid


# --------------------------------------------------------------------------- WSGI


def wsgi_app(environ: dict[str, Any], start_response: Any) -> Iterator[bytes]:
    with insightmon.get_tracer().start_as_current_span("handler"):
        pass
    start_response("200 OK", [("Content-Type", "text/plain")])

    def body() -> Iterator[bytes]:
        with insightmon.get_tracer().start_as_current_span("streaming"):
            yield b"part"
        yield b"end"

    return body()


def test_wsgi_continues_the_incoming_trace(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    app = InsightMonWSGIMiddleware(wsgi_app)
    statuses: list[str] = []
    result = app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda status, headers: statuses.append(status))
    assert b"".join(result) == b"partend"
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    assert_continues_the_platform_trace(spans["handler"])
    assert_continues_the_platform_trace(spans["streaming"])  # consumed after __call__ returned
    assert statuses == ["200 OK"]
    assert not trace.get_current_span().get_span_context().is_valid


def serve(result: Any, order: list[str]) -> None:
    """What a WSGI server does with a response iterable: write each chunk, then close it."""
    try:
        for chunk in result:
            order.append(f"write {chunk.decode()}")
    finally:
        close = getattr(result, "close", None)
        if close is not None:
            close()


@pytest.fixture
def order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def recording_flush(timeout_s: float = 10.0) -> bool:
        seen.append(f"flush {timeout_s:g}")
        return True

    monkeypatch.setattr(_setup, "flush", recording_flush)
    return seen


def test_wsgi_flushes_before_the_server_writes_the_last_chunk(
    sdk: insightmon.InsightMonHandle, order: list[str]
) -> None:
    app = InsightMonWSGIMiddleware(wsgi_app, flush_on_traceparent=True)
    serve(app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None), order)
    assert order == ["write part", "flush 5", "write end"]


def test_wsgi_flushes_an_empty_streamed_body(
    sdk: insightmon.InsightMonHandle, order: list[str]
) -> None:
    def empty(environ: Any, start: Any) -> Iterator[bytes]:
        yield from ()

    app = InsightMonWSGIMiddleware(empty, flush_on_traceparent=True, flush_timeout_s=2)
    serve(app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None), order)
    assert order == ["flush 2"]


def test_wsgi_flushes_a_list_body_as_soon_as_the_app_returns(
    sdk: insightmon.InsightMonHandle, order: list[str]
) -> None:
    body = [b"whole"]
    app = InsightMonWSGIMiddleware(lambda environ, start: body, flush_on_traceparent=True)
    result = app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None)
    assert order == ["flush 5"]
    assert result is body
    serve(result, order)
    assert order == ["flush 5", "write whole"]


def test_wsgi_keeps_spans_nested_across_yields(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    tracer = insightmon.get_tracer()

    def streaming(environ: Any, start: Any) -> Iterator[bytes]:
        with tracer.start_as_current_span("stream"):
            yield b"a"
            with tracer.start_as_current_span("inner"):
                pass
            yield b"b"

    for flush in (False, True):
        span_exporter.clear()
        app = InsightMonWSGIMiddleware(streaming, flush_on_traceparent=flush)
        serve(app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None), [])
        spans = {span.name: span for span in span_exporter.get_finished_spans()}
        inner_parent = spans["inner"].parent
        assert inner_parent is not None
        assert inner_parent.span_id == spans["stream"].context.span_id
        assert_continues_the_platform_trace(spans["stream"])
        assert not trace.get_current_span().get_span_context().is_valid


def test_flush_when_selects_the_requests_that_flush(
    sdk: insightmon.InsightMonHandle, order: list[str]
) -> None:
    def is_replay(environ: Any) -> bool:
        return environ.get("HTTP_X_REPLAY") == "1"

    app = InsightMonWSGIMiddleware(lambda environ, start: [b"x"], flush_when=is_replay)
    app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None)
    assert order == []  # a traced request of another caller
    app({"HTTP_TRACEPARENT": TRACEPARENT, "HTTP_X_REPLAY": "1"}, lambda *args: None)
    assert order == ["flush 5"]
    app({"HTTP_X_REPLAY": "1"}, lambda *args: None)  # no traceparent: nothing to flush for
    assert order == ["flush 5"]


async def test_asgi_flush_when_and_timeout(
    sdk: insightmon.InsightMonHandle, order: list[str]
) -> None:
    def is_replay(scope: Any) -> bool:
        return (b"x-replay", b"1") in scope["headers"]

    app = InsightMonASGIMiddleware(replay_app, flush_when=is_replay, flush_timeout_s=1.5)
    harness = ASGIHarness()
    await app(http_scope([(b"traceparent", TRACEPARENT.encode())]), harness.receive, harness.send)
    assert order == []
    await app(
        http_scope([(b"traceparent", TRACEPARENT.encode()), (b"x-replay", b"1")]),
        harness.receive,
        harness.send,
    )
    assert order == ["flush 1.5"]


def test_a_failing_flush_when_never_breaks_the_request(
    sdk: insightmon.InsightMonHandle, order: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    def broken(environ: Any) -> bool:
        raise KeyError("x")

    app = InsightMonWSGIMiddleware(lambda environ, start: [b"x"], flush_when=broken)
    assert app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None) == [b"x"]
    assert order == []
    assert any("flush_when" in r.getMessage() for r in caplog.records)


def test_middleware_arguments_are_checked() -> None:
    with pytest.raises(ValueError, match="flush_timeout_s"):
        InsightMonWSGIMiddleware(wsgi_app, flush_timeout_s=0)
    with pytest.raises(TypeError, match="flush_when"):
        InsightMonASGIMiddleware(replay_app, flush_when="yes")  # type: ignore[arg-type]


async def test_asgi_flush_does_not_use_the_default_executor(
    sdk: insightmon.InsightMonHandle, order: list[str]
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    busy = ThreadPoolExecutor(max_workers=1)
    blocker = threading.Event()
    loop.set_default_executor(busy)
    loop.run_in_executor(None, blocker.wait, 10)  # the application's work fills the executor
    try:
        app = InsightMonASGIMiddleware(replay_app, flush_on_traceparent=True)
        harness = ASGIHarness()
        await asyncio.wait_for(
            app(
                http_scope([(b"traceparent", TRACEPARENT.encode())]), harness.receive, harness.send
            ),
            5,
        )
        assert order == ["flush 5"]
    finally:
        blocker.set()
        busy.shutdown(wait=False)


def test_wsgi_flushes_on_close_and_closes_the_result(
    sdk: insightmon.InsightMonHandle, flushes: list[str]
) -> None:
    closed: list[bool] = []

    class Body:
        def __iter__(self) -> Iterator[bytes]:
            yield b"x"

        def close(self) -> None:
            closed.append(True)

    app = InsightMonWSGIMiddleware(lambda environ, start: Body(), flush_on_traceparent=True)
    result = app({"HTTP_TRACEPARENT": TRACEPARENT}, lambda *args: None)
    result.close()  # type: ignore[attr-defined]
    assert closed == [True]
    assert flushes == ["flush"]


def test_wsgi_without_traceparent_returns_the_app_result(
    sdk: insightmon.InsightMonHandle, flushes: list[str]
) -> None:
    body = [b"plain"]
    app = InsightMonWSGIMiddleware(lambda environ, start: body, flush_on_traceparent=True)
    assert app({}, lambda *args: None) is body
    assert flushes == []


def test_flush_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(_setup, "flush", lambda timeout_s=10.0: False)
    propagation._flush()
    assert any("did not complete" in record.getMessage() for record in caplog.records)
