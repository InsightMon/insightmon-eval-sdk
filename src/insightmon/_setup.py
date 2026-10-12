"""`init()`, `flush()`, `shutdown()` and `InsightMonHandle`.

`init()` wiring (docs/design.md §3):

1. `resolve_settings(...)` (arguments, then `INSIGHTMON_BASE_URL`, `INSIGHTMON_API_KEY`,
   `OTEL_SERVICE_NAME`). Base URL and key together, or neither: without them the SDK only stamps
   attributes and exports nothing (a collector or your own exporter forwards the spans).
2. Tracer provider: `tracer_provider` if given; else the global provider when it is an SDK
   `TracerProvider` (the SDK attaches to it and never shuts it down); else a new provider with
   resource `service.name` (`service_name`, else `agent_name`) and
   `deployment.environment.name` (when `environment` is set), set as the global provider when
   `set_global` is True and no global provider exists yet (`set_tracer_provider` is never
   called twice). The SDK never sets `service.version` (docs/contract.md §11). A provider
   whose active span processor is a `ConcurrentMultiSpanProcessor` gets a warning: stamping
   and holding need `on_start` in the caller's thread.
3. Adds one `InsightMonSpanProcessor` whose export processor is a `BatchSpanProcessor` around
   `build_span_exporter(settings)` (queue sized for the whole held-span buffer), and/or the
   `export_processor` the application passes (e.g. its collector pipeline): spans of
   `evaluate()` items are held only on that path. None of both: stamping only.
4. Logger provider: `logger_provider` if given, else (when exporting) a new SDK
   `LoggerProvider` (never taken from the global one: it may carry application logs). Its
   processor is an `EventFilterProcessor` (evaluation results; plus content events with
   `capture_content`) around a `BatchLogRecordProcessor` of `build_log_exporter(settings)`.
5. Stores the `RuntimeState`; registers `shutdown(ATEXIT_TIMEOUT_S)` with `atexit` once.

Deadlines: `flush(timeout_s)` and `shutdown(timeout_s)` return within `timeout_s` (plus
`SHUTDOWN_GRACE_S` for `shutdown`) whatever the platform does: each processor is flushed in a
helper thread joined with the time left (one flush per processor at a time; concurrent callers
join it), `shutdown()` caps every export request at its deadline and then aborts the SDK's
exporters. `flush()` returns False on timeout and when an export failed meanwhile. Neither
holds the SDK's lock during network I/O.

A second `init()` while one is active warns and returns the existing handle unchanged; after
`shutdown()`, `init()` sets everything up again (attaching to the provider it created before
when that one became the global provider).

Processes that fork (gunicorn, uWSGI, multiprocessing): OpenTelemetry's batch processors restart
their worker thread in the child, and the SDK recreates its own locks there (and forgets the
parent's held spans), so a provider set up before the fork keeps exporting from the children.
Calling `init()` in each worker after the fork (e.g. a `post_fork` hook) is still the most
predictable setup, and the only one when the master process never calls `init()`.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import warnings
import weakref
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    ConcurrentMultiSpanProcessor,
    SpanProcessor,
    SynchronousMultiSpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from insightmon import _export, semconv
from insightmon._config import (
    ComponentLike,
    RuntimeState,
    Settings,
    get_runtime,
    reset_runtime,
    resolve_settings,
    runtime_lock,
    set_runtime,
)
from insightmon._context import release_traces
from insightmon.logs import EventFilterProcessor
from insightmon.processor import MAX_HELD_SPANS, RELEASE_CHUNK, InsightMonSpanProcessor

__all__ = ["InsightMonHandle", "flush", "init", "shutdown"]

_logger = logging.getLogger(__name__)
_atexit_lock = threading.Lock()
_atexit_registered = False

#: Seconds the `atexit` hook gives `shutdown()`.
ATEXIT_TIMEOUT_S = 5.0
#: Seconds `shutdown()` waits after its deadline for the aborted exports to give up.
SHUTDOWN_GRACE_S = 0.5
#: Queue of the SDK's batch span processor: the whole held-span buffer can be released at once
#: on top of regular traffic (OpenTelemetry's default, 2048, drops the rest).
SPAN_QUEUE_SIZE = MAX_HELD_SPANS + 2048


class InsightMonHandle:
    """What `init()` returns: the resolved settings and providers. Usable as a context
    manager (`with insightmon.init(...):` shuts down on exit)."""

    def __init__(
        self,
        settings: Settings,
        tracer_provider: TracerProvider | None,
        logger_provider: LoggerProvider | None,
    ) -> None:
        self.settings = settings
        self.tracer_provider = tracer_provider
        self.logger_provider = logger_provider

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Same as `insightmon.flush()`."""
        return flush(timeout_s)

    def shutdown(self, timeout_s: float = 10.0) -> None:
        """Same as `insightmon.shutdown()`."""
        shutdown(timeout_s)

    def __enter__(self) -> InsightMonHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()

    def __repr__(self) -> str:
        return f"InsightMonHandle(settings={self.settings!r})"


def _resource(settings: Settings) -> Resource:
    attributes: dict[str, str] = {}
    service = settings.service_name or settings.agent_name
    if service is not None:
        attributes[semconv.SERVICE_NAME] = service
    if settings.environment is not None:
        attributes[semconv.DEPLOYMENT_ENVIRONMENT_NAME] = settings.environment
    return Resource.create(attributes)


def _tracer_provider(
    settings: Settings, given: TracerProvider | None, set_global: bool
) -> tuple[TracerProvider, bool]:
    """(provider, created by the SDK)."""
    if given is not None:
        if not isinstance(given, TracerProvider):
            raise TypeError("tracer_provider must be an opentelemetry.sdk.trace.TracerProvider")
        return given, False
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current, False
    provider = TracerProvider(resource=_resource(settings), shutdown_on_exit=False)
    if set_global:
        if isinstance(current, trace.ProxyTracerProvider):
            trace.set_tracer_provider(provider)
        else:
            _logger.warning(
                "insightmon: the global tracer provider (%s) is not an OpenTelemetry SDK "
                "provider; spans of other instrumentations will not reach InsightMon",
                type(current).__name__,
            )
    return provider, True


def _warn_concurrent(provider: TracerProvider) -> None:
    active = getattr(provider, "_active_span_processor", None)
    if isinstance(active, ConcurrentMultiSpanProcessor):
        _logger.warning(
            "insightmon: the tracer provider runs its span processors in a thread pool "
            "(ConcurrentMultiSpanProcessor); InsightMon reads threads, turns and experiment "
            "scopes from the caller's context in on_start, so nothing will be stamped or held. "
            "Use a provider with the default (synchronous) span processor"
        )


def _span_export(
    sdk: SpanProcessor | None, given: SpanProcessor | None
) -> tuple[SpanProcessor | None, int | None]:
    """(export processor of the InsightMonSpanProcessor, its release chunk)."""
    if sdk is not None and given is not None:
        both = SynchronousMultiSpanProcessor()  # type: ignore[no-untyped-call]
        both.add_span_processor(sdk)
        both.add_span_processor(given)
        return both, RELEASE_CHUNK
    if sdk is not None:
        return sdk, None  # its queue holds the whole held buffer
    return given, RELEASE_CHUNK


def init(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    service_name: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    components: Iterable[ComponentLike] | None = None,
    language: str | None = None,
    capture_content: bool = False,
    environment: str | None = None,
    content_attributes: Iterable[str] | None = None,
    tracer_provider: TracerProvider | None = None,
    logger_provider: LoggerProvider | None = None,
    set_global: bool = True,
    export_processor: SpanProcessor | None = None,
) -> InsightMonHandle:
    """Set up tracing and evaluation logs for the platform.

    Args:
        base_url: platform root URL, e.g. `https://eval.example.com` (env
            `INSIGHTMON_BASE_URL`).
        api_key: an `imk_...` key with scope `trace_ingest` (env `INSIGHTMON_API_KEY`).
        service_name: resource `service.name` of a provider the SDK creates (env
            `OTEL_SERVICE_NAME`; default: `agent_name`).
        agent_name / agent_version: `gen_ai.agent.name` / `gen_ai.agent.version` on every span.
        components: `Component`s or mappings `{type, name, version?, content_hash?}`.
        language: default declared content language (zh / en / ja / mixed / und).
        capture_content: send message content (off by default; see docs/contract.md §10).
            Content log events of instrumentations reach the platform only when they are
            emitted through `logger_provider` (the SDK's own logger provider is private).
        content_attributes: more content attributes to strip while `capture_content` is
            False, for instrumentations the built-in list does not know: attribute names, or
            prefixes ending in `*` (e.g. `["acme.prompt", "acme.documents.*"]`).
        environment: `deployment.environment.name` on the resource of a provider the SDK
            creates (the platform's environment, at most 100 characters); with a provider of
            your own it is stamped on local root and turn spans instead.
        tracer_provider / logger_provider: providers to attach to instead of the defaults.
        set_global: set a tracer provider the SDK creates as the global provider.
        export_processor: a span processor of your own (e.g. a `BatchSpanProcessor` to an
            OpenTelemetry Collector) that the SDK feeds instead of you adding it to the
            provider: spans of `evaluate()` items then wait until their items are reported
            on that path too. Do not also add it to the provider.

    Raises:
        InsightMonError: `config_error` (base URL without key or the reverse, key not `imk_`).
        ValueError: invalid language, component or field length.
        TypeError: `tracer_provider` is not an SDK `TracerProvider`, or `export_processor` is
            not a `SpanProcessor`.
    """
    if export_processor is not None and not isinstance(export_processor, SpanProcessor):
        raise TypeError("export_processor must be an opentelemetry.sdk.trace.SpanProcessor")
    with runtime_lock():
        state = get_runtime()
        if state is not None:
            warnings.warn(
                "insightmon.init() was already called; keeping the active configuration "
                "(call insightmon.shutdown() first to change it)",
                RuntimeWarning,
                stacklevel=2,
            )
            handle: InsightMonHandle = state.handle
            return handle
        settings = resolve_settings(
            base_url=base_url,
            api_key=api_key,
            service_name=service_name,
            agent_name=agent_name,
            agent_version=agent_version,
            components=components,
            language=language,
            capture_content=capture_content,
            environment=environment,
            content_attributes=content_attributes,
        )
        provider, owns_provider = _tracer_provider(settings, tracer_provider, set_global)
        _warn_concurrent(provider)

        control = _export.ExportControl() if settings.export_enabled else None
        sdk_spans = (
            BatchSpanProcessor(
                _export.build_span_exporter(settings, control), max_queue_size=SPAN_QUEUE_SIZE
            )
            if control is not None
            else None
        )
        export, release_chunk = _span_export(sdk_spans, export_processor)
        span_processor = InsightMonSpanProcessor(export, release_chunk=release_chunk)
        provider.add_span_processor(span_processor)

        log_processors: list[Any] = []
        logs = logger_provider
        owns_logs = False
        if control is not None:
            if logs is None:
                logs = LoggerProvider(resource=provider.resource, shutdown_on_exit=False)
                owns_logs = True
            allowed = {semconv.EVALUATION_RESULT_EVENT}
            if settings.capture_content:
                allowed |= semconv.CONTENT_EVENTS
            log_processor = EventFilterProcessor(
                BatchLogRecordProcessor(_export.build_log_exporter(settings, control)), allowed
            )
            logs.add_log_record_processor(log_processor)
            log_processors.append(log_processor)

        handle = InsightMonHandle(settings, provider, logs)
        set_runtime(
            RuntimeState(
                settings=settings,
                tracer_provider=provider,
                logger_provider=logs,
                owns_tracer_provider=owns_provider,
                owns_logger_provider=owns_logs,
                span_processors=[span_processor],
                log_processors=log_processors,
                handle=handle,
                export_control=control,
            )
        )
        _register_atexit()
        return handle


def _register_atexit() -> None:
    global _atexit_registered
    with _atexit_lock:
        if not _atexit_registered:
            atexit.register(_shutdown_at_exit)
            _atexit_registered = True


def _shutdown_at_exit() -> None:
    try:
        shutdown(ATEXIT_TIMEOUT_S)
    except Exception:  # never fail interpreter exit
        _logger.debug("insightmon: shutdown at exit failed", exc_info=True)


# --------------------------------------------------------------------------- deadlines


def _millis_left(deadline: float) -> int:
    return max(0, int((deadline - time.monotonic()) * 1000))


def _spawn(target: Callable[[], None], name: str) -> None:
    """Run `target` in a daemon thread (inline when no thread can start, e.g. at
    interpreter shutdown)."""
    try:
        threading.Thread(target=target, name=name, daemon=True).start()
    except RuntimeError:
        target()


class _FlushRun:
    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.done = threading.Event()
        self.result = False


class _FlushGate:
    """`force_flush()` of one processor in a helper thread, one at a time.

    A caller waits for a flush that started after its call (joining one another caller
    started, or starting one), at most until its deadline; a flush still running when the
    deadline passes keeps running in the background and later callers join it.
    """

    def __init__(self, processor: Any) -> None:
        self._processor = processor
        self._lock = threading.Lock()
        self._current: _FlushRun | None = None
        self._started = 0

    def flush(self, deadline: float) -> bool:
        with self._lock:
            ticket = self._started  # runs numbered above this one started after this call
        while True:
            start = False
            with self._lock:
                run = self._current
                if run is None or (run.done.is_set() and run.seq <= ticket):
                    self._started += 1
                    run = _FlushRun(self._started)
                    self._current = run
                    start = True
            if start:
                _spawn(self._runner(run, deadline), "insightmon-flush")
            if not run.done.wait(max(0.0, deadline - time.monotonic())):
                return False
            if run.seq > ticket:
                return run.result

    def _runner(self, run: _FlushRun, deadline: float) -> Callable[[], None]:
        def target() -> None:
            try:
                run.result = self._processor.force_flush(_millis_left(deadline)) is not False
            except Exception:
                _logger.warning("insightmon: flush failed", exc_info=True)
                run.result = False
            finally:
                run.done.set()

        return target


_gates_lock = threading.Lock()
_gates: weakref.WeakKeyDictionary[Any, _FlushGate] = weakref.WeakKeyDictionary()


def _gate(processor: Any) -> _FlushGate:
    with _gates_lock:
        try:
            gate = _gates.get(processor)
            if gate is None:
                gate = _gates[processor] = _FlushGate(processor)
        except TypeError:  # not weakly referenceable: no single-flight for it
            gate = _FlushGate(processor)
        return gate


def _flush_state(state: RuntimeState, deadline: float) -> bool:
    control: _export.ExportControl | None = state.export_control
    failures = control.failures if control is not None else 0
    ok = True
    # Spans first: the platform correlates evaluation results with stored spans.
    for processor in [*state.span_processors, *state.log_processors]:
        if time.monotonic() >= deadline or not _gate(processor).flush(deadline):
            ok = False
    if control is not None and control.failures != failures:
        ok = False  # an export failed meanwhile (OpenTelemetry's processors report success)
    return ok


def flush(timeout_s: float = 10.0) -> bool:
    """Export pending spans, then pending evaluation logs (spans first, so that the platform
    can correlate the logs). Flushes only the SDK's own processors and returns within
    `timeout_s`. Returns False on timeout or when an export failed meanwhile; True when there
    is nothing to flush (also before `init()`)."""
    state = get_runtime()
    if state is None:
        return True
    return _flush_state(state, time.monotonic() + max(0.0, timeout_s))


Pending = list[tuple[str, threading.Event]]


def _start_all(calls: list[tuple[str, Callable[[], None]]]) -> Pending:
    """Start each call in a helper thread; (name, done event) per call."""
    pending: Pending = []
    for name, call in calls:
        event = threading.Event()

        def target(
            call: Callable[[], None] = call, name: str = name, event: threading.Event = event
        ) -> None:
            try:
                call()
            except Exception:
                _logger.warning("insightmon: shutdown of %s failed", name, exc_info=True)
            finally:
                event.set()

        _spawn(target, "insightmon-shutdown")
        pending.append((name, event))
    return pending


def _wait_all(pending: Pending, deadline: float) -> Pending:
    """The calls of `pending` still running at `deadline`."""
    return [
        (name, event)
        for name, event in pending
        if not event.wait(max(0.0, deadline - time.monotonic()))
    ]


def shutdown(timeout_s: float = 10.0) -> None:
    """Release held spans, flush, and shut down the SDK's processors (and the providers the SDK
    created), within `timeout_s` (plus `SHUTDOWN_GRACE_S`): what is not exported by then is
    dropped and counted in a warning. Idempotent; `init()` may be called again afterwards. The
    global tracer provider stays in place (OpenTelemetry allows setting it once): a later
    `init()` attaches to it."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    with runtime_lock():  # only swaps the state: no network I/O under the lock
        state = get_runtime()
        if state is None:
            return
        reset_runtime()
    control: _export.ExportControl | None = state.export_control
    if control is not None:
        control.set_deadline(deadline)
    try:
        release_traces(None)
    except Exception:
        _logger.warning("insightmon: could not release held spans", exc_info=True)
    _flush_state(state, deadline)
    calls: list[tuple[str, Callable[[], None]]] = [
        (type(p).__name__, p.shutdown) for p in [*state.span_processors, *state.log_processors]
    ]
    provider = state.tracer_provider
    # A provider the SDK created and made global stays in place for a later init().
    if (
        state.owns_tracer_provider
        and provider is not None
        and trace.get_tracer_provider() is not provider
    ):
        calls.append(("tracer provider", provider.shutdown))
    if state.owns_logger_provider and state.logger_provider is not None:
        calls.append(("logger provider", state.logger_provider.shutdown))
    late = _wait_all(_start_all(calls), deadline)
    if control is not None:
        control.abort()  # backoff waits end and nothing more is sent
        late = _wait_all(late, time.monotonic() + SHUTDOWN_GRACE_S)
    if late:
        _logger.debug(
            "insightmon: still shutting down after the deadline: %s", [name for name, _ in late]
        )
    if control is None:
        return
    spans, records = control.dropped("spans"), control.dropped("log records")
    if spans or records:
        _logger.warning(
            "insightmon: %d spans and %d log records could not be exported since init() "
            "(platform unreachable, refusing or too slow; see the warnings above)",
            spans,
            records,
        )


def _reinit_after_fork() -> None:
    global _atexit_lock, _gates_lock, _gates
    _atexit_lock = threading.Lock()
    _gates_lock = threading.Lock()
    _gates = weakref.WeakKeyDictionary()  # flushes running in the parent do not exist here


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)
