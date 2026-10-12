"""Context variables shared by tracing and `evaluate()`: thread scope, experiment scope and the
registry of held traces.

- Thread scope (`insightmon.thread(...)`): the thread key and per-thread overrides that the span
  processor stamps on every span started inside it.
- Experiment scope (`evaluate()`): marks spans as experiment traffic. The span processor writes
  no thread key inside it and holds the spans of the item's trace until `release_traces()`;
  `set_component()` inside it changes that run only.
- Held traces: `evaluate()` reports an item (the platform then pre-creates an
  `origin = experiment` trace) before the item's spans are exported, so that the spans merge into
  that trace instead of creating a production trace (docs/contract.md §12, §14). Each held trace
  has an owner (the experiment id), so that one run releases or discards only its own traces.
  `discard_traces()` drops the spans of items that were never reported (a stopped run).
  `discard_owner()` / `close_owner()` end an owner: traces it would hold later (a task thread
  abandoned after a timeout that starts a new trace) are discarded at once.

It holds no OpenTelemetry objects, only ids and plain values. After `fork()` the child gets new
locks and forgets the parent's held and discarded traces (they belong to the parent's runs).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from insightmon._config import Component

AttributeValue = str | bool | int | float


@dataclass
class ThreadScope:
    """One conversation (platform: thread). Created by `insightmon.thread(...)`.

    `components` are thread-level overrides added by `set_component()` inside the scope.
    """

    key: str
    language: str | None = None
    metadata: dict[str, AttributeValue] = field(default_factory=dict)
    user_ref: str | None = None
    components: dict[tuple[str, str], Component] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def set_component(self, component: Component) -> None:
        with self._lock:
            self.components[component.key] = component

    def component_overrides(self) -> tuple[Component, ...]:
        with self._lock:
            return tuple(self.components.values())


@dataclass(frozen=True)
class ExperimentScope:
    """One (example, trial) of an `evaluate()` run.

    `hold`: the span processor holds the spans of traces started in this scope until
    `release_traces()` is called for them (owner: `owner`, default the experiment id).
    `stamp`: the experiment attributes are stamped on local roots (False for the scope of the
    client evaluators, whose spans are held and then discarded). `components`: overrides added
    by `set_component()` inside the scope; they apply to this run only, never to the process or
    to other runs.
    """

    experiment_id: str
    example_key: str
    trial_index: int
    hold: bool = True
    owner: str | None = None
    stamp: bool = True
    components: dict[tuple[str, str], Component] = field(
        default_factory=dict, repr=False, compare=False
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def set_component(self, component: Component) -> None:
        with self._lock:
            self.components[component.key] = component

    def component_overrides(self) -> tuple[Component, ...]:
        with self._lock:
            return tuple(self.components.values())

    @property
    def holder(self) -> str:
        """The owner of the traces held in this scope."""
        return self.owner if self.owner is not None else self.experiment_id

    def attributes(self) -> dict[str, AttributeValue]:
        from insightmon import semconv

        return {
            semconv.EXPERIMENT_ID: self.experiment_id,
            semconv.EXAMPLE_KEY: self.example_key,
            semconv.TRIAL_INDEX: self.trial_index,
        }


_thread: ContextVar[ThreadScope | None] = ContextVar("insightmon_thread", default=None)
_experiment: ContextVar[ExperimentScope | None] = ContextVar("insightmon_experiment", default=None)


def current_thread() -> ThreadScope | None:
    """The innermost `insightmon.thread(...)` scope of the current context, if any."""
    return _thread.get()


def current_experiment() -> ExperimentScope | None:
    """The experiment scope of the current context (inside `evaluate()`), if any."""
    return _experiment.get()


@contextmanager
def thread_scope(scope: ThreadScope) -> Iterator[ThreadScope]:
    """Make `scope` the current thread scope for the duration of the block."""
    token = _thread.set(scope)
    try:
        yield scope
    finally:
        _thread.reset(token)


@contextmanager
def experiment_scope(
    experiment_id: str,
    example_key: str,
    trial_index: int,
    *,
    hold: bool = True,
    owner: str | None = None,
    stamp: bool = True,
) -> Iterator[ExperimentScope]:
    """Mark the block as one (example, trial) of an experiment. Inside it the span processor
    writes no thread key (and ignores an enclosing thread scope) and, with `hold`, holds the
    spans of new traces (owner: `owner`, default `experiment_id`) until `release_traces()` or
    `discard_traces()`."""
    scope = ExperimentScope(experiment_id, example_key, trial_index, hold, owner, stamp)
    thread_token = _thread.set(None)
    token = _experiment.set(scope)
    try:
        yield scope
    finally:
        _experiment.reset(token)
        _thread.reset(thread_token)


# --------------------------------------------------------------------------- held traces

#: `listener(trace_ids, discard)`: `trace_ids` None = every held trace; `discard` = drop the
#: held spans instead of exporting them.
ReleaseListener = Callable[[frozenset[int] | None, bool], None]

#: Discarded trace ids remembered (oldest forgotten first): spans of such a trace that end after
#: the discard (e.g. a task abandoned after a timeout) are dropped as well.
MAX_DISCARDED = 10_000
#: Closed owners remembered (oldest forgotten first).
MAX_CLOSED_OWNERS = 10_000

TraceStatus = Literal["held", "discarded"]

_held_lock = threading.Lock()
_held: dict[int, str | None] = {}  # trace id -> owner (the experiment id), insertion ordered
_discarded: dict[int, None] = {}  # ordered set
_closed_owners: dict[str, None] = {}  # ordered set
_listeners: list[ReleaseListener] = []


def _remember_discarded(trace_id: int) -> None:  # under _held_lock
    _discarded[trace_id] = None
    while len(_discarded) > MAX_DISCARDED:
        del _discarded[next(iter(_discarded))]


def hold_trace(trace_id: int, owner: str | None = None) -> None:
    """Hold the spans of `trace_id` (an OpenTelemetry int trace id) until released. `owner`
    (the experiment id) lets one `evaluate()` run release or discard only its own traces. A
    trace of a closed owner (`close_owner`, `discard_owner`) is discarded at once."""
    with _held_lock:
        if owner is not None and owner in _closed_owners:
            if trace_id not in _held:
                _remember_discarded(trace_id)
            return
        _held.setdefault(trace_id, owner)
        _discarded.pop(trace_id, None)


def is_held(trace_id: int) -> bool:
    with _held_lock:
        return trace_id in _held


def is_discarded(trace_id: int) -> bool:
    """True for a trace whose held spans were discarded (`discard_traces`)."""
    with _held_lock:
        return trace_id in _discarded


def trace_status(trace_id: int) -> TraceStatus | None:
    """The state of a trace, read in one step: `"held"`, `"discarded"` or None (the span
    processor decides on it while holding its own lock)."""
    with _held_lock:
        if trace_id in _held:
            return "held"
        if trace_id in _discarded:
            return "discarded"
        return None


def has_release_listeners() -> bool:
    """True when a span processor holds spans until their traces are released."""
    with _held_lock:
        return bool(_listeners)


def held_traces(owner: str | None = None) -> frozenset[int]:
    """Every held trace, or (with `owner`) the traces held for that owner."""
    with _held_lock:
        if owner is None:
            return frozenset(_held)
        return frozenset(t for t, o in _held.items() if o == owner)


def _notify(trace_ids: frozenset[int] | None, discard: bool) -> None:
    with _held_lock:
        listeners = list(_listeners)
    for listener in listeners:
        listener(trace_ids, discard)


def release_traces(trace_ids: Iterable[int] | None = None) -> None:
    """Stop holding `trace_ids` (None = every held trace) and tell the listeners (the span
    processors), which then export the spans they kept for them."""
    with _held_lock:
        if trace_ids is None:
            released: frozenset[int] | None = None
            _held.clear()
        else:
            released = frozenset(trace_ids)
            for trace_id in released:
                _held.pop(trace_id, None)
    _notify(released, False)


def discard_traces(trace_ids: Iterable[int]) -> None:
    """Stop holding `trace_ids` and tell the listeners to drop the spans they kept for them;
    spans of these traces that end later are dropped too. Used when an `evaluate()` run stops
    before the items of these traces were reported: exported, they would become production
    traces on the platform (docs/contract.md §14)."""
    with _held_lock:
        dropped = frozenset(trace_ids)
        for trace_id in dropped:
            _held.pop(trace_id, None)
            _remember_discarded(trace_id)
    if dropped:
        _notify(dropped, True)


def _close(owner: str) -> None:  # under _held_lock
    _closed_owners[owner] = None
    while len(_closed_owners) > MAX_CLOSED_OWNERS:
        del _closed_owners[next(iter(_closed_owners))]


def close_owner(owner: str) -> None:
    """End `owner` without touching the traces it holds: traces it would hold from now on are
    discarded at once (the run is over; a late root span of an abandoned task would otherwise
    stay held and be exported as a production trace at shutdown)."""
    with _held_lock:
        _close(owner)


def discard_owner(owner: str) -> None:
    """Discard every trace held for `owner` and close it (`close_owner`)."""
    with _held_lock:
        _close(owner)
        traces = [t for t, o in _held.items() if o == owner]
    discard_traces(traces)


def add_release_listener(listener: ReleaseListener) -> None:
    with _held_lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_release_listener(listener: ReleaseListener) -> None:
    with _held_lock:
        if listener in _listeners:
            _listeners.remove(listener)


def reset_context_state() -> None:
    """Forget held traces, closed owners and listeners (tests)."""
    with _held_lock:
        _held.clear()
        _discarded.clear()
        _closed_owners.clear()
        _listeners.clear()


def _reinit_after_fork() -> None:
    """In a forked child: a new lock (the parent's may have been held by another thread at the
    fork) and no held or discarded traces (they belong to the parent's runs). The listeners,
    span processors of this process, stay."""
    global _held_lock
    _held_lock = threading.Lock()
    _held.clear()
    _discarded.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)


def trace_id_hex(trace_id: int) -> str:
    """32 lower-case hex digits: the `otel_trace_id` the platform expects."""
    return format(trace_id, "032x")


def merge_attributes(*groups: Mapping[str, Any] | None) -> dict[str, Any]:
    """Later groups win; None groups are skipped."""
    out: dict[str, Any] = {}
    for group in groups:
        if group:
            out.update(group)
    return out
