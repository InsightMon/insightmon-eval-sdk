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


#: The platform's content attribute denylist (`CONTENT_ATTRIBUTE_DENYLIST`, `*`: a prefix):
#: what it drops under `metadata_only`. The SDK strips at least these with capture off.
PLATFORM_DENYLIST = (
    "gen_ai.prompt",
    "gen_ai.prompt.*",
    "gen_ai.completion",
    "gen_ai.completion.*",
    "traceloop.entity.input",
    "traceloop.entity.output",
    "input.value",
    "output.value",
    "llm.input_messages",
    "llm.input_messages.*",
    "llm.output_messages",
    "llm.output_messages.*",
    "llm.prompts",
    "llm.prompts.*",
    "llm.prompt_template.variables",
    "retrieval.documents.*",
    "reranker.input_documents.*",
    "reranker.output_documents.*",
    "embedding.embeddings.*",
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.trace.input",
    "langfuse.trace.output",
    "mlflow.spanInputs",
    "mlflow.spanOutputs",
    "mlflow.chat.messages",
    "all_messages_events",
    "ai.prompt",
    "ai.prompt.messages",
    "ai.response.text",
    "ai.response.object",
    "ai.response.toolCalls",
    "ai.toolCall.args",
    "ai.toolCall.result",
    "ai.value",
    "ai.values",
    "ai.embedding",
    "ai.embeddings",
    "pydantic_ai.all_messages",
    "final_result",
    "tool_arguments",
    "tool_response",
    "request_data",
    "response_data",
    "gcp.vertex.agent.llm_request",
    "gcp.vertex.agent.llm_response",
    "gcp.vertex.agent.tool_call_args",
    "gcp.vertex.agent.tool_response",
)


@pytest.mark.parametrize("entry", PLATFORM_DENYLIST)
def test_the_platform_denylist_is_stripped(entry: str) -> None:
    key = entry[:-1] + "0.content" if entry.endswith("*") else entry
    assert semconv.is_content_attribute(key)


def test_thread_key_priority_order() -> None:
    assert semconv.THREAD_KEY_ATTRIBUTES[0] == semconv.GEN_AI_CONVERSATION_ID


def test_limits_leave_headroom() -> None:
    assert semconv.TARGET_BODY_BYTES < semconv.MAX_BODY_BYTES
    assert semconv.metadata_attribute("channel") == "insightmon.metadata.channel"
