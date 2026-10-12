"""Read-back helpers for the integration tests: a signed-in platform user (browser session).

The key-only REST API cannot read threads, traces or scores back, so the integration tests sign
in like the web UI (cookie + `X-CSRF-Token` from the `im_csrf` cookie) with an account that is
an admin of the key's project. Also used by `scripts/provision_platform.py`.

Everything here is test tooling; the SDK itself never signs in.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

T = TypeVar("T")


class PlatformError(RuntimeError):
    pass


def check(response: httpx.Response, ok: tuple[int, ...] = (200, 201, 202)) -> Any:
    if response.status_code not in ok:
        raise PlatformError(
            f"{response.request.method} {response.request.url.path} -> "
            f"{response.status_code} {response.text[:600]}"
        )
    return response.json() if response.content else None


def wait_for(
    what: str, probe: Callable[[], T | None], timeout_s: float = 60.0, every_s: float = 0.5
) -> T:
    """Poll `probe` until it returns something truthy."""
    deadline = time.monotonic() + timeout_s
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(every_s)


def responds(url: str) -> bool:
    """True when `GET url` answers 200 (False while the server is not up yet)."""
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


class AdminSession:
    """A signed-in user of the platform's web API (`/api/v1`)."""

    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url + "/api/v1", timeout=60)
        check(self.http.post("/auth/login", json={"email": email, "password": password}), (200,))
        self.project_id = ""

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.http.cookies.get("im_csrf") or ""}

    def get(self, path: str, ok: tuple[int, ...] = (200,), **params: Any) -> Any:
        return check(self.http.get(path, params=params or None), ok)

    def post(self, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 202)) -> Any:
        response = self.http.post(path, json={} if body is None else body, headers=self._headers())
        return check(response, ok)

    # ------------------------------------------------------------------ project scoped

    def p(self, path: str = "") -> str:
        if not self.project_id:
            raise PlatformError("project_id is not set")
        return f"/projects/{self.project_id}{path}"

    def projects(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.get("/projects")["items"]
        return items

    def create_api_key(self, name: str, scopes: list[str]) -> dict[str, Any]:
        """`{api_key: {id, key_prefix, scopes, ...}, secret}`; the secret is shown once."""
        created: dict[str, Any] = self.post(self.p("/api-keys"), {"name": name, "scopes": scopes})
        return created

    def revoke_api_key(self, key_id: str) -> dict[str, Any]:
        """Revoke a key of the project (the key stops working at once)."""
        revoked: dict[str, Any] = self.post(self.p(f"/api-keys/{key_id}:revoke"))
        return revoked

    def ensure_score_config(self, name: str, **spec: Any) -> dict[str, Any]:
        """A published score config named `name` (created and published when missing)."""
        configs = self.get(self.p("/score-configs"), limit=200)["items"]
        config = next((c for c in configs if c["name"] == name), None)
        if config is None:
            config = self.post(self.p("/score-configs"), {"name": name, **spec})
        if config["status"] != "published":
            config = self.post(
                self.p(f"/score-configs/{config['id']}:publish"),
                {"expected_updated_at": config["updated_at"]},
            )
        return dict(config)

    def ready_examples(self, dataset_id: str) -> list[str]:
        """Keys of the dataset's ready dev examples."""
        rows = self.get(self.p(f"/datasets/{dataset_id}/examples"), limit=200)["items"]
        return sorted(
            e["example_key"] for e in rows if e["status"] == "ready" and e.get("split") == "dev"
        )

    def find_dataset(self, min_ready: int = 2) -> tuple[str, list[str]]:
        """(dataset id, ready dev example keys) of the dataset with the most ready dev examples."""
        best: tuple[str, list[str]] | None = None
        for dataset in self.get(self.p("/datasets"), limit=200)["items"]:
            keys = self.ready_examples(dataset["id"])
            if len(keys) >= min_ready and (best is None or len(keys) > len(best[1])):
                best = (dataset["id"], keys)
        if best is None:
            raise PlatformError(f"no dataset with {min_ready} ready dev examples")
        return best

    def evaluator_version(self, name: str) -> str:
        """The published version id of the evaluator `name`."""
        for evaluator in self.get(self.p("/evaluators"), limit=200)["items"]:
            if evaluator["name"] == name and evaluator.get("published_version"):
                return str(evaluator["published_version"]["id"])
        raise PlatformError(f"no published evaluator named {name!r}")

    # ------------------------------------------------------------------ read back

    def thread_by_key(self, key: str) -> dict[str, Any] | None:
        items = self.get(self.p("/threads"), thread_key=key)["items"]
        return dict(items[0]) if items else None

    def thread_detail(self, thread_id: str, *, spans: bool = False) -> dict[str, Any]:
        detail: dict[str, Any] = self.get(
            self.p(f"/threads/{thread_id}"), spans=str(spans).lower(), content="full"
        )
        return detail

    def trace_by_otel_id(self, otel_trace_id: str) -> dict[str, Any] | None:
        items = self.get(self.p("/traces"), otel_trace_id=otel_trace_id)["items"]
        return dict(items[0]) if items else None

    def trace_detail(self, trace_id: str) -> dict[str, Any]:
        detail: dict[str, Any] = self.get(self.p(f"/traces/{trace_id}"), content="full")
        return detail

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment: dict[str, Any] = self.get(self.p(f"/experiments/{experiment_id}"))
        return experiment

    def experiments(self, dataset_id: str) -> list[dict[str, Any]]:
        """Every experiment of a dataset (all pages)."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"dataset_id": dataset_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = self.get(self.p("/experiments"), **params)
            out.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return out

    def experiment_items(self, experiment_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.get(
            self.p(f"/experiments/{experiment_id}/items"), limit=200
        )["items"]
        return items

    def wait_experiment(self, experiment_id: str, timeout_s: float = 300.0) -> dict[str, Any]:
        def finished() -> dict[str, Any] | None:
            experiment = self.experiment(experiment_id)
            done = experiment["status"] in ("completed", "failed", "cancelled")
            return experiment if done else None

        return wait_for(f"experiment {experiment_id}", finished, timeout_s, every_s=1.0)


def flat_spans(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The spans of a span tree (`children` lists), depth first."""
    out: list[dict[str, Any]] = []
    for node in nodes:
        out.append(node)
        out.extend(flat_spans(node.get("children") or []))
    return out
