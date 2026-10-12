"""`InsightMonSpanProcessor`: stamps platform attributes on spans and holds experiment traces.

On start of every recording span:

- thread scope → `gen_ai.conversation.id`, `insightmon.metadata.<k>`, `enduser.id`. Nothing
  inside an experiment scope (experiment traffic never joins a thread), and nothing without a
  thread scope: the SDK never invents a conversation id;
- `insightmon.language`: the thread's language, else `init(language=...)`;
- `gen_ai.agent.name` / `gen_ai.agent.version` from `init()` on every span (the platform falls
  back to `service.version` when a trace has none, which can conflict with replay receipts).
  Anchor spans (below) always get the `init()` values; other spans keep a value that an
  instrumentation already set (e.g. the name of a sub-agent);
- anchor spans only, i.e. local root spans (no parent, or a remote parent) and turn spans:
  components (`insightmon.component.*`: global defaults merged with the overrides of the thread
  scope, or of the experiment scope inside `evaluate()`; a component attribute already on the
  span wins), `deployment.environment.name` when the span's
  resource does not carry the `init()` environment, and, inside an experiment scope, the
  experiment attributes (`insightmon.experiment_id` / `example_key` / `trial_index`) on local
  roots. Keeping components off other spans keeps the platform's `spans.component` (the one
  component a span declares on its own attributes) meaningful;
- experiment scope with `hold` → `hold_trace(trace_id, owner=scope.holder)` for the trace of
  a local root span.

On end: spans of held traces are buffered (bounded by `max_held_spans`; beyond it a span is
exported at once, with a warning) and forwarded to `export_processor` when
`insightmon._context.release_traces()` releases their trace, or dropped when
`discard_traces()` discards it (spans of a discarded trace that end later are dropped too);
every other span goes straight to `export_processor`. Holding is decided per trace id, so
spans of a held trace that were started in another thread (without the experiment scope) are
held as well. The held / discarded state is read in one step under the processor's lock, so a
span that ends while its trace is discarded is never exported.

Released spans reach `export_processor` in chunks of `release_chunk` with a `force_flush()`
before each chunk when there are more than that: a `BatchSpanProcessor` drops spans beyond its
queue (2048 by default), and one report of 50 items can release far more. `init()` sizes the
queue of its own batch processor for the whole held buffer and turns chunking off.

The processor can be used without `init()`: add it to your own `TracerProvider` with your own
export processor (e.g. a `BatchSpanProcessor` around any exporter). Stamping never raises into
the application: a failure is logged once and the span is left as it is.

`on_start` reads the thread and experiment scopes from context variables, so it must run in
the thread that starts the span: a provider whose active span processor is a
`ConcurrentMultiSpanProcessor` runs it in a pool, and nothing is stamped or held there
(`init()` warns).
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from insightmon import semconv
from insightmon._config import get_settings, global_components, merge_components
from insightmon._context import (
    add_release_listener,
    current_experiment,
    current_thread,
    hold_trace,
    remove_release_listener,
    trace_status,
)

__all__ = [
    "MAX_HELD_SPANS",
    "RELEASE_CHUNK",
    "InsightMonSpanProcessor",
    "stamp_attributes",
    "starting_turn",
]

_logger = logging.getLogger(__name__)

#: Spans kept for held traces before further spans are exported anyway.
MAX_HELD_SPANS = 10_000
#: Released spans handed to the export processor between two `force_flush()` calls.
RELEASE_CHUNK = 512

# Set by `insightmon.turn()` while it starts its span. `on_start` runs synchronously inside
# `Tracer.start_span`, in the caller's context, so the flag marks exactly that span.
_turn_start: ContextVar[bool] = ContextVar("insightmon_turn_start", default=False)


@contextmanager
def starting_turn() -> Iterator[None]:
    """Mark the span started inside the block as a turn span (an anchor span)."""
    token = _turn_start.set(True)
    try:
        yield
    finally:
        _turn_start.reset(token)


def stamp_attributes(span: Span) -> dict[str, Any]:
    """The attributes `InsightMonSpanProcessor.on_start` sets on `span` (module docstring).
    Also calls `hold_trace()` for a local root span inside a holding experiment scope."""
    settings = get_settings()
    parent = span.parent
    local_root = parent is None or parent.is_remote
    anchor = local_root or _turn_start.get()
    existing = span.attributes or {}
    experiment = current_experiment()
    scope = None if experiment is not None else current_thread()
    out: dict[str, Any] = {}

    if settings is not None:
        for key, agent_value in (
            (semconv.AGENT_NAME, settings.agent_name),
            (semconv.AGENT_VERSION, settings.agent_version),
        ):
            if agent_value is not None and (anchor or key not in existing):
                out[key] = agent_value

    language = scope.language if scope is not None else None
    if language is None and settings is not None:
        language = settings.language
    if language is not None:
        out[semconv.LANGUAGE] = language

    if scope is not None:
        out[semconv.GEN_AI_CONVERSATION_ID] = scope.key
        for key, meta_value in scope.metadata.items():
            out[semconv.metadata_attribute(key)] = meta_value
        if scope.user_ref is not None:
            out[semconv.ENDUSER_ID] = scope.user_ref

    if anchor:
        if experiment is not None:
            overrides = experiment.component_overrides()
        else:
            overrides = scope.component_overrides() if scope is not None else ()
        for component in merge_components(global_components(), overrides):
            for key, component_value in component.attributes().items():
                if key not in existing:
                    out[key] = component_value
        environment = settings.environment if settings is not None else None
        if environment is not None:
            resource = span.resource.attributes if span.resource is not None else {}
            if resource.get(semconv.DEPLOYMENT_ENVIRONMENT_NAME) != environment:
                out[semconv.DEPLOYMENT_ENVIRONMENT_NAME] = environment

    if experiment is not None and local_root:
        if experiment.stamp:
            for key, experiment_value in experiment.attributes().items():
                if key not in existing:
                    out[key] = experiment_value
        context = span.get_span_context()
        if experiment.hold and context is not None and context.is_valid:
            hold_trace(context.trace_id, experiment.holder)
    return out


class InsightMonSpanProcessor(SpanProcessor):
    """Stamp InsightMon attributes on start; export (or hold) on end.

    Args:
        export_processor: the processor that exports ended spans (typically a
            `BatchSpanProcessor` around the SDK's OTLP exporter). None: stamp only; ended spans
            go nowhere (another processor on the provider exports them).
        max_held_spans: bound of the held-trace buffer.
        release_chunk: released spans handed to `export_processor` between two
            `force_flush()` calls (module docstring); None hands them over at once (for an
            export processor whose queue holds `max_held_spans` and more).
    """

    def __init__(
        self,
        export_processor: SpanProcessor | None = None,
        *,
        max_held_spans: int = MAX_HELD_SPANS,
        release_chunk: int | None = RELEASE_CHUNK,
    ) -> None:
        if max_held_spans < 0:
            raise ValueError("max_held_spans must be >= 0")
        if release_chunk is not None and release_chunk < 1:
            raise ValueError("release_chunk must be >= 1 or None")
        self._export = export_processor
        self._max_held_spans = max_held_spans
        self._release_chunk = release_chunk
        self._lock = threading.Lock()
        self._held: dict[int, list[ReadableSpan]] = {}
        self._held_count = 0
        self._overflow_warned = False
        self._stamp_failed = False
        self._shut_down = False
        if export_processor is not None:
            add_release_listener(self._on_release)
        if hasattr(os, "register_at_fork"):
            ref = weakref.WeakMethod(self._reinit_after_fork)

            def _after_in_child() -> None:
                method = ref()
                if method is not None:
                    method()

            os.register_at_fork(after_in_child=_after_in_child)

    @property
    def export_processor(self) -> SpanProcessor | None:
        return self._export

    @property
    def held_span_count(self) -> int:
        """Spans currently buffered for held traces."""
        with self._lock:
            return self._held_count

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if self._shut_down:
            return
        try:
            attributes = stamp_attributes(span)
            if attributes:
                span.set_attributes(attributes)
        except Exception:  # never break the application's span
            if not self._stamp_failed:
                self._stamp_failed = True
                _logger.warning("insightmon: could not stamp span attributes", exc_info=True)
        if self._export is not None:
            self._export.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if self._export is None or self._shut_down:
            return
        context = span.context
        overflow = False
        if context is not None:
            with self._lock:
                status = trace_status(context.trace_id)
                if status == "discarded":
                    return  # a stopped evaluate() run never reported this item
                if status == "held":
                    if self._held_count < self._max_held_spans:
                        self._held.setdefault(context.trace_id, []).append(span)
                        self._held_count += 1
                        return
                    overflow = True
        if overflow and not self._overflow_warned:
            self._overflow_warned = True
            _logger.warning(
                "insightmon: more than %d spans of experiment traces are waiting for their "
                "items to be reported; exporting further spans immediately",
                self._max_held_spans,
            )
        self._export.on_end(span)

    def _on_release(self, trace_ids: frozenset[int] | None, discard: bool = False) -> None:
        spans = self._pop_held(trace_ids)
        if discard:
            if spans:
                _logger.debug("insightmon: dropped %d spans of unreported items", len(spans))
            return
        self._forward(spans)

    def _forward(self, spans: list[ReadableSpan]) -> None:
        """Hand released spans to the export processor, in chunks (module docstring)."""
        export = self._export
        if export is None or not spans:
            return
        chunk = self._release_chunk
        if chunk is None or len(spans) <= chunk:
            for span in spans:
                export.on_end(span)
            return
        for start in range(0, len(spans), chunk):
            try:
                export.force_flush()
            except Exception:
                _logger.debug("insightmon: flushing before a release failed", exc_info=True)
            for span in spans[start : start + chunk]:
                export.on_end(span)

    def _pop_held(self, trace_ids: frozenset[int] | None) -> list[ReadableSpan]:
        with self._lock:
            if trace_ids is None:
                spans = [span for group in self._held.values() for span in group]
                self._held.clear()
            else:
                spans = []
                for trace_id in trace_ids:
                    spans.extend(self._held.pop(trace_id, ()))
            self._held_count -= len(spans)
        return spans

    def _reinit_after_fork(self) -> None:
        """In a forked child: a new lock, and no held spans (they belong to the parent)."""
        self._lock = threading.Lock()
        self._held = {}
        self._held_count = 0

    def shutdown(self) -> None:
        """Release every held span to the export processor, then shut it down."""
        if self._shut_down:
            return
        remove_release_listener(self._on_release)
        spans = self._pop_held(None)
        self._forward(spans)
        self._shut_down = True
        if self._export is not None:
            self._export.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush the export processor (held spans stay held until released)."""
        if self._export is None or self._shut_down:
            return True
        return bool(self._export.force_flush(timeout_millis))
