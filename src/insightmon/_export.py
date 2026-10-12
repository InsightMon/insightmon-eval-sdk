"""Exporters to the platform's OTLP/HTTP endpoints, with request splitting and content stripping.

- `PlatformSpanExporter` / `PlatformLogExporter` post to `<base>/otlp/v1/traces` and
  `<base>/otlp/v1/logs`: protobuf, gzip, and exactly the headers `Authorization: Bearer <key>`,
  `Content-Type`, `Content-Encoding` and `User-Agent`. They are the SDK's own (httpx) exporters,
  not OpenTelemetry's: those merge the generic `OTEL_EXPORTER_OTLP_*HEADERS` variables (which
  may carry another vendor's credentials) into the request, retry a different set of statuses
  in every release, follow or silently accept redirects, and ignore flush deadlines.
- Retries (docs/contract.md §0.1): 429, 500, 502, 503, 504 and network errors, honouring
  `Retry-After`, within `EXPORT_TIMEOUT_S` per export (and never past the deadline of a running
  `shutdown()`). The platform deduplicates spans by id and evaluation results by record digest,
  so a retry never writes twice. Other statuses fail at once. A 3xx answer is never followed
  (the API key must not travel): it is logged once per location as a WARNING, counted in
  `rejections().redirects`, and the export fails.
- `ExportControl` is shared by the exporters of one `init()`: `flush()` / `shutdown()` read its
  failure count (a failed export makes `flush()` return False) and `shutdown()` sets a deadline
  and finally aborts it, so that backoff waits end at once and nothing is sent after the
  deadline.
- The platform refuses bodies over 8 MiB (compressed or not). The splitting wrappers measure a
  batch the way it is serialized (protobuf, before compression) and split it in halves until
  every part is at most `max_bytes` (default `semconv.TARGET_BODY_BYTES`); a single span or
  record that is still too large is dropped with a warning (and counted as a failure).
- With content capture off, `ContentStrippingSpanExporter` removes content attributes
  (`semconv.is_content_attribute` plus `init(content_attributes=...)`) from spans and span
  events, and drops content span events (`semconv.CONTENT_EVENTS`), before export: content
  never leaves the process (an older platform keeps non-standard content attributes even under
  `metadata_only`; the current one drops there the third-party content attributes `semconv`
  lists, but not the ones added with `content_attributes`).
- Partial success: the platform answers 200 with `partial_success {rejected_spans |
  rejected_log_records, error_message}` when it refuses part of a request (e.g. an evaluation
  result whose name is not a published score config: `score_config_not_found`). Each distinct
  reason is logged once as a warning (later ones at debug level) and counted in `rejections()`
  (docs/contract.md §2, §3, §9.1).
- The exporters send with OpenTelemetry instrumentation suppressed, so that an httpx
  instrumentation never traces the export itself.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import random
import threading
import time
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx
import opentelemetry.sdk._logs.export as _log_export
from opentelemetry import context as otel_context
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util import BoundedList

from insightmon import semconv
from insightmon._config import OTLP_LOGS_PATH, OTLP_TRACES_PATH, Settings
from insightmon._errors import CONFIG_ERROR, InsightMonError, parse_retry_after
from insightmon._http import USER_AGENT, backoff_delay
from insightmon.content import has_content, strip_content

__all__ = [
    "ContentStrippingSpanExporter",
    "ExportControl",
    "PlatformLogExporter",
    "PlatformSpanExporter",
    "Rejections",
    "SplittingLogExporter",
    "SplittingSpanExporter",
    "build_log_exporter",
    "build_span_exporter",
    "rejections",
]

_logger = logging.getLogger(__name__)

#: Seconds one export (every attempt and wait together) may take.
EXPORT_TIMEOUT_S = 10.0
#: Statuses an export retries within its deadline.
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
#: First backoff ceiling and cap (full jitter) between export attempts.
RETRY_BASE_S = 0.5
RETRY_CAP_S = 4.0
#: Seconds between two warnings about failed exports (the others are logged at debug level).
FAILURE_WARNING_INTERVAL_S = 60.0

#: Module level so tests can replace it (zero waits between retries).
_jitter: Callable[[], float] = random.random

#: The log export result enum was renamed in later OpenTelemetry releases.
LogResult: Any = getattr(_log_export, "LogRecordExportResult", None) or getattr(
    _log_export, "LogExportResult", None
)

Signal = Literal["spans", "log records"]


def _require_export(settings: Settings) -> str:
    """The API key of export settings (raises `config_error` without base URL and key)."""
    if not settings.export_enabled or settings.api_key is None:
        raise InsightMonError(CONFIG_ERROR, None, "exporting needs a base URL and an API key")
    return settings.api_key


# --------------------------------------------------------------------------- partial success

#: Distinct platform reasons kept in `Rejections.reasons`.
MAX_REASONS = 20


@dataclass(frozen=True)
class Rejections:
    """What the platform refused in OTLP answers since the process started (or since
    `reset_rejections()`)."""

    spans: int = 0
    log_records: int = 0
    #: Distinct `error_message` values, oldest first, e.g. `"score_config_not_found"`.
    reasons: tuple[str, ...] = ()
    #: Export requests answered with a redirect (3xx): never followed, their data is lost.
    redirects: int = 0


_rejections_lock = threading.Lock()
_rejections = Rejections()
_warn_lock = threading.Lock()
_redirect_locations: set[str] = set()
_last_failure_warning: dict[str, float] = {}


def rejections() -> Rejections:
    """Counts of spans and log records the platform refused (partial success), and of export
    requests it answered with a redirect."""
    with _rejections_lock:
        return _rejections


def reset_rejections() -> None:
    global _rejections
    with _rejections_lock:
        _rejections = Rejections()
    with _warn_lock:
        _redirect_locations.clear()
        _last_failure_warning.clear()


def _record_rejection(signal: Signal, rejected: int, message: str) -> None:
    global _rejections
    with _rejections_lock:
        current = _rejections
        new_reason = bool(message) and message not in current.reasons
        reasons = (*current.reasons, message) if new_reason else current.reasons
        _rejections = replace(
            current,
            spans=current.spans + (rejected if signal == "spans" else 0),
            log_records=current.log_records + (rejected if signal == "log records" else 0),
            reasons=reasons[-MAX_REASONS:],
        )
    level = logging.WARNING if new_reason else logging.DEBUG
    _logger.log(
        level,
        "insightmon: the platform refused %d %s: %s (an evaluation result needs a published "
        "score config of that name and a stored span or response id; see docs/contract.md §9)",
        rejected,
        signal,
        message or "no reason given",
    )


def _record_redirect(signal: Signal, status: int, location: str) -> None:
    global _rejections
    with _rejections_lock:
        _rejections = replace(_rejections, redirects=_rejections.redirects + 1)
    with _warn_lock:
        first = location not in _redirect_locations
        _redirect_locations.add(location)
    _logger.log(
        logging.WARNING if first else logging.DEBUG,
        "insightmon: the platform answered an export of %s with HTTP %d (redirect to %r); the "
        "SDK never follows redirects (the API key must not travel), so these %s are lost: set "
        "the base URL to the final address",
        signal,
        status,
        location,
        signal,
    )


def _partial_success(signal: Signal, response: Any) -> tuple[int, str]:
    """(rejected count, error message) of a 2xx OTLP answer (protobuf or JSON)."""
    body = response.content
    if not body:
        return 0, ""
    content_type = str(response.headers.get("content-type", "")).lower()
    if "json" in content_type:
        data = json.loads(body)
        partial = data.get("partialSuccess") or data.get("partial_success") or {}
        key = "rejectedSpans" if signal == "spans" else "rejectedLogRecords"
        snake = "rejected_spans" if signal == "spans" else "rejected_log_records"
        count = int(partial.get(key) or partial.get(snake) or 0)
        message = partial.get("errorMessage") or partial.get("error_message") or ""
        return count, str(message)
    if signal == "spans":
        traces = ExportTraceServiceResponse()
        traces.ParseFromString(body)
        return int(traces.partial_success.rejected_spans), traces.partial_success.error_message
    logs = ExportLogsServiceResponse()
    logs.ParseFromString(body)
    return int(logs.partial_success.rejected_log_records), logs.partial_success.error_message


def partial_success_hook(signal: Signal) -> Callable[..., None]:
    """A response hook that records the partial success of a 2xx OTLP answer (any object with
    `status_code`, `headers` and `content`)."""

    def hook(response: Any, *args: Any, **kwargs: Any) -> None:
        try:
            status = getattr(response, "status_code", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                return
            rejected, message = _partial_success(signal, response)
        except Exception:  # never break an export over a body the SDK cannot read
            _logger.debug("insightmon: could not read an OTLP answer", exc_info=True)
            return
        if rejected or message:
            _record_rejection(signal, rejected, message)

    return hook


# --------------------------------------------------------------------------- export control

_controls: weakref.WeakSet[ExportControl] = weakref.WeakSet()


class ExportControl:
    """Shared state of the exporters of one `init()`.

    - `failures`: exports that failed for good (after their retries), plus single spans or
      records dropped for their size; `flush()` returns False when it grew during the flush.
    - `dropped(signal)`: spans / log records lost that way.
    - `set_deadline(t)`: no request runs past the monotonic time `t` (`shutdown()`).
    - `abort()`: every export fails at once from now on, and backoff waits end (`shutdown()`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self._deadline: float | None = None
        self._failures = 0
        self._dropped: dict[str, int] = {"spans": 0, "log records": 0}
        _controls.add(self)

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    @property
    def deadline(self) -> float | None:
        return self._deadline

    def dropped(self, signal: Signal) -> int:
        with self._lock:
            return self._dropped[signal]

    def abort(self) -> None:
        self._abort.set()

    def set_deadline(self, deadline: float | None) -> None:
        self._deadline = deadline

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True when the control was aborted meanwhile."""
        return self._abort.wait(max(0.0, seconds))

    def failed(self, signal: Signal, count: int) -> None:
        with self._lock:
            self._failures += 1
            self._dropped[signal] += max(0, count)

    def _after_fork(self) -> None:
        aborted = self._abort.is_set()
        self._lock = threading.Lock()
        self._abort = threading.Event()
        if aborted:
            self._abort.set()


def _warn_failure(signal: Signal, count: int, endpoint: str, reason: str) -> None:
    now = time.monotonic()
    with _warn_lock:
        last = _last_failure_warning.get(signal)
        warn = last is None or now - last >= FAILURE_WARNING_INTERVAL_S
        if warn:
            _last_failure_warning[signal] = now
    _logger.log(
        logging.WARNING if warn else logging.DEBUG,
        "insightmon: could not export %d %s to %s: %s",
        count,
        signal,
        endpoint,
        reason,
    )


# --------------------------------------------------------------------------- platform exporters


_REDIRECTED = "redirected (not followed):"


def _describe(response: httpx.Response) -> str:
    text = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return text
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        return f"{text} {body['code']}"
    return text


class _Poster:
    """Posts one encoded OTLP request (gzip) with retries; shared by both exporters."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        signal: Signal,
        control: ExportControl | None,
        timeout_s: float,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self.endpoint = endpoint
        self.signal: Signal = signal
        self.control = control if control is not None else ExportControl()
        self.timeout_s = timeout_s
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        }
        self._transport = transport
        self._client: httpx.Client | None = None
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._shut_down = False

    def _http(self) -> httpx.Client:
        if self._pid != os.getpid():  # forked: never share the parent's connections
            self._lock = threading.Lock()
            self._client = None
            self._pid = os.getpid()
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(
                    transport=self._transport, follow_redirects=False, trust_env=True
                )
            return self._client

    def post(self, body: bytes, count: int) -> bool:
        """Send `body` (an encoded OTLP request of `count` items); True on success."""
        if self._shut_down:
            self._failed(count, "the exporter is shut down")
            return False
        token = otel_context.attach(otel_context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            reason = self._attempts(gzip.compress(body))
        finally:
            otel_context.detach(token)
        if reason is None:
            return True
        self._failed(count, reason, warn=not reason.startswith(_REDIRECTED))
        return False

    def _attempts(self, payload: bytes) -> str | None:
        """None on success, else why the export failed (a redirect was already logged)."""
        control = self.control
        budget_end = time.monotonic() + self.timeout_s
        reason = "no attempt"
        attempt = 0
        while True:
            attempt += 1
            if control.aborted:
                return f"{reason}; shutting down" if attempt > 1 else "shutting down"
            end = budget_end
            deadline = control.deadline
            if deadline is not None:
                end = min(end, deadline)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return f"{reason}; out of time" if attempt > 1 else "out of time"
            retry_after: float | None = None
            try:
                response = self._http().post(
                    self.endpoint, content=payload, headers=self._headers, timeout=remaining
                )
            except httpx.TransportError as exc:
                reason = str(exc) or type(exc).__name__
            except Exception as exc:  # e.g. an invalid URL: retrying cannot help
                return f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    partial_success_hook(self.signal)(response)
                    return None
                if 300 <= status < 400:
                    location = response.headers.get("location", "")
                    _record_redirect(self.signal, status, location)
                    return f"{_REDIRECTED} HTTP {status} to {location!r}"
                reason = _describe(response)
                if status not in RETRY_STATUSES:
                    return reason
                retry_after = parse_retry_after(response.headers.get("retry-after"))
            delay = backoff_delay(
                attempt,
                base_s=RETRY_BASE_S,
                cap_s=RETRY_CAP_S,
                retry_after_s=retry_after,
                rng=_jitter,
            )
            if time.monotonic() + delay >= end:
                return f"{reason}; out of time"
            if control.wait(delay):
                return f"{reason}; shutting down"

    def _failed(self, count: int, reason: str, *, warn: bool = True) -> None:
        self.control.failed(self.signal, count)
        if warn:
            _warn_failure(self.signal, count, self.endpoint, reason)
        else:
            _logger.debug("insightmon: could not export %d %s: %s", count, self.signal, reason)

    def shutdown(self) -> None:
        self._shut_down = True
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


class PlatformSpanExporter(SpanExporter):
    """OTLP/HTTP span exporter to the platform (module docstring).

    Args:
        endpoint: `<base>/otlp/v1/traces`.
        api_key: the `imk_...` key.
        control: the `ExportControl` shared with the other exporters of the same `init()`.
        timeout_s: seconds one export may take, retries included.
        transport: an httpx transport (tests).
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        control: ExportControl | None = None,
        timeout_s: float = EXPORT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._poster = _Poster(endpoint, api_key, "spans", control, timeout_s, transport)

    @property
    def endpoint(self) -> str:
        return self._poster.endpoint

    @property
    def control(self) -> ExportControl:
        return self._poster.control

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        batch = list(spans)
        if not batch:
            return SpanExportResult.SUCCESS
        body = encode_spans(batch).SerializeToString()
        ok = self._poster.post(body, len(batch))
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._poster.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class PlatformLogExporter:
    """OTLP/HTTP log exporter to the platform (`<base>/otlp/v1/logs`); same arguments as
    `PlatformSpanExporter`. Duck-typed log exporter (`export`, `shutdown`, `force_flush`):
    the base class was renamed across OpenTelemetry releases."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        control: ExportControl | None = None,
        timeout_s: float = EXPORT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._poster = _Poster(endpoint, api_key, "log records", control, timeout_s, transport)

    @property
    def endpoint(self) -> str:
        return self._poster.endpoint

    @property
    def control(self) -> ExportControl:
        return self._poster.control

    def export(self, batch: Sequence[Any]) -> Any:
        records = list(batch)
        if not records:
            return LogResult.SUCCESS
        body = encode_logs(records).SerializeToString()
        ok = self._poster.post(body, len(records))
        return LogResult.SUCCESS if ok else LogResult.FAILURE

    def shutdown(self) -> None:
        self._poster.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def build_span_exporter(settings: Settings, control: ExportControl | None = None) -> SpanExporter:
    """The span exporter `init()` uses: platform OTLP/HTTP → splitting → (content stripping
    when `settings.capture_content` is False). Requires `settings.export_enabled`."""
    api_key = _require_export(settings)
    control = control if control is not None else ExportControl()
    platform = PlatformSpanExporter(settings.endpoint(OTLP_TRACES_PATH), api_key, control=control)
    exporter: SpanExporter = SplittingSpanExporter(platform, control=control)
    if not settings.capture_content:
        exporter = ContentStrippingSpanExporter(
            exporter, is_content=semconv.content_matcher(settings.content_attributes)
        )
    return exporter


def build_log_exporter(settings: Settings, control: ExportControl | None = None) -> Any:
    """The log exporter `init()` uses: platform OTLP/HTTP logs → splitting. Typed `Any`
    because the exporter base class was renamed across OpenTelemetry releases."""
    api_key = _require_export(settings)
    control = control if control is not None else ExportControl()
    platform = PlatformLogExporter(settings.endpoint(OTLP_LOGS_PATH), api_key, control=control)
    return SplittingLogExporter(platform, control=control)


def _reinit_after_fork() -> None:
    global _rejections_lock, _warn_lock
    _rejections_lock = threading.Lock()
    _warn_lock = threading.Lock()
    for control in list(_controls):
        control._after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)


# --------------------------------------------------------------------------- splitting


def _split(
    batch: list[Any], max_bytes: int, size_of: Callable[[list[Any]], int], kind: str
) -> tuple[list[list[Any]], int]:
    """(parts of at most `max_bytes`, number of items dropped because a single one is too big)."""
    parts: list[list[Any]] = []
    dropped = 0
    pending = [batch]
    while pending:
        part = pending.pop()
        if not part:
            continue
        if size_of(part) <= max_bytes:
            parts.append(part)
        elif len(part) == 1:
            dropped += 1
            _logger.warning(
                "insightmon: dropping one %s larger than %d bytes (the platform refuses "
                "requests over 8 MiB)",
                kind,
                max_bytes,
            )
        else:
            middle = len(part) // 2
            pending.append(part[middle:])
            pending.append(part[:middle])  # popped first: parts keep the batch order
    return parts, dropped


def _span_request_size(spans: list[ReadableSpan]) -> int:
    return int(encode_spans(spans).ByteSize())


def _log_request_size(batch: list[Any]) -> int:
    return int(encode_logs(batch).ByteSize())


def _flushed(result: Any) -> bool:
    """`SpanExporter.force_flush` in the SDK base class returns None ("nothing to flush")."""
    return result is not False


class SplittingSpanExporter(SpanExporter):
    """Split a batch so that each serialized OTLP request is at most `max_bytes`. Spans dropped
    for their size are counted in `control` (when given) as a failed export."""

    def __init__(
        self,
        inner: SpanExporter,
        max_bytes: int = semconv.TARGET_BODY_BYTES,
        *,
        control: ExportControl | None = None,
    ) -> None:
        self._inner = inner
        self._max_bytes = max_bytes
        self._control = control

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        batch = list(spans)
        if not batch:
            return SpanExportResult.SUCCESS
        parts, dropped = _split(batch, self._max_bytes, _span_request_size, "span")
        if dropped and self._control is not None:
            self._control.failed("spans", dropped)
        ok = dropped == 0
        for part in parts:
            if self._inner.export(part) is not SpanExportResult.SUCCESS:
                ok = False
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return _flushed(self._inner.force_flush(timeout_millis))


class SplittingLogExporter:
    """Split a batch of log records so that each serialized request is at most `max_bytes`
    and holds at most `semconv.OTLP_MAX_LOG_RECORDS` records (drops counted in `control`).
    Duck-typed log exporter (`export`, `shutdown`, `force_flush`)."""

    def __init__(
        self,
        inner: Any,
        max_bytes: int = semconv.TARGET_BODY_BYTES,
        *,
        control: ExportControl | None = None,
    ) -> None:
        self._inner = inner
        self._max_bytes = max_bytes
        self._control = control

    def export(self, batch: Sequence[Any]) -> Any:
        records = list(batch)
        if not records:
            return LogResult.SUCCESS
        step = semconv.OTLP_MAX_LOG_RECORDS
        ok = True
        for start in range(0, len(records), step):
            chunk = records[start : start + step]
            parts, dropped = _split(chunk, self._max_bytes, _log_request_size, "log record")
            if dropped and self._control is not None:
                self._control.failed("log records", dropped)
            ok = ok and dropped == 0
            for part in parts:
                if self._inner.export(part) is not LogResult.SUCCESS:
                    ok = False
        return LogResult.SUCCESS if ok else LogResult.FAILURE

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return _flushed(self._inner.force_flush(timeout_millis))


# --------------------------------------------------------------------------- content stripping


def _is_content_event(event: Event) -> bool:
    return event.name in semconv.CONTENT_EVENTS


def _bounded_attributes(attributes: dict[str, Any], dropped: int) -> BoundedAttributes:
    bounded = BoundedAttributes(maxlen=None, attributes=attributes, immutable=True)
    bounded.dropped = dropped
    return bounded


def _bounded_list(items: Sequence[Any], dropped: int) -> BoundedList[Any]:
    bounded = BoundedList.from_seq(None, items)
    bounded.dropped = dropped
    return bounded


def strip_span(
    span: ReadableSpan, is_content: Callable[[str], bool] = semconv.is_content_attribute
) -> ReadableSpan:
    """`span` itself when it holds no content, else a copy without content attributes
    (`is_content`) and content events (dropped attribute / event / link counts are kept)."""
    events = tuple(span.events)
    events_have_content = any(
        _is_content_event(event) or has_content(event.attributes, is_content) for event in events
    )
    if not has_content(span.attributes, is_content) and not events_have_content:
        return span
    kept: list[Event] = []
    for event in events:
        if _is_content_event(event):
            continue
        if has_content(event.attributes, is_content):
            event = Event(
                event.name, strip_content(event.attributes or {}, is_content), event.timestamp
            )
        kept.append(event)
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_bounded_attributes(
            strip_content(span.attributes or {}, is_content), span.dropped_attributes
        ),
        events=_bounded_list(kept, span.dropped_events),
        links=_bounded_list(span.links, span.dropped_links),
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ContentStrippingSpanExporter(SpanExporter):
    """Export copies of the spans without content attributes (`is_content`, by default
    `semconv.is_content_attribute`) and content events."""

    def __init__(
        self,
        inner: SpanExporter,
        *,
        is_content: Callable[[str], bool] = semconv.is_content_attribute,
    ) -> None:
        self._inner = inner
        self._is_content = is_content

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([strip_span(span, self._is_content) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return _flushed(self._inner.force_flush(timeout_millis))
