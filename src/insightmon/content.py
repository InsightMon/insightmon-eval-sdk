"""Message content: `record_io()` and the helpers the exporter uses to strip content.

Content capture is off by default (`init(capture_content=False)`): `record_io()` does nothing
and the exporter strips content attributes and content events before anything leaves the
process (docs/contract.md §10). With capture on, content is written in the OTel GenAI shape the
platform parses (`backend/app/telemetry/otlp.py`, `language.py`), as JSON strings:

- `gen_ai.input.messages` / `gen_ai.output.messages`:
  `[{"role": "user", "parts": [{"type": "text", "content": "..."}]}, ...]`
  (the platform stores them as span `input.messages` / `output.messages`);
- `gen_ai.system_instructions`: `[{"type": "text", "content": "..."}]` (span `input.system`).

Each attribute is at most 256 KiB of UTF-8 JSON (the platform's field limit): the longest
strings are cut first (with the platform's `…[truncated]` marker) so the value stays valid JSON
of the same shape.
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span

from insightmon import semconv
from insightmon._config import get_settings

__all__ = [
    "Messages",
    "has_content",
    "record_io",
    "shrink_json",
    "strip_content",
    "to_otel_messages",
]

#: A plain string (one message of the default role), one message mapping, or a list of them.
#: Mappings may be `{role, content}` or `{role, parts: [...]}`.
Messages = str | Mapping[str, Any] | Sequence[Mapping[str, Any]]

TRUNCATION_SUFFIX = "…[truncated]"
#: Nesting depth kept by `shrink_json`; deeper values (or reference cycles) become strings.
MAX_DEPTH = 32
_TEXT_TYPES = (None, "text", "input_text", "output_text")


def record_io(
    *,
    input_messages: Messages | None = None,
    output_messages: Messages | None = None,
    system_instructions: str | Sequence[Any] | None = None,
    span: Span | None = None,
) -> bool:
    """Record a turn's or a model call's content on `span` (default: the current span) as
    `gen_ai.input.messages`, `gen_ai.output.messages` and `gen_ai.system_instructions`.

    Returns True when something was recorded; False (and does nothing) when content capture
    is off (or `init()` was not called), there is no recording span, or every argument is None.
    Strings become one message (`user` for input, `assistant` for output). Each attribute is
    shrunk to at most 256 KiB of JSON (the platform's field limit) before it is set.

    Raises:
        TypeError: a message that is neither a string nor a mapping.
    """
    settings = get_settings()
    if settings is None or not settings.capture_content:
        return False
    if input_messages is None and output_messages is None and system_instructions is None:
        return False
    target = span if span is not None else trace.get_current_span()
    if not target.is_recording():
        return False
    attributes: dict[str, str] = {}
    if input_messages is not None:
        messages = to_otel_messages(input_messages, default_role="user")
        attributes[semconv.INPUT_MESSAGES] = shrink_json(messages)
    if output_messages is not None:
        messages = to_otel_messages(output_messages, default_role="assistant")
        attributes[semconv.OUTPUT_MESSAGES] = shrink_json(messages)
    if system_instructions is not None:
        attributes[semconv.SYSTEM_INSTRUCTIONS] = shrink_json(_parts(system_instructions))
    target.set_attributes(attributes)
    return True


# --------------------------------------------------------------------------- message shape


def to_otel_messages(value: Messages, *, default_role: str) -> list[dict[str, Any]]:
    """Normalize `value` to OTel GenAI messages `[{role, parts: [{type: "text", content}]}]`.
    Mappings that already have `parts` are kept; `{role, content}` mappings are converted
    (content strings and text part lists become text parts; `tool_calls` become `tool_call`
    parts and a `tool` message's `tool_call_id` a `tool_call_response` part)."""
    items: Any = value  # checked at run time: callers are not always typed
    if isinstance(items, str):
        return [_text_message(default_role, items)]
    if isinstance(items, Mapping):
        return [_message(items, default_role)]
    if isinstance(items, Sequence) and not isinstance(items, bytes | bytearray):
        out: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                out.append(_text_message(default_role, item))
            else:
                out.append(_message(item, default_role))
        return out
    raise TypeError(f"messages must be a str, a mapping or a list, not {type(value).__name__}")


def _text_message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "parts": [_text_part(text)]}


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "content": text}


def _role(message: Mapping[str, Any], default_role: str) -> str:
    role = message.get("role")
    return role if isinstance(role, str) and role else default_role


def _message(message: Any, default_role: str) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        raise TypeError(f"a message must be a str or a mapping, not {type(message).__name__}")
    role = _role(message, default_role)
    if "parts" in message:
        out = dict(message)
        out["role"] = role
        return out
    out = {k: v for k, v in message.items() if k not in ("content", "tool_calls", "tool_call_id")}
    out["role"] = role
    content = message.get("content")
    if role == "tool" and message.get("tool_call_id") is not None:
        parts = [{"type": "tool_call_response", "id": message["tool_call_id"], "response": content}]
    else:
        parts = _parts(content)
    for call in message.get("tool_calls") or ():
        parts.append(_tool_call(call))
    out["parts"] = parts
    return out


def _parts(content: Any) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, str):
        return [_text_part(content)]
    if isinstance(content, Mapping):
        return [_part(content)]
    if isinstance(content, Sequence) and not isinstance(content, bytes | bytearray):
        return [_part(item) for item in content]
    return [_text_part(str(content))]


def _part(part: Any) -> Any:
    if isinstance(part, str):
        return _text_part(part)
    if isinstance(part, Mapping):
        out = dict(part)
        if (
            out.get("type") in _TEXT_TYPES
            and "content" not in out
            and isinstance(out.get("text"), str)
        ):
            out.pop("text")
            out["type"] = "text"
            out["content"] = part["text"]
        return out
    return _text_part(str(part))


def _tool_call(call: Any) -> dict[str, Any]:
    if not isinstance(call, Mapping):
        return {"type": "tool_call", "arguments": call}
    if call.get("type") == "tool_call":
        return dict(call)
    function = call.get("function")
    function = function if isinstance(function, Mapping) else {}
    arguments: Any = function.get("arguments", call.get("arguments"))
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments)
    out: dict[str, Any] = {"type": "tool_call", "name": function.get("name", call.get("name"))}
    if call.get("id") is not None:
        out["id"] = call["id"]
    out["arguments"] = arguments
    return out


# --------------------------------------------------------------------------- JSON size


def _clean_text(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates: protobuf cannot serialize them
        return "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)
    return text


def _clean(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy: non-finite numbers → None, unknown objects → str, valid UTF-8."""
    if isinstance(value, str):
        return _clean_text(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    if depth >= MAX_DEPTH:
        return _clean_text(str(value)[:1000])
    if isinstance(value, Mapping):
        return {_clean_text(str(k)): _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_clean(v, depth + 1) for v in value]
    return _clean_text(str(value))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _longest_string(value: Any) -> int:
    longest = 0
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            longest = max(longest, len(node))
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return longest


def _cap_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + TRUNCATION_SUFFIX
    if isinstance(value, dict):
        return {k: _cap_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_strings(v, limit) for v in value]
    return value


def shrink_json(value: Any, max_bytes: int = semconv.CONTENT_FIELD_MAX_BYTES) -> str:
    """Serialize `value` to compact JSON (UTF-8, `ensure_ascii=False`, non-finite numbers as
    null) of at most `max_bytes` bytes, cutting the longest strings first so the shape stays
    valid JSON. When even empty strings do not fit, the result is the platform's own marker
    `{"truncated": true, "size_bytes": N}`."""
    data = _clean(value)
    text = _dumps(data)
    size = _size(text)
    if size <= max_bytes:
        return text
    # The largest string length limit whose result fits (binary search on the limit).
    low, high = 0, _longest_string(data)
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = _dumps(_cap_strings(data, middle))
        if _size(candidate) <= max_bytes:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if best is not None:
        return best
    marker = _dumps({"truncated": True, "size_bytes": size})
    return marker if _size(marker) <= max_bytes else "null"


# --------------------------------------------------------------------------- stripping


def strip_content(
    attributes: Mapping[str, Any],
    is_content: Callable[[str], bool] = semconv.is_content_attribute,
) -> dict[str, Any]:
    """`attributes` without message content (`semconv.is_content_attribute`, or the matcher
    of `init(content_attributes=...)`)."""
    return {k: v for k, v in attributes.items() if not is_content(k)}


def has_content(
    attributes: Mapping[str, Any] | None,
    is_content: Callable[[str], bool] = semconv.is_content_attribute,
) -> bool:
    """True when `attributes` holds a content attribute."""
    return bool(attributes) and any(is_content(k) for k in attributes or ())
