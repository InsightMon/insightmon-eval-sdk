"""HTTP plumbing for the REST client: JSON encoding, retries, error mapping.

`sanitize_json`, `encode_json` and `backoff_delay` are shared with `client`, `_batch` and
`evaluate`; `HttpTransport` sends the requests.

Rules (docs/contract.md §0.1):

- `Authorization: Bearer <key>`, `User-Agent: insightmon-eval-sdk/<version>`,
  `Content-Type: application/json`; redirects are never followed (`redirect` error).
- JSON bodies are encoded with `allow_nan=False` after `sanitize_json` (the platform answers
  422 to NaN / Infinity; lone surrogates, which UTF-8 cannot encode, become U+FFFD).
- Retries: idempotent requests retry `InsightMonError.retryable` errors; non-idempotent ones
  only `connect_error` and 429 `rate_limited`. Waits follow `Retry-After` when given, else
  exponential backoff with full jitter. A request body is encoded once and every retry sends
  the same bytes.
- The API key lives only in the httpx client's default headers (httpx masks it in `repr`);
  it is never logged, and no `repr()` of this module shows it.
- Requests are sent with OpenTelemetry instrumentation suppressed: an httpx instrumentation
  would otherwise trace the SDK's own calls (polls, item reports) as spans of the agent, and
  inject `traceparent` into requests to the platform.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from opentelemetry import context as otel_context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

from insightmon._errors import (
    CONNECT_ERROR,
    REDIRECT,
    UNEXPECTED_RESPONSE,
    InsightMonError,
)
from insightmon._version import __version__

__all__ = [
    "MAX_JSON_DEPTH",
    "USER_AGENT",
    "HttpTransport",
    "backoff_delay",
    "clean_text",
    "encode_json",
    "sanitize_json",
]

USER_AGENT = f"insightmon-eval-sdk/{__version__}"

log = logging.getLogger("insightmon")

#: Module level so tests can replace them (`HttpTransport(sleep=...)` also works).
_sleep: Callable[[float], None] = time.sleep
_jitter: Callable[[], float] = random.random


#: Nesting depth `sanitize_json` accepts (a deeper or self-containing value is refused).
MAX_JSON_DEPTH = 100


def clean_text(text: str) -> str:
    """`text` with lone UTF-16 surrogates replaced by U+FFFD: they cannot be encoded as UTF-8
    (e.g. text cut in the middle of an emoji by a service that counts UTF-16 units). A
    surrogate pair becomes the character it encodes."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


def sanitize_json(value: Any, _depth: int = 0) -> Any:
    """A JSON-safe copy of `value`: non-finite floats → None, tuples / sets → lists (sets
    sorted when possible), mapping keys → str, lone surrogates in strings and keys → U+FFFD
    (`clean_text`). Other non-JSON objects raise TypeError; a value nested more than
    `MAX_JSON_DEPTH` levels deep (or containing itself) raises ValueError."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if _depth >= MAX_JSON_DEPTH:
        raise ValueError(
            f"the value is nested more than {MAX_JSON_DEPTH} levels deep (or contains itself)"
        )
    depth = _depth + 1
    if isinstance(value, Mapping):
        return {clean_text(str(k)): sanitize_json(v, depth) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [sanitize_json(v, depth) for v in value]
    if isinstance(value, set | frozenset):
        items = [sanitize_json(v, depth) for v in value]
        try:
            return sorted(items)
        except TypeError:
            return items
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def encode_json(value: Any) -> bytes:
    """Compact UTF-8 JSON of `sanitize_json(value)` (`allow_nan=False`)."""
    return json.dumps(
        sanitize_json(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def backoff_delay(
    attempt: int,
    *,
    base_s: float = 0.5,
    cap_s: float = 8.0,
    retry_after_s: float | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before retry number `attempt` (1-based): `retry_after_s` when given
    (capped at 60 s), else full jitter in [0, min(cap, base * 2**(attempt-1))]."""
    if retry_after_s is not None:
        return min(max(0.0, retry_after_s), 60.0)
    ceiling = min(cap_s, base_s * float(2 ** max(0, attempt - 1)))
    return ceiling * rng()


class HttpTransport:
    """A small synchronous JSON client of the platform's REST API. Thread-safe.

    Args:
        base_url: normalized platform root URL (no trailing slash).
        api_key: the `imk_...` key.
        timeout: seconds per request (connect, read, write, pool).
        max_retries: retries after the first attempt (0 = no retry).
        transport: an httpx transport (tests pass `httpx.MockTransport`).
        sleep: the sleep function used between retries (tests pass a fake).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.base_url = base_url
        self.max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"HttpTransport(base_url={self.base_url!r}, max_retries={self.max_retries})"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool = True,
        expected: tuple[int, ...] = (200,),
        max_retries: int | None = None,
    ) -> Any:
        """Send one request (with retries) and return the decoded JSON body.

        `content` is an already encoded JSON body (sent as is, byte-identical on every
        retry); otherwise `json_body` is encoded once with `encode_json`. `max_retries`
        overrides the transport's budget for this request.

        Raises:
            InsightMonError: a platform error (status set) once retries are exhausted or the
                error is not retryable; `unexpected_response` when the status is not in
                `expected` or the body is not JSON; `redirect` for 3xx. `attempts` tells how
                many times the request was sent.
        """
        body = content
        if body is None and json_body is not None:
            body = encode_json(json_body)
        headers = {"Content-Type": "application/json"} if body is not None else None
        query = {k: v for k, v in (params or {}).items() if v is not None}
        retries = self.max_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._send(method, path, body, headers, query, expected)
            except InsightMonError as err:
                err.attempts = attempt
                if attempt > retries or not _may_retry(err, idempotent=idempotent):
                    raise
                delay = backoff_delay(attempt, retry_after_s=err.retry_after_s, rng=_jitter)
                log.info(
                    "insightmon: %s %s failed (%s), retry %d/%d in %.1f s",
                    method,
                    path,
                    err.code if err.status is None else f"HTTP {err.status} {err.code}",
                    attempt,
                    retries,
                    delay,
                )
                (self._sleep or _sleep)(delay)

    def _send(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
        query: Mapping[str, Any],
        expected: tuple[int, ...],
    ) -> Any:
        token = otel_context.attach(otel_context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            response = self._client.request(
                method, path, content=body, headers=headers, params=query or None
            )
        except httpx.TransportError as exc:
            raise InsightMonError.from_transport_error(exc) from exc
        except httpx.RequestError as exc:  # e.g. a response body that cannot be decoded
            raise InsightMonError(UNEXPECTED_RESPONSE, None, type(exc).__name__) from exc
        finally:
            otel_context.detach(token)
        status = response.status_code
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            raise InsightMonError(
                REDIRECT,
                status,
                f"{method} {path} was redirected to {location!r}; the SDK does not follow "
                "redirects (set the base URL to the final address)",
                {"location": location},
            )
        if status >= 400:
            raise InsightMonError.from_response(response)
        if status not in expected:
            raise InsightMonError(
                UNEXPECTED_RESPONSE,
                status,
                f"{method} {path} answered HTTP {status}, expected {expected}",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise InsightMonError(
                UNEXPECTED_RESPONSE, status, f"{method} {path} did not answer JSON"
            ) from exc

    def close(self) -> None:
        self._client.close()


def _may_retry(error: InsightMonError, *, idempotent: bool) -> bool:
    """Idempotent requests retry every retryable error; the others only errors that prove the
    server did not act: a connection that never opened, or a 429 (refused before any work)."""
    if not error.retryable:
        return False
    if idempotent:
        return True
    return error.code == CONNECT_ERROR or error.status == 429
