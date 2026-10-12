"""`insightmon._batch.ItemBatcher`."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx
import pytest

from insightmon import _batch, _http
from insightmon._batch import ItemBatcher
from insightmon._errors import InsightMonError
from insightmon.client import Client
from tests.conftest import API_KEY, BASE_URL, MockApi, error_response

PATH = "/api/v1/experiments/exp-1/items:batch"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(_http, "_sleep", recorded.append)
    return recorded


@pytest.fixture
def client(mock_api: MockApi, sleeps: list[float]) -> Iterator[Client]:
    with Client(BASE_URL, API_KEY, transport=mock_api.transport) as c:
        yield c


def accept_all(request: httpx.Request) -> httpx.Response:
    items = json.loads(request.content)["items"]
    return httpx.Response(
        200,
        json={
            "accepted": len(items),
            "duplicates": 0,
            "item_ids": [f"{i['example_key']}/{i['trial_index']}" for i in items],
        },
    )


def item(key: str, trial: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "example_key": key,
        "trial_index": trial,
        "status": "ok",
        "output": {"text": key},
        **extra,
    }


def sent_batches(mock_api: MockApi) -> list[list[str]]:
    return [
        [i["example_key"] for i in body["items"]] for body in mock_api.json_bodies("POST", PATH)
    ]


def test_count_threshold(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    reported: list[list[str]] = []
    batcher = ItemBatcher(
        client,
        "exp-1",
        max_items=3,
        clock=FakeClock(),
        timer=False,
        on_reported=lambda items: reported.append([i["example_key"] for i in items]),
    )
    for key in "abcdefg":
        batcher.add(item(key))
    assert sent_batches(mock_api) == [["a", "b", "c"], ["d", "e", "f"]]
    assert batcher.pending == 1
    batcher.close()
    assert sent_batches(mock_api) == [["a", "b", "c"], ["d", "e", "f"], ["g"]]
    assert reported == sent_batches(mock_api)
    assert (batcher.accepted, batcher.duplicates, batcher.reported) == (7, 0, 7)


def test_time_threshold_on_add_and_flush_if_due(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    clock = FakeClock()
    batcher = ItemBatcher(
        client, "exp-1", max_items=50, max_interval_s=5.0, clock=clock, timer=False
    )
    batcher.add(item("a"))
    clock.now += 4.9
    assert batcher.flush_if_due() is False
    batcher.add(item("b"))
    assert sent_batches(mock_api) == []
    clock.now += 0.1  # the oldest item is 5 s old
    batcher.add(item("c"))
    assert sent_batches(mock_api) == [["a", "b", "c"]]
    batcher.add(item("d"))
    clock.now += 5.0
    assert batcher.flush_if_due() is True
    assert sent_batches(mock_api) == [["a", "b", "c"], ["d"]]
    assert batcher.flush_if_due() is False
    batcher.close()
    assert len(sent_batches(mock_api)) == 2


def test_timer_thread_flushes_old_items(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    sent = threading.Event()
    batcher = ItemBatcher(
        client, "exp-1", max_items=50, max_interval_s=0.05, on_reported=lambda _items: sent.set()
    )
    batcher.add(item("a"))
    assert sent.wait(5.0)
    assert sent_batches(mock_api) == [["a"]]
    batcher.close()
    assert sent_batches(mock_api) == [["a"]]
    assert batcher._timer is not None
    assert not batcher._timer.is_alive()


def test_flush_close_and_idempotent_close(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    batcher = ItemBatcher(client, "exp-1", timer=False)
    batcher.flush()  # nothing pending: no request
    assert mock_api.requests == []
    batcher.add(item("a"))
    batcher.flush()
    batcher.close()
    batcher.close()
    assert sent_batches(mock_api) == [["a"]]
    with pytest.raises(RuntimeError, match="closed"):
        batcher.add(item("b"))


def test_context_manager_flushes(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    with ItemBatcher(client, "exp-1", timer=False) as batcher:
        batcher.add(item("a"))
    assert sent_batches(mock_api) == [["a"]]


def test_size_split_inside_one_flush(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    big = {"text": "x" * 1000}
    reported: list[int] = []
    batcher = ItemBatcher(
        client,
        "exp-1",
        max_items=10,
        max_body_bytes=2500,
        timer=False,
        on_reported=lambda items: reported.append(len(items)),
    )
    for key in "abcde":
        batcher.add({**item(key), "output": big})
    batcher.flush()
    assert sent_batches(mock_api) == [["a", "b"], ["c", "d"], ["e"]]
    assert reported == [2, 2, 1]


def test_retry_on_429_and_503_resends_identical_content(
    client: Client, mock_api: MockApi, sleeps: list[float]
) -> None:
    mock_api.add(
        "POST",
        PATH,
        error_response(429, "rate_limited", {"retry_after_s": 1}, headers={"Retry-After": "1"}),
        error_response(503, "internal_error"),
        accept_all,
    )
    batcher = ItemBatcher(client, "exp-1", max_items=2, timer=False)
    batcher.add(item("a", scores=[{"name": "short", "value": True}]))
    batcher.add(item("b"))
    bodies = [r.content for r in mock_api.calls("POST", PATH)]
    assert len(bodies) == 3
    assert len(set(bodies)) == 1
    assert sleeps[0] == 1.0
    assert batcher.accepted == 2


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "external_item_invalid"), (403, "permission_denied"), (409, "invalid_state")],
)
def test_failed_request_is_kept_and_raised_again(
    client: Client, mock_api: MockApi, status: int, code: str
) -> None:
    mock_api.add(
        "POST",
        PATH,
        error_response(status, code, {"example_key": "a", "reason": "already_reported"}),
    )
    reported: list[Any] = []
    batcher = ItemBatcher(client, "exp-1", max_items=1, timer=False, on_reported=reported.append)
    with pytest.raises(InsightMonError) as first:
        batcher.add(item("a"))
    assert first.value.code == code
    assert batcher.error is first.value
    with pytest.raises(InsightMonError) as again:
        batcher.add(item("b"))
    assert again.value is first.value
    with pytest.raises(InsightMonError):
        batcher.flush()
    assert len(mock_api.requests) == 1  # no retry of a 4xx, nothing sent after the failure
    assert reported == []


def test_score_invalid_drops_the_items_scores_and_resends(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    def platform(request: httpx.Request) -> httpx.Response:
        for entry in json.loads(request.content)["items"]:
            if any(s["name"] == "unknown" for s in entry.get("scores", [])):
                return error_response(
                    400,
                    "external_item_invalid",
                    {"example_key": entry["example_key"], "reason": "score_invalid"},
                )
        return accept_all(request)

    mock_api.add("POST", PATH, platform)
    reported: list[Sequence[Mapping[str, Any]]] = []
    batcher = ItemBatcher(client, "exp-1", max_items=10, timer=False, on_reported=reported.append)
    good = [{"name": "short", "value": True}]
    bad = [{"name": "unknown", "value": 1}]
    batcher.add(item("a", 0, scores=bad))
    batcher.add(item("c", 0, scores=bad))
    batcher.add(item("b", 0, scores=good))
    batcher.flush()
    bodies = mock_api.json_bodies("POST", PATH)
    assert len(bodies) == 3  # first attempt, then once per refused item
    final = bodies[-1]["items"]
    assert [("scores" in i) for i in final] == [False, False, True]
    assert final[2]["scores"] == good
    (sent,) = reported
    assert [("scores" in i) for i in sent] == [False, False, True]
    assert "unknown" in caplog.text
    assert batcher.error is None


def test_repeatedly_refused_score_names_are_dropped_for_the_rest_of_the_run(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    def platform(request: httpx.Request) -> httpx.Response:
        for entry in json.loads(request.content)["items"]:
            if any(s["name"] == "unknown" for s in entry.get("scores", [])):
                return error_response(
                    400,
                    "external_item_invalid",
                    {"example_key": entry["example_key"], "reason": "score_invalid"},
                )
        return accept_all(request)

    mock_api.add("POST", PATH, platform)
    caplog.set_level(logging.DEBUG, logger="insightmon")
    batcher = ItemBatcher(client, "exp-1", max_items=6, timer=False)
    bad = [{"name": "unknown", "value": 1}]
    good = [{"name": "short", "value": True}]
    for key in "abcde":
        batcher.add(item(key, scores=bad))
    batcher.add(item("f", scores=good))  # sends the six
    assert len(mock_api.requests) == 1 + _batch.REFUSALS_BEFORE_DROP
    final = mock_api.json_bodies("POST", PATH)[-1]["items"]
    assert [i.get("scores") for i in final] == [None] * 5 + [good]

    batcher.add(item("g", scores=bad))  # a later request: dropped up front
    batcher.flush()
    assert len(mock_api.requests) == 2 + _batch.REFUSALS_BEFORE_DROP
    assert "scores" not in mock_api.json_bodies("POST", PATH)[-1]["items"][0]

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # the first refusal, then the drop
    assert "refused the client scores ['unknown']" in warnings[0]
    assert "refused 3 times" in warnings[1]
    assert batcher.reported == 7


def value_checking_platform(
    *, with_trial: bool = False, requests: list[list[tuple[str, int]]] | None = None
) -> Any:
    """Refuses the first item whose `accuracy` score is above 1 (a published config with the
    range [0, 1]), naming its example key (and trial index with `with_trial`)."""

    def platform(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["items"]
        if requests is not None:
            requests.append([(i["example_key"], i["trial_index"]) for i in items])
        for entry in items:
            if any(s["value"] > 1 for s in entry.get("scores", [])):
                params = {"example_key": entry["example_key"], "reason": "score_invalid"}
                if with_trial:
                    params["trial_index"] = entry["trial_index"]
                return error_response(400, "external_item_invalid", params)
        return accept_all(request)

    return platform


def scored(value: float) -> list[dict[str, Any]]:
    return [{"name": "accuracy", "value": value}]


def test_a_refused_trial_never_costs_another_trial_its_scores(
    client: Client, mock_api: MockApi
) -> None:
    """The platform names only the example: the batcher finds which trial was refused instead
    of dropping the first trial's valid scores."""
    mock_api.add("POST", PATH, value_checking_platform())
    reported: list[Mapping[str, Any]] = []
    batcher = ItemBatcher(
        client, "exp-1", max_items=10, timer=False, on_reported=lambda items: reported.extend(items)
    )
    batcher.add(item("a", 0, scores=scored(0.8)))
    batcher.add(item("a", 1, scores=scored(1.5)))
    batcher.add(item("a", 2, scores=scored(0.4)))
    batcher.add(item("b", 0, scores=scored(0.9)))
    batcher.flush()
    final = {(i["example_key"], i["trial_index"]): i.get("scores") for i in reported}
    assert final == {
        ("a", 0): scored(0.8),
        ("a", 1): None,
        ("a", 2): scored(0.4),
        ("b", 0): scored(0.9),
    }
    assert batcher._refusals == {frozenset({"accuracy"}): 1}  # only the refused trial counts
    assert batcher.reported == 4
    assert batcher.error is None


def test_a_refusal_with_a_trial_index_names_the_item(client: Client, mock_api: MockApi) -> None:
    requests: list[list[tuple[str, int]]] = []
    mock_api.add("POST", PATH, value_checking_platform(with_trial=True, requests=requests))
    batcher = ItemBatcher(client, "exp-1", max_items=10, timer=False)
    batcher.add(item("a", 0, scores=scored(0.8)))
    batcher.add(item("a", 1, scores=scored(1.5)))
    batcher.flush()
    assert requests == [[("a", 0), ("a", 1)]] * 2  # no need to tell the trials apart
    final = mock_api.json_bodies("POST", PATH)[-1]["items"]
    assert [i.get("scores") for i in final] == [scored(0.8), None]


def test_refusals_of_varied_score_names_cost_a_bounded_number_of_requests(
    client: Client, mock_api: MockApi
) -> None:
    """Per-item score names that are not published (a different set on every item) never
    reach the run-wide drop; the requests of one chunk stay bounded all the same."""

    def platform(request: httpx.Request) -> httpx.Response:
        for entry in json.loads(request.content)["items"]:
            if any(s["name"].startswith("tool_") for s in entry.get("scores", [])):
                return error_response(
                    400,
                    "external_item_invalid",
                    {"example_key": entry["example_key"], "reason": "score_invalid"},
                )
        return accept_all(request)

    mock_api.add("POST", PATH, platform)
    batcher = ItemBatcher(client, "exp-1", max_items=50, timer=False)
    for index in range(20):
        batcher.add(item(f"k{index:02d}", scores=[{"name": f"tool_{index}", "value": 1}]))
    batcher.add(item("good", scores=[{"name": "short", "value": True}]))
    batcher.flush()
    bodies = mock_api.json_bodies("POST", PATH)
    sizes = [len(body["items"]) for body in bodies]
    full = [size for size in sizes if size > 1]
    assert len(full) <= _batch.REFUSALS_PER_REQUEST + 2  # never one full resend per refusal
    assert sum(sizes) <= 5 * 21  # before: 21 requests of 21 items
    reported = {i["example_key"]: i.get("scores") for body in bodies for i in body["items"]}
    assert reported["good"] == [{"name": "short", "value": True}]
    assert batcher.reported == 21
    assert batcher.error is None


def test_a_score_name_the_platform_accepted_is_never_dropped_up_front(
    client: Client, mock_api: MockApi
) -> None:
    """Refusals of an accepted name are about values: later items keep their scores."""
    mock_api.add("POST", PATH, value_checking_platform())
    batcher = ItemBatcher(client, "exp-1", max_items=1, timer=False)
    batcher.add(item("ok", scores=scored(0.5)))
    for key in "abc":
        batcher.add(item(key, scores=scored(2.0)))
    batcher.add(item("later", scores=scored(0.7)))
    final = mock_api.json_bodies("POST", PATH)[-1]["items"]
    assert final == [item("later", scores=scored(0.7))]


def listing_platform(requests: list[list[tuple[str, int]]] | None = None) -> Any:
    """A platform that lists every offending score of the request in `invalid_scores`: the
    published names are `short` and `accuracy` (range [0, 1])."""

    def invalid(score: Mapping[str, Any]) -> bool:
        if score["name"] == "accuracy":
            return not 0 <= score["value"] <= 1
        return score["name"] != "short"

    def platform(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["items"]
        if requests is not None:
            requests.append([(i["example_key"], i["trial_index"]) for i in items])
        listed = [
            {"example_key": i["example_key"], "trial_index": i["trial_index"], "name": s["name"]}
            for i in items
            for s in i.get("scores", [])
            if invalid(s)
        ]
        if listed:
            first = {k: listed[0][k] for k in ("example_key", "trial_index")}
            params = {**first, "reason": "score_invalid", "invalid_scores": listed}
            return error_response(400, "external_item_invalid", params)
        return accept_all(request)

    return platform


def test_listed_invalid_scores_are_dropped_in_one_resend(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[list[tuple[str, int]]] = []
    mock_api.add("POST", PATH, listing_platform(requests))
    reported: list[Mapping[str, Any]] = []
    batcher = ItemBatcher(
        client, "exp-1", max_items=10, timer=False, on_reported=lambda items: reported.extend(items)
    )
    short = {"name": "short", "value": True}
    batcher.add(item("a", 0, scores=[{"name": "unknown", "value": 1}, short]))
    batcher.add(item("a", 1, scores=[*scored(1.5), short]))
    batcher.add(item("a", 2, scores=scored(0.4)))
    batcher.add(item("b", 0, scores=[{"name": "unknown", "value": 2}]))
    batcher.flush()
    everything = [("a", 0), ("a", 1), ("a", 2), ("b", 0)]
    assert requests == [everything, everything]  # trials are told apart by the platform
    final = {(i["example_key"], i["trial_index"]): i.get("scores") for i in reported}
    assert final == {
        ("a", 0): [short],  # only the listed score is dropped
        ("a", 1): [short],
        ("a", 2): scored(0.4),
        ("b", 0): None,
    }
    assert batcher._refusals == {frozenset({"unknown"}): 2, frozenset({"accuracy"}): 1}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # one per name
    assert batcher.error is None


def test_a_listed_name_refused_repeatedly_is_dropped_for_the_rest_of_the_run(
    client: Client, mock_api: MockApi
) -> None:
    mock_api.add("POST", PATH, listing_platform())
    batcher = ItemBatcher(client, "exp-1", max_items=1, timer=False)
    short = {"name": "short", "value": True}
    for key in "abcd":
        batcher.add(item(key, scores=[{"name": "unknown", "value": 1}, short]))
    assert len(mock_api.requests) == 2 * _batch.REFUSALS_BEFORE_DROP + 1
    assert mock_api.json_bodies("POST", PATH)[-1]["items"] == [item("d", scores=[short])]


def test_score_invalid_for_an_item_without_scores_raises(client: Client, mock_api: MockApi) -> None:
    mock_api.add(
        "POST",
        PATH,
        error_response(
            400, "external_item_invalid", {"example_key": "a", "reason": "score_invalid"}
        ),
    )
    batcher = ItemBatcher(client, "exp-1", timer=False)
    batcher.add(item("a"))
    with pytest.raises(InsightMonError):
        batcher.flush()
    assert len(mock_api.requests) == 1


def test_oversized_item_is_an_error(client: Client, mock_api: MockApi) -> None:
    batcher = ItemBatcher(client, "exp-1", max_body_bytes=200, timer=False)
    batcher.add({**item("a"), "output": {"text": "x" * 500}})
    with pytest.raises(ValueError, match="alone"):
        batcher.flush()
    assert isinstance(batcher.error, ValueError)
    assert mock_api.requests == []


def test_callback_errors_do_not_break_reporting(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)

    def broken(_items: Sequence[Mapping[str, Any]]) -> None:
        raise RuntimeError("boom")

    batcher = ItemBatcher(client, "exp-1", max_items=1, timer=False, on_reported=broken)
    batcher.add(item("a"))
    batcher.add(item("b"))
    assert batcher.reported == 2
    assert batcher.error is None


def test_interrupted_send_keeps_the_items(client: Client, mock_api: MockApi) -> None:
    calls = {"n": 0}

    def interrupt_once(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return accept_all(request)

    mock_api.add("POST", PATH, interrupt_once)
    batcher = ItemBatcher(client, "exp-1", timer=False)
    batcher.add(item("a"))
    with pytest.raises(KeyboardInterrupt):
        batcher.flush()
    assert batcher.pending == 1
    assert batcher.error is None
    batcher.flush()
    assert batcher.reported == 1


def test_concurrent_adds_report_every_item_once(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, accept_all)
    batcher = ItemBatcher(client, "exp-1", max_items=7, max_interval_s=0.01)

    def worker(n: int) -> None:
        for i in range(25):
            batcher.add(item(f"w{n}-{i}"))
            if i % 10 == 0:
                time.sleep(0.002)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    batcher.close()
    keys = [k for batch in sent_batches(mock_api) for k in batch]
    assert sorted(keys) == sorted(f"w{n}-{i}" for n in range(4) for i in range(25))
    assert all(len(batch) <= 7 for batch in sent_batches(mock_api))
    assert batcher.reported == 100


def test_argument_validation(client: Client) -> None:
    with pytest.raises(ValueError, match="max_items"):
        ItemBatcher(client, "exp-1", max_items=0)
    with pytest.raises(ValueError, match="max_items"):
        ItemBatcher(client, "exp-1", max_items=201)
    with pytest.raises(ValueError, match="max_interval_s"):
        ItemBatcher(client, "exp-1", max_interval_s=0)
