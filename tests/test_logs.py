"""log_evaluation(): local validation, record shape and span correlation; EventFilterProcessor."""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import insightmon
from insightmon import _config, logs, semconv
from insightmon._config import RuntimeState, Settings
from insightmon.logs import EventFilterProcessor, evaluation_attributes, record_event_name

from .conftest import API_KEY, BASE_URL, InMemoryLogExporter


@pytest.fixture
def sdk(
    tracer_provider: TracerProvider, logger_provider: LoggerProvider
) -> insightmon.InsightMonHandle:
    return insightmon.init(tracer_provider=tracer_provider, logger_provider=logger_provider)


@pytest.fixture(autouse=True)
def _forget_warned_names() -> None:
    logs._warned_names.clear()


def records(exporter: Any) -> list[Any]:
    return [item.log_record for item in exporter.get_finished_logs()]


# --------------------------------------------------------------------------- attributes


def test_evaluation_attributes_shape() -> None:
    assert evaluation_attributes("helpfulness", value=0.8) == {
        "event.name": "gen_ai.evaluation.result",
        "gen_ai.evaluation.name": "helpfulness",
        "gen_ai.evaluation.score.value": 0.8,
    }
    full = evaluation_attributes(
        "Answer Quality!", label=" pass ", explanation="fine", response_id=" r-1 "
    )
    assert full == {
        "event.name": "gen_ai.evaluation.result",
        "gen_ai.evaluation.name": "answer_quality",
        "gen_ai.evaluation.score.label": "pass",
        "gen_ai.evaluation.explanation": "fine",
        "gen_ai.response.id": "r-1",
    }
    failed = evaluation_attributes("helpfulness", error_type="TimeoutError")
    assert failed[semconv.ERROR_TYPE] == "TimeoutError"


def test_bool_values_become_numbers() -> None:
    assert evaluation_attributes("ok", value=True)[semconv.EVALUATION_SCORE_VALUE] == 1.0
    assert evaluation_attributes("ok", value=False)[semconv.EVALUATION_SCORE_VALUE] == 0.0
    assert evaluation_attributes("ok", value=3)[semconv.EVALUATION_SCORE_VALUE] == 3.0


def test_explanation_is_cut_to_the_platform_cap() -> None:
    attributes = evaluation_attributes("ok", value=1, explanation="e" * 5000)
    assert len(attributes[semconv.EVALUATION_EXPLANATION]) == semconv.EVALUATION_EXPLANATION_MAX
    assert semconv.EVALUATION_EXPLANATION not in evaluation_attributes(
        "ok", value=1, explanation="   "
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": ""}, "non-empty"),
        ({"name": "!!!"}, "no letter"),
        ({"name": "ok"}, "needs one of"),
        ({"name": "ok", "value": 1.0, "label": "pass"}, "exactly one"),
        ({"name": "ok", "value": 1.0, "error_type": "Timeout"}, "exactly one"),
        ({"name": "ok", "value": float("nan")}, "finite"),
        ({"name": "ok", "value": float("inf")}, "finite"),
        ({"name": "ok", "value": "0.5"}, "number"),
        ({"name": "ok", "label": "  "}, "label"),
        ({"name": "ok", "error_type": ""}, "error_type"),
        ({"name": "ok", "value": 1, "explanation": 5}, "explanation"),
        ({"name": "ok", "value": 1, "response_id": "r" * 201}, "response_id"),
    ],
)
def test_platform_rejections_are_caught_locally(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        evaluation_attributes(**kwargs)


# --------------------------------------------------------------------------- correlation


def test_record_points_at_the_evaluated_span(
    sdk: insightmon.InsightMonHandle,
    log_exporter: Any,
    span_exporter: InMemorySpanExporter,
) -> None:
    with insightmon.thread("chat-1"):
        with insightmon.turn() as evaluated:
            pass
        with insightmon.turn("later"):
            insightmon.log_evaluation("helpfulness", value=0.9, span=evaluated)
    (record,) = records(log_exporter)
    context = evaluated.get_span_context()
    assert record.trace_id == context.trace_id
    assert record.span_id == context.span_id
    assert record.event_name == semconv.EVALUATION_RESULT_EVENT
    assert record.attributes[semconv.EVENT_NAME] == semconv.EVALUATION_RESULT_EVENT
    assert record.attributes[semconv.EVALUATION_SCORE_VALUE] == 0.9


def test_record_defaults_to_the_current_span(
    sdk: insightmon.InsightMonHandle, log_exporter: Any
) -> None:
    with insightmon.turn() as span:
        insightmon.log_evaluation("helpfulness", label="pass")
    (record,) = records(log_exporter)
    assert record.span_id == span.get_span_context().span_id


def test_record_accepts_a_span_context(sdk: insightmon.InsightMonHandle, log_exporter: Any) -> None:
    with insightmon.turn() as span:
        pass
    insightmon.log_evaluation("helpfulness", value=1, span=span.get_span_context())
    (record,) = records(log_exporter)
    assert record.span_id == span.get_span_context().span_id


def test_response_id_alone_never_takes_the_current_span(
    sdk: insightmon.InsightMonHandle, log_exporter: Any
) -> None:
    with insightmon.turn():
        insightmon.log_evaluation("helpfulness", value=1, response_id="resp-9")
    (record,) = records(log_exporter)
    assert not record.trace_id
    assert not record.span_id
    assert record.attributes[semconv.RESPONSE_ID] == "resp-9"


def test_span_and_response_id_together(sdk: insightmon.InsightMonHandle, log_exporter: Any) -> None:
    with insightmon.turn() as span:
        pass
    insightmon.log_evaluation("helpfulness", value=1, span=span, response_id="resp-9")
    (record,) = records(log_exporter)
    assert record.span_id == span.get_span_context().span_id
    assert record.attributes[semconv.RESPONSE_ID] == "resp-9"


def test_no_correlation_is_an_error(sdk: insightmon.InsightMonHandle, log_exporter: Any) -> None:
    with pytest.raises(ValueError, match="correlate"):
        insightmon.log_evaluation("helpfulness", value=1)
    with pytest.raises(ValueError, match="correlate"):
        insightmon.log_evaluation("helpfulness", value=1, span=trace.INVALID_SPAN)
    insightmon.log_evaluation("helpfulness", value=1, span=trace.INVALID_SPAN, response_id="r")
    assert len(records(log_exporter)) == 1


def test_without_a_logger_provider_records_go_nowhere(tracer_provider: TracerProvider) -> None:
    insightmon.init(tracer_provider=tracer_provider)  # export disabled: no logger provider
    with insightmon.turn():
        insightmon.log_evaluation("helpfulness", value=1)  # validated, then dropped
        with pytest.raises(ValueError, match="needs one of"):
            insightmon.log_evaluation("helpfulness")
    insightmon.shutdown()
    with insightmon.turn():  # before init(): the global no-op provider
        insightmon.log_evaluation("helpfulness", value=1, response_id="r")


def test_renamed_names_warn_once(
    sdk: insightmon.InsightMonHandle, log_exporter: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="insightmon.logs"), insightmon.turn():
        insightmon.log_evaluation("Answer Quality", value=1)
        insightmon.log_evaluation("Answer Quality", value=0)
        insightmon.log_evaluation("answer_quality", value=0)
    warnings = [r for r in caplog.records if "answer_quality" in r.getMessage()]
    assert len(warnings) == 1
    assert {r.attributes[semconv.EVALUATION_NAME] for r in records(log_exporter)} == {
        "answer_quality"
    }


def exporting_runtime(tracer_provider: TracerProvider, logger_provider: LoggerProvider) -> None:
    """A runtime that exports to the platform (as after `init(base_url=..., api_key=...)`),
    without real exporters."""
    _config.set_runtime(
        RuntimeState(
            settings=Settings(base_url=BASE_URL, api_key=API_KEY),
            tracer_provider=tracer_provider,
            logger_provider=logger_provider,
            export_control=object(),
        )
    )


def join_name_check() -> None:
    for thread in threading.enumerate():
        if thread.name == "insightmon-score-names":
            thread.join(5)


def unknown_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "is not a published score config" in r.getMessage()
    ]


def test_unknown_names_warn_once_when_the_platform_lists_the_configs(
    tracer_provider: TracerProvider,
    logger_provider: LoggerProvider,
    log_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetched: list[str | None] = []
    answer = threading.Event()

    def fetch(settings: Settings) -> frozenset[str]:
        fetched.append(settings.base_url)
        answer.wait(5)
        return frozenset({"helpfulness"})

    monkeypatch.setattr(logs, "_fetch_score_names", fetch)
    exporting_runtime(tracer_provider, logger_provider)
    with caplog.at_level(logging.WARNING, logger="insightmon.logs"), insightmon.turn():
        insightmon.log_evaluation("helpfulness", value=1)
        insightmon.log_evaluation("thumbs", value=1)  # the configs are not known yet: noted
        answer.set()
        join_name_check()
        insightmon.log_evaluation("thumbs", value=0)
        insightmon.log_evaluation("Other Name", value=0)
    warnings = unknown_warnings(caplog)
    assert len(warnings) == 2  # once per name
    assert "'thumbs'" in warnings[0]
    assert "'other_name'" in warnings[1]
    assert fetched == [BASE_URL]  # one read per runtime
    assert len(records(log_exporter)) == 4  # sent either way


def test_names_are_not_checked_when_the_platform_does_not_list_the_configs(
    tracer_provider: TracerProvider,
    logger_provider: LoggerProvider,
    log_exporter: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporting_runtime(tracer_provider, logger_provider)  # the conftest read answers None
    with caplog.at_level(logging.WARNING, logger="insightmon.logs"), insightmon.turn():
        insightmon.log_evaluation("thumbs", value=1)
        join_name_check()
        insightmon.log_evaluation("thumbs", value=0)
    assert unknown_warnings(caplog) == []
    assert len(records(log_exporter)) == 2


def test_names_are_not_checked_without_export_to_the_platform(
    sdk: insightmon.InsightMonHandle, log_exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(settings: Settings) -> frozenset[str]:
        raise AssertionError("no platform to ask")

    monkeypatch.setattr(logs, "_fetch_score_names", fetch)
    with insightmon.turn():
        insightmon.log_evaluation("thumbs", value=1)
    assert logs._name_check is None
    assert len(records(log_exporter)) == 1


# --------------------------------------------------------------------------- filtering


def test_event_filter_forwards_only_allowed_events() -> None:
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(
        EventFilterProcessor(SimpleLogRecordProcessor(exporter), {"gen_ai.evaluation.result"})
    )
    logger = provider.get_logger("app")
    context = trace.set_span_in_context(trace.INVALID_SPAN)
    logs.emit_event(logger, context, "gen_ai.evaluation.result", {"gen_ai.evaluation.name": "a"})
    logs.emit_event(logger, context, "gen_ai.user.message", {})
    # an application log line with the name only in the attribute
    logs.emit_event(logger, context, "", {"event.name": "gen_ai.evaluation.result"})
    logs.emit_event(logger, context, "", {"event.name": "app.audit"})
    names = [record_event_name(item) for item in exporter.get_finished_logs()]
    assert names == ["gen_ai.evaluation.result", "gen_ai.evaluation.result"]
    filter_processor = EventFilterProcessor(SimpleLogRecordProcessor(exporter))
    assert filter_processor.allowed == frozenset({"gen_ai.evaluation.result"})
    assert filter_processor.force_flush() is True
    provider.shutdown()
