"""`insightmon.Client` against `httpx.MockTransport`."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from insightmon import _config, _http
from insightmon._config import Component, RuntimeState, Settings
from insightmon._errors import CONFIG_ERROR, UNEXPECTED_RESPONSE, InsightMonError
from insightmon.client import (
    ApiKeyInfo,
    Client,
    Example,
    ScoreConfig,
    component_refs,
    normalize_evaluators,
    normalize_requested,
    plan_requests,
    published_score_names,
)
from tests.conftest import API_KEY, BASE_URL, MockApi, error_response

EXP = "/api/v1/experiments"
EXPERIMENT = {"id": "exp-1", "project_id": "proj-1", "status": "running", "trials": 1}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(_http, "_sleep", recorded.append)
    return recorded


@pytest.fixture
def client(mock_api: MockApi, sleeps: list[float]) -> Iterator[Client]:
    with Client(BASE_URL, API_KEY, transport=mock_api.transport) as c:
        yield c


def example_json(key: str, **extra: Any) -> dict[str, Any]:
    return {
        "example_key": key,
        "example_id": f"id-{key}",
        "input": {"city": key},
        "initial_state": {},
        "reference": {"city": key},
        "language": "en",
        "metadata": {},
        "trials": 1,
        **extra,
    }


# --------------------------------------------------------------------------- configuration


def test_explicit_arguments(mock_api: MockApi) -> None:
    client = Client(f"{BASE_URL}/", f"  {API_KEY} ", transport=mock_api.transport)
    assert client.base_url == BASE_URL


def test_environment_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSIGHTMON_BASE_URL", "https://env.example.test")
    monkeypatch.setenv("INSIGHTMON_API_KEY", "imk_from_env")
    assert Client().base_url == "https://env.example.test"
    assert Client(base_url=BASE_URL).base_url == BASE_URL


def test_init_settings_come_before_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSIGHTMON_BASE_URL", "https://env.example.test")
    monkeypatch.setenv("INSIGHTMON_API_KEY", "imk_from_env")
    settings = Settings(base_url="https://init.example.test", api_key="imk_from_init")
    _config.set_runtime(RuntimeState(settings=settings))
    assert Client().base_url == "https://init.example.test"


@pytest.mark.parametrize(
    ("base_url", "api_key", "fragment"),
    [
        (None, API_KEY, "base URL"),
        (BASE_URL, None, "API key"),
        ("ftp://host", API_KEY, "http(s)"),
        (BASE_URL, "sk_wrong", "imk_"),
    ],
)
def test_config_errors(base_url: str | None, api_key: str | None, fragment: str) -> None:
    with pytest.raises(InsightMonError) as info:
        Client(base_url, api_key)
    assert info.value.code == CONFIG_ERROR
    assert info.value.status is None
    assert fragment in info.value.message


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [("imk_live_SECRET123", None), ("imk_live_SECRET123", "https://eval.example.test")],
)
def test_a_key_given_as_the_base_url_is_never_repeated(
    base_url: str, api_key: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INSIGHTMON_API_KEY", "imk_from_env")
    with pytest.raises(InsightMonError) as info:
        Client(base_url, api_key)
    assert "looks like an API key" in str(info.value)
    assert "SECRET123" not in str(info.value)
    assert "SECRET123" not in repr(info.value)


def test_repr_hides_the_key(client: Client) -> None:
    assert repr(client) == f"Client(base_url={BASE_URL!r})"
    assert API_KEY not in repr(client)
    assert API_KEY not in repr(client.__dict__)


# --------------------------------------------------------------------------- api key


def test_api_key_info(client: Client, mock_api: MockApi) -> None:
    mock_api.add(
        "GET",
        "/api/v1/api-key",
        {
            "id": "k1",
            "name": "ci",
            "prefix": "imk_ab",
            "project_id": "proj-1",
            "scopes": ["experiment_write", "trace_ingest"],
        },
    )
    info = client.api_key_info()
    assert info == ApiKeyInfo("k1", "ci", "imk_ab", "proj-1", ("experiment_write", "trace_ingest"))
    assert info.has_scope("trace_ingest")
    assert not info.has_scope("score_write")


def test_api_key_info_unexpected_body(client: Client, mock_api: MockApi) -> None:
    mock_api.add("GET", "/api/v1/api-key", {"id": "k1"})
    with pytest.raises(InsightMonError) as info:
        client.api_key_info()
    assert info.value.code == UNEXPECTED_RESPONSE
    mock_api.routes.clear()
    mock_api.add("GET", "/api/v1/api-key", [1, 2])
    with pytest.raises(InsightMonError) as info:
        client.api_key_info()
    assert info.value.code == UNEXPECTED_RESPONSE


def test_not_authenticated_maps_to_error(client: Client, mock_api: MockApi) -> None:
    mock_api.add("GET", "/api/v1/api-key", error_response(401, "not_authenticated"))
    with pytest.raises(InsightMonError) as info:
        client.api_key_info()
    assert (info.value.code, info.value.status) == ("not_authenticated", 401)


# --------------------------------------------------------------------------- scores


def score(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "name": "thumbs_up",
        "source": "end_user",
        "subject": {"type": "thread", "thread_key": f"chat-{i}"},
        "value": True,
        **extra,
    }


def echo_scores(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    items = [
        {"index": i, "id": f"s{i}", "idempotency_key": s["idempotency_key"], "created": True}
        for i, s in enumerate(body["scores"])
    ]
    return httpx.Response(200, json={"items": items})


def test_scores_batch_fills_idempotency_keys_and_chunks(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", "/api/v1/scores:batch", echo_scores)
    scores = [score(i) for i in range(1203)]
    scores[7]["idempotency_key"] = "mine-7"
    items = client.scores_batch(scores)
    bodies = mock_api.json_bodies("POST", "/api/v1/scores:batch")
    assert [len(b["scores"]) for b in bodies] == [500, 500, 203]
    assert [item["index"] for item in items] == list(range(1203))
    sent = [s for b in bodies for s in b["scores"]]
    keys = [s["idempotency_key"] for s in sent]
    assert keys[7] == "mine-7"
    assert len(set(keys)) == 1203
    assert all(1 <= len(k) <= 128 for k in keys)
    assert "idempotency_key" not in scores[0]  # the caller's mappings are not changed


def test_scores_batch_retry_resends_the_same_keys(
    client: Client, mock_api: MockApi, sleeps: list[float]
) -> None:
    mock_api.add(
        "POST",
        "/api/v1/scores:batch",
        error_response(503, "internal_error"),
        httpx.ReadTimeout("slow"),
        echo_scores,
    )
    client.scores_batch([score(1), score(2)])
    bodies = [r.content for r in mock_api.calls("POST", "/api/v1/scores:batch")]
    assert len(bodies) == 3
    assert len(set(bodies)) == 1
    assert len(sleeps) == 2


def test_scores_batch_failure_tells_how_to_retry_without_duplicates(
    client: Client, mock_api: MockApi
) -> None:
    """The second chunk fails after the first was written: the error carries every key and
    the count written, and a retry with those keys writes nothing twice."""
    stored: dict[str, dict[str, Any]] = {}
    fail = {"next": True}

    def platform(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["scores"][0]["idempotency_key"] not in stored and len(stored) and fail["next"]:
            fail["next"] = False
            return error_response(400, "score_config_not_found", {"name": "x", "index": 3})
        items = []
        for index, entry in enumerate(body["scores"]):
            created = entry["idempotency_key"] not in stored
            stored.setdefault(entry["idempotency_key"], entry)
            items.append({"index": index, "id": entry["idempotency_key"], "created": created})
        return httpx.Response(200, json={"items": items})

    mock_api.add("POST", "/api/v1/scores:batch", platform)
    scores = [score(i) for i in range(700)]
    with pytest.raises(InsightMonError) as info:
        client.scores_batch(scores)
    params = info.value.params
    assert params["written"] == 500
    assert params["index"] == 503  # the position in `scores`
    assert params["name"] == "x"
    keys = params["idempotency_keys"]
    assert len(keys) == 700
    assert len(stored) == 500
    retried = client.scores_batch(
        [{**s, "idempotency_key": key} for s, key in zip(scores, keys, strict=True)]
    )
    assert len(stored) == 700  # the first chunk was not written twice
    assert sum(1 for item in retried if item["created"]) == 200


def test_scores_batch_empty_sends_nothing(client: Client, mock_api: MockApi) -> None:
    assert client.scores_batch([]) == []
    assert mock_api.requests == []


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ({"source": "human"}, "source"),
        ({"source": "llm_judge"}, "source"),
        ({"metadata": {"adjudicated": True}}, "adjudicated"),
        ({"metadata": {"accepted_by": "u-1"}}, "accepted_by"),
        ({"metadata": {"mt_assisted": True}}, "mt_assisted"),
        ({"metadata": {"self_review": True}}, "self_review"),
        ({"metadata": {"verification_id": "v-1"}}, "verification_id"),
        ({"idempotency_key": ""}, "idempotency_key"),
        ({"idempotency_key": "x" * 129}, "idempotency_key"),
        ({"metadata": [1]}, "metadata"),
    ],
)
def test_scores_batch_refuses_what_a_key_may_not_write(
    client: Client, mock_api: MockApi, bad: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        client.scores_batch([score(0), score(1, **bad)])
    assert mock_api.requests == []


def test_scores_batch_platform_error(client: Client, mock_api: MockApi) -> None:
    mock_api.add(
        "POST",
        "/api/v1/scores:batch",
        error_response(400, "score_config_not_found", {"name": "thumbs_up", "index": 0}),
    )
    with pytest.raises(InsightMonError) as info:
        client.scores_batch([score(0)])
    assert info.value.code == "score_config_not_found"
    assert len(mock_api.requests) == 1


# --------------------------------------------------------------------------- score configs

SCORE_CONFIGS = "/api/v1/score-configs"
CONFIGS = {
    "items": [
        {
            "name": "helpful",
            "data_type": "categorical",
            "labels": ["yes", "no"],
            "min": None,
            "max": None,
            "subject_levels": ["span", "trace"],
            "allow_unknown": True,
        },
        {
            "name": "quality",
            "data_type": "numeric",
            "labels": None,
            "min": 0,
            "max": 1,
            "subject_levels": ["experiment_item"],
            "allow_unknown": False,
        },
    ]
}


def test_score_configs(client: Client, mock_api: MockApi) -> None:
    mock_api.add("GET", SCORE_CONFIGS, CONFIGS)
    assert client.score_configs() == [
        ScoreConfig("helpful", "categorical", ("yes", "no"), None, None, ("span", "trace"), True),
        ScoreConfig("quality", "numeric", None, 0.0, 1.0, ("experiment_item",), False),
    ]
    assert published_score_names(client, ["experiment_item"]) == {"quality"}
    assert published_score_names(client, ["span", "thread"]) == {"helpful"}


@pytest.mark.parametrize(
    "answer",
    [
        error_response(404, "not_found", {"resource": "route"}),  # a platform without the route
        error_response(403, "permission_denied"),  # a key without its scopes
        httpx.Response(200, json={"configs": []}),
        httpx.Response(200, json={"items": [{"name": "x"}]}),
    ],
)
def test_published_score_names_unknown(
    client: Client, mock_api: MockApi, answer: httpx.Response
) -> None:
    mock_api.add("GET", SCORE_CONFIGS, answer)
    assert published_score_names(client, ["experiment_item"]) is None
    assert len(mock_api.requests) == 1  # never retried


# --------------------------------------------------------------------------- experiments


def key_refused() -> httpx.Response:
    """What a platform that predates `idempotency_key` answers a keyed create (422, nothing
    created)."""
    return error_response(
        422,
        "validation_error",
        {
            "errors": [
                {
                    "loc": ["body", "idempotency_key"],
                    "type": "extra_forbidden",
                    "msg": "Extra inputs are not permitted",
                }
            ]
        },
    )


def created() -> httpx.Response:
    return httpx.Response(201, json={"experiment": EXPERIMENT})


def test_create_experiment_body(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", EXP, httpx.Response(201, json={"experiment": EXPERIMENT, "examples": ""}))
    experiment = client.create_experiment(
        name="nightly",
        dataset_id="ds-1",
        dataset_version=3,
        example_keys=["a", "b"],
        trials=2,
        requested={
            "agent_version": "1.4.0",
            "components": [
                Component("prompt", "system", "v7", content_hash="sha256:x"),
                {"type": "tool", "name": "forecast", "version": "2"},
            ],
        },
        evaluators=[
            {"evaluator_version_id": "ev-1"},
            {"evaluator_version_id": "ev-2", "role": "diagnostic"},
        ],
        metadata={"branch": "main"},
    )
    assert experiment == EXPERIMENT
    (body,) = mock_api.json_bodies("POST", EXP)
    key = body.pop("idempotency_key")
    assert isinstance(key, str)
    assert len(key) == 32
    assert body == {
        "name": "nightly",
        "dataset_id": "ds-1",
        "trials": 2,
        "triggered_by": "sdk",
        "dataset_version": 3,
        "example_keys": ["a", "b"],
        "requested": {
            "agent_version": "1.4.0",
            "components": [
                {"type": "prompt", "name": "system", "version": "v7"},
                {"type": "tool", "name": "forecast", "version": "2"},
            ],
        },
        "evaluators": [
            {"evaluator_version_id": "ev-1", "role": "gating"},
            {"evaluator_version_id": "ev-2", "role": "diagnostic"},
        ],
        "metadata": {"branch": "main"},
    }


def test_create_experiment_minimal_body(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", EXP, created())
    client.create_experiment(name="n", dataset_id="ds-1", idempotency_key="run-1")
    client.create_experiment(name="n", dataset_id="ds-1")
    first, second = mock_api.json_bodies("POST", EXP)
    assert first == {
        "name": "n",
        "dataset_id": "ds-1",
        "trials": 1,
        "triggered_by": "sdk",
        "idempotency_key": "run-1",
    }
    assert second["idempotency_key"] != "run-1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "x" * 201},
        {"dataset_id": "x" * 37},
        {"trials": 0},
        {"trials": 11},
        {"trials": True},
        {"dataset_version": 0},
        {"example_keys": "abc"},
        {"example_keys": []},
        {"example_keys": ["x" * 101]},
        {"example_keys": [f"k{i}" for i in range(5001)]},
        {"evaluators": [{"evaluator_version_id": "e", "role": "boss"}]},
        {"evaluators": [{"evaluator_version_id": f"e{i}"} for i in range(21)]},
        {"evaluators": [{"id": "e"}]},
        {"requested": {"agent_version": "1", "extra": 1}},
        {"requested": {"components": [{"type": "prompt", "name": "a"}] * 2}},
        {"triggered_by": "human"},
        {"metadata": [1]},
        {"idempotency_key": ""},
        {"idempotency_key": "x" * 129},
    ],
)
def test_create_experiment_validates_locally(
    client: Client, mock_api: MockApi, kwargs: dict[str, Any]
) -> None:
    args: dict[str, Any] = {"name": "n", "dataset_id": "ds-1", **kwargs}
    with pytest.raises(ValueError, match=r"\w"):
        client.create_experiment(**args)
    assert mock_api.requests == []


def test_create_experiment_retries_connect_error_and_rate_limit(
    client: Client, mock_api: MockApi, sleeps: list[float]
) -> None:
    mock_api.add(
        "POST",
        EXP,
        httpx.ConnectError("refused"),
        error_response(429, "rate_limited", {"retry_after_s": 7}, headers={"Retry-After": "7"}),
        created(),
    )
    assert client.create_experiment(name="n", dataset_id="ds-1")["id"] == "exp-1"
    assert len(mock_api.requests) == 3
    assert sleeps[1] == 7.0


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("closed"),
        error_response(503, "internal_error"),
        error_response(500, "internal_error"),
        httpx.Response(504, text="<html>Gateway Timeout</html>"),
    ],
)
def test_create_experiment_with_a_key_retries_after_delivery(
    client: Client, mock_api: MockApi, answer: Any
) -> None:
    # the platform answers the same key and content with the experiment it created
    mock_api.add("POST", EXP, answer, created())
    assert client.create_experiment(name="n", dataset_id="ds-1")["id"] == "exp-1"
    first, second = mock_api.calls("POST", EXP)
    assert first.content == second.content  # same key, same bytes
    assert b'"idempotency_key"' in first.content


def test_create_experiment_with_a_key_that_may_exist_names_the_key(
    client: Client, mock_api: MockApi
) -> None:
    mock_api.add("POST", EXP, httpx.ReadTimeout("slow"))
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1", idempotency_key="run-1")
    assert len(mock_api.requests) == 4  # the retry budget
    assert "may have been created" in info.value.message
    assert "idempotency_key='run-1'" in info.value.message
    assert info.value.params["idempotency_key"] == "run-1"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (409, "idempotency_key_conflict"),
        (429, "judge_budget_exhausted"),
        (422, "validation_error"),
    ],
)
def test_create_experiment_with_a_key_does_not_retry_refusals(
    client: Client, mock_api: MockApi, status: int, code: str
) -> None:
    # a 422 about another field is not the key refusal of an older platform
    params = {"errors": [{"loc": ["body", "name"], "type": "string_too_long", "msg": "x"}]}
    mock_api.add("POST", EXP, error_response(status, code, params))
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1")
    assert (info.value.status, info.value.code) == (status, code)
    assert len(mock_api.requests) == 1


def test_create_experiment_without_key_support_sends_none(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    mock_api.add("POST", EXP, key_refused(), created())
    with caplog.at_level(logging.INFO, logger="insightmon"):
        assert client.create_experiment(name="n", dataset_id="ds-1")["id"] == "exp-1"
    first, second = mock_api.json_bodies("POST", EXP)
    assert "idempotency_key" in first
    assert second == {"name": "n", "dataset_id": "ds-1", "trials": 1, "triggered_by": "sdk"}
    assert "does not take idempotency keys" in caplog.text
    client.create_experiment(name="n", dataset_id="ds-1")  # remembered: no key sent again
    assert len(mock_api.requests) == 3
    assert "idempotency_key" not in mock_api.json_bodies("POST", EXP)[2]


@pytest.mark.parametrize(
    "answer",
    [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("closed")],
)
def test_create_experiment_without_key_support_does_not_retry_after_delivery(
    client: Client, mock_api: MockApi, answer: Exception
) -> None:
    mock_api.add("POST", EXP, key_refused(), answer, created())
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1")
    assert len(mock_api.requests) == 2  # the refused keyed request, then one attempt
    assert info.value.delivered is None
    assert "may have been created" in info.value.message
    assert "idempotency_key" not in info.value.params


@pytest.mark.parametrize(
    ("status", "code"),
    [(503, "internal_error"), (500, "internal_error"), (429, "judge_budget_exhausted")],
)
def test_create_experiment_without_key_support_does_not_retry_server_errors(
    client: Client, mock_api: MockApi, status: int, code: str
) -> None:
    mock_api.add("POST", EXP, key_refused(), error_response(status, code))
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1")
    assert (info.value.status, info.value.code) == (status, code)
    assert len(mock_api.requests) == 2


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(504, text="<html>Gateway Timeout</html>"),
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        error_response(502, "bad_gateway"),
        httpx.Response(500, text="upstream error"),
    ],
)
def test_create_experiment_without_key_support_answered_by_a_gateway_may_exist(
    client: Client, mock_api: MockApi, answer: httpx.Response
) -> None:
    mock_api.add("POST", EXP, key_refused(), answer)
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1")
    assert len(mock_api.requests) == 2  # still never retried without a key
    assert "may have been created" in info.value.message


def test_create_experiment_refused_by_the_platform_did_not_happen(
    client: Client, mock_api: MockApi
) -> None:
    mock_api.add("POST", EXP, error_response(500, "internal_error"))
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1")
    assert "may have been created" not in info.value.message


def test_create_experiment_unexpected_answer(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", EXP, httpx.Response(201, json={"examples": "x"}))
    with pytest.raises(InsightMonError) as info:
        client.create_experiment(name="n", dataset_id="ds-1")
    assert info.value.code == UNEXPECTED_RESPONSE


def test_get_experiment_quotes_the_id(client: Client, mock_api: MockApi) -> None:
    mock_api.add("GET", f"{EXP}/a/b", {**EXPERIMENT, "id": "a/b"})
    assert client.get_experiment("a/b")["id"] == "a/b"
    assert mock_api.requests[0].url.raw_path == b"/api/v1/experiments/a%2Fb"
    with pytest.raises(ValueError, match="non-empty"):
        client.get_experiment("")


def test_get_experiment_not_found(client: Client, mock_api: MockApi) -> None:
    mock_api.add("GET", f"{EXP}/nope", error_response(404, "not_found", {"resource": "experiment"}))
    with pytest.raises(InsightMonError) as info:
        client.get_experiment("nope")
    assert info.value.params == {"resource": "experiment"}


def test_list_and_iter_examples(client: Client, mock_api: MockApi) -> None:
    path = f"{EXP}/exp-1/examples"

    def page(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        assert request.url.params["limit"] == "200"
        if cursor is None:
            return httpx.Response(
                200, json={"items": [example_json("a"), example_json("b")], "next_cursor": "c1"}
            )
        assert cursor == "c1"
        return httpx.Response(200, json={"items": [example_json("c")], "next_cursor": None})

    mock_api.add("GET", path, page)
    examples = list(client.iter_examples("exp-1"))
    assert [e.example_key for e in examples] == ["a", "b", "c"]
    assert examples[0] == Example(
        example_key="a",
        example_id="id-a",
        input={"city": "a"},
        reference={"city": "a"},
        language="en",
    )
    first = client.list_examples("exp-1", limit=200)
    assert first.next_cursor == "c1"
    with pytest.raises(ValueError, match="limit"):
        client.list_examples("exp-1", limit=0)
    with pytest.raises(ValueError, match="limit"):
        client.list_examples("exp-1", limit=201)


def test_iter_examples_stops_on_a_repeated_cursor(client: Client, mock_api: MockApi) -> None:
    mock_api.add(
        "GET", f"{EXP}/exp-1/examples", {"items": [example_json("a")], "next_cursor": "same"}
    )
    with pytest.raises(InsightMonError) as info:
        list(client.iter_examples("exp-1"))
    assert info.value.code == UNEXPECTED_RESPONSE


def test_list_examples_bad_items(client: Client, mock_api: MockApi) -> None:
    mock_api.add("GET", f"{EXP}/exp-1/examples", {"items": [{"input": {}}], "next_cursor": None})
    with pytest.raises(InsightMonError) as info:
        client.list_examples("exp-1")
    assert info.value.code == UNEXPECTED_RESPONSE


# --------------------------------------------------------------------------- items:batch


def item(key: str, trial: int = 0, output: Any = None) -> dict[str, Any]:
    return {
        "example_key": key,
        "trial_index": trial,
        "status": "ok",
        "output": output if output is not None else {"text": key},
    }


def count_items(request: httpx.Request) -> httpx.Response:
    items = json.loads(request.content)["items"]
    return httpx.Response(
        200,
        json={
            "accepted": len(items),
            "duplicates": 0,
            "item_ids": [f"i-{x['example_key']}" for x in items],
        },
    )


def test_report_items_splits_by_count(client: Client, mock_api: MockApi) -> None:
    path = f"{EXP}/exp-1/items:batch"
    mock_api.add("POST", path, count_items)
    result = client.report_items("exp-1", [item(f"k{i}") for i in range(450)])
    assert [len(b["items"]) for b in mock_api.json_bodies("POST", path)] == [200, 200, 50]
    assert result.accepted == 450
    assert result.duplicates == 0
    assert len(result.item_ids) == 450


def test_report_items_splits_by_size(client: Client, mock_api: MockApi) -> None:
    path = f"{EXP}/exp-1/items:batch"
    mock_api.add("POST", path, count_items)
    big = "x" * 1000
    items = [item(f"k{i}", output={"text": big}) for i in range(10)]
    client.report_items("exp-1", items, max_body_bytes=3500)
    calls = mock_api.calls("POST", path)
    assert [len(json.loads(r.content)["items"]) for r in calls] == [3, 3, 3, 1]
    assert all(len(r.content) <= 3500 for r in calls)
    with pytest.raises(ValueError, match="alone"):
        client.report_items("exp-1", [item("k", output={"text": big * 4})], max_body_bytes=3500)
    assert len(mock_api.calls("POST", path)) == 4  # nothing sent for the oversized item


def test_report_items_retries_with_identical_bytes(
    client: Client, mock_api: MockApi, sleeps: list[float]
) -> None:
    path = f"{EXP}/exp-1/items:batch"
    mock_api.add(
        "POST",
        path,
        error_response(429, "rate_limited", {"retry_after_s": 2}),
        error_response(503, "internal_error"),
        httpx.ReadTimeout("slow"),
        {"accepted": 0, "duplicates": 2, "item_ids": ["x", "y"]},
    )
    result = client.report_items("exp-1", [item("a"), item("b")])
    bodies = [r.content for r in mock_api.calls("POST", path)]
    assert len(bodies) == 4
    assert len(set(bodies)) == 1
    assert result.duplicates == 2
    assert sleeps[0] == 2.0


def test_report_items_and_finalize_outlast_a_short_outage(
    client: Client, mock_api: MockApi, sleeps: list[float]
) -> None:
    """items:batch and :finalize are idempotent: a few seconds of 502 / 503 (a platform
    restart) must not stop a long evaluate() run."""
    path = f"{EXP}/exp-1/items:batch"
    outage = [error_response(503, "internal_error")] * 6
    mock_api.add("POST", path, *outage, {"accepted": 1, "duplicates": 0, "item_ids": ["x"]})
    assert client.report_items("exp-1", [item("a")]).accepted == 1
    assert len(mock_api.calls("POST", path)) == 7
    mock_api.add(
        "POST",
        f"{EXP}/exp-1:finalize",
        *[httpx.Response(502, text="bad gateway")] * 6,
        httpx.Response(202, json={"experiment": EXPERIMENT, "job_id": "j-1"}),
    )
    assert client.finalize_experiment("exp-1")["job_id"] == "j-1"
    assert len(sleeps) == 12
    # other calls keep the shorter budget
    mock_api.add("GET", f"{EXP}/exp-2", *[error_response(503, "internal_error")] * 5)
    with pytest.raises(InsightMonError):
        client.get_experiment("exp-2")
    assert len(mock_api.calls("GET", f"{EXP}/exp-2")) == 4


def test_report_retries_can_be_set(mock_api: MockApi, sleeps: list[float]) -> None:
    path = f"{EXP}/exp-1/items:batch"
    mock_api.add("POST", path, error_response(503, "internal_error"))
    with (
        Client(BASE_URL, API_KEY, report_retries=1, transport=mock_api.transport) as client,
        pytest.raises(InsightMonError),
    ):
        client.report_items("exp-1", [item("a")])
    assert len(mock_api.calls("POST", path)) == 2
    with pytest.raises(ValueError, match="report_retries"):
        Client(BASE_URL, API_KEY, report_retries=-1)


@pytest.mark.parametrize(
    ("status", "code", "params"),
    [
        (400, "external_item_invalid", {"example_key": "a", "reason": "already_reported"}),
        (403, "permission_denied", {"required": "experiment_write"}),
        (409, "invalid_state", {"resource": "experiment", "status": "finalized"}),
        (422, "validation_error", {"errors": []}),
    ],
)
def test_report_items_does_not_retry_client_errors(
    client: Client, mock_api: MockApi, status: int, code: str, params: dict[str, Any]
) -> None:
    path = f"{EXP}/exp-1/items:batch"
    mock_api.add("POST", path, error_response(status, code, params))
    with pytest.raises(InsightMonError) as info:
        client.report_items("exp-1", [item("a")])
    assert (info.value.status, info.value.code, info.value.params) == (status, code, params)
    assert len(mock_api.requests) == 1


def test_report_items_empty_and_unexpected(client: Client, mock_api: MockApi) -> None:
    assert client.report_items("exp-1", []).accepted == 0
    assert mock_api.requests == []
    mock_api.add("POST", f"{EXP}/exp-1/items:batch", {"accepted": 1})
    with pytest.raises(InsightMonError) as info:
        client.report_items("exp-1", [item("a")])
    assert info.value.code == UNEXPECTED_RESPONSE


# --------------------------------------------------------------------------- finalize


def test_finalize(client: Client, mock_api: MockApi) -> None:
    mock_api.add(
        "POST",
        f"{EXP}/exp-1:finalize",
        httpx.Response(202, json={"experiment": EXPERIMENT, "job_id": "job-1"}),
    )
    assert client.finalize_experiment("exp-1") == {"experiment": EXPERIMENT, "job_id": "job-1"}
    (request,) = mock_api.requests
    assert request.content == b"{}"


def test_finalize_retry_that_meets_finalized_is_success(
    client: Client, mock_api: MockApi, sleeps: list[float]
) -> None:
    mock_api.add(
        "POST",
        f"{EXP}/exp-1:finalize",
        httpx.ReadTimeout("slow"),
        error_response(
            409,
            "invalid_state",
            {"resource": "experiment", "status": "finalized", "action": "finalize"},
        ),
    )
    mock_api.add("GET", f"{EXP}/exp-1", {**EXPERIMENT, "status": "completed"})
    result = client.finalize_experiment("exp-1")
    assert result == {"experiment": {**EXPERIMENT, "status": "completed"}, "job_id": None}
    assert len(mock_api.calls("POST", f"{EXP}/exp-1:finalize")) == 2


def test_finalize_first_attempt_conflict_raises(client: Client, mock_api: MockApi) -> None:
    mock_api.add(
        "POST",
        f"{EXP}/exp-1:finalize",
        error_response(409, "invalid_state", {"status": "finalized", "action": "finalize"}),
    )
    with pytest.raises(InsightMonError) as info:
        client.finalize_experiment("exp-1")
    assert info.value.code == "invalid_state"


def test_finalize_unexpected_status(client: Client, mock_api: MockApi) -> None:
    mock_api.add("POST", f"{EXP}/exp-1:finalize", {"experiment": EXPERIMENT, "job_id": "j"})
    with pytest.raises(InsightMonError) as info:
        client.finalize_experiment("exp-1")
    assert (info.value.code, info.value.status) == (UNEXPECTED_RESPONSE, 200)


def test_experiment_url(client: Client) -> None:
    assert client.experiment_url(EXPERIMENT) == f"{BASE_URL}/projects/proj-1/experiments/exp-1"
    with pytest.raises(ValueError, match="project_id"):
        client.experiment_url({"id": "exp-1"})


# --------------------------------------------------------------------------- helpers


def test_plan_requests_bodies_are_exact() -> None:
    plans = plan_requests("items", [{"a": 1}, {"b": 2}, {"c": 3}], max_items=2)
    assert [body for _, body in plans] == [b'{"items":[{"a":1},{"b":2}]}', b'{"items":[{"c":3}]}']
    assert [len(chunk) for chunk, _ in plans] == [2, 1]
    assert plan_requests("items", [], max_items=2) == []
    with pytest.raises(TypeError):
        plan_requests("items", [1], max_items=2)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="max_items"):
        plan_requests("items", [{"a": 1}], max_items=0)


def test_normalize_helpers() -> None:
    assert normalize_requested({"components": []}) == {"components": []}
    # what the platform compares with the span attributes, which it strips
    assert normalize_requested(
        {
            "agent_version": "1.4.0\n",
            "components": [{"type": "prompt", "name": " p ", "version": "v1 "}],
        }
    ) == {
        "agent_version": "1.4.0",
        "components": [{"type": "prompt", "name": "p", "version": "v1"}],
    }
    assert component_refs([Component("tool", "b"), Component("prompt", "a", "1")]) == [
        {"type": "prompt", "name": "a", "version": "1"},
        {"type": "tool", "name": "b", "version": None},
    ]
    assert normalize_evaluators(["ev-1"]) == [{"evaluator_version_id": "ev-1", "role": "gating"}]
    with pytest.raises(ValueError, match="single string"):
        normalize_evaluators("ev-1")
    with pytest.raises(ValueError, match="version id"):
        normalize_evaluators([3])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="100"):
        component_refs([{"type": "prompt", "name": f"p{i}"} for i in range(101)])


def test_key_never_logged(
    client: Client, mock_api: MockApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mock_api.add(
        "POST",
        f"{EXP}/exp-1/items:batch",
        error_response(503, "internal_error"),
        error_response(400, "external_item_invalid", {"example_key": "a", "reason": "x"}),
    )
    with pytest.raises(InsightMonError) as info:
        client.report_items("exp-1", [item("a")])
    assert caplog.records
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(info.value)
