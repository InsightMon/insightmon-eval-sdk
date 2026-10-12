"""Session tracing: threads, turns, eval roots, thread end, components and response ids.

How it maps onto the platform (docs/contract.md §8):

- `thread(key)` → every span started inside gets `gen_ai.conversation.id = key` (stamped by
  `InsightMonSpanProcessor`), plus the thread's language, metadata and user ref. Spans outside
  a thread carry no conversation id: the SDK never invents one.
- `turn()` → one `invoke_agent` span: the root of one trace = one turn of the thread.
- `mark_eval_root()` → `insightmon.eval_root = true` on a span.
- `end_thread()` → `insightmon.thread.end = true` on a span of the last turn (never on the
  resource: that would end every thread of the process).
- `set_component()` → `insightmon.component.<type>.<name>.version|content_hash`.
- `set_response_id()` → `gen_ai.response.id` on the span itself (logs correlate by it).

Context propagation: the thread scope (like the current span) lives in `contextvars`, so it
follows the code that runs in the same context:

- asyncio: tasks copy the context when they are created, so `asyncio.create_task()` /
  `asyncio.gather()` inside a `thread()` block stay in the thread.
- threads: a new thread or a `ThreadPoolExecutor` worker starts with an empty context. Run the
  work in a copy of the caller's context to keep the thread scope and the parent span::

      ctx = contextvars.copy_context()
      executor.submit(ctx.run, work, arg)

  (one `copy_context()` per submitted call: a context can be entered by one thread at a time).
  Without it the worker's spans are separate traces without a conversation id.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

from insightmon import semconv
from insightmon._config import (
    Component,
    get_runtime,
    normalize_language,
    set_global_component,
)
from insightmon._context import AttributeValue, ThreadScope, current_experiment, thread_scope
from insightmon._context import current_thread as _current_thread
from insightmon._version import __version__
from insightmon.processor import starting_turn

__all__ = [
    "current_thread",
    "end_thread",
    "get_tracer",
    "mark_eval_root",
    "set_component",
    "set_response_id",
    "thread",
    "turn",
]


def get_tracer() -> Tracer:
    """The SDK's tracer (instrumentation scope `insightmon`), from the provider `init()` set
    up, else from the global provider (a no-op tracer before any provider exists)."""
    state = get_runtime()
    provider = state.tracer_provider if state is not None else None
    return trace.get_tracer(semconv.INSTRUMENTATION_NAME, __version__, tracer_provider=provider)


def current_thread() -> ThreadScope | None:
    """The innermost `thread(...)` scope of the current context, if any."""
    return _current_thread()


def _thread_key(key: str) -> str:
    if not isinstance(key, str):
        raise ValueError(f"thread key must be a string, not {type(key).__name__}")
    text = key.strip()
    if not text:
        raise ValueError("thread key must be a non-empty string")
    if len(text) > semconv.THREAD_KEY_MAX:
        raise ValueError(
            f"thread key is longer than {semconv.THREAD_KEY_MAX} characters "
            "(the platform ignores longer keys)"
        )
    return text


def _metadata(metadata: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]:
    out: dict[str, AttributeValue] = {}
    for key, value in (metadata or {}).items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"metadata keys must be non-empty strings, not {key!r}")
        if not isinstance(value, str | bool | int | float):
            raise ValueError(
                f"metadata value of {key!r} must be a str, bool, int or float, "
                f"not {type(value).__name__}"
            )
        out[key] = value
    return out


def _user_ref(user_ref: str | None) -> str | None:
    if user_ref is None:
        return None
    if not isinstance(user_ref, str) or not user_ref.strip():
        raise ValueError("user_ref must be a non-empty string or None")
    if len(user_ref) > semconv.ENDUSER_ID_MAX:
        raise ValueError(f"user_ref is longer than {semconv.ENDUSER_ID_MAX} characters")
    return user_ref


@contextmanager
def _thread(scope: ThreadScope) -> Iterator[ThreadScope]:
    with thread_scope(scope):
        yield scope


def thread(
    key: str,
    *,
    language: str | None = None,
    metadata: Mapping[str, AttributeValue] | None = None,
    user_ref: str | None = None,
) -> AbstractContextManager[ThreadScope]:
    """Group the turns started inside the block into one platform thread.

    Args:
        key: the conversation key, 1 to 200 characters (the platform ignores longer keys;
            surrounding whitespace is removed, as the platform does); stable across the turns
            of one conversation, e.g. your chat session id.
        language: declared content language, one of zh / en / ja / mixed / und; overrides
            `init(language=...)` for this thread.
        metadata: written as `insightmon.metadata.<k>` (becomes the thread's metadata).
        user_ref: an opaque end-user reference, at most 128 characters (`enduser.id`). Never
            put personal data here.

    Raises:
        ValueError: empty or too long key, invalid language, metadata or user_ref.

    Nested `thread()` blocks: the innermost wins (the outer thread is back after the inner
    block). Inside `evaluate()` the experiment scope suppresses thread keys (experiment
    traffic never joins a thread). See the module docstring for asyncio tasks and thread
    pools. The arguments are validated when `thread()` is called, before the block starts.
    """
    scope = ThreadScope(
        key=_thread_key(key),
        language=normalize_language(language),
        metadata=_metadata(metadata),
        user_ref=_user_ref(user_ref),
    )
    return _thread(scope)


def _error_type(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    name = (
        cls.__qualname__ if module in ("builtins", "__main__") else f"{module}.{cls.__qualname__}"
    )
    return name[: semconv.ERROR_TYPE_MAX]


@contextmanager
def _turn(
    name: str, attributes: dict[str, AttributeValue], explicit: dict[str, AttributeValue]
) -> Iterator[Span]:
    tracer = get_tracer()
    with starting_turn():
        span = tracer.start_span(name, attributes=attributes)
    if explicit and span.is_recording():
        span.set_attributes(explicit)  # explicit attributes win over stamped ones
    with trace.use_span(
        span, end_on_exit=True, record_exception=True, set_status_on_exception=True
    ):
        try:
            yield span
        except Exception as exc:
            if span.is_recording():
                span.set_attribute(semconv.ERROR_TYPE, _error_type(exc))
            raise


def turn(
    name: str = "turn",
    *,
    eval_root: bool = False,
    end_thread: bool = False,
    operation: str = semconv.OP_INVOKE_AGENT,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> AbstractContextManager[Span]:
    """Start one turn: a span (the trace root unless a span is already current) with
    `gen_ai.operation.name = operation`, current for the duration of the block.

    Args:
        name: the span name.
        eval_root: set `insightmon.eval_root = true` on this span.
        end_thread: set `insightmon.thread.end = true` (this is the last turn).
        operation: one of `semconv.OPERATIONS` (ValueError otherwise).
        attributes: extra span attributes; they win over the attributes the SDK stamps.

    The span records an exception raised in the block (status error, an `exception` event and
    `error.type`) and re-raises it. Turn spans carry the component versions (like local root
    spans), so a turn nested under a server span still reports them.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("turn name must be a non-empty string")
    if operation not in semconv.OPERATIONS:
        raise ValueError(
            f"operation must be one of {', '.join(sorted(semconv.OPERATIONS))}, not {operation!r}"
        )
    explicit = dict(attributes or {})
    merged: dict[str, AttributeValue] = {semconv.OPERATION_NAME: operation, **explicit}
    if eval_root:
        merged[semconv.EVAL_ROOT] = True
    if end_thread:
        merged[semconv.THREAD_END] = True
    return _turn(name, merged, explicit)


def _target(span: Span | None) -> Span | None:
    chosen = span if span is not None else trace.get_current_span()
    return chosen if chosen.is_recording() else None


def mark_eval_root(span: Span | None = None) -> None:
    """Set `insightmon.eval_root = true` on `span` (default: the current span). The platform
    uses the earliest such span of a trace. No-op when there is no recording span."""
    target = _target(span)
    if target is not None:
        target.set_attribute(semconv.EVAL_ROOT, True)


def end_thread(span: Span | None = None) -> None:
    """Set `insightmon.thread.end = true` on `span` (default: the current span): the platform
    closes the thread (`close_reason = explicit`) and never reopens it. No-op when there is no
    recording span."""
    target = _target(span)
    if target is not None:
        target.set_attribute(semconv.THREAD_END, True)


def set_component(
    type: str,
    name: str,
    *,
    version: str | None = None,
    content_hash: str | None = None,
) -> None:
    """Declare the version (and / or content hash) of a component in use.

    Where the override applies:

    - inside an `evaluate()` task: that (example, trial) only (its spans and the item's
      `receipt`), never the process or the other runs;
    - inside a `thread()` block: that thread;
    - elsewhere: the whole process (spans started from now on, and the default `requested` /
      `receipt` of a later `evaluate()`).

    It is also written on the current span. The span processor stamps components on local root
    spans and turn spans only, so `spans.component` of other spans stays meaningful.

    Raises:
        ValueError: type empty, containing '.', or longer than 64; name empty or longer than
            200; version / content_hash longer than 200; neither version nor content_hash.
    """
    if version is None and content_hash is None:
        raise ValueError("set_component needs a version, a content_hash or both")
    component = Component(type=type, name=name, version=version, content_hash=content_hash)
    experiment = current_experiment()
    scope = _current_thread()
    if experiment is not None:
        experiment.set_component(component)
    elif scope is not None:
        scope.set_component(component)
    else:
        set_global_component(component)
    target = _target(None)
    if target is not None:
        target.set_attributes(component.attributes())


def set_response_id(response_id: str, span: Span | None = None) -> None:
    """Set `gen_ai.response.id` (at most 200 characters) on `span` (default: the current
    span). Must be the span's own attribute: the platform ignores it on the resource.
    No-op when there is no recording span.

    Raises:
        ValueError: empty or longer than 200 characters.
    """
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("response_id must be a non-empty string")
    text = response_id.strip()
    if len(text) > semconv.RESPONSE_ID_MAX:
        raise ValueError(f"response_id is longer than {semconv.RESPONSE_ID_MAX} characters")
    target = _target(span)
    if target is not None:
        target.set_attribute(semconv.RESPONSE_ID, text)
