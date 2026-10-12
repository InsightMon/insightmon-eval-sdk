"""W3C trace context for platform replays, and the response receipt.

When the platform replays an example against your service it sends
`traceparent: 00-<otel_trace_id>-<random parent id>-01` and pre-creates the trace
(docs/contract.md §11). Spans your service creates under that remote parent share the trace id
and merge into the platform's experiment trace; the platform then reads agent and component
versions from it (`trace_attr` receipt) after `receipt_wait_s` seconds (default 120).

- `extract_context(headers)`: the remote parent context, always with the W3C propagator (the
  global propagator may be configured differently).
- `InsightMonASGIMiddleware` / `InsightMonWSGIMiddleware`: attach that context for the duration
  of a request. They create no server span (use your web framework's instrumentation for
  that). When an outer instrumentation already made a span of the incoming trace current, the
  middlewares leave the context alone.
- Flushing before the response completes (for targets with `receipt_wait_s = 0`):
  `flush_on_traceparent=True` flushes for every request with a valid `traceparent`, which in a
  service whose callers propagate W3C context is every request: use it in a deployment that
  only serves replays, or pass `flush_when=` (a predicate on the ASGI scope / WSGI environ,
  e.g. a header the replay target sends through its `headers_ref`). Each flush waits at most
  `flush_timeout_s` (default 5 s). The platform sends no header that marks a replay itself.
- `receipt()`: `{agent: {name, version}, components: [...]}` to return as the `receipt` field
  of a replay response (the platform's `response_field` receipt wins over `trace_attr`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, MutableMapping
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from insightmon._config import Settings, get_settings, global_components, merge_components
from insightmon._context import current_experiment, current_thread

__all__ = [
    "InsightMonASGIMiddleware",
    "InsightMonWSGIMiddleware",
    "extract_context",
    "receipt",
]

_logger = logging.getLogger(__name__)
_PROPAGATOR = TraceContextTextMapPropagator()
_TRACEPARENT = "traceparent"
_TRACESTATE = "tracestate"

Headers = Mapping[str, str] | Iterable[tuple[bytes | str, bytes | str]]

# Minimal ASGI / WSGI types (no framework dependency).
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


def _text(value: bytes | str) -> str:
    return value.decode("latin-1") if isinstance(value, bytes) else str(value)


def _carrier(headers: Headers) -> dict[str, str]:
    """`traceparent` (the first one) and `tracestate` (all, comma joined), lower-case names."""
    pairs: Iterable[tuple[Any, Any]] = headers.items() if isinstance(headers, Mapping) else headers
    traceparent: str | None = None
    states: list[str] = []
    for raw_name, raw_value in pairs:
        if raw_value is None:
            continue
        name = _text(raw_name).strip().lower()
        if name == _TRACEPARENT and traceparent is None:
            traceparent = _text(raw_value).strip()
        elif name == _TRACESTATE:
            states.append(_text(raw_value).strip())
    carrier: dict[str, str] = {}
    if traceparent:
        carrier[_TRACEPARENT] = traceparent
        if states:
            carrier[_TRACESTATE] = ",".join(s for s in states if s)
    return carrier


def _extract(headers: Headers, base: Context) -> tuple[Context, trace.SpanContext | None]:
    carrier = _carrier(headers)
    if not carrier:
        return base, None
    context = _PROPAGATOR.extract(carrier, context=base)
    span_context = trace.get_current_span(context).get_span_context()
    if not span_context.is_valid or not span_context.is_remote:
        return base, None
    return context, span_context


def extract_context(headers: Headers) -> Context:
    """The context carried by `traceparent` / `tracestate` in `headers` (a mapping with any
    header-name case, or ASGI-style `(name, value)` pairs of bytes or str). Returns an empty
    context when there is no valid `traceparent`.

    Use it to start the spans of a request under the platform's trace::

        with tracer.start_as_current_span("handle", context=extract_context(headers)):
            ...
    """
    context, _ = _extract(headers, Context())
    return context


def _request_context(headers: Headers) -> tuple[Context | None, bool]:
    """(context to attach or None, the request carried a valid traceparent)."""
    context, remote = _extract(headers, otel_context.get_current())
    if remote is None:
        return None, False
    current = trace.get_current_span().get_span_context()
    if current.is_valid and current.trace_id == remote.trace_id:
        return None, True  # an outer instrumentation already continues this trace
    return context, True


def receipt() -> dict[str, Any]:
    """The response receipt of the current context: `init()` agent name / version and the
    components in effect (global defaults merged with the overrides of the current thread, or
    of the current `evaluate()` run), including `content_hash`. Before `init()`: agent name /
    version None, and only the components declared with `set_component()`."""
    settings = get_settings() or Settings()
    experiment = current_experiment()
    scope = current_thread()
    if experiment is not None:
        overrides = experiment.component_overrides()
    else:
        overrides = scope.component_overrides() if scope is not None else ()
    return settings.receipt(merge_components(global_components(), overrides))


#: Seconds a middleware flush may take before the response goes on without it.
FLUSH_TIMEOUT_S = 5.0

FlushWhen = Callable[[Mapping[str, Any]], bool]


def _flush(timeout_s: float = FLUSH_TIMEOUT_S) -> None:
    from insightmon._setup import flush

    if not flush(timeout_s):
        _logger.warning("insightmon: flushing spans before the response did not complete")


async def _flush_async(timeout_s: float) -> None:
    """`_flush` in a thread of its own (never the loop's default executor, which the
    application's work shares); inline, bounded by `timeout_s`, under other event loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # e.g. trio
        _flush(timeout_s)
        return
    done: asyncio.Future[None] = loop.create_future()

    def resolve() -> None:
        if not done.done():
            done.set_result(None)

    def run() -> None:
        try:
            _flush(timeout_s)
        except Exception:
            _logger.warning("insightmon: flushing spans before the response failed", exc_info=True)
        finally:
            with contextlib.suppress(RuntimeError):  # a closed loop: nobody waits any more
                loop.call_soon_threadsafe(resolve)

    threading.Thread(target=run, name="insightmon-flush", daemon=True).start()
    await done


class _FlushPolicy:
    """When a middleware flushes: the request carried a valid `traceparent` and `flush_when`
    accepts it (without `flush_when`: `flush_on_traceparent`)."""

    def __init__(
        self, flush_on_traceparent: bool, flush_when: FlushWhen | None, flush_timeout_s: float
    ) -> None:
        if flush_when is not None and not callable(flush_when):
            raise TypeError("flush_when must be a callable or None")
        if not flush_timeout_s > 0:
            raise ValueError("flush_timeout_s must be > 0")
        self.on_traceparent = bool(flush_on_traceparent)
        self.when = flush_when
        self.timeout_s = float(flush_timeout_s)
        self._failed = False

    def applies(self, traced: bool, request: Mapping[str, Any]) -> bool:
        if not traced:
            return False
        if self.when is None:
            return self.on_traceparent
        try:
            return bool(self.when(request))
        except Exception:
            if not self._failed:
                self._failed = True
                _logger.warning("insightmon: flush_when raised; not flushing", exc_info=True)
            return False


class InsightMonASGIMiddleware:
    """ASGI middleware: attach the request's trace context (HTTP and WebSocket scopes).

    Args:
        app: the ASGI application.
        flush_on_traceparent: when the request carries a `traceparent`, flush spans (and
            evaluation logs) before the final `http.response.body` message is sent.
        flush_when: `flush_when(scope) -> bool` decides instead of `flush_on_traceparent`
            which requests with a `traceparent` flush (e.g. those with a header of the replay
            target).
        flush_timeout_s: the longest a flush delays the final body message.

    The flush runs in a thread of its own under asyncio (inline, bounded by
    `flush_timeout_s`, under other event loops).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        flush_on_traceparent: bool = False,
        flush_when: FlushWhen | None = None,
        flush_timeout_s: float = FLUSH_TIMEOUT_S,
    ) -> None:
        self.app = app
        self.flush_on_traceparent = flush_on_traceparent
        self._policy = _FlushPolicy(flush_on_traceparent, flush_when, flush_timeout_s)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        context, traced = _request_context(scope.get("headers") or ())
        if scope.get("type") == "http" and self._policy.applies(traced, scope):
            send = self._flushing_send(send, self._policy.timeout_s)
        if context is None:
            await self.app(scope, receive, send)
            return
        token = otel_context.attach(context)
        try:
            await self.app(scope, receive, send)
        finally:
            otel_context.detach(token)

    @staticmethod
    def _flushing_send(send: Send, timeout_s: float) -> Send:
        flushed = False

        async def flushing_send(message: Message) -> None:
            nonlocal flushed
            final_body = message.get("type") == "http.response.body" and not message.get(
                "more_body", False
            )
            if final_body and not flushed:
                flushed = True
                await _flush_async(timeout_s)
            await send(message)

        return flushing_send


_NOTHING: Any = object()


class _ContextIterable:
    """A WSGI response iterable around the application's one.

    - `ctx` (a `contextvars.Context` with the request's trace context attached once): every
      step of the wrapped iterable, and its `close()`, runs inside it, never detached between
      steps, so a span a generator keeps open across a `yield` stays the parent of the spans
      it starts after it.
    - `flush`: the iterable reads one chunk ahead and flushes before it hands out the last
      chunk (or reports an empty body), while the server has not yet written the end of the
      response; `close()` flushes when the iteration did not get that far.
    """

    def __init__(
        self,
        result: Iterable[bytes],
        ctx: contextvars.Context | None,
        flush: Callable[[], None] | None,
    ) -> None:
        self._result = result
        self._ctx = ctx
        self._flush = flush
        self._iterator: Iterator[bytes] | None = None
        self._next: Any = _NOTHING

    def __iter__(self) -> Iterator[bytes]:
        return self

    def _run(self, step: Callable[..., Any], *args: Any) -> Any:
        if self._ctx is None:
            return step(*args)
        return self._ctx.run(step, *args)

    def _read(self) -> Any:
        """The next chunk of the wrapped iterator, or `_NOTHING` at its end."""
        assert self._iterator is not None
        try:
            return self._run(next, self._iterator)
        except StopIteration:
            return _NOTHING

    def __next__(self) -> bytes:
        if self._iterator is None:
            self._iterator = self._run(iter, self._result)
            if self._flush is not None:
                self._next = self._read()
        if self._flush is None:  # no read-ahead: chunks go out as they come
            chunk = self._read()
            if chunk is _NOTHING:
                raise StopIteration
            return chunk  # type: ignore[no-any-return]
        current = self._next
        if current is _NOTHING:
            self._flush_once()  # an empty body
            raise StopIteration
        self._next = self._read()
        if self._next is _NOTHING:
            self._flush_once()  # `current` is the last chunk: flush before the server sends it
        return current  # type: ignore[no-any-return]

    def _flush_once(self) -> None:
        flush, self._flush = self._flush, None
        if flush is not None:
            flush()

    def close(self) -> None:
        try:
            close = getattr(self._result, "close", None)
            if close is not None:
                self._run(close)
        finally:
            self._flush_once()


class InsightMonWSGIMiddleware:
    """WSGI middleware: attach the request's trace context (`HTTP_TRACEPARENT`,
    `HTTP_TRACESTATE` from the environ) while the application runs and while its response
    iterable is consumed (one context per response, kept across the steps of a generator).

    Args:
        app: the WSGI application.
        flush_on_traceparent: when the request carries a `traceparent`, flush spans before the
            server sends the last chunk: right after the application returns a list or tuple,
            else by reading one chunk ahead (a streamed response is delayed by one chunk).
        flush_when: `flush_when(environ) -> bool` decides instead of `flush_on_traceparent`
            which requests with a `traceparent` flush.
        flush_timeout_s: the longest a flush delays the response.
    """

    def __init__(
        self,
        app: WSGIApp,
        *,
        flush_on_traceparent: bool = False,
        flush_when: FlushWhen | None = None,
        flush_timeout_s: float = FLUSH_TIMEOUT_S,
    ) -> None:
        self.app = app
        self.flush_on_traceparent = flush_on_traceparent
        self._policy = _FlushPolicy(flush_on_traceparent, flush_when, flush_timeout_s)

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        headers = {
            _TRACEPARENT: environ.get("HTTP_TRACEPARENT"),
            _TRACESTATE: environ.get("HTTP_TRACESTATE"),
        }
        context, traced = _request_context({k: v for k, v in headers.items() if isinstance(v, str)})
        if not traced:
            return self.app(environ, start_response)
        timeout_s = self._policy.timeout_s
        flush = (lambda: _flush(timeout_s)) if self._policy.applies(traced, environ) else None
        ctx: contextvars.Context | None = None
        if context is None:
            result = self.app(environ, start_response)
        else:
            ctx = contextvars.copy_context()
            ctx.run(otel_context.attach, context)  # never detached: ctx ends with the response
            result = ctx.run(self.app, environ, start_response)
        if isinstance(result, list | tuple):  # the whole body exists: no code runs after this
            if flush is not None:
                flush()
            return result
        if ctx is None and flush is None:
            return result
        return _ContextIterable(result, ctx, flush)
