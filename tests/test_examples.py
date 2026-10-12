"""The examples run.

- examples/quickstart.py runs as a script against a local OTLP endpoint (tests/collector.py);
- examples/http_agent.py: its ASGI app behind the SDK middleware joins the `traceparent` of a
  replay request and answers with a receipt when asked (in-process, `httpx.ASGITransport`);
- examples/evaluate_example.py: `main()` against the fake platform of tests/fake_platform.py.

Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import insightmon
from insightmon import _http, semconv
from insightmon.client import Client
from tests.collector import Collector, local_env
from tests.conftest import API_KEY, BASE_URL, MockApi
from tests.fake_platform import FakePlatform

evaluate_mod = importlib.import_module("insightmon.evaluate")

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"


def load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def in_memory_init(
    monkeypatch: pytest.MonkeyPatch,
    tracer_provider: TracerProvider,
    logger_provider: LoggerProvider,
) -> None:
    """`insightmon.init()` without export, on the in-memory providers of conftest.py."""
    real_init = insightmon.init

    def init(**kwargs: Any) -> insightmon.InsightMonHandle:
        kwargs.setdefault("tracer_provider", tracer_provider)
        kwargs.setdefault("logger_provider", logger_provider)
        return real_init(**kwargs)

    monkeypatch.setattr(insightmon, "init", init)


# --------------------------------------------------------------------------- quickstart


def test_quickstart_reports_one_closed_thread(collector: Collector) -> None:
    env = local_env(os.environ)
    env.update(INSIGHTMON_BASE_URL=collector.base_url, INSIGHTMON_API_KEY=API_KEY)
    env.pop("OTEL_SERVICE_NAME", None)
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / "quickstart.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "Light rain is expected in Oslo tomorrow."
    chat_id = lines[-1].removeprefix("reported thread ")
    assert chat_id.startswith("chat-")

    spans = collector.spans()
    assert sorted(s.name for s in spans) == ["forecast", "turn", "turn"]
    assert {s.attributes[semconv.GEN_AI_CONVERSATION_ID] for s in spans} == {chat_id}
    assert {s.attributes[semconv.AGENT_VERSION] for s in spans} == {"1.4.0"}
    assert {s.attributes[semconv.LANGUAGE] for s in spans} == {"en"}
    assert {s.attributes[semconv.ENDUSER_ID] for s in spans} == {"user-42"}
    turns = [s for s in spans if s.name == "turn"]
    assert len({s.trace_id for s in turns}) == 2  # one trace per turn
    assert [s.attributes.get(semconv.THREAD_END) for s in turns].count(True) == 1
    for turn in turns:
        assert turn.attributes[semconv.component_attribute("prompt", "system")] == "p7"
        assert turn.resource[semconv.SERVICE_NAME] == "weather-assistant"
    for request in collector.requests:
        assert request["headers"]["authorization"] == f"Bearer {API_KEY}"


# --------------------------------------------------------------------------- http agent


def _call(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(go())


def _replay_request(app: Any) -> httpx.Response:
    return _call(
        app,
        "POST",
        "/v1/respond",
        json={"input": {"text": "Will it rain in Oslo?"}, "case": "k1", "trial_index": 0},
        headers={"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01"},
    )


@pytest.mark.usefixtures("in_memory_init")
def test_http_agent_joins_the_replay_traceparent(span_exporter: InMemorySpanExporter) -> None:
    agent = load_example("http_agent")
    insightmon.init(agent_name=agent.AGENT_NAME, agent_version="2.1.0", components=agent.COMPONENTS)
    app = insightmon.InsightMonASGIMiddleware(agent.make_app())

    response = _replay_request(app)
    assert response.status_code == 200
    assert response.json() == {
        "output": {"text": "Light rain is expected in Oslo tomorrow, around 14 C."}
    }

    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert set(spans) == {"respond", "forecast"}
    respond = spans["respond"]
    assert format(respond.context.trace_id, "032x") == TRACE_ID
    assert respond.parent is not None
    assert format(respond.parent.span_id, "016x") == PARENT_ID
    attributes = respond.attributes or {}
    assert attributes[semconv.AGENT_VERSION] == "2.1.0"
    assert attributes[semconv.component_attribute("tool", "forecast")] == "1.2.0"
    assert semconv.GEN_AI_CONVERSATION_ID not in attributes  # a replay joins no thread


@pytest.mark.usefixtures("in_memory_init")
def test_http_agent_answers_with_a_receipt_and_groups_sessions(
    span_exporter: InMemorySpanExporter,
) -> None:
    agent = load_example("http_agent")
    insightmon.init(agent_name=agent.AGENT_NAME, agent_version="2.1.0", components=agent.COMPONENTS)
    app = insightmon.InsightMonASGIMiddleware(agent.make_app(receipt_field=True))

    answer = _replay_request(app).json()
    assert answer["receipt"]["agent"] == {"name": "weather-agent", "version": "2.1.0"}
    components = {(c["type"], c["name"]): c["version"] for c in answer["receipt"]["components"]}
    assert components == {("prompt", "system"): "p5", ("tool", "forecast"): "1.2.0"}

    production = _call(app, "POST", "/v1/respond", json={"input": "Rain?", "session_id": "s-1"})
    assert production.status_code == 200
    (turn,) = [s for s in span_exporter.get_finished_spans() if s.name == "respond"][1:]
    assert (turn.attributes or {})[semconv.GEN_AI_CONVERSATION_ID] == "s-1"


def test_http_agent_health_and_errors() -> None:
    app = load_example("http_agent").make_app()
    assert _call(app, "GET", "/healthz").json() == {"ok": True}
    assert _call(app, "GET", "/nowhere").status_code == 404
    assert _call(app, "POST", "/v1/respond", content=b"not json").status_code == 400
    assert _call(app, "POST", "/v1/respond", json=[1, 2]).status_code == 400


def test_http_agent_question_of() -> None:
    agent = load_example("http_agent")
    assert agent.question_of("Rain?") == "Rain?"
    assert agent.question_of({"text": "Rain?"}) == "Rain?"
    messages = [{"role": "user", "content": "Rain?"}, {"role": "assistant", "content": "No."}]
    assert agent.question_of({"messages": messages}) == "Rain?"


# --------------------------------------------------------------------------- evaluate


@pytest.mark.usefixtures("in_memory_init")
def test_evaluate_example_runs_against_the_fake_platform(
    monkeypatch: pytest.MonkeyPatch,
    mock_api: MockApi,
    span_exporter: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    platform = FakePlatform(["k1", "k2"], score_names=frozenset({"mentions_rain"})).install(
        mock_api
    )

    def open_client(client: Any, api_key: Any, base_url: Any) -> Client:
        return Client(BASE_URL, API_KEY, transport=mock_api.transport)

    async def no_async_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(evaluate_mod, "_open_client", open_client)
    monkeypatch.setattr(_http, "_sleep", lambda seconds: None)
    monkeypatch.setattr(evaluate_mod, "_sleep", lambda seconds: None)
    monkeypatch.setattr(evaluate_mod, "_async_sleep", no_async_sleep)

    result = load_example("evaluate_example").main("ds-1", ["ev-1"])

    assert result.status == "completed"
    assert [item.status for item in result.items] == ["ok", "ok"]
    assert platform.create_body is not None
    assert platform.create_body["evaluators"] == [
        {"evaluator_version_id": "ev-1", "role": "gating"}
    ]
    reported = sorted(platform.reports.values(), key=lambda item: item["example_key"])
    assert [item["scores"] for item in reported] == [
        [{"name": "mentions_rain", "value": 1.0}],
        [{"name": "mentions_rain", "value": 1.0}],
    ]
    names = [s.name for s in span_exporter.get_finished_spans()]
    assert names.count("evaluate") == 2
    assert names.count("forecast") == 2
    assert "completed: pass rate 0.75, 2 items" in capsys.readouterr().out
