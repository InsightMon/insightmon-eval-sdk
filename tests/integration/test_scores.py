"""Evaluation logs (`log_evaluation`) and `Client.scores_batch`, read back from the platform."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

import insightmon
from insightmon import _export
from tests.integration.conftest import ItConfig, unique
from tests.integration.platform_api import AdminSession, flat_spans, wait_for

pytestmark = pytest.mark.integration

Sdk = Callable[..., insightmon.InsightMonHandle]


def _hex_trace(span: Any) -> str:
    return format(span.get_span_context().trace_id, "032x")


def _hex_span(span: Any) -> str:
    return format(span.get_span_context().span_id, "016x")


def _scores_named(admin: AdminSession, trace_id: str, name: str) -> list[dict[str, Any]]:
    return [s for s in admin.trace_detail(trace_id)["scores"] if s["name"] == name]


@pytest.mark.usefixtures("score_configs")
def test_log_evaluation_becomes_an_import_score_on_the_span(admin: AdminSession, sdk: Sdk) -> None:
    sdk()
    with insightmon.thread(unique("sdk-it-eval")), insightmon.turn() as span:
        pass
    insightmon.log_evaluation(
        "sdk_it_quality", value=0.8, explanation="cites the forecast source", span=span
    )
    assert insightmon.flush()

    trace = wait_for("the trace", lambda: admin.trace_by_otel_id(_hex_trace(span)))
    scores = wait_for(
        "the score", lambda: _scores_named(admin, trace["id"], "sdk_it_quality"), timeout_s=60
    )
    assert len(scores) == 1
    score = scores[0]
    spans = {s["otel_span_id"]: s for s in flat_spans(admin.trace_detail(trace["id"])["spans"])}
    assert score["source"] == "import"
    assert score["subject_type"] == "span"
    assert score["span_id"] == spans[_hex_span(span)]["id"]
    assert score["trace_id"] == trace["id"]
    assert score["value_number"] == pytest.approx(0.8)
    assert score["passed"] is True
    assert score["explanation"] == "cites the forecast source"
    assert _export.rejections() == _export.Rejections()


@pytest.mark.usefixtures("score_configs")
def test_log_evaluation_with_an_unknown_name_is_rejected_and_reported(
    admin: AdminSession, sdk: Sdk, caplog: pytest.LogCaptureFixture
) -> None:
    sdk()
    with insightmon.thread(unique("sdk-it-unknown")), insightmon.turn() as span:
        pass
    insightmon.log_evaluation("sdk_it_no_such_config", value=1.0, span=span)
    insightmon.log_evaluation("sdk_it_quality", value=0.3, span=span)
    with caplog.at_level(logging.WARNING, logger="insightmon"):
        assert insightmon.flush()

    rejected = _export.rejections()
    assert rejected.log_records == 1
    assert rejected.spans == 0
    assert any("score_config_not_found" in reason for reason in rejected.reasons)
    assert any("score_config_not_found" in r.getMessage() for r in caplog.records)

    trace = wait_for("the trace", lambda: admin.trace_by_otel_id(_hex_trace(span)))
    wait_for("the valid score", lambda: _scores_named(admin, trace["id"], "sdk_it_quality"))
    assert _scores_named(admin, trace["id"], "sdk_it_no_such_config") == []


@pytest.mark.usefixtures("score_configs")
def test_scores_batch_writes_an_outcome_score_on_the_thread(
    admin: AdminSession, sdk: Sdk, it_config: ItConfig
) -> None:
    sdk()
    key = unique("sdk-it-outcome")
    with insightmon.thread(key), insightmon.turn():
        pass
    assert insightmon.flush()
    thread = wait_for("the thread", lambda: admin.thread_by_key(key))

    score = {
        "name": "sdk_it_outcome",
        "subject": {"type": "thread", "thread_key": key},
        "source": "outcome",
        "value": True,
        "explanation": "the user confirmed the answer",
    }
    with insightmon.Client(it_config.base_url, it_config.api_key) as client:
        first = client.scores_batch([score])
        again = client.scores_batch([{**score, "idempotency_key": first[0]["idempotency_key"]}])
    assert first[0]["created"] is True
    assert again[0]["created"] is False
    assert again[0]["id"] == first[0]["id"]

    stored = [
        s for s in admin.thread_detail(thread["id"])["scores"] if s["name"] == "sdk_it_outcome"
    ]
    assert len(stored) == 1
    assert stored[0]["id"] == first[0]["id"]
    assert stored[0]["source"] == "outcome"
    assert stored[0]["subject_type"] == "thread"
    assert stored[0]["thread_id"] == thread["id"]
    assert stored[0]["value_bool"] is True
    assert stored[0]["passed"] is True


@pytest.mark.usefixtures("score_configs")
def test_scores_batch_refuses_an_unknown_score_name(it_config: ItConfig) -> None:
    score = {
        "name": "sdk_it_no_such_config",
        "subject": {"type": "thread", "thread_key": unique("sdk-it-none")},
        "source": "outcome",
        "value": True,
    }
    with (
        insightmon.Client(it_config.base_url, it_config.api_key) as client,
        pytest.raises(insightmon.InsightMonError) as caught,
    ):
        client.scores_batch([score])
    assert caught.value.code == "score_config_not_found"
    assert caught.value.status == 400
