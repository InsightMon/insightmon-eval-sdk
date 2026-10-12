"""`insightmon._http.HttpTransport`: headers, retries, error mapping."""

from __future__ import annotations

import logging

import httpx
import pytest

from insightmon import _http
from insightmon._errors import (
    CONNECT_ERROR,
    NETWORK_ERROR,
    REDIRECT,
    TIMEOUT,
    UNEXPECTED_RESPONSE,
    InsightMonError,
)
from insightmon._http import USER_AGENT, HttpTransport, _may_retry
from tests.conftest import API_KEY, BASE_URL, MockApi, error_response

PATH = "/api/v1/thing"


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(_http, "_sleep", recorded.append)
    return recorded


def make(mock_api: MockApi, **kwargs: object) -> HttpTransport:
    return HttpTransport(BASE_URL, API_KEY, transport=mock_api.transport, **kwargs)  # type: ignore[arg-type]


def test_headers_and_json_body(mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, httpx.Response(200, json={"ok": True}))
    http = make(mock_api)
    assert http.request("POST", PATH, json_body={"text": "晴れ", "x": float("nan")}) == {"ok": True}
    (request,) = mock_api.requests
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"
    assert request.content == '{"text":"晴れ","x":null}'.encode()
    assert str(request.url) == f"{BASE_URL}{PATH}"


def test_get_has_no_body_and_drops_none_params(mock_api: MockApi) -> None:
    mock_api.add("GET", PATH, {"items": []})
    make(mock_api).request("GET", PATH, params={"limit": 5, "cursor": None})
    (request,) = mock_api.requests
    assert request.content == b""
    assert "content-type" not in request.headers
    assert dict(request.url.params) == {"limit": "5"}


def test_base_url_prefix_is_kept(mock_api: MockApi) -> None:
    mock_api.add("GET", "/eval/api/v1/thing", {"ok": 1})
    http = HttpTransport(f"{BASE_URL}/eval", API_KEY, transport=mock_api.transport)
    assert http.request("GET", PATH) == {"ok": 1}


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_idempotent_request_retries_5xx_then_succeeds(
    mock_api: MockApi, sleeps: list[float], status: int
) -> None:
    mock_api.add("POST", PATH, error_response(status, "internal_error"), {"done": True})
    assert make(mock_api).request("POST", PATH, json_body={"a": 1}) == {"done": True}
    assert len(mock_api.requests) == 2
    assert len(sleeps) == 1
    assert 0.0 <= sleeps[0] <= 0.5


def test_retry_after_header_is_honored(mock_api: MockApi, sleeps: list[float]) -> None:
    limited = error_response(
        429, "rate_limited", {"retry_after_s": 9}, headers={"Retry-After": "3"}
    )
    mock_api.add("GET", PATH, limited, {"ok": True})
    assert make(mock_api).request("GET", PATH) == {"ok": True}
    assert sleeps == [3.0]


def test_retry_after_param_without_header(mock_api: MockApi, sleeps: list[float]) -> None:
    mock_api.add("GET", PATH, error_response(429, "rate_limited", {"retry_after_s": 4}), {"x": 1})
    make(mock_api).request("GET", PATH)
    assert sleeps == [4.0]


def test_retries_are_bounded_and_backoff_grows(
    mock_api: MockApi, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http, "_jitter", lambda: 1.0)
    mock_api.add("GET", PATH, error_response(503, "internal_error"))
    with pytest.raises(InsightMonError) as info:
        make(mock_api, max_retries=3).request("GET", PATH)
    assert info.value.status == 503
    assert info.value.attempts == 4
    assert len(mock_api.requests) == 4
    assert sleeps == [0.5, 1.0, 2.0]


def test_max_retries_zero(mock_api: MockApi, sleeps: list[float]) -> None:
    mock_api.add("GET", PATH, error_response(503, "internal_error"))
    with pytest.raises(InsightMonError):
        make(mock_api, max_retries=0).request("GET", PATH)
    assert len(mock_api.requests) == 1
    assert sleeps == []


def test_sleep_argument_overrides_module_sleep(mock_api: MockApi) -> None:
    waited: list[float] = []
    mock_api.add("GET", PATH, error_response(502, "internal_error"), {"ok": 1})
    make(mock_api, sleep=waited.append).request("GET", PATH)
    assert len(waited) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "external_item_invalid"),
        (401, "not_authenticated"),
        (403, "permission_denied"),
        (404, "not_found"),
        (409, "invalid_state"),
        (413, "payload_too_large"),
        (415, "otlp_content_type_unsupported"),
        (422, "validation_error"),
        (429, "judge_budget_exhausted"),
        (429, "translation_budget_exhausted"),
    ],
)
def test_client_errors_are_not_retried(
    mock_api: MockApi, sleeps: list[float], status: int, code: str
) -> None:
    mock_api.add("POST", PATH, error_response(status, code, {"k": "v"}, "nope"))
    with pytest.raises(InsightMonError) as info:
        make(mock_api).request("POST", PATH, json_body={})
    err = info.value
    assert (err.code, err.status, err.params, err.message) == (code, status, {"k": "v"}, "nope")
    assert err.attempts == 1
    assert len(mock_api.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ConnectError("refused"), CONNECT_ERROR),
        (httpx.ConnectTimeout("slow"), CONNECT_ERROR),
        (httpx.ReadTimeout("slow"), TIMEOUT),
        (httpx.RemoteProtocolError("closed"), NETWORK_ERROR),
    ],
)
def test_network_errors_are_retried_for_idempotent_requests(
    mock_api: MockApi, sleeps: list[float], exc: Exception, code: str
) -> None:
    mock_api.add("GET", PATH, exc, {"ok": True})
    assert make(mock_api).request("GET", PATH) == {"ok": True}
    assert len(mock_api.requests) == 2
    mock_api.routes.clear()
    mock_api.add("GET", PATH, exc)
    with pytest.raises(InsightMonError) as info:
        make(mock_api, max_retries=1).request("GET", PATH)
    assert info.value.code == code
    assert info.value.status is None


def test_non_idempotent_request_retries_only_connect_errors_and_429(
    mock_api: MockApi, sleeps: list[float]
) -> None:
    http = make(mock_api)
    mock_api.add("POST", PATH, httpx.ConnectError("refused"), httpx.Response(201, json={"id": 1}))
    assert http.request("POST", PATH, json_body={}, idempotent=False, expected=(201,)) == {"id": 1}
    mock_api.routes.clear()
    mock_api.add(
        "POST",
        PATH,
        error_response(429, "rate_limited", headers={"Retry-After": "1"}),
        httpx.Response(201, json={"id": 2}),
    )
    assert http.request("POST", PATH, json_body={}, idempotent=False, expected=(201,)) == {"id": 2}
    for answer in (
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("closed"),
        error_response(503, "internal_error"),
        error_response(500, "internal_error"),
    ):
        mock_api.routes.clear()
        mock_api.requests.clear()
        mock_api.add("POST", PATH, answer, httpx.Response(201, json={"id": 3}))
        with pytest.raises(InsightMonError):
            http.request("POST", PATH, json_body={}, idempotent=False, expected=(201,))
        assert len(mock_api.requests) == 1


def test_may_retry_rules() -> None:
    assert _may_retry(InsightMonError("rate_limited", 429), idempotent=False)
    assert not _may_retry(InsightMonError("judge_budget_exhausted", 429), idempotent=True)
    assert not _may_retry(InsightMonError("timeout"), idempotent=False)
    assert _may_retry(InsightMonError("timeout"), idempotent=True)
    assert not _may_retry(InsightMonError("validation_error", 422), idempotent=True)


def test_retries_send_identical_bytes(mock_api: MockApi, sleeps: list[float]) -> None:
    mock_api.add(
        "POST",
        PATH,
        error_response(503, "internal_error"),
        httpx.ReadTimeout("slow"),
        {"ok": True},
    )
    make(mock_api).request("POST", PATH, json_body={"items": [{"b": 2, "a": [1.5, None]}]})
    bodies = [r.content for r in mock_api.requests]
    assert len(bodies) == 3
    assert len(set(bodies)) == 1


def test_preencoded_content_is_sent_as_is(mock_api: MockApi) -> None:
    mock_api.add("POST", PATH, {"ok": True})
    make(mock_api).request("POST", PATH, content=b'{"items":[]}', json_body={"ignored": 1})
    assert mock_api.requests[0].content == b'{"items":[]}'


def test_redirect_is_not_followed(mock_api: MockApi) -> None:
    mock_api.add("GET", PATH, httpx.Response(307, headers={"Location": "https://other.test/x"}))
    mock_api.add("GET", "/x", {"leak": True})
    with pytest.raises(InsightMonError) as info:
        make(mock_api).request("GET", PATH)
    assert info.value.code == REDIRECT
    assert info.value.status == 307
    assert info.value.params == {"location": "https://other.test/x"}
    assert len(mock_api.requests) == 1


def test_unexpected_status_and_non_json(mock_api: MockApi) -> None:
    http = make(mock_api)
    mock_api.add("POST", PATH, httpx.Response(200, json={"ok": True}))
    with pytest.raises(InsightMonError) as info:
        http.request("POST", PATH, json_body={}, expected=(201,))
    assert (info.value.code, info.value.status) == (UNEXPECTED_RESPONSE, 200)
    mock_api.routes.clear()
    mock_api.add("GET", PATH, httpx.Response(200, text="<html>"))
    with pytest.raises(InsightMonError) as info:
        http.request("GET", PATH)
    assert (info.value.code, info.value.status) == (UNEXPECTED_RESPONSE, 200)


def test_gateway_html_502_is_retried_then_reported(mock_api: MockApi, sleeps: list[float]) -> None:
    mock_api.add("GET", PATH, httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(InsightMonError) as info:
        make(mock_api, max_retries=2).request("GET", PATH)
    assert (info.value.code, info.value.status) == (UNEXPECTED_RESPONSE, 502)
    assert len(mock_api.requests) == 3


def test_negative_max_retries_is_refused() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        HttpTransport(BASE_URL, API_KEY, max_retries=-1)


def test_key_never_in_repr_errors_or_logs(
    mock_api: MockApi, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mock_api.add(
        "GET",
        PATH,
        error_response(503, "internal_error"),
        httpx.ConnectError("refused"),
        error_response(403, "permission_denied", {"required": "experiment_write"}),
    )
    http = make(mock_api)
    with pytest.raises(InsightMonError) as info:
        http.request("GET", PATH)
    texts = [
        repr(http),
        str(http.__dict__),
        repr(http._client.headers),
        str(info.value),
        repr(info.value),
        caplog.text,
    ]
    assert "retry" in caplog.text
    for text in texts:
        assert API_KEY not in text
    http.close()


def test_requests_run_with_instrumentation_suppressed(mock_api: MockApi) -> None:
    from opentelemetry import context as otel_context
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # What an httpx instrumentation checks before it starts a span or injects headers.
        seen.append(otel_context.get_value(_SUPPRESS_INSTRUMENTATION_KEY))
        return httpx.Response(200, json={"ok": True})

    mock_api.add("GET", "/api/v1/api-key", handler)
    transport = HttpTransport(BASE_URL, API_KEY, transport=mock_api.transport)
    transport.request("GET", "/api/v1/api-key")
    assert seen == [True]
    assert otel_context.get_value(_SUPPRESS_INSTRUMENTATION_KEY) is None  # restored
