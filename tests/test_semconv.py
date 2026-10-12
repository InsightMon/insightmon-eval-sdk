from __future__ import annotations

import pytest

from insightmon import semconv


def test_component_attribute() -> None:
    assert (
        semconv.component_attribute("prompt", "system.v2")
        == "insightmon.component.prompt.system.v2.version"
    )
    assert semconv.component_attribute("tool", "geo", "content_hash").endswith(".content_hash")
    with pytest.raises(ValueError, match=r"no '\.'"):
        semconv.component_attribute("a.b", "x")
    with pytest.raises(ValueError, match="field"):
        semconv.component_attribute("tool", "x", "hash")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Helpfulness", "helpfulness"),
        ("  Answer Quality! ", "answer_quality"),
        ("__x__", "x"),
        ("a" * 80, "a" * 64),
        ("!!!", None),
        (3, None),
    ],
)
def test_normalize_score_name(raw: object, expected: str | None) -> None:
    assert semconv.normalize_score_name(raw) == expected


def test_content_attributes() -> None:
    for key in semconv.CONTENT_ATTRIBUTES:
        assert semconv.is_content_attribute(key)
    assert semconv.is_content_attribute("input.value")
    assert semconv.is_content_attribute("gen_ai.prompt.0.content")
    assert semconv.is_content_attribute("llm.input_messages.0.message.content")
    assert not semconv.is_content_attribute(semconv.RESPONSE_ID)
    assert not semconv.is_content_attribute(semconv.AGENT_VERSION)


def test_thread_key_priority_order() -> None:
    assert semconv.THREAD_KEY_ATTRIBUTES[0] == semconv.GEN_AI_CONVERSATION_ID


def test_limits_leave_headroom() -> None:
    assert semconv.TARGET_BODY_BYTES < semconv.MAX_BODY_BYTES
    assert semconv.metadata_attribute("channel") == "insightmon.metadata.channel"
