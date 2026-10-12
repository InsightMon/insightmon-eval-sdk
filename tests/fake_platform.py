"""A fake platform for `evaluate()`: the external-experiment routes on `httpx.MockTransport`,
with the platform's validation and deduplication (tests/test_evaluate.py, test_examples.py,
test_readme.py)."""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx

from tests.conftest import MockApi, error_response

EXP_ID = "exp-1"
EXP = "/api/v1/experiments"
BATCH = f"{EXP}/{EXP_ID}/items:batch"
FINALIZE = f"{EXP}/{EXP_ID}:finalize"


def example_json(
    key: str, trials: int, initial_state: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "example_key": key,
        "example_id": f"id-{key}",
        "input": {"city": key, "seen": []},
        "initial_state": initial_state or {},
        "reference": {"city": key},
        "language": "en",
        "metadata": {"tier": "basic"},
        "trials": trials,
    }


class FakePlatform:
    """The external-experiment routes, with the platform's validation and deduplication."""

    def __init__(
        self,
        keys: list[str],
        *,
        page_size: int = 200,
        polls_until_done: int = 2,
        score_names: frozenset[str] = frozenset({"short", "mentions_city", "length"}),
        score_max: float | None = None,
        lists_configs: bool = False,
    ) -> None:
        self.keys = keys
        self.page_size = page_size
        self.polls_until_done = polls_until_done
        self.score_names = score_names
        #: numeric score values above this do not fit the score config (score_invalid)
        self.score_max = score_max
        #: serve `GET /api/v1/score-configs` (otherwise 404, as on a platform without it)
        self.lists_configs = lists_configs
        self.lock = threading.Lock()
        #: example key -> initial_state served with that example
        self.initial_states: dict[str, dict[str, Any]] = {}
        self.trials = 1
        self.create_body: dict[str, Any] | None = None
        self.reports: dict[tuple[str, int], dict[str, Any]] = {}
        self.events: list[str] = []
        self.finalized = False
        self.polls = 0
        self.batch_errors: list[httpx.Response | Exception] = []
        self.finalize_errors: list[httpx.Response | Exception] = []

    def install(self, mock_api: MockApi) -> FakePlatform:
        mock_api.add("POST", EXP, self.create)
        mock_api.add("GET", f"{EXP}/{EXP_ID}/examples", self.examples)
        mock_api.add("POST", BATCH, self.batch)
        mock_api.add("POST", FINALIZE, self.finalize)
        mock_api.add("GET", f"{EXP}/{EXP_ID}", self.get)
        if self.lists_configs:
            mock_api.add("GET", "/api/v1/score-configs", self.score_configs)
        mock_api.add(
            "GET",
            "/api/v1/api-key",
            {
                "id": "k1",
                "name": "notes-ci",
                "prefix": "imk_te",
                "project_id": "proj-1",
                "scopes": ["trace_ingest"],
            },
        )
        return self

    def experiment(self, status: str = "running", summary: Any = None) -> dict[str, Any]:
        return {
            "id": EXP_ID,
            "project_id": "proj-1",
            "status": status,
            "trials": self.trials,
            "summary": summary,
            "executor": "external",
        }

    def create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.create_body = body
        self.trials = body.get("trials", 1)
        self.events.append("create")
        return httpx.Response(201, json={"experiment": self.experiment(), "examples": "x"})

    def examples(self, request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("cursor") or 0)
        limit = min(int(request.url.params["limit"]), self.page_size)
        page = self.keys[offset : offset + limit]
        more = offset + limit < len(self.keys)
        return httpx.Response(
            200,
            json={
                "items": [example_json(k, self.trials, self.initial_states.get(k)) for k in page],
                "next_cursor": str(offset + limit) if more else None,
            },
        )

    def batch(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if self.batch_errors:
                answer = self.batch_errors.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            if self.finalized:
                return error_response(409, "invalid_state", {"status": "finalized"})
            items = json.loads(request.content)["items"]
            assert 1 <= len(items) <= 200
            accepted = duplicates = 0
            seen: set[tuple[str, int]] = set()
            for item in items:
                ref = (item["example_key"], item["trial_index"])
                if item["example_key"] not in self.keys:
                    return error_response(
                        400,
                        "external_item_invalid",
                        {"example_key": ref[0], "reason": "unknown_example"},
                    )
                if ref in seen:
                    return error_response(
                        400,
                        "external_item_invalid",
                        {"example_key": ref[0], "reason": "duplicate_in_batch"},
                    )
                seen.add(ref)
                if ref in self.reports and self.reports[ref] != item:
                    return error_response(
                        400,
                        "external_item_invalid",
                        {"example_key": ref[0], "reason": "already_reported"},
                    )
                if not all(self.score_fits(score) for score in item.get("scores", [])):
                    return self.score_invalid(item, items)
            for item in items:
                ref = (item["example_key"], item["trial_index"])
                if ref in self.reports:
                    duplicates += 1
                else:
                    accepted += 1
                    self.reports[ref] = item
            self.events.append(f"batch:{len(items)}")
            return httpx.Response(
                200,
                json={
                    "accepted": accepted,
                    "duplicates": duplicates,
                    "item_ids": [f"item-{i}" for i in range(len(items))],
                },
            )

    def score_invalid(self, first: dict[str, Any], items: list[dict[str, Any]]) -> httpx.Response:
        """The first offending item, with every offending score of the request listed."""
        listed = [
            {"example_key": i["example_key"], "trial_index": i["trial_index"], "name": s["name"]}
            for i in items
            if (i["example_key"], i["trial_index"]) not in self.reports
            for s in i.get("scores", [])
            if not self.score_fits(s)
        ]
        params = {
            "example_key": first["example_key"],
            "trial_index": first["trial_index"],
            "reason": "score_invalid",
            "invalid_scores": listed,
        }
        return error_response(400, "external_item_invalid", params)

    def score_configs(self, request: httpx.Request) -> httpx.Response:
        self.events.append("score-configs")
        items = [
            {
                "name": name,
                "data_type": "numeric",
                "labels": None,
                "min": None,
                "max": self.score_max,
                "subject_levels": ["experiment_item"],
                "allow_unknown": False,
            }
            for name in sorted(self.score_names)
        ]
        return httpx.Response(200, json={"items": items})

    def score_fits(self, score: dict[str, Any]) -> bool:
        if score["name"] not in self.score_names:
            return False
        value = score["value"]
        number = isinstance(value, int | float) and not isinstance(value, bool)
        return self.score_max is None or not number or value <= self.score_max

    def finalize(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if self.finalize_errors:
                answer = self.finalize_errors.pop(0)
                if isinstance(answer, Exception):
                    self.finalized = True  # the server acted, the answer got lost
                    self.events.append("finalize")
                    raise answer
                return answer
            if self.finalized:
                return error_response(
                    409,
                    "invalid_state",
                    {"resource": "experiment", "status": "finalized", "action": "finalize"},
                )
            self.finalized = True
            self.events.append("finalize")
            return httpx.Response(202, json={"experiment": self.experiment(), "job_id": "job-1"})

    def get(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.polls += 1
            if self.finalized and self.polls >= self.polls_until_done:
                summary = {"pass_rate": 0.75, "counts": {"ok": len(self.reports)}}
                return httpx.Response(200, json=self.experiment("completed", summary))
            return httpx.Response(200, json=self.experiment())
