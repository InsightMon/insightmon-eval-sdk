"""Platform replay against `examples/http_agent.py`: the receipt comes from the SDK spans.

The agent runs as a subprocess (uvicorn) with the integration key. The platform calls it with a
`traceparent` whose trace id is the item's `otel_trace_id`; the agent's spans join that
pre-created trace, and after `receipt_wait_s` the platform reads the agent and component
versions from it (`version_source = trace_attr`). With `--receipt-field` the response carries
the same receipt as well (`version_source = both`, no conflicts).

The Target waits `receipt_wait_s = 5` and the agent exports in the background (batch delay
200 ms): on a platform backed by SQLite, exporting before the response
(`flush_on_traceparent`) blocks until the call times out (see README, "Replay receipts").
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import ItConfig, unique
from tests.integration.platform_api import AdminSession, flat_spans, responds, wait_for

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT = REPO_ROOT / "examples" / "http_agent.py"
AGENT_VERSION = "2.1.0"
COMPONENTS = {("prompt", "system"): "p5", ("tool", "forecast"): "1.2.0"}


@dataclass
class Agent:
    url: str
    log: Path

    def trace_ids(self) -> list[str]:
        return re.findall(r"respond trace_id=([0-9a-f]{32})", self.log.read_text())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def start_agent(it_config: ItConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    pytest.importorskip("uvicorn")
    processes: list[subprocess.Popen[bytes]] = []

    def start(*flags: str) -> Agent:
        port = _free_port()
        log = tmp_path_factory.mktemp("agent") / "agent.log"
        env = {
            **os.environ,
            "INSIGHTMON_BASE_URL": it_config.base_url,
            "INSIGHTMON_API_KEY": it_config.api_key,
            "OTEL_BSP_SCHEDULE_DELAY": "200",
        }
        with log.open("wb") as out:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(AGENT),
                    "--port",
                    str(port),
                    "--agent-version",
                    AGENT_VERSION,
                    *flags,
                ],
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        processes.append(process)
        url = f"http://127.0.0.1:{port}"

        def up() -> bool:
            if process.poll() is not None:
                raise AssertionError(f"the agent exited: {log.read_text()[-2000:]}")
            return responds(url + "/healthz")

        wait_for("the agent", up, timeout_s=30)
        return Agent(url=url, log=log)

    try:
        yield start
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()


def _replay(
    admin: AdminSession,
    agent: Agent,
    dataset: tuple[str, list[str]],
    server_evaluator: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    suffix = unique("sdk-it")
    environment = admin.post(
        admin.p("/environments"),
        {"name": suffix, "kind": "live_staging", "evidence_class": "stub"},
    )
    environment = admin.post(admin.p(f"/environments/{environment['id']}:publish"))
    target = admin.post(
        admin.p("/targets"),
        {
            "name": f"{suffix} http agent",
            "kind": "http",
            "agent_name": "weather-agent",
            "evidence_class": "stub",
            "config": {
                "url": agent.url + "/v1/respond",
                "receipt_wait_s": 5,
                "timeout_ms": 10000,
                "propagate_traceparent": True,
            },
        },
    )
    dataset_id, keys = dataset
    created = admin.post(
        admin.p("/experiments"),
        {
            "name": f"{suffix} replay",
            "dataset_id": dataset_id,
            "example_keys": keys[:2],
            "target_id": target["id"],
            "environment_id": environment["id"],
            "requested": {"agent_version": AGENT_VERSION, "components": []},
            "evaluators": [{"evaluator_version_id": server_evaluator, "role": "gating"}],
        },
    )
    experiment = admin.wait_experiment(created["id"], timeout_s=180)
    return experiment, admin.experiment_items(created["id"])


def _check_trace(admin: AdminSession, item: dict[str, Any]) -> None:
    trace = wait_for("the item's trace", lambda: admin.trace_by_otel_id(item["otel_trace_id"]))
    assert trace["id"] == item["trace_id"]
    assert trace["origin"] == "experiment"
    assert trace["agent_version"] == AGENT_VERSION
    spans = flat_spans(admin.trace_detail(trace["id"])["spans"])
    by_name = {s["name"]: s for s in spans}
    assert set(by_name) == {"respond", "forecast"}
    # the turn span's parent is the platform's remote parent from the traceparent header
    assert by_name["respond"]["otel_parent_span_id"]
    assert by_name["respond"]["orphan"] is True


def _components(receipt: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(c["type"], c["name"]): c["version"] for c in receipt["components"]}


def test_replay_reads_the_receipt_from_sdk_spans(
    admin: AdminSession,
    start_agent: Any,
    dataset: tuple[str, list[str]],
    server_evaluator: str,
) -> None:
    agent = start_agent()
    experiment, items = _replay(admin, agent, dataset, server_evaluator)

    assert experiment["status"] == "completed"
    assert len(items) == 2
    seen = agent.trace_ids()
    for item in items:
        assert item["status"] == "ok", item.get("error")
        assert item["otel_trace_id"] in seen  # the traceparent's trace id reached the agent
        receipt = item["receipt"]
        assert receipt["final"] is True
        assert receipt["version_source"] == "trace_attr"
        assert receipt["response"] is None
        assert receipt["agent"] == {"name": "weather-agent", "version": AGENT_VERSION}
        assert _components(receipt) == COMPONENTS
        assert receipt["receipt_conflicts"] == []
        _check_trace(admin, item)


def test_replay_with_a_receipt_field_agrees_with_the_spans(
    admin: AdminSession,
    start_agent: Any,
    dataset: tuple[str, list[str]],
    server_evaluator: str,
) -> None:
    agent = start_agent("--receipt-field")
    experiment, items = _replay(admin, agent, dataset, server_evaluator)

    assert experiment["status"] == "completed"
    for item in items:
        assert item["status"] == "ok", item.get("error")
        receipt = item["receipt"]
        assert receipt["version_source"] == "both"
        assert receipt["receipt_conflicts"] == []
        assert receipt["agent"]["version"] == AGENT_VERSION
        assert _components(receipt) == COMPONENTS
        assert _components(receipt["response"]) == COMPONENTS
        _check_trace(admin, item)
