"""A local OTLP/HTTP endpoint for tests that exercise the real exporters (see the `collector`
fixture in conftest.py), plus helpers that decode what it received."""

from __future__ import annotations

import gzip
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

TRACES_PATH = "/otlp/v1/traces"
LOGS_PATH = "/otlp/v1/logs"

#: Proxy variables removed (and NO_PROXY set) so that requests to 127.0.0.1 stay local.
PROXY_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def local_env(env: Mapping[str, str]) -> dict[str, str]:
    """`env` without proxy variables and with `NO_PROXY` covering the loopback address."""
    out = {k: v for k, v in env.items() if k not in PROXY_VARS}
    out["NO_PROXY"] = out["no_proxy"] = "127.0.0.1,localhost"
    return out


class Collector:
    """A local OTLP/HTTP endpoint that records every request.

    `answers`: path -> (content type, body) of the answer (default: an empty protobuf answer).
    `statuses`: path -> HTTP status of the answer (default 200). `redirects`: path -> Location
    header (sent with the status, e.g. 301). `hang`: while set to an unset event, every request
    waits for it (at most 30 s) before it is answered: a platform that accepts connections but
    never answers.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answers: dict[str, tuple[str, bytes]] = {}
        self.statuses: dict[str, int] = {}
        self.redirects: dict[str, str] = {}
        self.hang: threading.Event | None = None
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                collector.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                hang = collector.hang
                if hang is not None:
                    hang.wait(30)
                content_type, answer = collector.answers.get(
                    self.path, ("application/x-protobuf", b"")
                )
                self.send_response(collector.statuses.get(self.path, 200))
                location = collector.redirects.get(self.path)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}"

    def close(self) -> None:
        if self.hang is not None:
            self.hang.set()
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]

    def body(self, index: int) -> bytes:
        request = self.requests[index]
        raw: bytes = request["body"]
        if request["headers"].get("content-encoding") == "gzip":
            return gzip.decompress(raw)
        return raw

    def spans(self) -> list[ExportedSpan]:
        """Every span received on the traces path, in arrival order."""
        out: list[ExportedSpan] = []
        for index, request in enumerate(self.requests):
            if request["path"] != TRACES_PATH:
                continue
            message = ExportTraceServiceRequest()
            message.ParseFromString(self.body(index))
            for resource_spans in message.resource_spans:
                resource = attributes_of(resource_spans.resource.attributes)
                for scope in resource_spans.scope_spans:
                    for span in scope.spans:
                        out.append(
                            ExportedSpan(
                                name=span.name,
                                trace_id=span.trace_id.hex(),
                                span_id=span.span_id.hex(),
                                parent_span_id=span.parent_span_id.hex(),
                                attributes=attributes_of(span.attributes),
                                resource=resource,
                            )
                        )
        return out

    def log_records(self) -> list[dict[str, Any]]:
        """Every log record received on the logs path: its attributes plus `event_name`,
        `trace_id` and `span_id` (hex)."""
        out: list[dict[str, Any]] = []
        for index, request in enumerate(self.requests):
            if request["path"] != LOGS_PATH:
                continue
            message = ExportLogsServiceRequest()
            message.ParseFromString(self.body(index))
            for resource_logs in message.resource_logs:
                for scope in resource_logs.scope_logs:
                    for record in scope.log_records:
                        out.append(
                            {
                                **attributes_of(record.attributes),
                                "event_name": record.event_name,
                                "trace_id": record.trace_id.hex(),
                                "span_id": record.span_id.hex(),
                            }
                        )
        return out


@dataclass(frozen=True)
class ExportedSpan:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str
    attributes: dict[str, Any]
    resource: dict[str, Any]


def any_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    return getattr(value, kind) if kind else None


def attributes_of(pairs: Any) -> dict[str, Any]:
    items: list[KeyValue] = list(pairs)
    return {kv.key: any_value(kv.value) for kv in items}
