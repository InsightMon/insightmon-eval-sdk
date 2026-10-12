from __future__ import annotations

import httpx
import pytest

from insightmon._errors import (
    CONNECT_ERROR,
    NETWORK_ERROR,
    REDIRECT,
    TIMEOUT,
    UNEXPECTED_RESPONSE,
    InsightMonError,
    is_retryable,
    parse_retry_after,
)


def test_from_response_envelope() -> None:
    response = httpx.Response(
        400,
        json={
            "code": "external_item_invalid",
            "params": {"example_key": "k1", "reason": "score_invalid"},
            "message": "Item k1 is invalid: score_invalid.",
        },
    )
    err = InsightMonError.from_response(response)
    assert (err.code, err.status) == ("external_item_invalid", 400)
    assert err.params == {"example_key": "k1", "reason": "score_invalid"}
    assert not err.retryable
    assert "external_item_invalid" in str(err)


def test_rate_limited_uses_retry_after_header_then_params() -> None:
    with_header = httpx.Response(
        429,
        json={"code": "rate_limited", "params": {"retry_after_s": 9}, "message": ""},
        headers={"Retry-After": "3"},
    )
    assert InsightMonError.from_response(with_header).retry_after_s == 3.0
    params_only = httpx.Response(
        429, json={"code": "rate_limited", "params": {"retry_after_s": 9}, "message": ""}
    )
    err = InsightMonError.from_response(params_only)
    assert err.retry_after_s == 9.0
    assert err.retryable


def test_budget_429_is_not_retryable() -> None:
    response = httpx.Response(
        429,
        json={
            "code": "judge_budget_exhausted",
            "params": {"calls": 10, "remaining": 0, "max_calls": 50000},
            "message": "",
        },
    )
    assert not InsightMonError.from_response(response).retryable


def test_non_envelope_and_redirect() -> None:
    err = InsightMonError.from_response(httpx.Response(502, text="bad gateway"))
    assert (err.code, err.status) == (UNEXPECTED_RESPONSE, 502)
    assert err.retryable
    moved = InsightMonError.from_response(
        httpx.Response(307, headers={"Location": "https://elsewhere.test/"})
    )
    assert moved.code == REDIRECT
    assert not moved.retryable


@pytest.mark.parametrize(
    ("exc", "code", "delivered"),
    [
        (httpx.ConnectError("refused"), CONNECT_ERROR, False),
        (httpx.ConnectTimeout("slow"), CONNECT_ERROR, False),
        (httpx.ReadTimeout("slow"), TIMEOUT, None),
        (httpx.RemoteProtocolError("closed"), NETWORK_ERROR, None),
    ],
)
def test_from_transport_error(exc: httpx.TransportError, code: str, delivered: bool | None) -> None:
    err = InsightMonError.from_transport_error(exc)
    assert err.code == code
    assert err.status is None
    assert err.delivered is delivered
    assert err.retryable


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("internal_error", 500, True),
        ("x", 502, True),
        ("x", 503, True),
        ("x", 504, True),
        ("rate_limited", 429, True),
        ("translation_budget_exhausted", 429, False),
        ("validation_error", 422, False),
        ("invalid_state", 409, False),
        ("payload_too_large", 413, False),
        ("not_authenticated", 401, False),
        ("config_error", None, False),
        ("timeout", None, True),
    ],
)
def test_is_retryable(code: str, status: int | None, expected: bool) -> None:
    assert is_retryable(code, status) is expected


def test_parse_retry_after() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-4") == 0.0
    assert parse_retry_after("soon") is None
    now = 1_700_000_000.0
    assert parse_retry_after("Tue, 14 Nov 2023 22:13:30 GMT", now=now) == pytest.approx(10.0)


def test_repr_has_no_surprises() -> None:
    err = InsightMonError("config_error", None, "no API key configured")
    assert repr(err).startswith("InsightMonError(code='config_error'")
    assert err.delivered is False
