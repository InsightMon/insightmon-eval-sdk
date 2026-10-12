"""Evaluation results as OTLP log events (`gen_ai.evaluation.result`).

Contract (docs/contract.md §9): the event name goes into both the LogRecord `event_name` field
and the `event.name` attribute; a record correlates with its span through its span context, or
through `gen_ai.response.id` when it has none. Only evaluation results (and, with content
capture on, message content events) are forwarded to the platform: the SDK's logger provider
is not a general purpose log pipeline.

`log_evaluation()` checks locally what the platform would reject or silently misread, and
raises `ValueError` before anything is sent: an empty name or one without any `[a-z0-9_]`
character, none or several of value / label / error_type, a non-finite value, no way to
correlate the record. The explanation is cut to 4000 characters (the platform's cap).

The platform refuses a record whose name is not a published score config (with the `span`,
`trace` or `thread` level); its OTLP answer names the reason, not the name, and only the SDK's
own exporter reads it. When the SDK exports to the platform, the first `log_evaluation()` of a
runtime starts a background read of the published configs (`GET /api/v1/score-configs`, once
per runtime; `log_evaluation()` never waits for it) and the SDK warns once per name the
project does not have. A platform without that route, or a key without its scopes, means no
check (the record is sent either way).

OpenTelemetry compatibility: `LogRecordProcessor.on_emit` receives a `LogData` (with
`.log_record`) in 1.35 and a `ReadWriteLogRecord` (also with `.log_record`) in later releases;
`EventFilterProcessor` reads `event_name` / `attributes` through `.log_record` for both.
Records are emitted with keyword arguments where the SDK logger accepts them (1.38 and later)
and as an SDK `LogRecord` before that.
"""

from __future__ import annotations

import inspect
import logging
import math
import os
import threading
import time
from collections.abc import Collection, Mapping
from typing import Any

import opentelemetry.sdk._logs as _sdk_logs
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.context import Context
from opentelemetry.sdk._logs import LogRecordProcessor
from opentelemetry.trace import INVALID_SPAN, NonRecordingSpan, Span, SpanContext

from insightmon import semconv
from insightmon._config import RuntimeState, Settings, get_runtime
from insightmon._version import __version__

__all__ = ["EventFilterProcessor", "evaluation_attributes", "log_evaluation"]

_logger = logging.getLogger(__name__)
_warned_lock = threading.Lock()
_warned_names: set[str] = set()
#: Subject levels a `log_evaluation()` score may take (its span, else the trace, else the thread).
LOG_SCORE_LEVELS: tuple[str, ...] = ("span", "trace", "thread")
#: Names noted before the published configs are known (beyond: not checked).
_PENDING_NAMES_MAX = 1000


class _NameCheck:
    """The published score names of one runtime, read once in a background thread."""

    def __init__(self, state: RuntimeState) -> None:
        self.state = state
        self.lock = threading.Lock()
        self.done = False
        self.names: frozenset[str] | None = None
        self.pending: set[str] = set()

    def note(self, name: str) -> None:
        with self.lock:
            if not self.done:
                if len(self.pending) < _PENDING_NAMES_MAX:
                    self.pending.add(name)
                return
            names = self.names
        if names is not None and name not in names:
            _warn_unknown(name)

    def run(self) -> None:
        try:
            names = _fetch_score_names(self.state.settings)
        except Exception:  # never fail the caller's process over a diagnostic
            _logger.debug("insightmon: could not read the score configs", exc_info=True)
            names = None
        with self.lock:
            self.done = True
            self.names = names
            pending, self.pending = sorted(self.pending), set()
        if names is not None:
            for name in pending:
                if name not in names:
                    _warn_unknown(name)


_name_check: _NameCheck | None = None


def _fetch_score_names(settings: Settings) -> frozenset[str] | None:
    """Published score names usable by `log_evaluation()`, or None when the platform does not
    say (tests replace this function)."""
    from insightmon.client import Client, published_score_names

    with Client(settings.base_url, settings.api_key, timeout=10.0, max_retries=1) as client:
        return published_score_names(client, LOG_SCORE_LEVELS)


def _check_name(state: RuntimeState, name: str) -> None:
    """Note `name` for the background check of the runtime `state` (exporting to the
    platform only)."""
    global _name_check
    if state.export_control is None:
        return
    start = False
    with _warned_lock:
        check = _name_check
        if check is None or check.state is not state:
            check = _name_check = _NameCheck(state)
            start = True
    check.note(name)
    if start:
        threading.Thread(target=check.run, name="insightmon-score-names", daemon=True).start()


def _warn_unknown(name: str) -> None:
    key = f"unknown:{name}"
    with _warned_lock:
        if key in _warned_names or len(_warned_names) >= _PENDING_NAMES_MAX:
            return
        _warned_names.add(key)
    _logger.warning(
        "insightmon: evaluation name %r is not a published score config of the project (for "
        "spans, traces or threads); the platform drops these records: create and publish the "
        "score config, or use another name",
        name,
    )


def _reinit_after_fork() -> None:
    global _warned_lock, _name_check
    _warned_lock = threading.Lock()
    _name_check = None  # its thread did not survive the fork


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)


def _check_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def evaluation_attributes(
    name: str,
    *,
    value: float | bool | None = None,
    label: str | None = None,
    explanation: str | None = None,
    error_type: str | None = None,
    response_id: str | None = None,
) -> dict[str, Any]:
    """The record attributes of one evaluation result (pure function, validated as above):
    `event.name`, `gen_ai.evaluation.name`, `.score.value`, `.score.label`, `.explanation`,
    `error.type`, `gen_ai.response.id`; only the fields that are set. The name is sent in the
    platform's normalized form (lower case, `[^a-z0-9_]` → `_`, at most 64 characters)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("evaluation name must be a non-empty string")
    normalized = semconv.normalize_score_name(name)
    if normalized is None:
        raise ValueError(
            f"evaluation name {name!r} has no letter, digit or '_': the platform cannot map it "
            "to a score config"
        )
    given = [
        field
        for field, item in (("value", value), ("label", label), ("error_type", error_type))
        if item is not None
    ]
    if not given:
        raise ValueError(
            "an evaluation result needs one of value, label or error_type "
            "(the platform rejects it otherwise)"
        )
    if len(given) > 1:
        raise ValueError(
            f"pass exactly one of value, label or error_type, not {' and '.join(given)}: the "
            "platform reads only one of them (label wins for boolean configs, value for "
            "numeric ones, error_type marks the evaluation as failed)"
        )
    attributes: dict[str, Any] = {
        semconv.EVENT_NAME: semconv.EVALUATION_RESULT_EVENT,
        semconv.EVALUATION_NAME: normalized,
    }
    if value is not None:
        if isinstance(value, bool):
            number = 1.0 if value else 0.0
        elif isinstance(value, int | float):
            number = float(value)
        else:
            raise ValueError(
                f"value must be a number or a bool, not {type(value).__name__} "
                "(use label= for text and categories)"
            )
        if not math.isfinite(number):
            raise ValueError(f"value must be a finite number, not {value!r}")
        attributes[semconv.EVALUATION_SCORE_VALUE] = number
    if label is not None:
        attributes[semconv.EVALUATION_SCORE_LABEL] = _check_text("label", label)
    if explanation is not None:
        if not isinstance(explanation, str):
            raise ValueError("explanation must be a string")
        text = explanation[: semconv.EVALUATION_EXPLANATION_MAX]
        if text.strip():
            attributes[semconv.EVALUATION_EXPLANATION] = text
    if error_type is not None:
        text = _check_text("error_type", error_type)
        attributes[semconv.ERROR_TYPE] = text[: semconv.EVALUATION_EXPLANATION_MAX]
    if response_id is not None:
        text = _check_text("response_id", response_id)
        if len(text) > semconv.RESPONSE_ID_MAX:
            raise ValueError(f"response_id is longer than {semconv.RESPONSE_ID_MAX} characters")
        attributes[semconv.RESPONSE_ID] = text
    return attributes


def _warn_renamed(name: str, normalized: str) -> None:
    if name == normalized:
        return
    with _warned_lock:
        if name in _warned_names:
            return
        _warned_names.add(name)
    _logger.warning(
        "insightmon: evaluation name %r is sent as %r (the platform's name rule); the project "
        "needs a published score config of that name",
        name,
        normalized,
    )


def _record_context(
    span: Span | SpanContext | None, response_id: str | None
) -> tuple[Context, bool]:
    """(context the record is emitted in, has a span context)."""
    if span is not None:
        span_context = span if isinstance(span, SpanContext) else span.get_span_context()
    elif response_id is not None:
        span_context = None  # correlate by response id only: never the current span
    else:
        span_context = trace.get_current_span().get_span_context()
    if span_context is not None and span_context.is_valid:
        return trace.set_span_in_context(NonRecordingSpan(span_context), Context()), True
    # A non-empty context holding the invalid span: OpenTelemetry treats an empty context as
    # "use the current one", which would attach the current span.
    return trace.set_span_in_context(INVALID_SPAN, Context()), False


def log_evaluation(
    name: str,
    *,
    value: float | bool | None = None,
    label: str | None = None,
    explanation: str | None = None,
    error_type: str | None = None,
    span: Span | SpanContext | None = None,
    response_id: str | None = None,
) -> None:
    """Report one evaluation result of a span (becomes a `source = import` score).

    Args:
        name: the score name; the platform lower-cases it, replaces `[^a-z0-9_]` with `_` and
            needs a published score config of that name (the platform refuses unknown names:
            `rejections()` counts them; the SDK warns once per name it had to normalize, and
            once per name the project's published configs do not have, when it can read
            them).
        value: numeric value; a bool is sent as 1.0 / 0.0.
        label: label for boolean (pass / fail ...), categorical and text configs.
        explanation: cut to 4000 characters.
        error_type: the evaluator failed: an unscored score with `evaluator_error`.
        span: the evaluated span or its context (default: the current span).
        response_id: correlate by `gen_ai.response.id` instead of a span. When given without
            `span`, the record is emitted with an empty context (never the current span). With
            `span` as well, the platform uses the span and falls back to the response id when
            that span is not stored.

    Exactly one of value, label and error_type must be given.

    Raises:
        ValueError: no correlation (no span, no current span, no response_id); none or more
            than one of value, label, error_type; empty name or one without any `[a-z0-9_]`;
            a value that is not a finite number.

    Without `init()` (or with export disabled) the record goes to the SDK's logger provider
    if one exists, otherwise nowhere.
    """
    attributes = evaluation_attributes(
        name,
        value=value,
        label=label,
        explanation=explanation,
        error_type=error_type,
        response_id=response_id,
    )
    context, has_span = _record_context(span, response_id)
    if not has_span and response_id is None:
        raise ValueError(
            "nothing to correlate the evaluation with: pass span=, response_id=, or call "
            "log_evaluation inside the evaluated span"
        )
    _warn_renamed(name, attributes[semconv.EVALUATION_NAME])
    state = get_runtime()
    provider = state.logger_provider if state is not None else None
    if state is None or provider is None:
        return
    _check_name(state, attributes[semconv.EVALUATION_NAME])
    logger = provider.get_logger(semconv.INSTRUMENTATION_NAME, __version__)
    emit_event(logger, context, semconv.EVALUATION_RESULT_EVENT, attributes)


# --------------------------------------------------------------------------- emitting

_keyword_emit: dict[type, bool] = {}


def _emits_keywords(logger: Any) -> bool:
    cls = type(logger)
    known = _keyword_emit.get(cls)
    if known is None:
        try:
            known = "event_name" in inspect.signature(logger.emit).parameters
        except (TypeError, ValueError):
            known = False
        _keyword_emit[cls] = known
    return known


def emit_event(
    logger: Any, context: Context, event_name: str, attributes: Mapping[str, Any]
) -> None:
    """Emit one event record (INFO, no body) through an SDK logger of any supported
    OpenTelemetry release."""
    now = time.time_ns()
    fields: dict[str, Any] = {
        "timestamp": now,
        "observed_timestamp": now,
        "context": context,
        "severity_number": SeverityNumber.INFO,
        "attributes": dict(attributes),
        "event_name": event_name,
    }
    if _emits_keywords(logger):
        logger.emit(**fields)
        return
    sdk_record = getattr(_sdk_logs, "LogRecord", None)
    if sdk_record is not None:
        logger.emit(sdk_record(resource=getattr(logger, "resource", None), **fields))
        return
    from opentelemetry._logs import LogRecord as APILogRecord

    logger.emit(APILogRecord(**fields))


# --------------------------------------------------------------------------- filtering


def record_event_name(log_record: Any) -> str | None:
    """The event name of a `LogData` / `ReadWriteLogRecord` / `LogRecord`: the `event_name`
    field, else the `event.name` attribute."""
    record = getattr(log_record, "log_record", log_record)
    name = getattr(record, "event_name", None)
    if isinstance(name, str) and name:
        return name
    attributes = getattr(record, "attributes", None) or {}
    value = attributes.get(semconv.EVENT_NAME)
    return value if isinstance(value, str) and value else None


class EventFilterProcessor(LogRecordProcessor):
    """Forward only records whose event name (field, else `event.name` attribute) is in
    `allowed` to `inner`; drop everything else."""

    def __init__(
        self,
        inner: LogRecordProcessor,
        allowed: Collection[str] = (semconv.EVALUATION_RESULT_EVENT,),
    ) -> None:
        self._inner = inner
        self._allowed = frozenset(allowed)

    @property
    def allowed(self) -> frozenset[str]:
        return self._allowed

    def on_emit(self, log_record: Any) -> None:
        if record_event_name(log_record) in self._allowed:
            self._inner.on_emit(log_record)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._inner.force_flush(timeout_millis))
