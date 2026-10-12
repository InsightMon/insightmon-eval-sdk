"""Shared fixtures.

- Isolation (autouse): `INSIGHTMON_BASE_URL`, `INSIGHTMON_API_KEY` and `OTEL_SERVICE_NAME` are
  removed from the environment, and the SDK's runtime state, global component overrides, held
  traces and the partial-success counters are reset around every test. Integration tests
  (tests/integration) read their own `INSIGHTMON_IT_*` variables.
- `span_exporter` + `tracer_provider`: a fresh SDK `TracerProvider` per test exporting to an
  in-memory exporter through a `SimpleSpanProcessor` (spans are visible as soon as they end).
  Pass it to `insightmon.init(tracer_provider=...)`: the global OpenTelemetry provider can only
  be set once per process, so tests never rely on it.
- `log_exporter` + `logger_provider`: the same for OTLP logs.
- `mock_api`: an `httpx.MockTransport` that routes by (method, path), records every request
  and replays queued responses (`MockApi.add`).
- `collector`: a local OTLP/HTTP endpoint (tests/collector.py) for the real exporters, with the
  proxy variables removed; `insightmon.shutdown()` runs before it closes. The generic
  `OTEL_EXPORTER_OTLP_*HEADERS` variables are left as they are: the SDK's exporters must
  ignore them.

Add fixtures that only one area needs to that area's test modules instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import opentelemetry.sdk._logs.export as _log_export
import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import insightmon
from insightmon import _config, _context, _export

from .collector import PROXY_VARS, Collector

#: The in-memory log exporter was renamed in later OpenTelemetry releases.
InMemoryLogExporter: Any = (
    getattr(_log_export, "InMemoryLogRecordExporter", None) or _log_export.InMemoryLogExporter
)

ENV_VARS: tuple[str, ...] = ("INSIGHTMON_BASE_URL", "INSIGHTMON_API_KEY", "OTEL_SERVICE_NAME")

#: Fake values for unit tests; never a real key.
BASE_URL = "https://eval.example.test"
API_KEY = "imk_test_unit_key"


@pytest.fixture(autouse=True)
def _isolate_sdk_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    _config.reset_runtime()
    _context.reset_context_state()
    _export.reset_rejections()
    yield
    _config.reset_runtime()
    _context.reset_context_state()
    _export.reset_rejections()


# --------------------------------------------------------------------------- tracing


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(span_exporter: InMemorySpanExporter) -> Iterator[TracerProvider]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    yield provider
    provider.shutdown()


@pytest.fixture
def log_exporter() -> Any:
    return InMemoryLogExporter()


@pytest.fixture
def logger_provider(log_exporter: Any) -> Iterator[LoggerProvider]:
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    yield provider
    provider.shutdown()


# --------------------------------------------------------------------------- REST

Responder = httpx.Response | Callable[[httpx.Request], httpx.Response] | BaseException


def error_response(
    status: int,
    code: str,
    params: dict[str, Any] | None = None,
    message: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """A platform error envelope `{code, params, message}`."""
    return httpx.Response(
        status,
        json={"code": code, "params": params or {}, "message": message},
        headers=headers,
    )


@dataclass
class MockApi:
    """Route table for `httpx.MockTransport`.

    `add(method, path, *responses)` queues responses for a route; each request consumes the
    next one and the last one repeats. A response may be an `httpx.Response`, a JSON-able
    value (200 JSON), a callable `request -> Response`, or an exception to raise (e.g.
    `httpx.ConnectError("boom")`). Unrouted requests get a 404 `not_found` envelope.
    """

    routes: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, method: str, path: str, *responses: Any) -> MockApi:
        if not responses:
            raise ValueError("add() needs at least one response")
        self.routes.setdefault((method.upper(), path), []).extend(responses)
        return self

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return error_response(404, "not_found", {"resource": "route"})
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(responder, BaseException):
            raise responder
        if isinstance(responder, httpx.Response):
            return responder
        if callable(responder):
            result: httpx.Response = responder(request)
            return result
        return httpx.Response(200, json=responder)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method.upper() and r.url.path == path]

    def json_bodies(self, method: str, path: str) -> list[Any]:
        return [json.loads(r.content) if r.content else None for r in self.calls(method, path)]


@pytest.fixture
def mock_api() -> MockApi:
    return MockApi()


# --------------------------------------------------------------------------- OTLP endpoint


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    endpoint = Collector()
    yield endpoint
    insightmon.shutdown()
    endpoint.close()
