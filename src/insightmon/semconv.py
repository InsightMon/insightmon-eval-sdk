"""Attribute names, event names and limits of the InsightMon platform contract.

Everything here mirrors the platform code (see docs/contract.md, §8 to §10); the SDK never
invents attribute names of its own except the three experiment attributes at the bottom, which
the platform stores as plain span attributes.

The module is dependency free and safe to import from anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Final

INSTRUMENTATION_NAME: Final = "insightmon"

# --------------------------------------------------------------------------- thread (session)

GEN_AI_CONVERSATION_ID: Final = "gen_ai.conversation.id"
SESSION_ID: Final = "session.id"
INSIGHTMON_THREAD_ID: Final = "insightmon.thread_id"
#: Thread key attributes in the platform's priority order (the first one found on any span of
#: a trace wins). The SDK writes `gen_ai.conversation.id` only.
THREAD_KEY_ATTRIBUTES: Final[tuple[str, ...]] = (
    GEN_AI_CONVERSATION_ID,
    SESSION_ID,
    INSIGHTMON_THREAD_ID,
)
THREAD_END: Final = "insightmon.thread.end"
EVAL_ROOT: Final = "insightmon.eval_root"
METADATA_PREFIX: Final = "insightmon.metadata."
ENDUSER_ID: Final = "enduser.id"
LANGUAGE: Final = "insightmon.language"

# --------------------------------------------------------------------------- agent and components

AGENT_NAME: Final = "gen_ai.agent.name"
AGENT_VERSION: Final = "gen_ai.agent.version"
COMPONENT_PREFIX: Final = "insightmon.component."
COMPONENT_VERSION: Final = "version"
COMPONENT_CONTENT_HASH: Final = "content_hash"
#: Component types a project has when it configured none (platform default).
DEFAULT_COMPONENT_TYPES: Final[tuple[str, ...]] = (
    "agent",
    "prompt",
    "skill",
    "knowledge",
    "tool",
    "model",
)

# --------------------------------------------------------------------------- resource

SERVICE_NAME: Final = "service.name"
SERVICE_VERSION: Final = "service.version"
DEPLOYMENT_ENVIRONMENT_NAME: Final = "deployment.environment.name"
DEPLOYMENT_ENVIRONMENT: Final = "deployment.environment"

# --------------------------------------------------------------------------- span facts

OPERATION_NAME: Final = "gen_ai.operation.name"
OP_CHAT: Final = "chat"
OP_TEXT_COMPLETION: Final = "text_completion"
OP_GENERATE_CONTENT: Final = "generate_content"
OP_EMBEDDINGS: Final = "embeddings"
OP_EXECUTE_TOOL: Final = "execute_tool"
OP_INVOKE_AGENT: Final = "invoke_agent"
OP_CREATE_AGENT: Final = "create_agent"
#: Operation names the platform understands; any other value is stored as no operation.
OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        OP_CHAT,
        OP_TEXT_COMPLETION,
        OP_GENERATE_CONTENT,
        OP_EMBEDDINGS,
        OP_EXECUTE_TOOL,
        OP_INVOKE_AGENT,
        OP_CREATE_AGENT,
    }
)
REQUEST_MODEL: Final = "gen_ai.request.model"
RESPONSE_MODEL: Final = "gen_ai.response.model"
RESPONSE_ID: Final = "gen_ai.response.id"
TOOL_NAME: Final = "gen_ai.tool.name"
TOOL_CALL_ID: Final = "gen_ai.tool.call.id"
USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
COST: Final = "insightmon.cost"
ERROR_TYPE: Final = "error.type"

# --------------------------------------------------------------------------- content

SYSTEM_INSTRUCTIONS: Final = "gen_ai.system_instructions"
INPUT_MESSAGES: Final = "gen_ai.input.messages"
OUTPUT_MESSAGES: Final = "gen_ai.output.messages"
TOOL_CALL_ARGUMENTS: Final = "gen_ai.tool.call.arguments"
TOOL_CALL_RESULT: Final = "gen_ai.tool.call.result"
#: The standard content attributes the platform maps into span input / output.
CONTENT_ATTRIBUTES: Final[tuple[str, ...]] = (
    SYSTEM_INSTRUCTIONS,
    INPUT_MESSAGES,
    OUTPUT_MESSAGES,
    TOOL_CALL_ARGUMENTS,
    TOOL_CALL_RESULT,
)
#: Content-like attributes of common third-party instrumentations: the platform's content
#: attribute denylist. The platform drops them under `metadata_only` and redacts them otherwise
#: (an older platform kept them as they were); the SDK strips them when content capture is off,
#: so they never leave the process. The list cannot be complete: check the attributes your
#: instrumentations write and add their content attributes with `init(content_attributes=...)`.
THIRD_PARTY_CONTENT_ATTRIBUTES: Final[tuple[str, ...]] = (
    "input.value",
    "output.value",
    "llm.prompt_template.variables",
    "traceloop.entity.input",
    "traceloop.entity.output",
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
THIRD_PARTY_CONTENT_PREFIXES: Final[tuple[str, ...]] = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "llm.input_messages",
    "llm.output_messages",
    "llm.prompts",
    "retrieval.documents.",
    "reranker.input_documents.",
    "reranker.output_documents.",
    "embedding.embeddings.",
)
#: Span event that may carry the content attributes instead of the span itself.
DETAILS_EVENT: Final = "gen_ai.client.inference.operation.details"

# --------------------------------------------------------------------------- log events

EVENT_NAME: Final = "event.name"
EVALUATION_RESULT_EVENT: Final = "gen_ai.evaluation.result"
EVALUATION_NAME: Final = "gen_ai.evaluation.name"
EVALUATION_SCORE_VALUE: Final = "gen_ai.evaluation.score.value"
EVALUATION_SCORE_LABEL: Final = "gen_ai.evaluation.score.label"
EVALUATION_EXPLANATION: Final = "gen_ai.evaluation.explanation"
SYSTEM_MESSAGE_EVENT: Final = "gen_ai.system.message"
USER_MESSAGE_EVENT: Final = "gen_ai.user.message"
ASSISTANT_MESSAGE_EVENT: Final = "gen_ai.assistant.message"
TOOL_MESSAGE_EVENT: Final = "gen_ai.tool.message"
CHOICE_EVENT: Final = "gen_ai.choice"
#: Message content events (log records) the platform applies to a span's input / output.
CONTENT_EVENTS: Final[frozenset[str]] = frozenset(
    {
        SYSTEM_MESSAGE_EVENT,
        USER_MESSAGE_EVENT,
        ASSISTANT_MESSAGE_EVENT,
        TOOL_MESSAGE_EVENT,
        CHOICE_EVENT,
        DETAILS_EVENT,
    }
)
#: Boolean labels the platform accepts (case-insensitive).
TRUE_LABELS: Final[frozenset[str]] = frozenset({"true", "pass", "passed", "yes", "1"})
FALSE_LABELS: Final[frozenset[str]] = frozenset({"false", "fail", "failed", "no", "0"})

# --------------------------------------------------------------------------- server-owned

#: Attributes the platform writes itself; the SDK never sets them.
SERVER_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"insightmon.redaction", "insightmon.dropped_attributes", "insightmon.content_events"}
)

# --------------------------------------------------------------------------- experiments (SDK)

#: Written by `evaluate()` on its per-item root span. The platform marks a trace whose spans
#: carry `insightmon.experiment_id` of an open client-run experiment that the ingesting key
#: created as experiment traffic (an older platform stores it as a plain attribute); the other
#: two are plain span attributes (docs/contract.md §8).
EXPERIMENT_ID: Final = "insightmon.experiment_id"
EXAMPLE_KEY: Final = "insightmon.example_key"
TRIAL_INDEX: Final = "insightmon.trial_index"

# --------------------------------------------------------------------------- languages

CONTENT_LANGUAGES: Final[tuple[str, ...]] = ("zh", "en", "ja", "mixed", "und")

# --------------------------------------------------------------------------- limits

MIB: Final = 1024 * 1024
KIB: Final = 1024
#: Request body limit of every platform route (compressed and decompressed).
MAX_BODY_BYTES: Final = 8 * MIB
#: Target size of one serialized OTLP or REST request built by the SDK (headroom below 8 MiB).
TARGET_BODY_BYTES: Final = 7 * MIB
OTLP_MAX_LOG_RECORDS: Final = 10_000
THREAD_KEY_MAX: Final = 200
AGENT_FIELD_MAX: Final = 200
COMPONENT_TYPE_MAX: Final = 64
COMPONENT_NAME_MAX: Final = 200
COMPONENT_VALUE_MAX: Final = 200
COMPONENTS_MAX: Final = 100
ENVIRONMENT_MAX: Final = 100
ENDUSER_ID_MAX: Final = 128
RESPONSE_ID_MAX: Final = 200
ERROR_TYPE_MAX: Final = 200
CONTENT_FIELD_MAX_BYTES: Final = 256 * KIB
SPAN_ATTRIBUTES_MAX_BYTES: Final = 64 * KIB
EVALUATION_NAME_MAX: Final = 64
EVALUATION_EXPLANATION_MAX: Final = 4000
SCORES_BATCH_MAX: Final = 500
SCORE_NAME_MAX: Final = 64
IDEMPOTENCY_KEY_MAX: Final = 128
EXPLANATION_MAX: Final = 20_000
ITEMS_BATCH_MAX: Final = 200
ITEM_OUTPUT_MAX_BYTES: Final = 256 * KIB
ITEM_SCORES_MAX: Final = 20
ITEM_SCORE_NAME_MAX: Final = 200
EXAMPLE_KEY_MAX: Final = 100
ITEM_ERROR_CODE_MAX: Final = 64
ITEM_ERROR_MESSAGE_MAX: Final = 4000
EXPERIMENT_NAME_MAX: Final = 200
EXAMPLE_KEYS_MAX: Final = 5000
TRIALS_MAX: Final = 10
EVALUATORS_MAX: Final = 20
PAGE_LIMIT_MAX: Final = 200

# --------------------------------------------------------------------------- helpers

_COMPONENT_FIELDS: Final = frozenset({COMPONENT_VERSION, COMPONENT_CONTENT_HASH})
_SCORE_NAME_CLEAN: Final = re.compile(r"[^a-z0-9_]")


def component_attribute(component_type: str, name: str, field: str = COMPONENT_VERSION) -> str:
    """`insightmon.component.<type>.<name>.<field>`; `field` is `version` or `content_hash`.

    The platform splits `<type>.<name>` at the first dot, so a type must not contain a dot (a
    name may).
    """
    if field not in _COMPONENT_FIELDS:
        raise ValueError(f"component field must be 'version' or 'content_hash', not {field!r}")
    if not component_type or "." in component_type:
        raise ValueError(f"component type must be non-empty and contain no '.': {component_type!r}")
    if not name:
        raise ValueError("component name must be non-empty")
    return f"{COMPONENT_PREFIX}{component_type}.{name}.{field}"


def metadata_attribute(key: str) -> str:
    """`insightmon.metadata.<key>` (becomes `metadata[key]` of the thread)."""
    if not key:
        raise ValueError("metadata key must be non-empty")
    return f"{METADATA_PREFIX}{key}"


def is_content_attribute(key: str) -> bool:
    """True for message content attributes: the standard ones and common third-party ones."""
    return (
        key in CONTENT_ATTRIBUTES
        or key in THIRD_PARTY_CONTENT_ATTRIBUTES
        or key.startswith(THIRD_PARTY_CONTENT_PREFIXES)
    )


def content_matcher(patterns: Iterable[str] = ()) -> Callable[[str], bool]:
    """`is_content_attribute` plus `patterns` (`init(content_attributes=...)`): attribute
    names, or prefixes written with a trailing `*` (`"acme.document.*"`)."""
    names = frozenset(p for p in patterns if not p.endswith("*"))
    prefixes = tuple(p[:-1] for p in patterns if p.endswith("*"))
    if not names and not prefixes:
        return is_content_attribute

    def matches(key: str) -> bool:
        return (
            is_content_attribute(key)
            or key in names
            or (bool(prefixes) and key.startswith(prefixes))
        )

    return matches


def normalize_score_name(raw: Any) -> str | None:
    """The platform's evaluation-name rule: lower case, `[^a-z0-9_]` -> `_`, `_` trimmed from
    both ends, cut to 64 characters. None when nothing is left (or `raw` is not a string)."""
    if not isinstance(raw, str):
        return None
    name = _SCORE_NAME_CLEAN.sub("_", raw.strip().lower()).strip("_")
    return name[:EVALUATION_NAME_MAX] or None
