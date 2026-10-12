"""The public surface: every name in `insightmon.__all__` exists and keeps its signature shape."""

from __future__ import annotations

import inspect
from pathlib import Path

import insightmon

KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY


def test_all_names_resolve() -> None:
    missing = [name for name in insightmon.__all__ if not hasattr(insightmon, name)]
    assert not missing
    assert sorted(insightmon.__all__) == list(insightmon.__all__)


def test_version() -> None:
    assert insightmon.__version__ == "0.1.0"


def test_package_is_typed() -> None:
    assert (Path(insightmon.__file__).parent / "py.typed").is_file()


def test_init_takes_keywords_only() -> None:
    params = inspect.signature(insightmon.init).parameters
    assert params
    assert all(p.kind is KEYWORD_ONLY for p in params.values())
    assert {"base_url", "api_key", "agent_name", "agent_version", "capture_content"} <= set(params)
    assert params["capture_content"].default is False


def test_evaluate_and_aevaluate_share_a_signature() -> None:
    sync = inspect.signature(insightmon.evaluate)
    async_ = inspect.signature(insightmon.aevaluate)
    assert list(sync.parameters) == list(async_.parameters)
    assert sync.parameters["task"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind is KEYWORD_ONLY for name, p in sync.parameters.items() if name != "task")
    assert inspect.iscoroutinefunction(insightmon.aevaluate)


def test_log_evaluation_correlation_arguments() -> None:
    params = inspect.signature(insightmon.log_evaluation).parameters
    assert {"value", "label", "explanation", "error_type", "span", "response_id"} <= set(params)


def test_error_is_an_exception() -> None:
    err = insightmon.InsightMonError("rate_limited", 429, retry_after_s=2.0)
    assert isinstance(err, Exception)
    assert err.retryable


def test_rejections_start_empty() -> None:
    assert insightmon.rejections() == insightmon.Rejections()
    assert insightmon.rejections().log_records == 0
