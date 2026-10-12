"""Threads, turns, components, language and content capture, read back from the platform."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

import insightmon
from tests.integration.conftest import unique
from tests.integration.platform_api import AdminSession, flat_spans, wait_for

pytestmark = pytest.mark.integration

Sdk = Callable[..., insightmon.InsightMonHandle]


def _hex_trace(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


def _hex_span(span: Any) -> str:
    return format(span.get_span_context().span_id, "016x")


def _components(trace: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(c["type"], c["name"]): c for c in trace["components"]}


def _wait_thread(admin: AdminSession, key: str, traces: int) -> dict[str, Any]:
    def ready() -> dict[str, Any] | None:
        thread = admin.thread_by_key(key)
        return thread if thread and thread["trace_count"] >= traces else None

    return wait_for(f"thread {key} with {traces} traces", ready)


def test_two_turn_thread_is_one_platform_thread_closed_explicitly(
    admin: AdminSession, sdk: Sdk
) -> None:
    sdk(components=[{"type": "prompt", "name": "system", "version": "p3", "content_hash": "h3"}])
    key = unique("sdk-it-thread")
    tracer = insightmon.get_tracer()
    with insightmon.thread(key, language="en", metadata={"channel": "it"}, user_ref="user-7"):
        with insightmon.turn("turn one") as first:
            insightmon.set_component("tool", "forecast", version="1.2.0")  # this thread only
            with tracer.start_as_current_span(
                "forecast",
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "forecast",
                },
            ):
                pass
        with insightmon.turn("turn two", end_thread=True) as second:
            pass
    assert insightmon.flush()

    def closed() -> dict[str, Any] | None:
        thread = admin.thread_by_key(key)
        return thread if thread and thread["status"] == "closed" else None

    thread = wait_for(f"thread {key} to close", closed)
    assert thread["thread_key"] == key
    assert thread["trace_count"] == 2
    assert thread["close_reason"] == "explicit"
    assert thread["agent_name"] == "sdk-it-weather"
    assert thread["primary_agent_version"] == "1.4.0"
    assert thread["language"] == "en"
    assert thread["language_source"] == "declared"
    assert thread["user_ref"] == "user-7"
    assert thread["metadata"].get("channel") == "it"

    detail = admin.thread_detail(thread["id"], spans=True)
    traces = sorted(detail["traces"], key=lambda t: t["turn_index"])
    assert [t["otel_trace_id"] for t in traces] == [_hex_trace(first), _hex_trace(second)]
    for trace in traces:
        assert trace["origin"] == "otlp"
        assert trace["agent_version"] == "1.4.0"
        components = _components(trace)
        assert components[("prompt", "system")]["version"] == "p3"
        assert components[("prompt", "system")]["content_hash"] == "h3"
        assert components[("tool", "forecast")]["version"] == "1.2.0"
    first_spans = {s["name"]: s for s in flat_spans(traces[0]["spans"])}
    assert set(first_spans) == {"turn one", "forecast"}
    assert first_spans["turn one"]["operation"] == "invoke_agent"
    assert first_spans["forecast"]["operation"] == "execute_tool"
    assert first_spans["forecast"]["tool_name"] == "forecast"


def test_a_turn_outside_a_thread_creates_no_thread(admin: AdminSession, sdk: Sdk) -> None:
    sdk()
    with insightmon.turn("standalone") as span:
        pass
    assert insightmon.flush()
    trace = wait_for("the trace", lambda: admin.trace_by_otel_id(_hex_trace(span)))
    assert trace["thread_id"] is None
    assert trace["origin"] == "otlp"
    assert trace["agent_version"] == "1.4.0"


def test_content_is_stored_when_capture_is_on(admin: AdminSession, sdk: Sdk) -> None:
    sdk(capture_content=True)
    key = unique("sdk-it-content")
    with insightmon.thread(key, language="en"), insightmon.turn() as span:
        assert insightmon.record_io(
            input_messages="Will it rain in Oslo tomorrow?",
            output_messages="Light rain is expected in Oslo tomorrow.",
            system_instructions="You are a concise weather assistant.",
        )
    assert insightmon.flush()
    thread = _wait_thread(admin, key, 1)

    detail = admin.thread_detail(thread["id"], spans=True)
    spans = {s["otel_span_id"]: s for s in flat_spans(detail["traces"][0]["spans"])}
    root = spans[_hex_span(span)]
    assert root["content_source"] == "attributes"
    assert "Will it rain in Oslo tomorrow?" in json.dumps(root["input"])
    assert "concise weather assistant" in json.dumps(root["input"])
    assert "Light rain is expected" in json.dumps(root["output"])
    assert detail["traces"][0]["input_preview"]


def test_no_content_leaves_the_process_when_capture_is_off(admin: AdminSession, sdk: Sdk) -> None:
    sdk()  # capture_content=False is the default
    key = unique("sdk-it-private")
    secret = "my private note 4711"
    with insightmon.thread(key), insightmon.turn() as span:
        assert insightmon.record_io(input_messages=secret) is False
        # what a third-party instrumentation might set: stripped by the SDK before export
        span.set_attribute(
            "gen_ai.input.messages", json.dumps([{"role": "user", "content": secret}])
        )
        span.set_attribute("input.value", secret)
    assert insightmon.flush()
    thread = _wait_thread(admin, key, 1)

    detail = admin.thread_detail(thread["id"], spans=True)
    spans = {s["otel_span_id"]: s for s in flat_spans(detail["traces"][0]["spans"])}
    root = spans[_hex_span(span)]
    assert root["input"] is None
    assert root["output"] is None
    assert secret not in json.dumps(detail)
    assert thread["first_input"] is None
