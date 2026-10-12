"""record_io(), the OTel message shape and the JSON size limit."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import insightmon
from insightmon import semconv
from insightmon.content import (
    TRUNCATION_SUFFIX,
    has_content,
    shrink_json,
    strip_content,
    to_otel_messages,
)


def platform_text(message: Any) -> str:
    """What the platform reads as the text of one message (app/telemetry/language.py)."""
    if "parts" in message:
        return "\n".join(
            part.get("content") or part.get("text") or ""
            for part in message["parts"]
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    return str(message.get("content") or "")


def recorded(exporter: InMemorySpanExporter) -> dict[str, Any]:
    (span,) = exporter.get_finished_spans()
    return dict(span.attributes or {})


# --------------------------------------------------------------------------- record_io


def test_record_io_is_off_by_default(
    tracer_provider: TracerProvider, span_exporter: InMemorySpanExporter
) -> None:
    insightmon.init(tracer_provider=tracer_provider)
    with insightmon.turn():
        assert insightmon.record_io(input_messages="hi", output_messages="hello") is False
    assert not has_content(recorded(span_exporter))


def test_record_io_without_init_does_nothing() -> None:
    assert insightmon.record_io(input_messages="hi") is False


def test_record_io_writes_the_platform_shape(
    tracer_provider: TracerProvider, span_exporter: InMemorySpanExporter
) -> None:
    insightmon.init(tracer_provider=tracer_provider, capture_content=True)
    with insightmon.turn():
        assert insightmon.record_io(
            input_messages="Will it rain in Oslo tomorrow?",
            output_messages="Light rain is expected.",
            system_instructions="You are a weather assistant.",
        )
    attributes = recorded(span_exporter)
    inputs = json.loads(attributes[semconv.INPUT_MESSAGES])
    outputs = json.loads(attributes[semconv.OUTPUT_MESSAGES])
    system = json.loads(attributes[semconv.SYSTEM_INSTRUCTIONS])
    assert inputs == [
        {"role": "user", "parts": [{"type": "text", "content": "Will it rain in Oslo tomorrow?"}]}
    ]
    assert outputs[0]["role"] == "assistant"
    assert platform_text(outputs[0]) == "Light rain is expected."
    assert system == [{"type": "text", "content": "You are a weather assistant."}]


def test_record_io_needs_a_recording_span_and_something_to_record(
    tracer_provider: TracerProvider,
) -> None:
    insightmon.init(tracer_provider=tracer_provider, capture_content=True)
    assert insightmon.record_io(input_messages="hi") is False  # no current span
    with insightmon.turn():
        assert insightmon.record_io() is False


def test_record_io_on_an_explicit_span(
    tracer_provider: TracerProvider, span_exporter: InMemorySpanExporter
) -> None:
    insightmon.init(tracer_provider=tracer_provider, capture_content=True)
    tracer = tracer_provider.get_tracer("app")
    span = tracer.start_span("chat model")
    assert insightmon.record_io(output_messages=[{"role": "assistant", "content": "ok"}], span=span)
    span.end()
    assert semconv.OUTPUT_MESSAGES in recorded(span_exporter)


def test_record_io_shrinks_large_content(
    tracer_provider: TracerProvider, span_exporter: InMemorySpanExporter
) -> None:
    insightmon.init(tracer_provider=tracer_provider, capture_content=True)
    with insightmon.turn():
        insightmon.record_io(input_messages="雨" * 200_000)  # 600 KB of UTF-8
    value = recorded(span_exporter)[semconv.INPUT_MESSAGES]
    assert len(value.encode("utf-8")) <= semconv.CONTENT_FIELD_MAX_BYTES
    parsed = json.loads(value)
    assert parsed[0]["parts"][0]["content"].endswith(TRUNCATION_SUFFIX)


# --------------------------------------------------------------------------- message shape


def test_to_otel_messages_conversions() -> None:
    messages = to_otel_messages(
        [
            "plain question",
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "a"}, "b"]},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "forecast", "arguments": '{"city": "Oslo"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "rain"},
            {"role": "assistant", "parts": [{"type": "text", "content": "kept"}], "extra": 1},
            {"content": "no role", "name": "helper"},
        ],
        default_role="user",
    )
    assert messages[0] == {"role": "user", "parts": [{"type": "text", "content": "plain question"}]}
    assert messages[1] == {"role": "system", "parts": [{"type": "text", "content": "be brief"}]}
    assert messages[2]["parts"] == [
        {"type": "text", "content": "a"},
        {"type": "text", "content": "b"},
    ]
    assert messages[3]["parts"] == [
        {"type": "tool_call", "name": "forecast", "id": "call_1", "arguments": {"city": "Oslo"}}
    ]
    assert messages[4]["parts"] == [
        {"type": "tool_call_response", "id": "call_1", "response": "rain"}
    ]
    assert messages[5] == {
        "role": "assistant",
        "parts": [{"type": "text", "content": "kept"}],
        "extra": 1,
    }
    assert messages[6] == {
        "role": "user",
        "name": "helper",
        "parts": [{"type": "text", "content": "no role"}],
    }
    assert [platform_text(m) for m in messages[:3]] == ["plain question", "be brief", "a\nb"]


def test_to_otel_messages_rejects_other_types() -> None:
    with pytest.raises(TypeError):
        to_otel_messages(42, default_role="user")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_otel_messages([42], default_role="user")  # type: ignore[list-item]


# --------------------------------------------------------------------------- shrink_json


def test_shrink_json_small_values_are_compact() -> None:
    assert shrink_json({"a": [1, 2.5, None, True], "b": "é"}) == '{"a":[1,2.5,null,true],"b":"é"}'


def test_shrink_json_cleans_values() -> None:
    value = {"nan": math.nan, "inf": -math.inf, "bytes": b"ok", "lone": "\ud800x", "obj": object}
    parsed = json.loads(shrink_json(value))
    assert parsed["nan"] is None
    assert parsed["inf"] is None
    assert parsed["bytes"] == "ok"
    assert parsed["lone"] == "�x"
    assert isinstance(parsed["obj"], str)


def test_shrink_json_cuts_the_longest_strings_first() -> None:
    value = [{"role": "user", "content": "short"}, {"role": "assistant", "content": "x" * 5000}]
    text = shrink_json(value, max_bytes=1000)
    assert len(text.encode("utf-8")) <= 1000
    parsed = json.loads(text)
    assert parsed[0]["content"] == "short"
    assert parsed[1]["content"].endswith(TRUNCATION_SUFFIX)
    assert len(parsed[1]["content"]) > 800  # as much as fits is kept


def test_shrink_json_falls_back_to_the_platform_marker() -> None:
    value = [{"k": ""} for _ in range(100)]
    text = shrink_json(value, max_bytes=60)
    size = len(json.dumps(value, separators=(",", ":")))
    assert json.loads(text) == {"truncated": True, "size_bytes": size}
    assert shrink_json(value, max_bytes=5) == "null"


def test_shrink_json_survives_cycles() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    assert json.loads(shrink_json(cyclic))


# --------------------------------------------------------------------------- stripping


def test_strip_content() -> None:
    attributes = {
        semconv.INPUT_MESSAGES: "[]",
        semconv.TOOL_CALL_ARGUMENTS: "{}",
        "gen_ai.prompt.0.content": "hi",
        "llm.input_messages.0.message.content": "hi",
        "input.value": "hi",
        "output.value": "ho",
        semconv.OPERATION_NAME: "chat",
        semconv.REQUEST_MODEL: "small-model",
    }
    assert strip_content(attributes) == {
        semconv.OPERATION_NAME: "chat",
        semconv.REQUEST_MODEL: "small-model",
    }
    assert has_content(attributes)
    assert not has_content(strip_content(attributes))
    assert not has_content(None)
