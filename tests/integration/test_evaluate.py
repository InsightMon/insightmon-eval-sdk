"""`evaluate()` on a platform dataset with a client and a server evaluator, read back."""

from __future__ import annotations

import _thread
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import insightmon
from tests.integration.conftest import ItConfig, unique
from tests.integration.platform_api import AdminSession, flat_spans, wait_for

pytestmark = pytest.mark.integration

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

    with caplog.at_level(logging.WARNING, logger="insightmon"):
        result = insightmon.evaluate(
            weather_task,
            dataset=dataset_id,
            example_keys=keys[:2],
            evaluators=[sdk_it_no_such_config],
            name=unique("sdk it unknown score"),
            wait_timeout_s=300,
        )
    assert result.status == "completed"
    assert all(item.status == "ok" for item in result.items)
    assert any("sdk_it_no_such_config" in r.getMessage() for r in caplog.records)
    items = admin.experiment_items(result.experiment_id)
    assert len(items) == 2
    assert all(s["name"] != "sdk_it_no_such_config" for i in items for s in i["scores"])


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
