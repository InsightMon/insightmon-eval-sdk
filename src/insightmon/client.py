"""`Client`: the platform's key-only REST API (docs/contract.md §4 to §6).

Every route is key-only: the project is the key's project, so no method takes a project id.
Retries follow docs/contract.md §0.1 (see `insightmon._http`):

- GETs, `items:batch` (deduplicated by report content), `scores:batch` (the SDK fills an
  `idempotency_key` into every score) and `:finalize` retry 429, 5xx and network errors;
- `POST /experiments` has no idempotency key on the platform: it is retried only when the
  connection never opened or on 429 `rate_limited`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, TypeVar
from urllib.parse import quote

import httpx

from insightmon import semconv
from insightmon._config import (
    ENV_API_KEY,
    ENV_BASE_URL,
    coerce_component,
    get_settings,
    normalize_base_url,
    validate_api_key,
)
from insightmon._errors import (
    CONFIG_ERROR,
    INVALID_STATE,
    UNEXPECTED_RESPONSE,
    InsightMonError,
    with_message,
    with_params,
)
from insightmon._http import HttpTransport, encode_json

__all__ = ["ApiKeyInfo", "Client", "Example", "ExamplesPage", "ReportResult"]

API_PREFIX = "/api/v1"
#: Sources an API key may write (`scores:batch`); anything else is 400
#: `source_not_allowed_for_key`. The SDK never writes `human` scores or adjudications.
KEY_SCORE_SOURCES: frozenset[str] = frozenset({"end_user", "outcome", "import"})
#: Score metadata keys only the platform writes (adjudications, batch acceptance, assisted
#: decisions, recusal fallbacks, blind review labels): `scores:batch` refuses them and the SDK
#: never sends them (`evaluate()` drops them from client scores with a warning).
RESERVED_SCORE_METADATA_KEYS: frozenset[str] = frozenset(
    {"adjudicated", "accepted_by", "mt_assisted", "self_review", "verification_id"}
)
EVALUATOR_ROLES: frozenset[str] = frozenset({"gating", "diagnostic"})
#: Retries of `items:batch` and `:finalize`: both are idempotent, and a platform restart or
#: a proxy blip of a few seconds must not stop a long `evaluate()` run (up to about 40 s of
#: backoff: 0.5, 1, 2, 4, then 8 s ceilings with full jitter).
REPORT_RETRIES = 8

_T = TypeVar("_T")


@dataclass(frozen=True)
class ApiKeyInfo:
    """`GET /api/v1/api-key`: the key's id, name, prefix, project and scopes."""

    id: str
    name: str
    prefix: str
    project_id: str
    scopes: tuple[str, ...]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ApiKeyInfo:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            prefix=str(data["prefix"]),
            project_id=str(data["project_id"]),
            scopes=tuple(str(s) for s in data.get("scopes") or ()),
        )


@dataclass(frozen=True)
class Example:
    """One frozen example of an external experiment (`GET .../examples`)."""

    example_key: str
    example_id: str
    input: Any
    initial_state: dict[str, Any] = field(default_factory=dict)
    reference: Any = None
    language: str = "und"
    metadata: dict[str, Any] = field(default_factory=dict)
    trials: int = 1

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Example:
        return cls(
            example_key=str(data["example_key"]),
            example_id=str(data["example_id"]),
            input=data.get("input"),
            initial_state=dict(data.get("initial_state") or {}),
            reference=data.get("reference"),
            language=str(data.get("language") or "und"),
            metadata=dict(data.get("metadata") or {}),
            trials=int(data.get("trials") or 1),
        )


@dataclass(frozen=True)
class ExamplesPage:
    items: list[Example]
    next_cursor: str | None


@dataclass(frozen=True)
class ReportResult:
    """`items:batch` totals over every request the call made. `item_ids` are not in request
    order (the platform lists new items first, then duplicates)."""

    accepted: int
    duplicates: int
    item_ids: list[str]


class Client:
    """Synchronous client of the key-only REST API.

    Args:
        base_url: platform root URL (default: `init()` settings, then `INSIGHTMON_BASE_URL`).
        api_key: `imk_...` key (default: `init()` settings, then `INSIGHTMON_API_KEY`).
        timeout: seconds per request.
        max_retries: retries of retryable errors (see docs/contract.md §0.1).
        report_retries: retries of `items:batch` and `:finalize` (idempotent, so retried
            longer: a short outage must not stop an `evaluate()` run).
        transport: an httpx transport (tests: `httpx.MockTransport`).

    Raises:
        InsightMonError: `config_error` when no base URL or key can be found, or the key does
            not start with `imk_`.

    Thread-safe: one client may be shared by worker threads. The key never appears in
    `repr()` or in log records.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        report_retries: int = REPORT_RETRIES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if report_retries < 0:
            raise ValueError("report_retries must be >= 0")
        self._report_retries = report_retries
        settings = get_settings()
        url = base_url or (settings.base_url if settings is not None else None)
        url = url or os.environ.get(ENV_BASE_URL) or None
        key = api_key or (settings.api_key if settings is not None else None)
        key = key or os.environ.get(ENV_API_KEY) or None
        if url is None:
            raise InsightMonError(
                CONFIG_ERROR, None, f"no base URL: pass base_url or set {ENV_BASE_URL}"
            )
        if key is None:
            raise InsightMonError(
                CONFIG_ERROR, None, f"no API key: pass api_key or set {ENV_API_KEY}"
            )
        self._http = HttpTransport(
            normalize_base_url(url),
            validate_api_key(key),
            timeout=timeout,
            max_retries=max_retries,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return self._http.base_url

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r})"

    # ------------------------------------------------------------------ key

    def api_key_info(self) -> ApiKeyInfo:
        """`GET /api/v1/api-key` (any scope)."""
        data = self._object("GET", "/api-key")
        return _parse(ApiKeyInfo.from_json, data, "GET /api/v1/api-key")

    # ------------------------------------------------------------------ scores

    def scores_batch(self, scores: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """`POST /api/v1/scores:batch` (scope `score_write`) in chunks of 500.

        Each score is a `ScoreInput` mapping (docs/contract.md §5). A missing
        `idempotency_key` is filled with a random UUID before the first attempt, so the
        transport's retries never write twice. Returns the platform's `items` of every chunk,
        with `index` rewritten to the position in `scores`. Chunks are also kept under
        `semconv.TARGET_BODY_BYTES`.

        A failing chunk raises; earlier chunks stay written. The error's `params` then also
        carry `written` (scores written before the failing chunk) and `idempotency_keys` (the
        key of every score, in order; a platform `index` is rewritten to the position in
        `scores`): call again with those keys set and the platform writes nothing twice.

        Raises:
            ValueError: a score the platform would refuse for any key: not a mapping, a
                `source` other than end_user / outcome / import, a metadata key reserved for
                the platform (`RESERVED_SCORE_METADATA_KEYS`: `adjudicated`, `accepted_by`,
                ...), a bad `idempotency_key`, or one score alone larger than the body limit.
            InsightMonError: platform errors (e.g. 400 `score_config_not_found`).
        """
        prepared = [_prepare_score(score, index) for index, score in enumerate(scores)]
        keys = [str(score["idempotency_key"]) for score in prepared]
        out: list[dict[str, Any]] = []
        offset = 0
        for chunk, body in plan_requests("scores", prepared, max_items=semconv.SCORES_BATCH_MAX):
            try:
                data = self._object("POST", "/scores:batch", content=body)
            except InsightMonError as err:
                params = {**err.params, "written": offset, "idempotency_keys": keys}
                index = err.params.get("index")
                if isinstance(index, int) and not isinstance(index, bool):
                    params["index"] = offset + index
                raise with_params(err, params) from err
            items = data.get("items")
            if not isinstance(items, list):
                raise InsightMonError(
                    UNEXPECTED_RESPONSE, None, "POST /api/v1/scores:batch answered no items"
                )
            for entry in items:
                if not isinstance(entry, dict) or not isinstance(entry.get("index"), int):
                    raise InsightMonError(
                        UNEXPECTED_RESPONSE, None, "POST /api/v1/scores:batch: bad item"
                    )
                out.append({**entry, "index": offset + entry["index"]})
            offset += len(chunk)
        return out

    # ------------------------------------------------------------------ experiments

    def create_experiment(
        self,
        *,
        name: str,
        dataset_id: str,
        dataset_version: int | None = None,
        example_keys: Sequence[str] | None = None,
        trials: int = 1,
        requested: Mapping[str, Any] | None = None,
        evaluators: Sequence[Mapping[str, Any]] = (),
        triggered_by: Literal["sdk", "ci"] = "sdk",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v1/experiments` (scope `experiment_write`): returns the experiment.

        Not idempotent on the platform: retried only on `connect_error` and 429
        `rate_limited`. Fields are validated locally against the platform limits first
        (ValueError). `requested.components` may be `Component`s or mappings; their
        `content_hash` is dropped (the platform refuses it). When the request was sent but no
        answer from the platform came back (`timeout`, `network_error`, a gateway's 502 / 504,
        or a 5xx that is not the platform's error envelope), the error says that the
        experiment may exist: look it up before calling again.
        """
        body: dict[str, Any] = {
            "name": _text("name", name, semconv.EXPERIMENT_NAME_MAX),
            "dataset_id": _text("dataset_id", dataset_id, 36),
            "trials": _trials(trials),
            "triggered_by": _choice("triggered_by", triggered_by, {"sdk", "ci"}),
        }
        if dataset_version is not None:
            if isinstance(dataset_version, bool) or not isinstance(dataset_version, int):
                raise ValueError("dataset_version must be an int >= 1 or None")
            if dataset_version < 1:
                raise ValueError("dataset_version must be >= 1")
            body["dataset_version"] = dataset_version
        if example_keys is not None:
            body["example_keys"] = _example_keys(example_keys)
        if requested is not None:
            body["requested"] = normalize_requested(requested)
        if evaluators:
            body["evaluators"] = normalize_evaluators(evaluators)
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise ValueError("metadata must be a mapping or None")
            body["metadata"] = dict(metadata)
        try:
            data = self._object(
                "POST", "/experiments", json_body=body, idempotent=False, expected=(201,)
            )
        except InsightMonError as err:
            if _may_have_acted(err):
                raise with_message(
                    err,
                    f"{err.message or err.code}; the request reached the server, so the "
                    "experiment may have been created: check the experiments page before "
                    "creating it again",
                ) from err
            raise
        experiment = data.get("experiment")
        if not isinstance(experiment, dict) or not isinstance(experiment.get("id"), str):
            raise InsightMonError(
                UNEXPECTED_RESPONSE, None, "POST /api/v1/experiments answered no experiment"
            )
        return experiment

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        """`GET /api/v1/experiments/{id}`."""
        data = self._object("GET", f"/experiments/{_id(experiment_id)}")
        if not isinstance(data.get("id"), str):
            raise InsightMonError(
                UNEXPECTED_RESPONSE, None, "GET /api/v1/experiments/{id} answered no id"
            )
        return data

    def list_examples(
        self, experiment_id: str, *, limit: int = 200, cursor: str | None = None
    ) -> ExamplesPage:
        """One page of `GET /api/v1/experiments/{id}/examples` (`limit` 1 to 200)."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an int")
        if not 1 <= limit <= semconv.PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be between 1 and {semconv.PAGE_LIMIT_MAX}")
        data = self._object(
            "GET",
            f"/experiments/{_id(experiment_id)}/examples",
            params={"limit": limit, "cursor": cursor},
        )
        items = data.get("items")
        next_cursor = data.get("next_cursor")
        if not isinstance(items, list) or not (next_cursor is None or isinstance(next_cursor, str)):
            raise InsightMonError(
                UNEXPECTED_RESPONSE, None, "GET .../examples answered no items / next_cursor"
            )
        examples = [_parse(Example.from_json, item, "GET .../examples") for item in items]
        return ExamplesPage(items=examples, next_cursor=next_cursor or None)

    def iter_examples(self, experiment_id: str) -> Iterator[Example]:
        """Every example, following `next_cursor`."""
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.list_examples(experiment_id, limit=semconv.PAGE_LIMIT_MAX, cursor=cursor)
            yield from page.items
            if page.next_cursor is None:
                return
            if page.next_cursor in seen:
                raise InsightMonError(
                    UNEXPECTED_RESPONSE, None, "GET .../examples repeated a cursor"
                )
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def report_items(
        self,
        experiment_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        max_body_bytes: int = semconv.TARGET_BODY_BYTES,
    ) -> ReportResult:
        """`POST .../items:batch` in requests of at most 200 items and `max_body_bytes`
        bytes (default `semconv.TARGET_BODY_BYTES`). Each request body is encoded once and
        re-sent byte-identical on retry (the platform deduplicates by report content), up to
        `report_retries` times.

        Raises:
            InsightMonError: e.g. 400 `external_item_invalid {example_key, reason}`, 409
                `invalid_state`. Requests sent before the failing one stay written.
            ValueError: a single item that alone exceeds the body limit (checked before any
                request is sent).
        """
        path = f"/experiments/{_id(experiment_id)}/items:batch"
        accepted = 0
        duplicates = 0
        item_ids: list[str] = []
        for _chunk, body in plan_requests(
            "items", items, max_items=semconv.ITEMS_BATCH_MAX, max_body_bytes=max_body_bytes
        ):
            data = self._object("POST", path, content=body, max_retries=self._report_retries)
            ids = data.get("item_ids")
            if (
                not isinstance(data.get("accepted"), int)
                or not isinstance(data.get("duplicates"), int)
                or not isinstance(ids, list)
            ):
                raise InsightMonError(
                    UNEXPECTED_RESPONSE, None, "POST .../items:batch answered no totals"
                )
            accepted += data["accepted"]
            duplicates += data["duplicates"]
            item_ids.extend(str(i) for i in ids)
        return ReportResult(accepted=accepted, duplicates=duplicates, item_ids=item_ids)

    def finalize_experiment(self, experiment_id: str) -> dict[str, Any]:
        """`POST .../:finalize` (retried up to `report_retries` times): returns
        `{experiment, job_id}`. A 409 `invalid_state` with `status = finalized` on a retry (the
        first attempt went through) is returned as `{experiment: <current>, job_id: None}`; on
        the first attempt it raises."""
        try:
            data = self._object(
                "POST",
                f"/experiments/{_id(experiment_id)}:finalize",
                json_body={},
                expected=(202,),
                max_retries=self._report_retries,
            )
        except InsightMonError as err:
            if err.attempts > 1 and is_already_finalized(err):
                return {"experiment": self.get_experiment(experiment_id), "job_id": None}
            raise
        experiment = data.get("experiment")
        if not isinstance(experiment, dict):
            raise InsightMonError(
                UNEXPECTED_RESPONSE, None, "POST .../:finalize answered no experiment"
            )
        job_id = data.get("job_id")
        return {"experiment": experiment, "job_id": job_id if isinstance(job_id, str) else None}

    def experiment_url(self, experiment: Mapping[str, Any]) -> str:
        """`<base>/projects/<project_id>/experiments/<id>`."""
        project_id = experiment.get("project_id")
        experiment_id = experiment.get("id")
        if not isinstance(project_id, str) or not isinstance(experiment_id, str):
            raise ValueError("the experiment needs `id` and `project_id`")
        return f"{self.base_url}/projects/{_id(project_id)}/experiments/{_id(experiment_id)}"

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ internals

    def _object(
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
    ) -> dict[str, Any]:
        data = self._http.request(
            method,
            API_PREFIX + path,
            json_body=json_body,
            content=content,
            params=params,
            idempotent=idempotent,
            expected=expected,
            max_retries=max_retries,
        )
        if not isinstance(data, dict):
            raise InsightMonError(
                UNEXPECTED_RESPONSE, None, f"{method} {API_PREFIX}{path} did not answer an object"
            )
        return data


# --------------------------------------------------------------------------- helpers


def is_already_finalized(error: InsightMonError) -> bool:
    """409 `invalid_state {status: "finalized"}`: the experiment no longer takes reports."""
    return (
        error.status == 409
        and error.code == INVALID_STATE
        and error.params.get("status") == "finalized"
    )


def plan_requests(
    field_name: str,
    entries: Sequence[Mapping[str, Any]],
    *,
    max_items: int,
    max_body_bytes: int = semconv.TARGET_BODY_BYTES,
) -> list[tuple[list[Mapping[str, Any]], bytes]]:
    """Split `entries` into request bodies `{"<field_name>": [...]}` of at most `max_items`
    entries and `max_body_bytes` bytes. Every entry is encoded once; a body is the exact
    bytes sent (and re-sent on retry).

    Raises:
        ValueError: an entry that alone does not fit in `max_body_bytes`.
        TypeError: an entry that is not JSON.
    """
    if max_items < 1:
        raise ValueError("max_items must be >= 1")
    prefix = b'{"' + field_name.encode("ascii") + b'":['
    suffix = b"]}"
    overhead = len(prefix) + len(suffix)
    out: list[tuple[list[Mapping[str, Any]], bytes]] = []
    chunk: list[Mapping[str, Any]] = []
    parts: list[bytes] = []
    size = overhead

    def close_chunk() -> None:
        out.append((list(chunk), prefix + b",".join(parts) + suffix))

    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TypeError(f"{field_name}[{index}] must be a mapping")
        part = encode_json(entry)
        if overhead + len(part) > max_body_bytes:
            raise ValueError(
                f"{field_name}[{index}] alone is {len(part)} bytes encoded, more than the "
                f"{max_body_bytes} bytes a request may carry"
            )
        extra = len(part) + (1 if parts else 0)
        if parts and (len(parts) >= max_items or size + extra > max_body_bytes):
            close_chunk()
            chunk.clear()
            parts.clear()
            size = overhead
            extra = len(part)
        chunk.append(entry)
        parts.append(part)
        size += extra
    if parts:
        close_chunk()
    return out


def normalize_requested(requested: Mapping[str, Any]) -> dict[str, Any]:
    """`{agent_version?, components: [{type, name, version}]}` for `POST /experiments`:
    components may be `Component`s or mappings; `content_hash` is dropped (the platform's
    schema refuses it). Duplicate (type, name) pairs and unknown keys raise ValueError."""
    if not isinstance(requested, Mapping):
        raise ValueError("requested must be a mapping")
    unknown = set(requested) - {"agent_version", "components"}
    if unknown:
        raise ValueError(f"unknown requested fields: {sorted(unknown)}")
    out: dict[str, Any] = {}
    version = requested.get("agent_version")
    if version is not None:
        if isinstance(version, str):
            version = version.strip()  # the platform strips span attributes: R must match S
        out["agent_version"] = _text("requested.agent_version", version, semconv.AGENT_FIELD_MAX)
    out["components"] = component_refs(requested.get("components") or ())
    return out


def component_refs(components: Iterable[Any]) -> list[dict[str, Any]]:
    """`[{type, name, version}]` sorted by (type, name), at most 100, no duplicates."""
    refs: dict[tuple[str, str], dict[str, Any]] = {}
    for value in components:
        component = coerce_component(value)
        if component.key in refs:
            raise ValueError(f"component {component.type}/{component.name} is listed twice")
        refs[component.key] = component.as_ref()
    if len(refs) > semconv.COMPONENTS_MAX:
        raise ValueError(f"at most {semconv.COMPONENTS_MAX} components")
    return [refs[key] for key in sorted(refs)]


def normalize_evaluators(evaluators: Iterable[str | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Server-side evaluators for `POST /experiments`: an evaluator version id (gating) or
    `{evaluator_version_id, role: "gating" | "diagnostic"}`; at most 20."""
    if isinstance(evaluators, str | bytes):
        raise ValueError("evaluators must be a list, not a single string")
    out: list[dict[str, Any]] = []
    for value in evaluators:
        if isinstance(value, str):
            entry: dict[str, Any] = {"evaluator_version_id": value, "role": "gating"}
        elif isinstance(value, Mapping):
            unknown = set(value) - {"evaluator_version_id", "role"}
            if unknown:
                raise ValueError(f"unknown evaluator fields: {sorted(unknown)}")
            entry = {
                "evaluator_version_id": value.get("evaluator_version_id"),
                "role": value.get("role", "gating"),
            }
        else:
            raise ValueError("an evaluator is a version id or a mapping")
        entry["evaluator_version_id"] = _text(
            "evaluator_version_id", entry["evaluator_version_id"], 36
        )
        _choice("evaluator role", entry["role"], EVALUATOR_ROLES)
        out.append(entry)
    if len(out) > semconv.EVALUATORS_MAX:
        raise ValueError(f"at most {semconv.EVALUATORS_MAX} server evaluators")
    return out


def _prepare_score(score: Any, index: int) -> dict[str, Any]:
    if not isinstance(score, Mapping):
        raise ValueError(f"scores[{index}] must be a mapping")
    out = dict(score)
    source = out.get("source")
    if source not in KEY_SCORE_SOURCES:
        raise ValueError(
            f"scores[{index}].source must be one of {sorted(KEY_SCORE_SOURCES)}, not {source!r}"
        )
    metadata = out.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError(f"scores[{index}].metadata must be a mapping")
    reserved = sorted(RESERVED_SCORE_METADATA_KEYS & set(metadata or ()))
    if reserved:
        raise ValueError(f"scores[{index}].metadata.{reserved[0]} is reserved for the platform")
    key = out.get("idempotency_key")
    if key is None:
        out["idempotency_key"] = uuid.uuid4().hex
    elif not isinstance(key, str) or not 1 <= len(key) <= semconv.IDEMPOTENCY_KEY_MAX:
        raise ValueError(
            f"scores[{index}].idempotency_key must be 1 to {semconv.IDEMPOTENCY_KEY_MAX} characters"
        )
    return out


#: Statuses a gateway in front of the platform answers while the platform may still act.
GATEWAY_STATUSES: frozenset[int] = frozenset({502, 504})


def _may_have_acted(error: InsightMonError) -> bool:
    """Whether a failed request that is not idempotent may still have been carried out: no
    answer came back, or the answer came from a gateway rather than the platform (502 / 504,
    or a 5xx without the platform's error envelope)."""
    if error.delivered is None:
        return True
    if error.status is None:
        return False
    return error.status in GATEWAY_STATUSES or (
        error.status >= 500 and error.code == UNEXPECTED_RESPONSE
    )


def _id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("an id must be a non-empty string")
    return quote(value, safe="")


def _text(label: str, value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{label} is longer than {limit} characters")
    return value


def _choice(label: str, value: Any, allowed: Iterable[str]) -> str:
    choices = sorted(allowed)
    if value not in choices:
        raise ValueError(f"{label} must be one of {choices}, not {value!r}")
    return str(value)


def _trials(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("trials must be an int")
    if not 1 <= value <= semconv.TRIALS_MAX:
        raise ValueError(f"trials must be between 1 and {semconv.TRIALS_MAX}")
    return value


def _example_keys(keys: Sequence[str]) -> list[str]:
    if isinstance(keys, str | bytes):
        raise ValueError("example_keys must be a list of keys, not a single string")
    out = [_text("example key", key, semconv.EXAMPLE_KEY_MAX) for key in keys]
    if not out:
        raise ValueError("example_keys must not be empty (None selects the default examples)")
    if len(out) > semconv.EXAMPLE_KEYS_MAX:
        raise ValueError(f"at most {semconv.EXAMPLE_KEYS_MAX} example keys")
    return out


def _parse(parser: Callable[[Any], _T], data: Any, what: str) -> _T:
    try:
        return parser(data)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InsightMonError(
            UNEXPECTED_RESPONSE, None, f"{what}: unexpected body ({type(exc).__name__}: {exc})"
        ) from exc
