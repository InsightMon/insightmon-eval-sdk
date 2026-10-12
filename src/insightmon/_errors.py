"""`InsightMonError` and the retry classification of platform errors (docs/contract.md §0.1, §7).

Platform errors keep the platform's `code`, HTTP `status` and `params`. Errors the SDK raises on
its own have `status = None` and one of the SDK codes below.
"""

from __future__ import annotations

import email.utils
import math
import time
from collections.abc import Mapping
from typing import Any, Final

import httpx

# --------------------------------------------------------------------------- SDK codes

#: The request never reached the server (DNS, refused connection, connect timeout). Always safe
#: to retry, even for requests that are not idempotent.
CONNECT_ERROR: Final = "connect_error"
#: The connection broke after the request was sent: the server may have processed it.
NETWORK_ERROR: Final = "network_error"
#: No response in time after the request was sent: the server may have processed it.
TIMEOUT: Final = "timeout"
#: The platform answered with a redirect; the SDK never follows one (the key must not travel).
REDIRECT: Final = "redirect"
#: The response is not the documented JSON (wrong content type, missing fields).
UNEXPECTED_RESPONSE: Final = "unexpected_response"
#: Missing base URL / API key, a malformed key, an invalid argument combination.
CONFIG_ERROR: Final = "config_error"

SDK_CODES: Final[frozenset[str]] = frozenset(
    {CONNECT_ERROR, NETWORK_ERROR, TIMEOUT, REDIRECT, UNEXPECTED_RESPONSE, CONFIG_ERROR}
)

# --------------------------------------------------------------------------- platform codes

RATE_LIMITED: Final = "rate_limited"
INVALID_STATE: Final = "invalid_state"
EXTERNAL_ITEM_INVALID: Final = "external_item_invalid"
#: 429 codes that are daily budgets: retrying within the same UTC day cannot succeed.
NON_RETRYABLE_429: Final[frozenset[str]] = frozenset(
    {"judge_budget_exhausted", "translation_budget_exhausted"}
)
#: Server-side statuses worth retrying for idempotent requests.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})
#: SDK codes worth retrying for idempotent requests.
RETRYABLE_SDK_CODES: Final[frozenset[str]] = frozenset({CONNECT_ERROR, NETWORK_ERROR, TIMEOUT})


class InsightMonError(Exception):
    """An error from the platform (`status` set) or from the SDK itself (`status = None`).

    Attributes:
        code: the platform error code (e.g. `rate_limited`) or an SDK code (e.g. `timeout`).
        status: the HTTP status, or None for SDK errors.
        message: a human readable message (the platform's English message when present).
        params: the platform's `params` object (empty for SDK errors unless given).
        retry_after_s: seconds to wait before retrying (429), when known.
        attempts: how many times the REST transport sent the request before giving up with
            this error (1 = the first attempt failed for good).
    """

    def __init__(
        self,
        code: str,
        status: int | None = None,
        message: str = "",
        params: Mapping[str, Any] | None = None,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.message = message
        self.params: dict[str, Any] = dict(params or {})
        self.retry_after_s = retry_after_s
        self.attempts = 1
        super().__init__(self.__str__())

    def __str__(self) -> str:
        where = f"HTTP {self.status} " if self.status is not None else ""
        text = f"{where}{self.code}"
        if self.message:
            text = f"{text}: {self.message}"
        return text

    def __repr__(self) -> str:
        return (
            f"InsightMonError(code={self.code!r}, status={self.status!r}, "
            f"message={self.message!r}, params={self.params!r})"
        )

    @property
    def retryable(self) -> bool:
        """Whether an **idempotent** request that failed with this error may be retried."""
        return is_retryable(self.code, self.status)

    @property
    def delivered(self) -> bool | None:
        """False when the request certainly never reached the server, True when the server
        answered, None when unknown (broken connection, timeout)."""
        if self.status is not None:
            return True
        if self.code == CONNECT_ERROR:
            return False
        if self.code in (NETWORK_ERROR, TIMEOUT):
            return None
        return False

    @classmethod
    def from_response(cls, response: httpx.Response) -> InsightMonError:
        """Build the error of a non-2xx response from the platform envelope
        `{code, params, message}`; a body that is not the envelope gives `unexpected_response`
        with the HTTP status kept."""
        status = response.status_code
        header_wait = parse_retry_after(response.headers.get("retry-after"))
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("code"), str):
            params = body.get("params")
            params = params if isinstance(params, dict) else {}
            message = body.get("message")
            wait = header_wait
            if wait is None:
                wait = _seconds(params.get("retry_after_s"))
            return cls(
                body["code"],
                status,
                message if isinstance(message, str) else "",
                params,
                retry_after_s=wait,
            )
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            return cls(REDIRECT, status, f"redirect to {location!r} not followed")
        snippet = response.text[:200] if response.content else ""
        return cls(UNEXPECTED_RESPONSE, status, snippet, retry_after_s=header_wait)

    @classmethod
    def from_transport_error(cls, exc: httpx.TransportError) -> InsightMonError:
        """Map an httpx transport exception to `connect_error`, `timeout` or `network_error`."""
        if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
            return cls(CONNECT_ERROR, None, str(exc) or type(exc).__name__)
        if isinstance(exc, httpx.TimeoutException):
            return cls(TIMEOUT, None, str(exc) or type(exc).__name__)
        return cls(NETWORK_ERROR, None, str(exc) or type(exc).__name__)


def with_message(error: InsightMonError, message: str) -> InsightMonError:
    """A copy of `error` (same code, status, params, retry hint and attempts) with another
    message, e.g. one that names the experiment an error interrupted."""
    copy = InsightMonError(
        error.code, error.status, message, error.params, retry_after_s=error.retry_after_s
    )
    copy.attempts = error.attempts
    return copy


def with_params(error: InsightMonError, params: Mapping[str, Any]) -> InsightMonError:
    """A copy of `error` (same code, status, message, retry hint and attempts) with other
    `params`, e.g. what a multi-request call wrote before it failed."""
    copy = InsightMonError(
        error.code, error.status, error.message, params, retry_after_s=error.retry_after_s
    )
    copy.attempts = error.attempts
    return copy


def is_retryable(code: str, status: int | None) -> bool:
    """Retry classification for idempotent requests (docs/contract.md §0.1).

    - SDK errors: `connect_error`, `network_error`, `timeout`;
    - 429 except the daily budgets (`judge_budget_exhausted`, `translation_budget_exhausted`);
    - 500, 502, 503, 504.
    """
    if status is None:
        return code in RETRYABLE_SDK_CODES
    if status == 429:
        return code not in NON_RETRYABLE_429
    return status in RETRYABLE_STATUSES


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds from a `Retry-After` header (delta seconds or an HTTP date); None if absent or
    unparsable. Never negative."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    seconds = _seconds(value)
    if seconds is not None:
        return seconds
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    current = time.time() if now is None else now
    return max(0.0, moment.timestamp() - current)


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, number)
