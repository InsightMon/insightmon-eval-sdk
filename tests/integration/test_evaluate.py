"""`evaluate()` on a platform dataset with a client and a server evaluator, read back."""

from __future__ import annotations

import _thread
import importlib
import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

import httpx
import pytest

import insightmon
from insightmon.client import ReportResult
from tests.integration.conftest import ItConfig, unique
from tests.integration.platform_api import AdminSession, flat_spans, wait_for

pytestmark = pytest.mark.integration

evaluate_mod = importlib.import_module("insightmon.evaluate")

Sdk = Callable[..., insightmon.InsightMonHandle]
ANSWER = "Light rain is expected tomorrow, around 14 C."


def weather_task(input: Any) -> dict[str, Any]:
    with insightmon.get_tracer().start_as_current_span(
        "forecast",
        attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "forecast"},
    ):
        return {"text": ANSWER}


def sdk_it_client(input: Any, output: Any, reference: Any) -> dict[str, Any]:
    return {"name": "sdk_it_client", "value": 1.0 if output.get("text") else 0.0}


@pytest.mark.usefixtures("score_configs")
def test_evaluate_reports_items_scores_and_spans(
    admin: AdminSession,
    sdk: Sdk,
    it_config: ItConfig,
    project_id: str,
    dataset: tuple[str, list[str]],
    server_evaluator: str,
) -> None:
    sdk()
    dataset_id, keys = dataset
    chosen = keys[:3]
    result = insightmon.evaluate(
        weather_task,
        dataset=dataset_id,
        example_keys=chosen,
        evaluators=[sdk_it_client],
        server_evaluators=[server_evaluator],
        name=unique("sdk it evaluate"),
        max_concurrency=2,
        wait_timeout_s=300,
    )

    assert result.status == "completed"
    assert (
        result.url
        == f"{it_config.base_url}/projects/{project_id}/experiments/{result.experiment_id}"
    )
    assert httpx.get(result.url, timeout=30).status_code == 200  # the web UI serves the page
    assert sorted(item.example_key for item in result.items) == sorted(chosen)
    assert all(item.status == "ok" and item.otel_trace_id for item in result.items)
    assert all(item.scores == [{"name": "sdk_it_client", "value": 1.0}] for item in result.items)

    experiment = admin.experiment(result.experiment_id)
    assert experiment["executor"] == "external"
    assert experiment["triggered_by"] == "sdk"
    assert experiment["finalized_at"] is not None
    assert experiment["requested"]["agent_version"] == "1.4.0"
    client_scores = experiment["summary"]["client_scores"]
    assert client_scores["n"] == len(chosen)
    assert client_scores["names"] == ["sdk_it_client"]

    items = admin.experiment_items(result.experiment_id)
    assert len(items) == len(chosen)
    by_trace = {item["otel_trace_id"]: item for item in items}
    assert set(by_trace) == {item.otel_trace_id for item in result.items}
    for item in items:
        assert item["status"] == "ok"
        assert item["output"]["text"] == ANSWER
        assert item["receipt"]["agent"]["version"] == "1.4.0"
        names = {score["name"]: score for score in item["scores"]}
        assert names["sdk_it_client"]["source"] == "import"
        assert names["sdk_it_client"]["value_number"] == 1.0
        server = [s for s in item["scores"] if s["evaluator_version_id"] == server_evaluator]
        assert server, f"no score of the server evaluator on {item['example_key']}"

    # the spans of each run were held until the item was reported, then joined its trace
    for otel_trace_id, item in by_trace.items():
        trace = wait_for("the item's trace", lambda t=otel_trace_id: admin.trace_by_otel_id(t))
        assert trace["origin"] == "experiment"
        assert trace["thread_id"] is None
        assert trace["experiment_item_id"] == item["id"]
        names = {s["name"] for s in flat_spans(admin.trace_detail(trace["id"])["spans"])}
        assert names == {"evaluate", "forecast"}


def test_evaluate_with_two_trials(
    admin: AdminSession, sdk: Sdk, dataset: tuple[str, list[str]]
) -> None:
    sdk()
    dataset_id, keys = dataset
    chosen = keys[:2]
    result = insightmon.evaluate(
        weather_task,
        dataset=dataset_id,
        example_keys=chosen,
        trials=2,
        name=unique("sdk it trials"),
        wait_timeout_s=300,
    )
    assert result.status == "completed"
    runs = sorted((item.example_key, item.trial_index) for item in result.items)
    assert runs == sorted((key, trial) for key in chosen for trial in (0, 1))
    assert all(item.status == "ok" for item in result.items)

    experiment = admin.experiment(result.experiment_id)
    assert experiment["trials"] == 2
    items = admin.experiment_items(result.experiment_id)
    assert sorted((i["example_key"], i["trial_index"]) for i in items) == runs
    assert len({i["otel_trace_id"] for i in items}) == 4


@pytest.mark.usefixtures("score_configs")
def test_a_client_score_without_a_published_config_is_dropped_with_a_warning(
    admin: AdminSession,
    sdk: Sdk,
    dataset: tuple[str, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk()
    dataset_id, keys = dataset

    def sdk_it_no_such_config(input: Any, output: Any, reference: Any) -> float:
        return 1.0

    calls: list[int] = []
    lock = threading.Lock()

    def sdk_it_client(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        with lock:
            calls.append(1)
            first = len(calls) == 1
        return {"name": "sdk_it_client", "value": 2.0 if first else 0.5}  # 2.0 is out of range

    with caplog.at_level(logging.WARNING, logger="insightmon"):
        result = insightmon.evaluate(
            weather_task,
            dataset=dataset_id,
            example_keys=keys[:2],
            evaluators=[sdk_it_no_such_config, sdk_it_client],
            name=unique("sdk it unknown score"),
            wait_timeout_s=300,
        )
    assert result.status == "completed"
    assert all(item.status == "ok" for item in result.items)
    messages = [r.getMessage() for r in caplog.records]
    # the unknown name is dropped before reporting (GET /api/v1/score-configs)
    assert any("'sdk_it_no_such_config' is not a published score config" in m for m in messages)
    # the out-of-range value is listed by the platform (invalid_scores) and only it is dropped
    assert any("refused the client score ['sdk_it_client']" in m for m in messages)
    assert sorted(len(item.scores) for item in result.items) == [0, 1]
    items = admin.experiment_items(result.experiment_id)
    assert len(items) == 2
    assert all(s["name"] != "sdk_it_no_such_config" for i in items for s in i["scores"])
    client_values = sorted(
        s["value_number"] for i in items for s in i["scores"] if s["name"] == "sdk_it_client"
    )
    assert client_values == [0.5]


class LoseFirstCreateAnswer(httpx.BaseTransport):
    """Sends every request to the platform, but loses the answer of the first
    `POST /api/v1/experiments` (a read timeout after the platform created the experiment)."""

    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport()
        self.creates: list[bytes] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if request.method == "POST" and request.url.path == "/api/v1/experiments":
            self.creates.append(request.content)
            if len(self.creates) == 1:
                response.read()
                response.close()
                raise httpx.ReadTimeout("the answer got lost", request=request)
        return response

    def close(self) -> None:
        self.inner.close()


@pytest.mark.usefixtures("score_configs")
def test_a_create_whose_answer_was_lost_creates_one_experiment(
    admin: AdminSession, sdk: Sdk, it_config: ItConfig, dataset: tuple[str, list[str]]
) -> None:
    sdk()
    dataset_id, keys = dataset
    name = unique("sdk it idempotent")
    transport = LoseFirstCreateAnswer()
    with insightmon.Client(it_config.base_url, it_config.api_key, transport=transport) as client:
        result = insightmon.evaluate(
            weather_task,
            dataset=dataset_id,
            example_keys=keys[:1],
            name=name,
            client=client,
            wait_timeout_s=300,
        )
    assert result.status == "completed"
    assert len(transport.creates) == 2
    assert transport.creates[0] == transport.creates[1]  # the retry: same key, same bytes
    created = [e["id"] for e in admin.experiments(dataset_id) if e["name"] == name]
    assert created == [result.experiment_id]

    # the same key and content answer that experiment; the same key with other content: 409
    key = uuid.uuid4().hex
    args: dict[str, Any] = {
        "dataset_id": dataset_id,
        "example_keys": keys[:1],
        "idempotency_key": key,
    }
    with insightmon.Client(it_config.base_url, it_config.api_key) as client:
        first = client.create_experiment(name=unique("sdk it key"), **args)
        assert client.create_experiment(name=first["name"], **args)["id"] == first["id"]
        with pytest.raises(insightmon.InsightMonError) as info:
            client.create_experiment(name=unique("sdk it other content"), **args)
        assert (info.value.status, info.value.code) == (409, "idempotency_key_conflict")
        assert info.value.params == {"idempotency_key": key}
        client.finalize_experiment(first["id"])
    assert [e["id"] for e in admin.experiments(dataset_id) if e["name"] == first["name"]] == [
        first["id"]
    ]


def test_spans_before_the_report_become_experiment_traffic(
    admin: AdminSession,
    sdk: Sdk,
    it_config: ItConfig,
    dataset: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the hold (turned off here), the spans of a run reach the platform before its
    item is reported: the platform reads `insightmon.experiment_id` and stores the trace as
    experiment traffic, never as a production trace, then `items:batch` links it."""
    sdk()
    dataset_id, keys = dataset
    item_scope = evaluate_mod.experiment_scope

    def without_hold(*args: Any, **kwargs: Any) -> AbstractContextManager[Any]:
        if kwargs.get("owner") is None:  # the item's scope (evaluators keep theirs)
            kwargs["hold"] = False
        scope: AbstractContextManager[Any] = item_scope(*args, **kwargs)
        return scope

    monkeypatch.setattr(evaluate_mod, "experiment_scope", without_hold)
    before: dict[str, dict[str, Any]] = {}

    class SpansFirst(insightmon.Client):
        def report_items(
            self, experiment_id: str, items: Sequence[Mapping[str, Any]], **kwargs: Any
        ) -> ReportResult:
            assert insightmon.flush(30)  # the spans were not held: export them first
            for item in items:
                trace_id = str(item["otel_trace_id"])
                before[trace_id] = wait_for(
                    "the trace before its report", lambda t=trace_id: admin.trace_by_otel_id(t)
                )
            return super().report_items(experiment_id, items, **kwargs)

    with SpansFirst(it_config.base_url, it_config.api_key) as client:
        result = insightmon.evaluate(
            weather_task,
            dataset=dataset_id,
            example_keys=keys[:2],
            name=unique("sdk it spans first"),
            client=client,
            wait_timeout_s=300,
        )
    assert result.status == "completed"
    assert set(before) == {item.otel_trace_id for item in result.items}
    for trace in before.values():
        assert trace["origin"] == "experiment"  # never a production trace
        assert trace["thread_id"] is None
        assert trace["experiment_item_id"] is None  # not reported yet

    items = {i["otel_trace_id"]: i for i in admin.experiment_items(result.experiment_id)}
    for otel_trace_id, item in items.items():
        trace = wait_for("the item's trace", lambda t=otel_trace_id: admin.trace_by_otel_id(t))
        assert trace["id"] == before[otel_trace_id]["id"]
        assert trace["origin"] == "experiment"
        assert trace["experiment_item_id"] == item["id"]
        names = {s["name"] for s in flat_spans(admin.trace_detail(trace["id"])["spans"])}
        assert names == {"evaluate", "forecast"}


def test_an_interrupted_run_is_not_finalized_and_can_be_finalized_later(
    admin: AdminSession,
    sdk: Sdk,
    it_config: ItConfig,
    dataset: tuple[str, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk()
    dataset_id, keys = dataset
    chosen = keys[:3]
    release = threading.Event()

    def task(input: Any, example: insightmon.Example) -> dict[str, Any]:
        if example.example_key == chosen[1]:
            _thread.interrupt_main()  # what Ctrl-C does to the calling thread
            release.wait(10)
        return weather_task(input)

    with caplog.at_level(logging.WARNING, logger="insightmon"), pytest.raises(KeyboardInterrupt):
        insightmon.evaluate(
            task,
            dataset=dataset_id,
            example_keys=chosen,
            name=unique("sdk it interrupted"),
            max_concurrency=1,
        )
    release.set()
    notes = [r.getMessage() for r in caplog.records if "was not finalized" in r.getMessage()]
    assert notes, "no warning names the experiment"
    match = re.search(r"experiment (\S+) was not finalized \((\d+) of (\d+) items", notes[0])
    assert match is not None
    experiment_id, reported, planned = match.group(1), int(match.group(2)), int(match.group(3))
    assert (reported, planned) == (1, 3)

    experiment = admin.experiment(experiment_id)
    assert experiment["finalized_at"] is None
    assert experiment["status"] not in ("completed", "failed", "cancelled")
    items = {i["example_key"]: i["status"] for i in admin.experiment_items(experiment_id)}
    assert items[chosen[0]] == "ok"
    assert {items[chosen[1]], items[chosen[2]]} == {"pending"}  # never reported

    with insightmon.Client(it_config.base_url, it_config.api_key) as client:
        client.finalize_experiment(experiment_id)
    finished = admin.wait_experiment(experiment_id)
    assert finished["status"] in ("completed", "failed")
    assert finished["finalized_at"] is not None
