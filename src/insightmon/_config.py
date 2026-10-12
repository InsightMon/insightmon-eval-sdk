"""Resolving settings, components and the process-wide runtime state.

- `Settings`: what `init()` resolved (arguments first, then `INSIGHTMON_*` environment variables).
- `Component`: one versioned building block of the agent (`insightmon.component.<type>.<name>.*`).
- Runtime state: the active settings and providers, plus the process-wide component defaults
  that `set_component()` changes outside a thread. Tests reset it with `reset_runtime()`.

Text values the platform compares (agent name and version, environment, component type, name,
version and hash) are stripped of surrounding whitespace: the platform strips the span
attribute side of a replay receipt, so `"1.4.0\n"` read from a file would otherwise conflict
with the `"1.4.0"` it reads from the trace.

The span processor reads the global components on every anchor span, so they are an immutable
snapshot read without a lock; `init()` / `shutdown()` take a lock only to swap state, never
while doing network I/O.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeAlias
from urllib.parse import urlsplit

from insightmon import semconv
from insightmon._errors import CONFIG_ERROR, InsightMonError

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.trace import TracerProvider

ENV_BASE_URL = "INSIGHTMON_BASE_URL"
ENV_API_KEY = "INSIGHTMON_API_KEY"
ENV_SERVICE_NAME = "OTEL_SERVICE_NAME"
API_KEY_PREFIX = "imk_"
OTLP_TRACES_PATH = "/otlp/v1/traces"
OTLP_LOGS_PATH = "/otlp/v1/logs"


# --------------------------------------------------------------------------- components


@dataclass(frozen=True)
class Component:
    """A versioned building block of the agent: a prompt, a tool, a knowledge base, ...

    `type` must be one of the project's component types (default: agent, prompt, skill,
    knowledge, tool, model) for experiments to accept it; it may not contain a dot.
    """

    type: str
    name: str
    version: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("type", "name", "version", "content_hash"):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, value.strip())
        if not isinstance(self.type, str) or not self.type or "." in self.type:
            raise ValueError(f"component type must be non-empty and contain no '.': {self.type!r}")
        if len(self.type) > semconv.COMPONENT_TYPE_MAX:
            raise ValueError(f"component type is longer than {semconv.COMPONENT_TYPE_MAX}")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("component name must be a non-empty string")
        if len(self.name) > semconv.COMPONENT_NAME_MAX:
            raise ValueError(f"component name is longer than {semconv.COMPONENT_NAME_MAX}")
        for label, value in (("version", self.version), ("content_hash", self.content_hash)):
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"component {label} must be a non-empty string or None")
            if len(value) > semconv.COMPONENT_VALUE_MAX:
                raise ValueError(f"component {label} is longer than {semconv.COMPONENT_VALUE_MAX}")

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.name)

    def attributes(self) -> dict[str, str]:
        """The span attributes of this component (only the fields that are set)."""
        out: dict[str, str] = {}
        if self.version is not None:
            out[semconv.component_attribute(self.type, self.name, semconv.COMPONENT_VERSION)] = (
                self.version
            )
        if self.content_hash is not None:
            key = semconv.component_attribute(self.type, self.name, semconv.COMPONENT_CONTENT_HASH)
            out[key] = self.content_hash
        return out

    def as_ref(self) -> dict[str, Any]:
        """`{type, name, version}` for REST bodies (`requested`, `receipt`): the platform's
        experiment schemas reject `content_hash`."""
        return {"type": self.type, "name": self.name, "version": self.version}

    def as_receipt(self) -> dict[str, Any]:
        """`{type, name, version, content_hash?}` for a replay target's response receipt."""
        out = self.as_ref()
        if self.content_hash is not None:
            out["content_hash"] = self.content_hash
        return out


ComponentLike: TypeAlias = Component | Mapping[str, Any]


def coerce_component(value: ComponentLike) -> Component:
    """A `Component` from itself or from a mapping `{type, name, version?, content_hash?}`."""
    if isinstance(value, Component):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a Component or a mapping, got {type(value).__name__}")
    unknown = set(value) - {"type", "name", "version", "content_hash"}
    if unknown:
        raise ValueError(f"unknown component fields: {sorted(unknown)}")
    return Component(
        type=value.get("type"),  # type: ignore[arg-type]  # validated in __post_init__
        name=value.get("name"),  # type: ignore[arg-type]
        version=value.get("version"),
        content_hash=value.get("content_hash"),
    )


def merge_components(*groups: Iterable[Component]) -> tuple[Component, ...]:
    """Later groups override earlier ones per (type, name); the result is sorted."""
    merged: dict[tuple[str, str], Component] = {}
    for group in groups:
        for component in group:
            merged[component.key] = component
    return tuple(merged[key] for key in sorted(merged))


# --------------------------------------------------------------------------- validation


def normalize_base_url(url: str) -> str:
    """`http(s)://host[:port][/prefix]` without a trailing slash (raises `config_error`).

    The error never repeats the value: a key passed where the URL belongs (swapped arguments or
    variables) would otherwise end up in tracebacks and error trackers. A URL with a user name
    or password is refused (it would show in `repr()` and in experiment links, and httpx would
    send it as Basic auth instead of the key)."""
    if not isinstance(url, str):
        raise InsightMonError(CONFIG_ERROR, None, "base URL must be a string")
    text = url.strip()
    if text.startswith(API_KEY_PREFIX):
        raise InsightMonError(
            CONFIG_ERROR,
            None,
            "base URL must be http(s)://host, and this value looks like an API key: check that "
            f"the base URL and the API key ({ENV_BASE_URL} / {ENV_API_KEY}) are not swapped",
        )
    try:
        parts = urlsplit(text)
        has_credentials = parts.username is not None or parts.password is not None
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        raise InsightMonError(CONFIG_ERROR, None, "base URL is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InsightMonError(CONFIG_ERROR, None, "base URL must be http(s)://host[:port]")
    if has_credentials:
        raise InsightMonError(
            CONFIG_ERROR, None, "base URL must not contain a user name or password"
        )
    if parts.query or parts.fragment:
        raise InsightMonError(CONFIG_ERROR, None, "base URL must not have a query or fragment")
    return text.rstrip("/")


def validate_api_key(key: str) -> str:
    """The key, stripped; it must start with `imk_` (the platform answers 401 otherwise)."""
    text = key.strip()
    if not text.startswith(API_KEY_PREFIX) or len(text) <= len(API_KEY_PREFIX):
        raise InsightMonError(CONFIG_ERROR, None, "API key must start with 'imk_'")
    if any(ch.isspace() for ch in text):
        raise InsightMonError(CONFIG_ERROR, None, "API key must not contain whitespace")
    return text


def normalize_language(value: str | None) -> str | None:
    """zh / en / ja / mixed / und (stripped, lower case), None for None; ValueError otherwise."""
    if value is None:
        return None
    text = value.strip().lower()
    if text not in semconv.CONTENT_LANGUAGES:
        raise ValueError(
            f"language must be one of {', '.join(semconv.CONTENT_LANGUAGES)}, not {value!r}"
        )
    return text


def _bounded(label: str, value: str | None, limit: int) -> str | None:
    """`value` stripped (module docstring); ValueError when blank or longer than `limit`."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string or None")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{label} is longer than {limit} characters")
    return text


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    """What `init()` resolved. `api_key` is never shown in `repr()`."""

    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    service_name: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    components: tuple[Component, ...] = ()
    language: str | None = None
    capture_content: bool = False
    environment: str | None = None
    #: Extra content attributes stripped when `capture_content` is False: names, or prefixes
    #: with a trailing `*` (`semconv.content_matcher`).
    content_attributes: tuple[str, ...] = ()

    @property
    def export_enabled(self) -> bool:
        """True when spans and logs are exported to the platform (base URL and key both set).
        Without them the SDK only stamps attributes (a collector or a test exporter forwards)."""
        return self.base_url is not None and self.api_key is not None

    def endpoint(self, path: str) -> str:
        """Absolute URL of a platform path (e.g. `OTLP_TRACES_PATH`)."""
        if self.base_url is None:
            raise InsightMonError(CONFIG_ERROR, None, "no base URL configured")
        return f"{self.base_url}{path}"

    def auth_headers(self) -> dict[str, str]:
        if self.api_key is None:
            raise InsightMonError(CONFIG_ERROR, None, "no API key configured")
        return {"Authorization": f"Bearer {self.api_key}"}

    def requested(self) -> dict[str, Any]:
        """`requested` of an external experiment: agent version and components (no hashes)."""
        out: dict[str, Any] = {"components": [c.as_ref() for c in self.components]}
        if self.agent_version is not None:
            out["agent_version"] = self.agent_version
        return out

    def receipt(self, components: Iterable[Component] | None = None) -> dict[str, Any]:
        """`{agent: {name, version}, components: [...]}`: a replay target's response receipt."""
        chosen = self.components if components is None else tuple(components)
        return {
            "agent": {"name": self.agent_name, "version": self.agent_version},
            "components": [c.as_receipt() for c in sorted(chosen, key=lambda c: c.key)],
        }

    def with_components(self, components: Iterable[Component]) -> Settings:
        return replace(self, components=merge_components(self.components, components))


def resolve_settings(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    service_name: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    components: Iterable[ComponentLike] | None = None,
    language: str | None = None,
    capture_content: bool = False,
    environment: str | None = None,
    content_attributes: Iterable[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Arguments first, then the environment (`INSIGHTMON_BASE_URL`, `INSIGHTMON_API_KEY`,
    `OTEL_SERVICE_NAME`). Base URL and key must be given together or not at all."""
    environ = os.environ if env is None else env
    url = base_url if base_url is not None else (environ.get(ENV_BASE_URL) or None)
    key = api_key if api_key is not None else (environ.get(ENV_API_KEY) or None)
    if (url is None) != (key is None):
        missing = ENV_API_KEY if key is None else ENV_BASE_URL
        raise InsightMonError(
            CONFIG_ERROR, None, f"base URL and API key go together: {missing} is missing"
        )
    service = service_name if service_name is not None else (environ.get(ENV_SERVICE_NAME) or None)
    return Settings(
        base_url=normalize_base_url(url) if url is not None else None,
        api_key=validate_api_key(key) if key is not None else None,
        service_name=_bounded("service_name", service, semconv.AGENT_FIELD_MAX),
        agent_name=_bounded("agent_name", agent_name, semconv.AGENT_FIELD_MAX),
        agent_version=_bounded("agent_version", agent_version, semconv.AGENT_FIELD_MAX),
        components=merge_components(coerce_component(c) for c in (components or ())),
        language=normalize_language(language),
        capture_content=bool(capture_content),
        environment=_bounded("environment", environment, semconv.ENVIRONMENT_MAX),
        content_attributes=_content_patterns(content_attributes),
    )


def _content_patterns(patterns: Iterable[str] | None) -> tuple[str, ...]:
    """`content_attributes` of `init()`: attribute names or prefixes ending in `*`."""
    if patterns is None:
        return ()
    if isinstance(patterns, str | bytes):
        raise ValueError("content_attributes must be a list of attribute names, not one string")
    out: list[str] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip().rstrip("*"):
            raise ValueError(
                "content_attributes holds attribute names or prefixes ending in '*', "
                f"not {pattern!r}"
            )
        out.append(pattern.strip())
    return tuple(dict.fromkeys(out))


# --------------------------------------------------------------------------- runtime state


@dataclass
class RuntimeState:
    """The process-wide state `init()` sets up (owned by `insightmon._setup`).

    `owns_tracer_provider` / `owns_logger_provider`: the SDK created the provider (and shuts it
    down); otherwise it only added its processors to a provider the application owns.
    `span_processors` / `log_processors`: the processors the SDK added (flushed by `flush()`).
    `export_control`: the `ExportControl` of the SDK's exporters (None without export).
    """

    settings: Settings
    tracer_provider: TracerProvider | None = None
    logger_provider: LoggerProvider | None = None
    owns_tracer_provider: bool = False
    owns_logger_provider: bool = False
    span_processors: list[Any] = field(default_factory=list)
    log_processors: list[Any] = field(default_factory=list)
    handle: Any = None
    export_control: Any = None


_lock = threading.RLock()
_runtime: RuntimeState | None = None
#: `set_component()` overrides made outside a thread: an immutable snapshot, swapped under
#: `_lock` and read without it.
_global_overrides: tuple[Component, ...] = ()


def get_runtime() -> RuntimeState | None:
    return _runtime


def set_runtime(state: RuntimeState | None) -> None:
    global _runtime
    with _lock:
        _runtime = state


def get_settings() -> Settings | None:
    """The settings of the active `init()`, or None before `init()` (or after `shutdown()`)."""
    state = _runtime
    return state.settings if state is not None else None


def runtime_lock() -> threading.RLock:
    """The lock that guards `init()` / `shutdown()` against concurrent calls."""
    return _lock


def set_global_component(component: Component) -> None:
    """Override (type, name) for every span started from now on (outside a thread scope)."""
    global _global_overrides
    with _lock:
        _global_overrides = merge_components(_global_overrides, (component,))


def global_components() -> tuple[Component, ...]:
    """`init(components=...)` merged with `set_component()` overrides made outside a thread.
    Lock-free: the span processor calls it for every anchor span."""
    overrides = _global_overrides
    state = _runtime
    base = state.settings.components if state is not None else ()
    return merge_components(base, overrides)


def reset_runtime() -> None:
    """Forget the runtime state and the global component overrides (`shutdown()`, tests)."""
    global _runtime, _global_overrides
    with _lock:
        _runtime = None
        _global_overrides = ()


def _reinit_after_fork() -> None:
    """In a forked child: a new lock (another thread may have held it at the fork)."""
    global _lock
    _lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)
