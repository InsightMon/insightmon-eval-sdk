"""thread() / turn() / mark_eval_root() / end_thread() / set_component() / set_response_id(),
and context propagation across asyncio tasks and thread pools."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import insightmon
from insightmon import semconv

CONV = semconv.GEN_AI_CONVERSATION_ID


@pytest.fixture
def sdk(tracer_provider: TracerProvider) -> insightmon.InsightMonHandle:
    return insightmon.init(
        tracer_provider=tracer_provider,
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=[{"type": "prompt", "name": "system", "version": "v7"}],
        language="en",
    )


def spans_by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


# --------------------------------------------------------------------------- thread


def test_thread_stamps_every_span_with_the_key(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    third_party = tracer_provider.get_tracer("some.instrumentation")
    with (
        insightmon.thread("chat-42", metadata={"channel": "web", "tier": 2}, user_ref="u-17"),
        insightmon.turn(),
    ):
        with third_party.start_as_current_span("chat model") as llm:
            llm.set_attribute(semconv.OPERATION_NAME, semconv.OP_CHAT)
        with third_party.start_as_current_span("geo lookup"):
            pass
    spans = spans_by_name(span_exporter)
    assert set(spans) == {"turn", "chat model", "geo lookup"}
    for span in spans.values():
        a = attrs(span)
        assert a[CONV] == "chat-42"
        assert a[semconv.metadata_attribute("channel")] == "web"
        assert a[semconv.metadata_attribute("tier")] == 2
        assert a[semconv.ENDUSER_ID] == "u-17"
        assert a[semconv.AGENT_NAME] == "weather-assistant"
        assert a[semconv.AGENT_VERSION] == "1.4.0"
        assert a[semconv.LANGUAGE] == "en"


def test_spans_outside_a_thread_have_no_conversation_id(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    with insightmon.turn("lonely"):
        pass
    with insightmon.thread("chat-1"):
        pass
    with insightmon.turn("after"):
        pass
    for span in span_exporter.get_finished_spans():
        assert not set(semconv.THREAD_KEY_ATTRIBUTES) & set(attrs(span))


def test_nested_threads_innermost_wins(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    with insightmon.thread("outer") as outer:
        assert insightmon.current_thread() is outer
        with insightmon.turn("t1"):
            pass
        with insightmon.thread("inner", language="zh") as inner:
            assert insightmon.current_thread() is inner
            with insightmon.turn("t2"):
                pass
        with insightmon.turn("t3"):
            pass
    assert insightmon.current_thread() is None
    spans = spans_by_name(span_exporter)
    assert attrs(spans["t1"])[CONV] == "outer"
    assert attrs(spans["t2"])[CONV] == "inner"
    assert attrs(spans["t2"])[semconv.LANGUAGE] == "zh"
    assert attrs(spans["t3"])[CONV] == "outer"
    assert attrs(spans["t3"])[semconv.LANGUAGE] == "en"


def test_thread_validates_its_arguments() -> None:
    for bad_key in ("", "   ", "k" * 201):
        with pytest.raises(ValueError, match="thread key"):
            insightmon.thread(bad_key)
    with pytest.raises(ValueError, match="language"):
        insightmon.thread("chat", language="fr")
    with pytest.raises(ValueError, match="user_ref"):
        insightmon.thread("chat", user_ref="u" * 129)
    with pytest.raises(ValueError, match="metadata"):
        insightmon.thread("chat", metadata={"nested": {"a": 1}})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="metadata"):
        insightmon.thread("chat", metadata={"": "x"})


def test_thread_key_is_stripped_like_the_platform_does(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    with insightmon.thread("  chat-7 \n") as scope, insightmon.turn():
        pass
    assert scope.key == "chat-7"
    assert attrs(span_exporter.get_finished_spans()[0])[CONV] == "chat-7"


def test_thread_works_without_init(
    tracer_provider: TracerProvider, span_exporter: InMemorySpanExporter
) -> None:
    # No init(): get_tracer() falls back to the global (no-op) provider; nothing breaks.
    with insightmon.thread("chat-1"), insightmon.turn() as span:
        assert not span.is_recording()
    assert span_exporter.get_finished_spans() == ()


# --------------------------------------------------------------------------- turn


def test_turn_span_shape(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    with insightmon.thread("chat-1"):
        with insightmon.turn(attributes={"app.route": "/ask"}) as first:
            assert trace.get_current_span() is first
        with insightmon.turn("last", eval_root=True, end_thread=True):
            pass
    spans = spans_by_name(span_exporter)
    turn_attrs = attrs(spans["turn"])
    assert turn_attrs[semconv.OPERATION_NAME] == semconv.OP_INVOKE_AGENT
    assert turn_attrs["app.route"] == "/ask"
    assert semconv.EVAL_ROOT not in turn_attrs
    assert semconv.THREAD_END not in turn_attrs
    assert spans["turn"].parent is None  # a new trace per turn
    last = attrs(spans["last"])
    assert last[semconv.EVAL_ROOT] is True
    assert last[semconv.THREAD_END] is True
    assert spans["turn"].context.trace_id != spans["last"].context.trace_id


def test_turn_records_exceptions(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    class ForecastUnavailable(Exception):
        pass

    with pytest.raises(ForecastUnavailable), insightmon.turn():
        raise ForecastUnavailable("no data")
    (span,) = span_exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert attrs(span)[semconv.ERROR_TYPE].endswith("ForecastUnavailable")
    assert [event.name for event in span.events] == ["exception"]


def test_turn_rejects_unknown_operations() -> None:
    with pytest.raises(ValueError, match="operation"):
        insightmon.turn(operation="dance")
    with pytest.raises(ValueError, match="name"):
        insightmon.turn("")


def test_turn_inside_a_span_is_a_child_and_still_an_anchor(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    server = tracer_provider.get_tracer("web.framework")
    with (
        server.start_as_current_span("POST /chat"),
        insightmon.thread("chat-1"),
        insightmon.turn(),
        server.start_as_current_span("db query"),
    ):
        pass
    spans = spans_by_name(span_exporter)
    component_key = semconv.component_attribute("prompt", "system")
    assert spans["turn"].parent is not None
    assert attrs(spans["turn"])[component_key] == "v7"  # turn spans are anchors
    assert attrs(spans["POST /chat"])[component_key] == "v7"  # local root
    assert component_key not in attrs(spans["db query"])  # neither


def test_explicit_turn_attributes_win_over_stamped_ones(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    key = semconv.component_attribute("prompt", "system")
    with insightmon.turn(attributes={key: "v8-canary", semconv.LANGUAGE: "ja"}):
        pass
    a = attrs(span_exporter.get_finished_spans()[0])
    assert a[key] == "v8-canary"
    assert a[semconv.LANGUAGE] == "ja"


# --------------------------------------------------------------------------- span helpers


def test_mark_eval_root_end_thread_and_response_id(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    tracer = tracer_provider.get_tracer("app")
    with insightmon.thread("chat-1"), insightmon.turn() as turn_span:
        with tracer.start_as_current_span("chat model"):
            insightmon.set_response_id(" resp-123 ")
            insightmon.mark_eval_root()
        insightmon.end_thread(turn_span)
    spans = spans_by_name(span_exporter)
    assert attrs(spans["chat model"])[semconv.RESPONSE_ID] == "resp-123"
    assert attrs(spans["chat model"])[semconv.EVAL_ROOT] is True
    assert attrs(spans["turn"])[semconv.THREAD_END] is True
    assert semconv.THREAD_END not in attrs(spans["chat model"])


def test_span_helpers_are_noops_without_a_recording_span() -> None:
    insightmon.mark_eval_root()
    insightmon.end_thread()
    insightmon.set_response_id("resp-1")
    with pytest.raises(ValueError, match="response_id"):
        insightmon.set_response_id("")
    with pytest.raises(ValueError, match="response_id"):
        insightmon.set_response_id("r" * 201)


# --------------------------------------------------------------------------- components


def test_components_default_and_thread_override(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    key = semconv.component_attribute("prompt", "system")
    tool_key = semconv.component_attribute("tool", "geo")
    hash_key = semconv.component_attribute("tool", "geo", semconv.COMPONENT_CONTENT_HASH)
    tracer = tracer_provider.get_tracer("app")
    with insightmon.thread("chat-a"):
        with insightmon.turn("a1"):
            pass
        insightmon.set_component("prompt", "system", version="v8")
        with insightmon.turn("a2"), tracer.start_as_current_span("a2-child"):
            pass
    with insightmon.thread("chat-b"), insightmon.turn("b1"):
        pass
    insightmon.set_component("tool", "geo", version="2", content_hash="sha256:ab")
    with insightmon.turn("c1"):
        pass
    spans = spans_by_name(span_exporter)
    assert attrs(spans["a1"])[key] == "v7"  # init() default
    assert attrs(spans["a2"])[key] == "v8"  # thread override
    assert key not in attrs(spans["a2-child"])  # not an anchor span
    assert attrs(spans["b1"])[key] == "v7"  # the override stayed in thread a
    assert attrs(spans["c1"])[key] == "v7"
    assert attrs(spans["c1"])[tool_key] == "2"  # process-wide override
    assert attrs(spans["c1"])[hash_key] == "sha256:ab"


def test_set_component_writes_the_current_span(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    tracer = tracer_provider.get_tracer("app")
    with (
        insightmon.thread("chat-1"),
        insightmon.turn(),
        tracer.start_as_current_span("tool call"),
    ):
        insightmon.set_component("tool", "geo", version="3")
    spans = spans_by_name(span_exporter)
    key = semconv.component_attribute("tool", "geo")
    assert attrs(spans["tool call"])[key] == "3"
    # The turn started before the declaration; the next anchor span picks the override up.
    assert key not in attrs(spans["turn"])


def test_set_component_inside_an_experiment_scope_stays_there() -> None:
    from insightmon import _config
    from insightmon._context import experiment_scope

    with insightmon.thread("chat-1") as thread_scope, experiment_scope("exp-1", "k1", 0) as run:
        insightmon.set_component("tool", "geo", version="3")
        assert insightmon.receipt()["components"] == [
            {"type": "tool", "name": "geo", "version": "3"}
        ]
    assert run.component_overrides()[0].version == "3"
    assert thread_scope.component_overrides() == ()
    assert _config.global_components() == ()
    assert insightmon.receipt()["components"] == []


def test_set_component_validation() -> None:
    with pytest.raises(ValueError, match="version"):
        insightmon.set_component("tool", "geo")
    with pytest.raises(ValueError, match="type"):
        insightmon.set_component("bad.type", "geo", version="1")
    with pytest.raises(ValueError, match="name"):
        insightmon.set_component("tool", "", version="1")
    with pytest.raises(ValueError, match="version"):
        insightmon.set_component("tool", "geo", version="v" * 201)


# --------------------------------------------------------------------------- propagation


async def test_asyncio_tasks_inherit_the_thread(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    tracer = tracer_provider.get_tracer("app")

    async def step(name: str) -> None:
        await asyncio.sleep(0)
        with tracer.start_as_current_span(name):
            await asyncio.sleep(0)

    async def conversation(key: str) -> None:
        with insightmon.thread(key), insightmon.turn(f"turn-{key}"):
            await asyncio.gather(step(f"{key}-a"), step(f"{key}-b"))

    await asyncio.gather(conversation("x"), conversation("y"))
    spans = spans_by_name(span_exporter)
    for key in ("x", "y"):
        root = spans[f"turn-{key}"]
        for child in (f"{key}-a", f"{key}-b"):
            assert attrs(spans[child])[CONV] == key
            assert spans[child].context.trace_id == root.context.trace_id


def test_thread_pools_need_copy_context(
    sdk: insightmon.InsightMonHandle,
    tracer_provider: TracerProvider,
    span_exporter: InMemorySpanExporter,
) -> None:
    tracer = tracer_provider.get_tracer("app")

    def work(name: str) -> str | None:
        with tracer.start_as_current_span(name):
            scope = insightmon.current_thread()
            return scope.key if scope else None

    with (
        ThreadPoolExecutor(max_workers=2) as pool,
        insightmon.thread("chat-pool"),
        insightmon.turn("pool turn"),
    ):
        plain = pool.submit(work, "plain").result()
        copied = pool.submit(contextvars.copy_context().run, work, "copied").result()
    assert plain is None
    assert copied == "chat-pool"
    spans = spans_by_name(span_exporter)
    root = spans["pool turn"]
    assert CONV not in attrs(spans["plain"])
    assert spans["plain"].context.trace_id != root.context.trace_id
    assert attrs(spans["copied"])[CONV] == "chat-pool"
    assert spans["copied"].context.trace_id == root.context.trace_id


def test_threads_keep_their_own_scopes(
    sdk: insightmon.InsightMonHandle, span_exporter: InMemorySpanExporter
) -> None:
    barrier = threading.Barrier(4)

    def conversation(key: str) -> None:
        with insightmon.thread(key):
            barrier.wait()
            with insightmon.turn(f"turn-{key}"):
                barrier.wait()

    workers = [threading.Thread(target=conversation, args=(f"k{i}",)) for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    for name, span in spans_by_name(span_exporter).items():
        assert attrs(span)[CONV] == name.removeprefix("turn-")
