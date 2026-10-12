"""A small weather agent served over HTTP (ASGI), instrumented with the InsightMon SDK.

It can be registered on the platform as an HTTP Target: the platform replays dataset examples
against `POST /v1/respond` with a `traceparent` header, and reads the agent / component versions
of each run from the spans this service reports (the `trace_attr` receipt), or from the
`receipt` field of the response when started with `--receipt-field`.

Run it (needs `pip install uvicorn`):

    export INSIGHTMON_BASE_URL=https://eval.example.com
    export INSIGHTMON_API_KEY=imk_...            # scope trace_ingest
    python examples/http_agent.py --port 8000

Request body (the platform's default request template): `{"input": ..., "case": ...,
"agent_version": ..., "trial_index": ...}`; an optional `session_id` groups production requests
into one platform thread. Answer: `{"output": {"text": ...}}` (+ `"receipt"`).

`--flush-on-traceparent` exports the spans of a replayed request before its response is sent,
for Targets configured with `receipt_wait_s = 0`. Do not use it against a platform that runs on
SQLite: the platform holds its database write lock while it waits for the response, so the
export blocks until the call times out (see README, "Replay receipts").
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import insightmon

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

AGENT_NAME = "weather-agent"
COMPONENTS = [
    {"type": "prompt", "name": "system", "version": "p5"},
    {"type": "tool", "name": "forecast", "version": "1.2.0"},
]

logger = logging.getLogger("http_agent")


def question_of(value: Any) -> str:
    """The text to answer: a string, `{"text": ...}`, `{"question": ...}`, or the last user
    message of `{"messages": [...]}`."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "question"):
            if isinstance(value.get(key), str):
                return str(value[key])
        for message in reversed(value.get("messages") or []):
            if isinstance(message, dict) and message.get("role", "user") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else json.dumps(content)
    return json.dumps(value, ensure_ascii=False)


def forecast(question: str) -> str:
    """The "tool": a canned forecast (a real agent would call a weather service here)."""
    city = "Oslo" if "oslo" in question.lower() else "your city"
    return f"Light rain is expected in {city} tomorrow, around 14 C."


def respond(request: dict[str, Any]) -> tuple[str, str]:
    """Answer one request inside a turn span; returns (answer, trace id)."""
    question = question_of(request.get("input"))
    with insightmon.turn("respond") as span:
        insightmon.record_io(input_messages=question)  # no-op unless capture_content=True
        tracer = insightmon.get_tracer()
        with tracer.start_as_current_span(
            "forecast",
            attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "forecast"},
        ):
            answer = forecast(question)
        insightmon.record_io(output_messages=answer)
        trace_id = format(span.get_span_context().trace_id, "032x")
    return answer, trace_id


def handle(request: dict[str, Any], receipt_field: bool) -> dict[str, Any]:
    session_id = request.get("session_id")
    if isinstance(session_id, str) and session_id:
        # production traffic: one platform thread per chat session
        with insightmon.thread(session_id):
            answer, trace_id = respond(request)
    else:
        # a platform replay (the traceparent made the turn part of the platform's trace)
        answer, trace_id = respond(request)
    logger.info("respond trace_id=%s case=%s", trace_id, request.get("case"))
    payload: dict[str, Any] = {"output": {"text": answer}}
    if receipt_field:
        payload["receipt"] = insightmon.receipt()
    return payload


async def read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def send_json(send: Send, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def make_app(*, receipt_field: bool = False) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    insightmon.shutdown()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"] == "/healthz":
            await send_json(send, 200, {"ok": True})
            return
        if scope["path"] != "/v1/respond" or scope["method"] != "POST":
            await send_json(send, 404, {"error": "not found"})
            return
        try:
            request = json.loads(await read_body(receive) or b"{}")
        except ValueError:
            await send_json(send, 400, {"error": "body is not JSON"})
            return
        if not isinstance(request, dict):
            await send_json(send, 400, {"error": "body is not a JSON object"})
            return
        await send_json(send, 200, handle(request, receipt_field))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--agent-version", default="2.1.0")
    parser.add_argument("--receipt-field", action="store_true", help="answer with a receipt")
    parser.add_argument("--flush-on-traceparent", action="store_true")
    parser.add_argument("--capture-content", action="store_true")
    args = parser.parse_args()

    import uvicorn  # the example's only extra dependency

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    insightmon.init(
        agent_name=AGENT_NAME,
        agent_version=args.agent_version,
        components=COMPONENTS,
        language="en",
        capture_content=args.capture_content,
    )
    app = insightmon.InsightMonASGIMiddleware(
        make_app(receipt_field=args.receipt_field),
        flush_on_traceparent=args.flush_on_traceparent,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
