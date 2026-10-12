"""`insightmon.evaluate()` against the fake platform of tests/fake_platform.py.

The tracing seams are replaced (`_flush_telemetry`, `_release_traces` record their calls) so
these tests do not depend on the span processor; span attributes are checked with an SDK
`TracerProvider` and an in-memory exporter.
"""

from __future__ import annotations

import _thread
import asyncio
import importlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import insightmon
from insightmon import _config, _context, _http, semconv
from insightmon._config import Component, RuntimeState, Settings
from insightmon._errors import InsightMonError
from insightmon.client import Client, Example
from insightmon.evaluate import ExperimentResult
from tests.conftest import API_KEY, BASE_URL, MockApi, error_response
from tests.fake_platform import BATCH, EXP, EXP_ID, FINALIZE, FakePlatform

#: The module (the package attribute `insightmon.evaluate` is the function).
evaluate_mod = importlib.import_module("insightmon.evaluate")


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def async_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(_http, "_sleep", slept.append)
    monkeypatch.setattr(evaluate_mod, "_sleep", slept.append)
    monkeypatch.setattr(evaluate_mod, "_async_sleep", async_sleep)
    return slept


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"released": [], "flushed": []}

    def release(trace_ids: Any) -> None:
        calls["released"].append(sorted(trace_ids))

    def flush(timeout_s: float, *, experiment_id: str, discard: bool = False) -> bool:
        assert experiment_id == EXP_ID
        calls["flushed"].append("discard" if discard else "release")
        return True

    monkeypatch.setattr(evaluate_mod, "_release_traces", release)
    monkeypatch.setattr(evaluate_mod, "_flush_telemetry", flush)
    return calls


@pytest.fixture
def client(mock_api: MockApi, no_sleep: list[float], seams: Any) -> Iterator[Client]:
    with Client(BASE_URL, API_KEY, transport=mock_api.transport) as c:
        yield c


@pytest.fixture
def traced(tracer_provider: TracerProvider) -> TracerProvider:
    """Make `get_tracer()` use a real SDK provider (as after `init()`)."""
    _config.set_runtime(RuntimeState(settings=Settings(), tracer_provider=tracer_provider))
    return tracer_provider


def answer(input: dict[str, Any]) -> str:
    return f"It is sunny in {input['city']}."


def mentions_city(input: Any, output: Any, reference: Any) -> dict[str, Any]:
    return {"name": "mentions_city", "value": reference["city"] in output["text"]}


def keys(n: int) -> list[str]:
    return [f"k{i:03d}" for i in range(n)]


class Bodies:
    """Counts task bodies running at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.max = 0

    @contextmanager
    def running(self) -> Iterator[None]:
        with self.lock:
            self.now += 1
            self.max = max(self.max, self.now)
        try:
            yield
        finally:
            with self.lock:
                self.now -= 1


def wait_for_task_threads() -> None:
    deadline = time.monotonic() + 10
    while any(t.name == "insightmon-evaluate-task" for t in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.01)


# --------------------------------------------------------------------------- happy path


def test_happy_path(
    client: Client,
    mock_api: MockApi,
    seams: dict[str, list[Any]],
    no_sleep: list[float],
) -> None:
    platform = FakePlatform(keys(5)).install(mock_api)
    result = insightmon.evaluate(
        answer,
        dataset="ds-weather",
        trials=2,
        evaluators=[mentions_city],
        server_evaluators=["evv-1", {"evaluator_version_id": "evv-2", "role": "diagnostic"}],
        metadata={"branch": "main"},
        client=client,
    )
    assert isinstance(result, ExperimentResult)
    assert result.experiment_id == EXP_ID
    assert result.status == "completed"
    assert result.finished
    assert result.pass_rate == 0.75
    assert result.summary == {"pass_rate": 0.75, "counts": {"ok": 10}}
    assert result.url == f"{BASE_URL}/projects/proj-1/experiments/{EXP_ID}"
    body = platform.create_body
    assert body is not None
    assert body["triggered_by"] == "sdk"
    assert body["dataset_id"] == "ds-weather"
    assert body["trials"] == 2
    assert body["name"].startswith("evaluate ds-weather ")
    assert body["metadata"] == {"branch": "main"}
    assert body["evaluators"] == [
        {"evaluator_version_id": "evv-1", "role": "gating"},
        {"evaluator_version_id": "evv-2", "role": "diagnostic"},
    ]
    assert "requested" not in body  # nothing from init()
    assert [(i.example_key, i.trial_index) for i in result.items] == [
        (k, t) for k in keys(5) for t in range(2)
    ]
    first = result.items[0]
    assert first.status == "ok"
    assert first.output == {"text": "It is sunny in k000."}
    assert first.scores == [{"name": "mentions_city", "value": True}]
    assert isinstance(first.latency_ms, int)
    assert first.otel_trace_id is None  # no tracer provider: no trace id
    assert len(platform.reports) == 10
    report = platform.reports[("k000", 1)]
    assert report == {
        "example_key": "k000",
        "trial_index": 1,
        "status": "ok",
        "output": {"text": "It is sunny in k000."},
        "latency_ms": report["latency_ms"],
        "scores": [{"name": "mentions_city", "value": True}],
    }
    assert platform.events == ["create", "batch:10", "finalize"]
    assert seams["flushed"] == ["release"]
    assert platform.polls == 2
    assert no_sleep == [1.0, 2.0]


def test_examples_are_paged(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(450)).install(mock_api)
    result = insightmon.evaluate(answer, dataset="ds", client=client, max_concurrency=8)
    pages = mock_api.calls("GET", f"{EXP}/{EXP_ID}/examples")
    assert [r.url.params.get("cursor") for r in pages] == [None, "200", "400"]
    assert all(r.url.params["limit"] == "200" for r in pages)
    assert len(result.items) == 450
    assert len(platform.reports) == 450


def test_trials_and_fresh_input_per_run(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(2)).install(mock_api)
    seen: list[tuple[str, int]] = []
    lock = threading.Lock()

    def task(input: dict[str, Any], example: Example) -> dict[str, Any]:
        input["seen"].append("me")  # mutating the input must not leak into other trials
        with lock:
            seen.append((example.example_key, len(input["seen"])))
        return {"n": len(input["seen"]), "tier": example.metadata["tier"]}

    result = insightmon.evaluate(task, dataset="ds", trials=3, client=client)
    assert sorted(seen) == [("k000", 1)] * 3 + [("k001", 1)] * 3
    assert [i.trial_index for i in result.items] == [0, 1, 2, 0, 1, 2]
    assert all(i.output == {"n": 1, "tier": "basic"} for i in result.items)


def test_concurrency_limit_is_respected(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(12)).install(mock_api)
    lock = threading.Lock()
    active = {"now": 0, "max": 0}

    def task(input: Any) -> str:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.03)
        with lock:
            active["now"] -= 1
        return "ok"

    insightmon.evaluate(task, dataset="ds", client=client, max_concurrency=3)
    assert active["max"] == 3


# --------------------------------------------------------------------------- outcomes


def test_task_error_is_reported_and_skips_evaluators(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(2)).install(mock_api)
    called: list[str] = []

    def task(input: dict[str, Any]) -> str:
        if input["city"] == "k001":
            raise ValueError("no forecast for k001")
        return "fine"

    def evaluator(input: Any, output: Any, reference: Any) -> bool:
        called.append(input["city"])
        return True

    result = insightmon.evaluate(task, dataset="ds", evaluators=[evaluator], client=client)
    failed = result.items[1]
    assert failed.status == "task_error"
    assert failed.output is None
    assert failed.error == {"code": "exception", "message": "ValueError: no forecast for k001"}
    assert failed.scores == []
    assert called == ["k000"]
    report = platform.reports[("k001", 0)]
    assert report["status"] == "task_error"
    assert "output" not in report
    assert report["error"] == failed.error


def test_timeout(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(3)).install(mock_api)
    release = threading.Event()

    def task(input: dict[str, Any]) -> str:
        if input["city"] == "k001":
            release.wait(5)
            return "late"
        return "quick"

    started = time.perf_counter()
    try:
        result = insightmon.evaluate(task, dataset="ds", timeout_s=0.2, client=client)
    finally:
        release.set()
    assert time.perf_counter() - started < 4
    late = result.items[1]
    assert late.status == "timeout"
    assert late.output is None
    assert late.error == {"code": "timeout", "message": "no result within 0.2 s"}
    assert late.latency_ms == 200
    assert [i.status for i in result.items] == ["ok", "timeout", "ok"]
    assert platform.reports[("k001", 0)]["status"] == "timeout"


def test_timed_out_tasks_keep_their_place_in_max_concurrency(
    client: Client, mock_api: MockApi
) -> None:
    """A hung downstream service never gets more than max_concurrency calls at once: a run
    that timed out keeps its place until its task returns, and the run stops when no place
    frees up within another timeout_s."""
    platform = FakePlatform(keys(8)).install(mock_api)
    hang = threading.Event()
    bodies = Bodies()

    def task(input: dict[str, Any]) -> str:
        with bodies.running():
            hang.wait(10)
        return "late"

    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match=EXP_ID) as info:
            insightmon.evaluate(task, dataset="ds", timeout_s=0.2, max_concurrency=2, client=client)
    finally:
        hang.set()
    assert time.monotonic() - started < 5
    assert bodies.max == 2
    assert "max_concurrency=2" in str(info.value)
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0)]
    assert {r["status"] for r in platform.reports.values()} == {"timeout"}
    assert not platform.finalized
    wait_for_task_threads()


def test_a_late_task_frees_its_place_when_it_returns(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(3)).install(mock_api)
    bodies = Bodies()
    order: list[str] = []

    def task(input: dict[str, Any]) -> str:
        with bodies.running():
            order.append(f"start {input['city']}")
            if input["city"] == "k000":
                time.sleep(0.8)
            order.append(f"end {input['city']}")
        return "ok"

    result = insightmon.evaluate(
        task, dataset="ds", timeout_s=0.5, max_concurrency=1, client=client
    )
    assert [i.status for i in result.items] == ["timeout", "ok", "ok"]
    assert bodies.max == 1
    assert order[:3] == ["start k000", "end k000", "start k001"]
    assert result.status == "completed"


def test_evaluator_error_is_logged_and_others_still_score(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)

    def broken(input: Any, output: Any, reference: Any) -> Any:
        raise RuntimeError("judge unavailable")

    result = insightmon.evaluate(
        answer, dataset="ds", evaluators=[broken, mentions_city], client=client
    )
    item = result.items[0]
    assert item.status == "ok"
    assert item.scores == [{"name": "mentions_city", "value": True}]
    assert "broken" in caplog.text
    assert "judge unavailable" in caplog.text
    assert platform.reports[("k000", 0)]["scores"] == item.scores


def test_evaluator_result_shapes(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1), score_names=frozenset({"length", "short", "a", "b"})).install(mock_api)

    def length(input: Any, output: Any, reference: Any) -> int:
        return len(output["text"])

    def several(input: Any, output: Any, reference: Any) -> list[dict[str, Any]]:
        return [
            {"name": "a", "value": 1.5, "explanation": "x" * 30000, "metadata": {"k": math.nan}},
            {"name": "b", "value": "label"},
            {"name": "length", "value": 0},  # duplicate name: the first one wins
        ]

    def nothing(input: Any, output: Any, reference: Any) -> None:
        return None

    def no_value(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        return {"name": "short", "value": None}

    def invalid(input: Any, output: Any, reference: Any) -> object:
        return object()

    result = insightmon.evaluate(
        lambda input: "hello",
        dataset="ds",
        evaluators=[length, several, nothing, no_value, invalid],
        client=client,
    )
    scores = result.items[0].scores
    assert [s["name"] for s in scores] == ["length", "a", "b"]
    assert scores[0]["value"] == 5
    assert len(scores[1]["explanation"]) == semconv.EXPLANATION_MAX
    assert scores[1]["metadata"] == {"k": None}


def test_output_normalization(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(9)).install(mock_api)
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    deep: Any = "leaf"
    for _ in range(_http.MAX_JSON_DEPTH + 1):
        deep = [deep]
    outputs: dict[str, Any] = {
        "k000": "text",
        "k001": None,
        "k002": {"score": math.nan, "values": (1, 2)},
        "k003": {"blob": "x" * (semconv.ITEM_OUTPUT_MAX_BYTES + 1)},
        "k004": {"when": object()},
        "k005": [1, True, None],
        "k006": 3.5,
        "k007": cyclic,
        "k008": deep,
    }
    result = insightmon.evaluate(lambda input: outputs[input["city"]], dataset="ds", client=client)
    by_key = {i.example_key: i for i in result.items}
    assert by_key["k000"].output == {"text": "text"}
    assert by_key["k001"].output == {}
    assert by_key["k002"].output == {"score": None, "values": [1, 2]}
    assert by_key["k003"].status == "task_error"
    assert by_key["k003"].error is not None
    assert by_key["k003"].error["code"] == "output_too_large"
    # anything but a str, a mapping or None is wrapped as the platform wraps target outputs
    assert by_key["k005"].output == {"json": [1, True, None]}
    assert by_key["k006"].output == {"json": 3.5}
    for key in ("k004", "k007", "k008"):  # not JSON, a cycle, too deep: task errors, no abort
        assert by_key[key].status == "task_error"
        assert by_key[key].error is not None
        assert by_key[key].error["code"] == "output_not_json"
    assert result.status == "completed"
    sent = {key: item for (key, _), item in platform.reports.items()}
    assert sent["k005"]["output"] == {"json": [1, True, None]}
    assert sent["k007"]["status"] == "task_error"


def test_a_non_finite_result_is_reported_as_json_null(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(2)).install(mock_api)
    result = insightmon.evaluate(lambda input: math.nan, dataset="ds", client=client)
    assert [i.status for i in result.items] == ["ok", "ok"]
    assert [i.output for i in result.items] == [{"json": None}, {"json": None}]
    sent = list(platform.reports.values())
    assert [i["output"] for i in sent] == [{"json": None}] * 2  # never `ok` with a null output
    assert platform.events[-1] == "finalize"


def test_lone_surrogates_never_abort_the_run(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    platform = FakePlatform(keys(4)).install(mock_api)
    cut = json.loads('"answer \\ud83d"')  # text cut in the middle of an emoji

    def task(input: dict[str, Any]) -> Any:
        key = input["city"]
        if key == "k000":
            return cut
        if key == "k001":
            return {cut: [cut]}
        if key == "k002":
            raise ValueError(cut)
        return "fine"

    def judge(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        return {"name": "length", "value": 1, "explanation": cut, "metadata": {"note": cut}}

    with caplog.at_level(logging.WARNING, logger="insightmon"):
        result = insightmon.evaluate(task, dataset="ds", evaluators=[judge], client=client)
    by_key = {i.example_key: i for i in result.items}
    assert by_key["k000"].output == {"text": "answer \ufffd"}
    assert by_key["k001"].output == {"answer \ufffd": ["answer \ufffd"]}
    assert by_key["k002"].status == "task_error"
    assert by_key["k002"].error is not None
    assert by_key["k002"].error["message"] == "ValueError: answer \ufffd"
    assert by_key["k000"].scores == [
        {
            "name": "length",
            "value": 1,
            "explanation": "answer \ufffd",
            "metadata": {"note": "answer \ufffd"},
        }
    ]
    assert platform.events[-1] == "finalize"
    assert len(platform.reports) == 4


def test_clean_text_keeps_pairs_and_replaces_lone_surrogates() -> None:
    assert _http.clean_text("plain") == "plain"
    assert _http.clean_text("\ud83d\ude00") == "\U0001f600"  # a pair is the character
    assert _http.clean_text("a\udc80b") == "a\ufffdb"
    assert _http.sanitize_json({"\ud800": ["\udfff"]}) == {"\ufffd": ["\ufffd"]}
    assert _http.encode_json("x\ud83d") == '"x\ufffd"'.encode()


def test_example_is_passed_when_the_task_takes_it(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1)).install(mock_api)
    got: list[Any] = []

    class Assistant:
        def __call__(self, input: Any, example: Example) -> str:
            got.append(example)
            return "ok"

    insightmon.evaluate(Assistant(), dataset="ds", client=client)
    assert isinstance(got[0], Example)
    assert got[0].reference == {"city": "k000"}


def test_task_args() -> None:
    def one(input: Any) -> None: ...
    def two(input: Any, example: Any) -> None: ...
    def optional(input: Any, config: Any = None) -> None: ...
    def named(input: Any, example: Any = None) -> None: ...
    def star(*args: Any) -> None: ...
    def state(input: Any, initial_state: Any) -> None: ...
    def state_default(input: Any, initial_state: Any = None, *rest: Any) -> None: ...

    assert evaluate_mod._task_args(one) == "input"
    assert evaluate_mod._task_args(two) == "example"
    assert evaluate_mod._task_args(optional) == "input"
    assert evaluate_mod._task_args(named) == "example"
    assert evaluate_mod._task_args(star) == "example"
    assert evaluate_mod._task_args(len) == "input"
    assert evaluate_mod._task_args(state) == "initial_state"
    assert evaluate_mod._task_args(state_default) == "initial_state"


def _with_state(platform: FakePlatform) -> FakePlatform:
    for index, key in enumerate(platform.keys):
        platform.initial_states[key] = {"notes": [f"seed {index}"]}
    return platform


def test_initial_state_is_passed_when_the_task_names_it(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    _with_state(FakePlatform(keys(2))).install(mock_api)
    got: list[Any] = []

    def task(input: dict[str, Any], initial_state: dict[str, Any]) -> str:
        initial_state["notes"].append("changed by the task")  # a copy: trials never share it
        got.append((input["city"], initial_state))
        return "ok"

    with caplog.at_level(logging.WARNING, logger="insightmon"):
        insightmon.evaluate(task, dataset="ds", trials=2, client=client)
    assert sorted((city, state["notes"][0]) for city, state in got) == [
        ("k000", "seed 0"),
        ("k000", "seed 0"),
        ("k001", "seed 1"),
        ("k001", "seed 1"),
    ]
    assert all(len(state["notes"]) == 2 for _, state in got)
    assert "initial_state" not in caplog.text


def test_a_task_that_cannot_receive_the_initial_state_is_warned_about(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    _with_state(FakePlatform(keys(3))).install(mock_api)
    with caplog.at_level(logging.WARNING, logger="insightmon"):
        insightmon.evaluate(answer, dataset="ds", client=client)
    warnings = [r.getMessage() for r in caplog.records if "initial_state" in r.getMessage()]
    assert len(warnings) == 1
    assert "3 examples have an initial_state" in warnings[0]
    assert EXP_ID in warnings[0]


def test_reserved_score_metadata_keys_are_dropped(
    client: Client,
    mock_api: MockApi,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evaluate_mod, "_reserved_warned", set())
    platform = FakePlatform(keys(2)).install(mock_api)

    def copied(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        metadata = {"adjudicated": {"by": "u-1"}, "accepted_by": "u-2", "tool": "review"}
        return {"name": "length", "value": 1, "metadata": metadata}

    with caplog.at_level(logging.WARNING, logger="insightmon"):
        result = insightmon.evaluate(answer, dataset="ds", evaluators=[copied], client=client)
    expected = [{"name": "length", "value": 1, "metadata": {"tool": "review"}}]
    assert [i.scores for i in result.items] == [expected, expected]
    assert [r["scores"] for r in platform.reports.values()] == [expected, expected]
    warnings = [r.getMessage() for r in caplog.records if "reserved" in r.getMessage()]
    assert len(warnings) == 1  # once per score name
    assert "accepted_by" in warnings[0]
    assert "adjudicated" in warnings[0]


# --------------------------------------------------------------------------- batching


def test_items_are_batched_by_fifty(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(120)).install(mock_api)
    insightmon.evaluate(answer, dataset="ds", client=client, max_concurrency=1)
    assert platform.events == ["create", "batch:50", "batch:50", "batch:20", "finalize"]


def test_batch_retry_on_429_and_503_sends_identical_content(
    client: Client, mock_api: MockApi, no_sleep: list[float]
) -> None:
    platform = FakePlatform(keys(3)).install(mock_api)
    platform.batch_errors = [
        error_response(429, "rate_limited", {"retry_after_s": 2}, headers={"Retry-After": "2"}),
        error_response(503, "internal_error"),
    ]
    result = insightmon.evaluate(answer, dataset="ds", client=client)
    bodies = [r.content for r in mock_api.calls("POST", BATCH)]
    assert len(bodies) == 3
    assert len(set(bodies)) == 1
    assert no_sleep[0] == 2.0
    assert result.status == "completed"
    assert len(platform.reports) == 3


def test_lost_answer_is_retried_and_counted_as_duplicate(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(2)).install(mock_api)
    original = platform.batch
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        response = original(request)
        if calls["n"] == 1:
            raise httpx.ReadTimeout("answer lost")  # written, but the answer never arrives
        return response

    mock_api.routes[("POST", BATCH)] = [flaky]
    result = insightmon.evaluate(answer, dataset="ds", client=client)
    assert result.status == "completed"
    assert calls["n"] == 2
    assert len(platform.reports) == 2


@pytest.mark.parametrize(
    ("status", "code", "params"),
    [
        (400, "external_item_invalid", {"example_key": "k000", "reason": "unknown_example"}),
        (403, "permission_denied", {"required": "experiment_write"}),
        (422, "validation_error", {"errors": []}),
    ],
)
def test_report_failure_stops_without_finalize(
    client: Client,
    mock_api: MockApi,
    seams: dict[str, list[Any]],
    status: int,
    code: str,
    params: dict[str, Any],
) -> None:
    platform = FakePlatform(keys(3)).install(mock_api)
    platform.batch_errors = [error_response(status, code, params)]
    with pytest.raises(InsightMonError) as info:
        insightmon.evaluate(answer, dataset="ds", client=client)
    err = info.value
    assert (err.code, err.status, err.params) == (code, status, params)
    assert EXP_ID in str(err)
    assert "not finalized" in str(err)
    assert len(mock_api.calls("POST", BATCH)) == 1
    assert mock_api.calls("POST", FINALIZE) == []
    assert "finalize" not in platform.events
    assert seams["flushed"] == ["discard"]  # spans of unreported items are dropped


def test_score_invalid_drops_client_scores(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    platform = FakePlatform(keys(2), score_names=frozenset({"short"})).install(mock_api)
    result = insightmon.evaluate(answer, dataset="ds", evaluators=[mentions_city], client=client)
    assert result.status == "completed"
    assert all(i.scores == [] for i in result.items)
    assert all("scores" not in r for r in platform.reports.values())
    assert "mentions_city" in caplog.text


def test_an_out_of_range_score_of_one_trial_keeps_the_other_trials_scores(
    client: Client, mock_api: MockApi
) -> None:
    platform = FakePlatform(keys(2), score_max=1.0).install(mock_api)
    calls: list[int] = []

    def accuracy(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        calls.append(1)
        trial = (len(calls) - 1) % 3  # runs in order with max_concurrency=1
        return {"name": "length", "value": 1.5 if trial == 1 else 0.5}

    result = insightmon.evaluate(
        answer, dataset="ds", trials=3, evaluators=[accuracy], max_concurrency=1, client=client
    )
    assert result.status == "completed"
    scores = {(i.example_key, i.trial_index): i.scores for i in result.items}
    valid = [{"name": "length", "value": 0.5}]
    assert scores == {
        ("k000", 0): valid,
        ("k000", 1): [],
        ("k000", 2): valid,
        ("k001", 0): valid,
        ("k001", 1): [],
        ("k001", 2): valid,
    }
    assert {ref: r.get("scores", []) for ref, r in platform.reports.items()} == scores


# --------------------------------------------------------------------------- finalize, wait


def test_permission_denied_on_create_names_the_scopes(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1)).install(mock_api)
    mock_api.routes[("POST", EXP)] = [
        error_response(403, "permission_denied", {"required": "experiment_write"})
    ]
    with pytest.raises(InsightMonError) as info:
        insightmon.evaluate(answer, dataset="ds", client=client)
    assert info.value.code == "permission_denied"
    assert "experiment_write" in info.value.message
    assert "['trace_ingest']" in info.value.message
    assert API_KEY not in str(info.value)


def test_examples_not_ready_raises_before_running(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1)).install(mock_api)
    mock_api.routes[("POST", EXP)] = [
        error_response(409, "examples_not_ready", {"example_keys": ["k000"]})
    ]
    ran: list[Any] = []
    with pytest.raises(InsightMonError) as info:
        insightmon.evaluate(ran.append, dataset="ds", client=client)
    assert info.value.code == "examples_not_ready"
    assert ran == []


def test_example_listing_failure_does_not_finalize(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1)).install(mock_api)
    mock_api.routes[("GET", f"{EXP}/{EXP_ID}/examples")] = [
        error_response(422, "validation_error", {"errors": []})
    ]
    with pytest.raises(InsightMonError) as info:
        insightmon.evaluate(answer, dataset="ds", client=client)
    assert EXP_ID in str(info.value)
    assert mock_api.calls("POST", FINALIZE) == []


def test_finalize_retry_meeting_finalized_counts_as_success(
    client: Client, mock_api: MockApi
) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)
    platform.finalize_errors = [httpx.ReadTimeout("answer lost")]
    result = insightmon.evaluate(answer, dataset="ds", client=client)
    assert result.status == "completed"
    assert len(mock_api.calls("POST", FINALIZE)) == 2


def test_already_finalized_experiment_is_still_awaited(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)
    platform.finalize_errors = [
        error_response(409, "invalid_state", {"status": "finalized", "action": "finalize"})
    ]
    platform.finalized = False
    original_get = platform.get

    def get(request: httpx.Request) -> httpx.Response:
        platform.finalized = True
        return original_get(request)

    mock_api.routes[("GET", f"{EXP}/{EXP_ID}")] = [get]
    result = insightmon.evaluate(answer, dataset="ds", client=client)
    assert result.status == "completed"
    assert "already finalized" in caplog.text


def test_finalize_failure_names_the_experiment(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)
    platform.finalize_errors = [error_response(409, "project_archived")]
    with pytest.raises(InsightMonError) as info:
        insightmon.evaluate(answer, dataset="ds", client=client)
    assert info.value.code == "project_archived"
    assert EXP_ID in str(info.value)
    assert len(platform.reports) == 1


def test_wait_false_returns_after_finalize(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(2)).install(mock_api)
    result = insightmon.evaluate(answer, dataset="ds", wait=False, client=client)
    assert result.status == "running"
    assert not result.finished
    assert result.summary is None
    assert result.pass_rate is None
    assert platform.polls == 0
    assert len(result.items) == 2
    assert platform.finalized


def test_wait_timeout_returns_the_current_state(
    client: Client, mock_api: MockApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = FakePlatform(keys(1), polls_until_done=10**6).install(mock_api)
    clock = {"now": 0.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(evaluate_mod, "_sleep", sleep)
    monkeypatch.setattr(evaluate_mod, "_monotonic", lambda: clock["now"])
    result = insightmon.evaluate(answer, dataset="ds", wait_timeout_s=30.0, client=client)
    assert result.status == "running"
    assert waits == [1.0, 2.0, 4.0, 8.0, 10.0, 5.0]
    assert platform.polls == 6


def test_poll_error_returns_the_last_state(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    FakePlatform(keys(1)).install(mock_api)
    mock_api.routes[("GET", f"{EXP}/{EXP_ID}")] = [error_response(404, "not_found")]
    result = insightmon.evaluate(answer, dataset="ds", client=client)
    assert result.status == "running"
    assert "could not read experiment" in caplog.text


# --------------------------------------------------------------------------- versions


def test_requested_and_receipt_come_from_init(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)
    settings = Settings(
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=(Component("prompt", "system", "v7", content_hash="sha256:abc"),),
    )
    _config.set_runtime(RuntimeState(settings=settings))
    _config.set_global_component(Component("tool", "forecast", "2"))
    insightmon.evaluate(answer, dataset="ds", client=client)
    expected = {
        "agent_version": "1.4.0",
        "components": [
            {"type": "prompt", "name": "system", "version": "v7"},
            {"type": "tool", "name": "forecast", "version": "2"},
        ],
    }
    assert platform.create_body is not None
    assert platform.create_body["requested"] == expected
    assert platform.reports[("k000", 0)]["receipt"] == expected


def test_explicit_requested_wins(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)
    _config.set_runtime(RuntimeState(settings=Settings(agent_version="1.4.0")))
    insightmon.evaluate(answer, dataset="ds", requested={"agent_version": "2.0.0"}, client=client)
    assert platform.create_body is not None
    assert platform.create_body["requested"] == {"agent_version": "2.0.0", "components": []}
    assert platform.reports[("k000", 0)]["receipt"] == {"agent_version": "1.4.0", "components": []}


# --------------------------------------------------------------------------- tracing


def test_item_spans_carry_the_experiment_attributes(
    client: Client,
    mock_api: MockApi,
    traced: TracerProvider,
    span_exporter: InMemorySpanExporter,
    seams: dict[str, list[Any]],
) -> None:
    platform = FakePlatform(keys(2)).install(mock_api)
    tracer = traced.get_tracer("app")

    def task(input: dict[str, Any]) -> str:
        with tracer.start_as_current_span("chat model"):
            if input["city"] == "k001":
                raise KeyError("city")
            return "ok"

    result = insightmon.evaluate(task, dataset="ds", trials=2, client=client)
    spans = span_exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "evaluate"]
    assert len(roots) == 4
    for item in result.items:
        (root,) = [
            s
            for s in roots
            if s.attributes is not None
            and s.attributes[semconv.EXAMPLE_KEY] == item.example_key
            and s.attributes[semconv.TRIAL_INDEX] == item.trial_index
        ]
        assert root.parent is None
        assert root.attributes is not None
        assert root.attributes[semconv.EXPERIMENT_ID] == EXP_ID
        assert root.attributes[semconv.OPERATION_NAME] == semconv.OP_INVOKE_AGENT
        assert item.otel_trace_id == format(root.context.trace_id, "032x")
        assert platform.reports[(item.example_key, item.trial_index)]["otel_trace_id"] == (
            item.otel_trace_id
        )
        (child,) = [
            s for s in spans if s.name == "chat model" and s.parent == root.get_span_context()
        ]
        if item.status == "task_error":
            assert root.status.status_code is StatusCode.ERROR
            assert root.attributes[semconv.ERROR_TYPE] == "KeyError"
        else:
            assert root.status.status_code is not StatusCode.ERROR
            assert child is not None
    assert len({r.context.trace_id for r in roots}) == 4  # one new trace per run
    released = sorted(t for batch in seams["released"] for t in batch)
    assert released == sorted(i.otel_trace_id for i in result.items if i.otel_trace_id)


def test_traces_are_released_after_their_batch_and_before_finalize(
    client: Client,
    mock_api: MockApi,
    traced: TracerProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = FakePlatform(keys(60)).install(mock_api)
    reported_when_released: list[int] = []

    def release(trace_ids: Any) -> None:
        ids = list(trace_ids)
        reported = {r.get("otel_trace_id") for r in platform.reports.values()}
        assert set(ids) <= reported  # only items the platform already has
        reported_when_released.append(len(ids))
        platform.events.append(f"release:{len(ids)}")

    def flush(timeout_s: float, *, experiment_id: str, discard: bool = False) -> bool:
        platform.events.append(f"flush:{'discard' if discard else 'release'}")
        return True

    monkeypatch.setattr(evaluate_mod, "_release_traces", release)
    monkeypatch.setattr(evaluate_mod, "_flush_telemetry", flush)
    insightmon.evaluate(answer, dataset="ds", client=client, max_concurrency=1)
    assert platform.events == [
        "create",
        "batch:50",
        "release:50",
        "batch:10",
        "release:10",
        "flush:release",
        "finalize",
    ]


def test_flush_telemetry_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    from insightmon import _setup

    flushed: list[float] = []
    monkeypatch.setattr(_setup, "flush", lambda timeout_s: flushed.append(timeout_s) or True)
    released: list[tuple[frozenset[int] | None, bool]] = []
    _context.add_release_listener(lambda ids, discard: released.append((ids, discard)))
    _context.hold_trace(7, EXP_ID)
    _context.hold_trace(8, "another-run")
    assert evaluate_mod._flush_telemetry(3.0, experiment_id=EXP_ID)
    assert not _context.is_held(7)
    assert _context.is_held(8)  # another run's traces stay held
    # the run is over: a trace a late task starts for it is dropped at once
    _context.hold_trace(10, EXP_ID)
    assert _context.trace_status(10) == "discarded"
    _context.hold_trace(9, "exp-2")
    assert evaluate_mod._flush_telemetry(4.0, experiment_id="exp-2", discard=True)
    assert _context.is_discarded(9)
    assert _context.is_held(8)
    _context.hold_trace(11, "exp-2")
    assert _context.trace_status(11) == "discarded"
    assert released == [(frozenset({7}), False), (frozenset({9}), True)]
    assert flushed == [3.0, 4.0]
    _context.hold_trace(int("ab" * 16, 16))
    evaluate_mod._release_traces(["ab" * 16])
    assert not _context.is_held(int("ab" * 16, 16))


def test_telemetry_failure_does_not_fail_the_run(
    client: Client,
    mock_api: MockApi,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    FakePlatform(keys(1)).install(mock_api)

    def broken(timeout_s: float, *, experiment_id: str, discard: bool = False) -> bool:
        raise RuntimeError("exporter down")

    monkeypatch.setattr(evaluate_mod, "_flush_telemetry", broken)
    assert insightmon.evaluate(answer, dataset="ds", client=client).status == "completed"
    assert "could not flush" in caplog.text


def test_spans_are_held_until_their_item_is_reported(
    mock_api: MockApi, no_sleep: list[float], span_exporter: InMemorySpanExporter
) -> None:
    """End to end with the SDK's span processor: no span of an item reaches the exporter
    before the platform has the item (its trace is then `origin = experiment`), and an
    enclosing thread scope never leaks a thread key into experiment traces."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from insightmon.processor import InsightMonSpanProcessor

    processor = InsightMonSpanProcessor(SimpleSpanProcessor(span_exporter))
    provider = TracerProvider()
    provider.add_span_processor(processor)
    _config.set_runtime(
        RuntimeState(
            settings=Settings(agent_name="notes-assistant", agent_version="0.3.0"),
            tracer_provider=provider,
            span_processors=[processor],
        )
    )
    platform = FakePlatform(keys(7)).install(mock_api)
    original = platform.batch
    early: list[str] = []

    def batch(request: httpx.Request) -> httpx.Response:
        exported = {format(s.context.trace_id, "032x") for s in span_exporter.get_finished_spans()}
        for item in json.loads(request.content)["items"]:
            if item.get("otel_trace_id") in exported:
                early.append(item["example_key"])
        return original(request)

    mock_api.routes[("POST", BATCH)] = [batch]
    tracer = provider.get_tracer("app")

    def task(input: dict[str, Any]) -> str:
        with tracer.start_as_current_span("tool call"):
            return "ok"

    try:
        with (
            Client(BASE_URL, API_KEY, transport=mock_api.transport) as client,
            insightmon.thread("chat-outside"),
        ):
            result = insightmon.evaluate(task, dataset="ds", client=client, max_concurrency=2)
    finally:
        provider.shutdown()
    assert early == []
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 14
    assert processor.held_span_count == 0
    assert {format(s.context.trace_id, "032x") for s in spans} == {
        i.otel_trace_id for i in result.items
    }
    for span in spans:
        assert span.attributes is not None
        assert semconv.GEN_AI_CONVERSATION_ID not in span.attributes
        assert span.attributes[semconv.AGENT_VERSION] == "0.3.0"


def _processor_runtime(
    span_exporter: InMemorySpanExporter, settings: Settings
) -> tuple[Any, TracerProvider]:
    """The SDK's span processor exporting to `span_exporter`, installed as after `init()`."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from insightmon.processor import InsightMonSpanProcessor

    processor = InsightMonSpanProcessor(SimpleSpanProcessor(span_exporter))
    provider = TracerProvider()
    provider.add_span_processor(processor)
    _config.set_runtime(
        RuntimeState(settings=settings, tracer_provider=provider, span_processors=[processor])
    )
    return processor, provider


def test_set_component_in_a_task_applies_to_that_item_only(
    mock_api: MockApi, no_sleep: list[float], span_exporter: InMemorySpanExporter
) -> None:
    system_v1 = Component("prompt", "system", version="v1")
    _, provider = _processor_runtime(
        span_exporter, Settings(agent_version="1.0.0", components=(system_v1,))
    )
    platform = FakePlatform(keys(3)).install(mock_api)

    def task(input: dict[str, Any]) -> str:
        if input["city"] == "k001":
            insightmon.set_component("prompt", "system", version="v2")
            insightmon.set_component("tool", "geocoder", version="3")
        return "ok"

    try:
        with Client(BASE_URL, API_KEY, transport=mock_api.transport) as client:
            result = insightmon.evaluate(task, dataset="ds", client=client, max_concurrency=2)
    finally:
        provider.shutdown()
    assert result.status == "completed"
    assert platform.create_body is not None
    assert platform.create_body["requested"] == {
        "agent_version": "1.0.0",
        "components": [{"type": "prompt", "name": "system", "version": "v1"}],
    }
    receipts = {key[0]: report["receipt"] for key, report in platform.reports.items()}
    assert receipts["k000"] == receipts["k002"] == platform.create_body["requested"]
    assert receipts["k001"] == {
        "agent_version": "1.0.0",
        "components": [
            {"type": "prompt", "name": "system", "version": "v2"},
            {"type": "tool", "name": "geocoder", "version": "3"},
        ],
    }
    # nothing leaked into the process: a later run starts from init() again
    assert _config.global_components() == (system_v1,)
    version_key = semconv.component_attribute("prompt", "system")
    roots = {
        s.attributes[semconv.EXAMPLE_KEY]: s
        for s in span_exporter.get_finished_spans()
        if s.name == "evaluate" and s.attributes is not None
    }
    assert roots["k001"].attributes is not None
    assert roots["k001"].attributes[version_key] == "v2"
    assert roots["k000"].attributes is not None
    assert roots["k000"].attributes[version_key] == "v1"


def test_a_run_releases_only_its_own_held_traces(
    mock_api: MockApi, no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    from insightmon import _setup

    monkeypatch.setattr(_setup, "flush", lambda timeout_s: True)
    FakePlatform(keys(2)).install(mock_api)
    _context.hold_trace(11, "another-experiment")
    _context.hold_trace(12, EXP_ID)  # e.g. a separate trace the task started
    with Client(BASE_URL, API_KEY, transport=mock_api.transport) as client:
        insightmon.evaluate(answer, dataset="ds", client=client)
    assert _context.held_traces() == frozenset({11})


def test_stopped_run_drops_the_spans_of_unreported_items(
    mock_api: MockApi, no_sleep: list[float], span_exporter: InMemorySpanExporter
) -> None:
    processor, provider = _processor_runtime(span_exporter, Settings(agent_version="1.0.0"))
    platform = FakePlatform(keys(6)).install(mock_api)
    release = threading.Event()
    tracer = provider.get_tracer("app")

    def task(input: dict[str, Any]) -> str:
        with tracer.start_as_current_span("tool call"):
            if input["city"] == "k002":
                _thread.interrupt_main()
                release.wait(10)
        return "ok"

    try:
        with (
            Client(BASE_URL, API_KEY, transport=mock_api.transport) as client,
            pytest.raises(KeyboardInterrupt),
        ):
            insightmon.evaluate(task, dataset="ds", client=client, max_concurrency=1)
        release.set()  # the abandoned item ends after the run stopped
        deadline = time.monotonic() + 10
        while any(t.name.startswith("insightmon-evaluate") for t in threading.enumerate()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        provider.shutdown()
    reported = {r["otel_trace_id"] for r in platform.reports.values()}
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0)]
    exported = {format(s.context.trace_id, "032x") for s in span_exporter.get_finished_spans()}
    assert exported == reported  # the in-flight item's spans were dropped, even late ones
    assert len(span_exporter.get_finished_spans()) == 4
    assert processor.held_span_count == 0
    assert _context.held_traces() == frozenset()


def test_evaluators_run_outside_the_item_trace_and_their_spans_are_dropped(
    mock_api: MockApi, no_sleep: list[float], span_exporter: InMemorySpanExporter
) -> None:
    processor, provider = _processor_runtime(span_exporter, Settings(agent_version="1.0.0"))
    FakePlatform(keys(2)).install(mock_api)
    tracer = provider.get_tracer("app")
    current: list[bool] = []
    judged: list[tuple[int, int]] = []

    def task(input: dict[str, Any]) -> str:
        with tracer.start_as_current_span("tool call"):
            return "ok"

    def judge(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        current.append(trace.get_current_span().get_span_context().is_valid)
        with tracer.start_as_current_span("judge model call") as span:
            judged.append((span.get_span_context().trace_id, time.time_ns()))
        return {"name": "length", "value": 1}

    try:
        with Client(BASE_URL, API_KEY, transport=mock_api.transport) as client:
            result = insightmon.evaluate(
                task, dataset="ds", evaluators=[judge], client=client, max_concurrency=1
            )
    finally:
        provider.shutdown()
    assert current == [False, False]  # no current span: never a child of the agent's turn
    spans = span_exporter.get_finished_spans()
    assert sorted(s.name for s in spans) == ["evaluate", "evaluate", "tool call", "tool call"]
    items = {int(i.otel_trace_id, 16) for i in result.items if i.otel_trace_id}
    assert len(items) == 2
    assert not items & {trace_id for trace_id, _ in judged}
    roots = sorted((s for s in spans if s.name == "evaluate"), key=lambda s: s.start_time or 0)
    for root, (_, judged_at) in zip(roots, judged, strict=True):
        assert root.end_time is not None
        assert root.end_time <= judged_at  # the turn ended before its evaluators ran
    assert [i.scores for i in result.items] == [[{"name": "length", "value": 1}]] * 2
    assert processor.held_span_count == 0
    assert _context.held_traces() == frozenset()


async def test_aevaluate_evaluators_run_outside_the_item_trace(
    mock_api: MockApi, no_sleep: list[float], span_exporter: InMemorySpanExporter
) -> None:
    _, provider = _processor_runtime(span_exporter, Settings(agent_version="1.0.0"))
    FakePlatform(keys(2)).install(mock_api)
    tracer = provider.get_tracer("app")
    current: list[bool] = []

    async def task(input: dict[str, Any]) -> str:
        return "ok"

    async def async_judge(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        current.append(trace.get_current_span().get_span_context().is_valid)
        with tracer.start_as_current_span("judge model call"):
            return {"name": "length", "value": 1}

    def sync_judge(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        current.append(trace.get_current_span().get_span_context().is_valid)
        with tracer.start_as_current_span("judge model call"):
            return {"name": "short", "value": True}

    try:
        with Client(BASE_URL, API_KEY, transport=mock_api.transport) as client:
            await insightmon.aevaluate(
                task, dataset="ds", evaluators=[async_judge, sync_judge], client=client
            )
    finally:
        provider.shutdown()
    assert current == [False] * 4
    assert sorted(s.name for s in span_exporter.get_finished_spans()) == ["evaluate"] * 2
    assert _context.held_traces() == frozenset()


def test_evaluate_warns_when_its_spans_cannot_be_held(
    client: Client,
    mock_api: MockApi,
    traced: TracerProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Collector mode without init(export_processor=...): another processor exports the
    spans as they end, before their items are reported."""
    FakePlatform(keys(3)).install(mock_api)
    with caplog.at_level(logging.WARNING, logger="insightmon"):
        insightmon.evaluate(answer, dataset="ds", client=client, max_concurrency=3)
    warnings = [r.getMessage() for r in caplog.records if "export_processor" in r.getMessage()]
    assert len(warnings) == 1  # once per run
    assert EXP_ID in warnings[0]


@pytest.mark.parametrize("tracing", ["off", "held"])
def test_no_holding_warning_when_spans_are_held_or_not_recorded(
    mock_api: MockApi,
    no_sleep: list[float],
    span_exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
    tracing: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="insightmon")
    FakePlatform(keys(1)).install(mock_api)
    provider = None
    if tracing == "held":
        _, provider = _processor_runtime(span_exporter, Settings())
    try:
        with Client(BASE_URL, API_KEY, transport=mock_api.transport) as client:
            insightmon.evaluate(answer, dataset="ds", client=client)
    finally:
        if provider is not None:
            provider.shutdown()
    assert "export_processor" not in caplog.text


# --------------------------------------------------------------------------- interruption


def test_ctrl_c_reports_finished_items_and_does_not_finalize(
    client: Client,
    mock_api: MockApi,
    seams: dict[str, list[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    platform = FakePlatform(keys(10)).install(mock_api)
    caplog.set_level(logging.WARNING)
    release = threading.Event()

    def task(input: dict[str, Any]) -> str:
        if input["city"] == "k002":
            _thread.interrupt_main()  # what Ctrl-C does to the calling thread
            release.wait(10)
        return "ok"

    with pytest.raises(KeyboardInterrupt):
        insightmon.evaluate(task, dataset="ds", client=client, max_concurrency=1)
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0)]
    assert mock_api.calls("POST", FINALIZE) == []
    assert seams["flushed"] == ["discard"]
    assert f"Client().finalize_experiment('{EXP_ID}')" in caplog.text
    # the abandoned run finishes later: it must not be reported, and nothing new starts
    release.set()
    deadline = time.monotonic() + 10
    while any(t.name.startswith("insightmon-evaluate") for t in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0)]
    assert len(mock_api.calls("POST", BATCH)) == 1


def test_task_base_exception_stops_the_run(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(5)).install(mock_api)

    def task(input: dict[str, Any]) -> str:
        if input["city"] == "k003":
            raise SystemExit(3)
        return "ok"

    with pytest.raises(SystemExit):
        insightmon.evaluate(task, dataset="ds", client=client, max_concurrency=1)
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0), ("k002", 0)]
    assert not platform.finalized


def _interrupting_batches(platform: FakePlatform, mock_api: MockApi, times: int) -> list[str]:
    """Ctrl-C while the first `times` items:batch requests are sent (the request runs in the
    thread that called evaluate(), which is where Ctrl-C lands)."""
    original = platform.batch
    seen: list[str] = []

    def batch(request: httpx.Request) -> httpx.Response:
        if len(seen) < times:
            seen.append("interrupted")
            raise KeyboardInterrupt
        return original(request)

    mock_api.routes[("POST", BATCH)] = [batch]
    return seen


def test_ctrl_c_during_the_last_batch_reports_and_drops_without_finalizing(
    mock_api: MockApi,
    no_sleep: list[float],
    span_exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="insightmon")
    processor, provider = _processor_runtime(span_exporter, Settings())
    platform = FakePlatform(keys(3)).install(mock_api)
    _interrupting_batches(platform, mock_api, times=1)
    try:
        with (
            Client(BASE_URL, API_KEY, transport=mock_api.transport) as client,
            pytest.raises(KeyboardInterrupt),
        ):
            insightmon.evaluate(answer, dataset="ds", client=client, max_concurrency=1)
        assert _context.held_traces() == frozenset()
        _context.release_traces(None)  # what shutdown() does at exit
    finally:
        provider.shutdown()
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0), ("k002", 0)]
    assert not platform.finalized
    exported = {format(s.context.trace_id, "032x") for s in span_exporter.get_finished_spans()}
    assert exported == {r["otel_trace_id"] for r in platform.reports.values()}
    assert processor.held_span_count == 0
    assert f"experiment {EXP_ID} was not finalized" in caplog.text


def test_a_second_ctrl_c_while_stopping_still_drops_the_spans(
    mock_api: MockApi,
    no_sleep: list[float],
    span_exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="insightmon")
    processor, provider = _processor_runtime(span_exporter, Settings())
    platform = FakePlatform(keys(3)).install(mock_api)
    _interrupting_batches(platform, mock_api, times=2)
    try:
        with (
            Client(BASE_URL, API_KEY, transport=mock_api.transport) as client,
            pytest.raises(KeyboardInterrupt),
        ):
            insightmon.evaluate(answer, dataset="ds", client=client, max_concurrency=1)
        assert _context.held_traces() == frozenset()
        _context.release_traces(None)  # what shutdown() does at exit
    finally:
        provider.shutdown()
    assert platform.reports == {}
    assert not platform.finalized
    assert span_exporter.get_finished_spans() == ()  # never exported as production traces
    assert processor.held_span_count == 0
    assert f"experiment {EXP_ID} was not finalized" in caplog.text


async def test_aevaluate_cancelled_during_the_last_batch_waits_for_it(
    mock_api: MockApi, no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client aevaluate created is closed only after the last report ended; the
    cancellation is raised then, without finalizing."""
    platform = FakePlatform(keys(3)).install(mock_api)
    original = platform.batch
    entered = threading.Event()
    go = threading.Event()

    def batch(request: httpx.Request) -> httpx.Response:
        entered.set()
        go.wait(10)
        return original(request)

    mock_api.routes[("POST", BATCH)] = [batch]

    class RecordingClient(Client):
        def close(self) -> None:
            platform.events.append("close")
            super().close()

    monkeypatch.setattr(
        evaluate_mod,
        "_open_client",
        lambda client, api_key, base_url: RecordingClient(
            BASE_URL, API_KEY, transport=mock_api.transport
        ),
    )
    run = asyncio.ensure_future(insightmon.aevaluate(answer, dataset="ds"))
    deadline = time.monotonic() + 10
    while not entered.is_set():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    run.cancel()
    await asyncio.sleep(0.05)
    assert platform.events == ["create"]  # still reporting, the client still open
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 10)
    assert platform.events == ["create", "batch:3", "close"]
    assert not platform.finalized


# --------------------------------------------------------------------------- arguments and client


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trials": 0},
        {"trials": 11},
        {"max_concurrency": 0},
        {"timeout_s": 0},
        {"timeout_s": math.inf},
        {"wait_timeout_s": -1},
        {"evaluators": [1]},
        {"server_evaluators": [{"evaluator_version_id": ""}]},
        {"server_evaluators": "evv-1"},
        {"name": "x" * 201},
        {"example_keys": "k000"},
        {"dataset": ""},
        {"requested": {"agent": "x"}},
        {"metadata": [1]},
        {"dataset_version": 0},
    ],
)
def test_arguments_are_checked_before_any_request(
    mock_api: MockApi, kwargs: dict[str, Any]
) -> None:
    args: dict[str, Any] = {"dataset": "ds", **kwargs}
    with pytest.raises(ValueError, match=r"\w"):
        insightmon.evaluate(
            answer, client=Client(BASE_URL, API_KEY, transport=mock_api.transport), **args
        )
    assert mock_api.requests == []


def test_task_must_be_callable(mock_api: MockApi) -> None:
    with pytest.raises(ValueError, match="callable"):
        insightmon.evaluate("not a task", dataset="ds")  # type: ignore[arg-type]


def test_missing_configuration_is_a_config_error() -> None:
    with pytest.raises(InsightMonError) as info:
        insightmon.evaluate(answer, dataset="ds")
    assert info.value.code == "config_error"


def test_own_client_is_created_from_arguments_and_closed(
    mock_api: MockApi, monkeypatch: pytest.MonkeyPatch, no_sleep: Any, seams: Any
) -> None:
    FakePlatform(keys(1)).install(mock_api)
    created: list[Any] = []

    class RecordingClient(Client):
        def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
            super().__init__(base_url, api_key, transport=mock_api.transport)
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True
            super().close()

    monkeypatch.setattr(evaluate_mod, "Client", RecordingClient)
    result = insightmon.evaluate(answer, dataset="ds", base_url=BASE_URL, api_key=API_KEY)
    assert result.status == "completed"
    (own,) = created
    assert own.base_url == BASE_URL
    assert own.closed
    assert mock_api.requests[0].headers["authorization"] == f"Bearer {API_KEY}"


def test_given_client_is_not_closed(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1)).install(mock_api)
    insightmon.evaluate(answer, dataset="ds", client=client)
    assert client.get_experiment(EXP_ID)["id"] == EXP_ID


def test_no_examples_still_finalizes(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform([]).install(mock_api)
    result = insightmon.evaluate(answer, dataset="ds", client=client)
    assert result.items == []
    assert platform.events == ["create", "finalize"]


def test_key_never_logged_during_a_run(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    platform = FakePlatform(keys(2)).install(mock_api)
    platform.batch_errors = [error_response(503, "internal_error")]

    def broken(input: Any, output: Any, reference: Any) -> Any:
        raise RuntimeError("boom")

    result = insightmon.evaluate(answer, dataset="ds", evaluators=[broken], client=client)
    assert caplog.records
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(result)


# --------------------------------------------------------------------------- aevaluate


async def test_aevaluate_async_task_and_evaluators(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(4)).install(mock_api)

    async def task(input: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return f"It is sunny in {input['city']}."

    async def short(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"name": "short", "value": len(output["text"]) < 40}

    result = await insightmon.aevaluate(
        task, dataset="ds", trials=2, evaluators=[short, mentions_city], client=client
    )
    assert result.status == "completed"
    assert [(i.example_key, i.trial_index) for i in result.items] == [
        (k, t) for k in keys(4) for t in range(2)
    ]
    assert result.items[0].scores == [
        {"name": "short", "value": True},
        {"name": "mentions_city", "value": True},
    ]
    assert len(platform.reports) == 8
    assert platform.events[-1] == "finalize"


async def test_aevaluate_runs_sync_tasks_in_threads(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(3)).install(mock_api)
    loop_thread = threading.get_ident()
    threads: set[int] = set()

    def task(input: Any, example: Example) -> str:
        threads.add(threading.get_ident())
        return example.example_key

    result = await insightmon.aevaluate(task, dataset="ds", client=client)
    assert loop_thread not in threads
    assert [i.output for i in result.items] == [{"text": k} for k in keys(3)]


async def test_aevaluate_concurrency_limit(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(10)).install(mock_api)
    active = {"now": 0, "max": 0}

    async def task(input: Any) -> str:
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        return "ok"

    await insightmon.aevaluate(task, dataset="ds", max_concurrency=4, client=client)
    assert active["max"] == 4


async def test_aevaluate_timeout_cancels_the_late_run(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(3)).install(mock_api)
    cancelled: list[str] = []

    async def task(input: dict[str, Any]) -> str:
        if input["city"] == "k001":
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(input["city"])
                raise
        if input["city"] == "k002":
            raise asyncio.TimeoutError("the task's own timeout")
        return "ok"

    result = await insightmon.aevaluate(task, dataset="ds", timeout_s=0.05, client=client)
    assert [i.status for i in result.items] == ["ok", "timeout", "task_error"]
    assert result.items[1].error == {"code": "timeout", "message": "no result within 0.05 s"}
    assert result.items[2].error is not None
    assert result.items[2].error["code"] == "exception"
    assert cancelled == ["k001"]
    assert platform.reports[("k001", 0)]["status"] == "timeout"


async def test_aevaluate_task_error(client: Client, mock_api: MockApi) -> None:
    FakePlatform(keys(1)).install(mock_api)

    async def task(input: Any) -> str:
        raise LookupError("missing")

    result = await insightmon.aevaluate(task, dataset="ds", client=client)
    assert result.items[0].status == "task_error"
    assert result.items[0].error == {"code": "exception", "message": "LookupError: missing"}


async def test_aevaluate_cancellation_reports_finished_items(
    client: Client, mock_api: MockApi, seams: dict[str, list[Any]]
) -> None:
    platform = FakePlatform(keys(6)).install(mock_api)
    third_started = asyncio.Event()

    async def task(input: dict[str, Any]) -> str:
        if input["city"] == "k002":
            third_started.set()
            await asyncio.sleep(10)
        return "ok"

    run = asyncio.ensure_future(
        insightmon.aevaluate(task, dataset="ds", max_concurrency=1, client=client)
    )
    await asyncio.wait_for(third_started.wait(), 5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0)]
    assert not platform.finalized
    assert seams["flushed"] == ["discard"]


async def test_aevaluate_report_failure(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(2)).install(mock_api)
    platform.batch_errors = [error_response(403, "permission_denied", {"required": "x"})]
    with pytest.raises(InsightMonError) as info:
        await insightmon.aevaluate(answer, dataset="ds", client=client)
    assert EXP_ID in str(info.value)
    assert not platform.finalized


async def test_aevaluate_wait_uses_async_sleep(
    client: Client, mock_api: MockApi, no_sleep: list[float]
) -> None:
    FakePlatform(keys(1), polls_until_done=3).install(mock_api)
    result = await insightmon.aevaluate(answer, dataset="ds", client=client)
    assert result.status == "completed"
    assert no_sleep == [1.0, 2.0, 4.0]


async def test_aevaluate_wait_false(client: Client, mock_api: MockApi) -> None:
    platform = FakePlatform(keys(1)).install(mock_api)
    result = await insightmon.aevaluate(answer, dataset="ds", wait=False, client=client)
    assert result.status == "running"
    assert platform.polls == 0


async def test_aevaluate_validates_before_requests(mock_api: MockApi) -> None:
    with pytest.raises(ValueError, match="trials"):
        await insightmon.aevaluate(
            answer,
            dataset="ds",
            trials=0,
            client=Client(BASE_URL, API_KEY, transport=mock_api.transport),
        )
    assert mock_api.requests == []


class NoDefaultExecutor(ThreadPoolExecutor):
    """Fails any use of the event loop's default executor."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        raise AssertionError("the loop's default executor was used")


async def test_aevaluate_never_waits_on_the_default_executor(
    client: Client, mock_api: MockApi
) -> None:
    """Hung sync tasks (more than any executor has workers) cannot block the SDK's calls."""
    platform = FakePlatform(keys(12)).install(mock_api)
    asyncio.get_running_loop().set_default_executor(NoDefaultExecutor(max_workers=1))
    hang = threading.Event()

    def task(input: dict[str, Any]) -> str:
        if input["city"] != "k000":
            hang.wait(10)
        return "ok"

    def judge(input: Any, output: Any, reference: Any) -> dict[str, Any]:
        return {"name": "length", "value": 1}

    try:
        result = await asyncio.wait_for(
            insightmon.aevaluate(
                task,
                dataset="ds",
                evaluators=[judge],
                timeout_s=0.2,
                max_concurrency=12,
                client=client,
            ),
            10,
        )
    finally:
        hang.set()
    assert [i.status for i in result.items] == ["ok"] + ["timeout"] * 11
    assert result.items[0].scores == [{"name": "length", "value": 1}]
    assert platform.finalized
    wait_for_task_threads()


async def test_aevaluate_timeout_counts_only_the_task_time(
    client: Client, mock_api: MockApi
) -> None:
    """max_concurrency larger than the default executor: no run waits in a queue, so none is
    reported as a timeout it never had."""
    FakePlatform(keys(8)).install(mock_api)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
    bodies = Bodies()

    def task(input: dict[str, Any]) -> str:
        with bodies.running():
            time.sleep(0.3)
        return "ok"

    result = await insightmon.aevaluate(
        task, dataset="ds", timeout_s=1.0, max_concurrency=8, client=client
    )
    assert [i.status for i in result.items] == ["ok"] * 8
    assert bodies.max == 8


async def test_aevaluate_timed_out_sync_tasks_keep_their_place(
    client: Client, mock_api: MockApi
) -> None:
    platform = FakePlatform(keys(6)).install(mock_api)
    hang = threading.Event()
    bodies = Bodies()

    def task(input: dict[str, Any]) -> str:
        with bodies.running():
            hang.wait(10)
        return "late"

    try:
        with pytest.raises(RuntimeError, match=EXP_ID):
            await asyncio.wait_for(
                insightmon.aevaluate(
                    task, dataset="ds", timeout_s=0.2, max_concurrency=2, client=client
                ),
                10,
            )
    finally:
        hang.set()
    assert bodies.max == 2
    assert sorted(platform.reports) == [("k000", 0), ("k001", 0)]
    assert not platform.finalized
    wait_for_task_threads()
