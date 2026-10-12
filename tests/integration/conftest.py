"""Integration tests against a running platform (`scripts/integration.sh` boots one).

Every test module here is marked `integration`; the tests are skipped unless these are set:

- `INSIGHTMON_IT_BASE_URL`: the platform root URL;
- `INSIGHTMON_IT_API_KEY`: a key with scopes `trace_ingest`, `score_write` and
  `experiment_write`;
- `INSIGHTMON_IT_ADMIN_EMAIL` / `INSIGHTMON_IT_ADMIN_PASSWORD`: an admin of the key's project.
  The key-only API cannot read threads, traces or scores back, so the tests sign in like the
  web UI to check what the platform stored.

Optional: `INSIGHTMON_IT_DATASET_ID` (a dataset with at least two ready dev examples; default:
the project's dataset with the most of them) and `INSIGHTMON_IT_EVALUATOR` (the name of a
published `experiment_item` evaluator; default `exact`).

The tests create their own score configs (`sdk_it_*`), environments and targets, and use
random thread keys, so they can run repeatedly against the same platform.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

import insightmon
from tests.integration.platform_api import AdminSession

#: Score configs the tests create (published) when missing.
SCORE_CONFIGS: dict[str, dict[str, Any]] = {
    "sdk_it_quality": {
        "display_name": "SDK integration: quality",
        "data_type": "numeric",
        "min": 0,
        "max": 1,
        "threshold": 0.5,
        "direction": "higher_is_better",
        "subject_levels": ["span", "trace"],
    },
    "sdk_it_outcome": {
        "display_name": "SDK integration: outcome",
        "data_type": "boolean",
        "pass_value": True,
        "subject_levels": ["thread"],
    },
    "sdk_it_client": {
        "display_name": "SDK integration: client check",
        "data_type": "numeric",
        "min": 0,
        "max": 1,
        "threshold": 0.5,
        "direction": "higher_is_better",
        "subject_levels": ["experiment_item"],
    },
}


@dataclass(frozen=True)
class ItConfig:
    base_url: str
    api_key: str
    admin_email: str
    admin_password: str
    dataset_id: str | None
    evaluator: str


def _read_config() -> ItConfig | str:
    """The configuration, or the reason to skip."""
    base_url = os.environ.get("INSIGHTMON_IT_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("INSIGHTMON_IT_API_KEY", "")
    if not base_url or not api_key:
        return "INSIGHTMON_IT_BASE_URL and INSIGHTMON_IT_API_KEY are not set"
    email = os.environ.get("INSIGHTMON_IT_ADMIN_EMAIL", "")
    password = os.environ.get("INSIGHTMON_IT_ADMIN_PASSWORD", "")
    if not email or not password:
        return "INSIGHTMON_IT_ADMIN_EMAIL and INSIGHTMON_IT_ADMIN_PASSWORD are not set"
    return ItConfig(
        base_url=base_url,
        api_key=api_key,
        admin_email=email,
        admin_password=password,
        dataset_id=os.environ.get("INSIGHTMON_IT_DATASET_ID") or None,
        evaluator=os.environ.get("INSIGHTMON_IT_EVALUATOR") or "exact",
    )


# Read once, before the unit-test isolation fixture runs.
_CONFIG = _read_config()


@pytest.fixture(scope="session")
def it_config() -> ItConfig:
    if isinstance(_CONFIG, str):
        pytest.skip(_CONFIG)
    return _CONFIG


@pytest.fixture(scope="session")
def project_id(it_config: ItConfig) -> str:
    with insightmon.Client(it_config.base_url, it_config.api_key) as client:
        info = client.api_key_info()
    missing = {"trace_ingest", "score_write", "experiment_write"} - set(info.scopes)
    if missing:
        pytest.fail(f"INSIGHTMON_IT_API_KEY lacks the scopes {sorted(missing)}")
    return info.project_id


@pytest.fixture(scope="session")
def admin(it_config: ItConfig, project_id: str) -> Iterator[AdminSession]:
    session = AdminSession(it_config.base_url, it_config.admin_email, it_config.admin_password)
    session.project_id = project_id
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def score_configs(admin: AdminSession) -> dict[str, dict[str, Any]]:
    return {name: admin.ensure_score_config(name, **spec) for name, spec in SCORE_CONFIGS.items()}


@pytest.fixture(scope="session")
def dataset(admin: AdminSession, it_config: ItConfig) -> tuple[str, list[str]]:
    """(dataset id, ready dev example keys)."""
    if it_config.dataset_id:
        keys = admin.ready_examples(it_config.dataset_id)
        if len(keys) < 2:
            pytest.fail(f"dataset {it_config.dataset_id} has fewer than 2 ready dev examples")
        return it_config.dataset_id, keys
    return admin.find_dataset(min_ready=2)


@pytest.fixture(scope="session")
def server_evaluator(admin: AdminSession, it_config: ItConfig) -> str:
    return admin.evaluator_version(it_config.evaluator)


@pytest.fixture
def sdk(it_config: ItConfig) -> Iterator[Callable[..., insightmon.InsightMonHandle]]:
    """`sdk(**init_kwargs)`: `insightmon.init()` against the platform with a fresh tracer
    provider (the global one can be set once per process); shut down after the test."""

    def start(**kwargs: Any) -> insightmon.InsightMonHandle:
        kwargs.setdefault("agent_name", "sdk-it-weather")
        kwargs.setdefault("agent_version", "1.4.0")
        return insightmon.init(
            base_url=it_config.base_url,
            api_key=it_config.api_key,
            tracer_provider=TracerProvider(),
            **kwargs,
        )

    try:
        yield start
    finally:
        insightmon.shutdown()


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"
