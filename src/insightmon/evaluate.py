"""`evaluate()` / `aevaluate()`: run a task on a dataset client-side; the platform runs the gating
evaluators and draws the conclusion (external experiment, docs/contract.md §6 and §12).

Flow: create the experiment (`triggered_by = "sdk"`), read its frozen examples, run the task
on every (example, trial) with at most `max_concurrency` runs at once, each inside a new trace
whose root is an `insightmon.turn("evaluate")` span carrying `insightmon.experiment_id /
example_key / trial_index`; run the client evaluators once that turn has ended, outside its
trace; report the items in batches (every 50 items or 5 s, `items:batch`); release the held
spans of each reported batch; flush telemetry; `:finalize`; poll until the platform finished
the evaluation.

The task gets the example's input, plus a copy of its `initial_state` when its second positional
parameter is named `initial_state`, or the `Example` itself when it takes another second
positional argument (docs/contract.md §12 step 4). Its result is reported the way the platform
shapes target outputs: a str becomes `{"text": ...}`, a mapping stays an object, None becomes
`{}`, any other JSON value becomes `{"json": value}`.

`insightmon.set_component()` called by the task applies to that (example, trial) only: its spans
and the item's `receipt` (never the process or the other runs).

Client evaluators run after the item's turn span ended, in an empty context: spans they start
(e.g. the instrumented model call of a judge) begin traces of their own, which are held and then
dropped. They never mix into the agent's trajectory that the platform's gating evaluators read,
and never become production traces.

Timeouts: a thread cannot be killed. A run that times out is reported as `timeout`, but its task
keeps its place among the `max_concurrency` task runs until it returns, so a hung downstream
service never gets more than `max_concurrency` calls at once. When no place frees up within
another `timeout_s`, the run stops (not finalized) with an error that says so. `aevaluate()` runs
sync tasks, sync evaluators and its REST calls in dedicated daemon threads, never in the event
loop's default executor (it is shared with the application, and hung tasks filling it would
block the SDK); its timeout clock starts when the task starts.

Stopping early (Ctrl-C, task cancellation, a platform error that cannot be retried), including
while the last items are reported: no new run starts, the items that finished are reported (not
after a failed report), the spans of reported items are exported, the spans of the other items
are dropped (exported, they would become production traces), and the experiment is **not**
finalized: finalize it later with `Client().finalize_experiment(<id>)` or let the platform
finish it 24 hours after creation. The error message, or the warning logged for an
interruption, names the experiment id.

Seams (`_item_span`, `_evaluator_scope`, `_release_traces`, `_flush_telemetry`) are the only
places this module touches tracing; tests monkeypatch them. Each run releases or drops only its
own held traces (they are held under the experiment id), so concurrent runs in one process do
not interfere.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import inspect
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from opentelemetry import context as otel_context
from opentelemetry.trace import Span, Status, StatusCode

from insightmon import semconv
from insightmon._batch import ItemBatcher
from insightmon._config import Component, get_settings, global_components, merge_components
from insightmon._context import (
    close_owner,
    current_experiment,
    discard_owner,
    experiment_scope,
    has_release_listeners,
    held_traces,
    release_traces,
    trace_id_hex,
)
from insightmon._errors import InsightMonError, with_message
from insightmon._http import clean_text, encode_json, sanitize_json
from insightmon.client import (
    RESERVED_SCORE_METADATA_KEYS,
    Client,
    Example,
    component_refs,
    is_already_finalized,
    normalize_evaluators,
    normalize_requested,
)

__all__ = ["Evaluator", "ExperimentResult", "ItemResult", "Task", "aevaluate", "evaluate"]

log = logging.getLogger("insightmon")

#: `task(input)`, `task(input, initial_state)` or `task(input, example)`; may be async for
#: `aevaluate()`.
Task = Callable[..., Any]
#: What the task takes after the input (`_task_args`).
TaskArgs = Literal["input", "initial_state", "example"]
#: `evaluator(input, output, reference)` → Mapping `{name?, value, explanation?, metadata?}`,
#: a list of such mappings, a scalar (named after the function), or None (no score).
Evaluator = Callable[[Any, Any, Any], Any]
ItemStatus = Literal["ok", "task_error", "timeout"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

#: Item batches (docs/contract.md §12 step 5).
BATCH_MAX_ITEMS = 50
BATCH_MAX_INTERVAL_S = 5.0
#: Polling of the experiment after `:finalize`: 1 s, doubling up to 10 s.
POLL_INITIAL_S = 1.0
POLL_MAX_S = 10.0
#: Time given to the exporters before `:finalize`.
TELEMETRY_FLUSH_TIMEOUT_S = 30.0
#: How often a run waiting for a free task place checks whether the run is stopping.
SLOT_POLL_S = 0.05
#: Error codes of reported items.
ERROR_EXCEPTION = "exception"
ERROR_TIMEOUT = "timeout"
ERROR_OUTPUT_TOO_LARGE = "output_too_large"
ERROR_OUTPUT_NOT_JSON = "output_not_json"

# Module level so tests can replace them.
_sleep: Callable[[float], None] = time.sleep
_async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_monotonic: Callable[[], float] = time.monotonic


@dataclass
class ItemResult:
    """What the SDK reported for one (example, trial)."""

    example_key: str
    trial_index: int
    status: ItemStatus
    output: Any = None
    error: dict[str, str] | None = None
    latency_ms: int | None = None
    otel_trace_id: str | None = None
    scores: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExperimentResult:
    """The experiment after `evaluate()`: platform status and summary plus the local items.

    `status` is the platform status (`completed`, `failed`, `cancelled`, or `running` when
    `wait=False` or the wait timed out). `summary` is the platform summary
    (docs/contract.md §6.2) or None before the evaluation finished.
    """

    experiment_id: str
    url: str
    status: str
    summary: dict[str, Any] | None
    items: list[ItemResult]
    experiment: dict[str, Any]

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def pass_rate(self) -> float | None:
        value = (self.summary or {}).get("pass_rate")
        return float(value) if isinstance(value, int | float) else None


# --------------------------------------------------------------------------- seams


@contextmanager
def _item_span(
    experiment_id: str, example_key: str, trial_index: int
) -> Iterator[tuple[Span, str | None]]:
    """One item's turn: inside its experiment scope, a new trace whose root is
    `insightmon.turn("evaluate")` (operation `invoke_agent`, the experiment attributes, the
    agent and component versions). Yields the span and its 32-hex trace id (None when tracing
    is a no-op)."""
    from insightmon.tracing import turn

    with experiment_scope(experiment_id, example_key, trial_index) as scope:
        token = otel_context.attach(otel_context.Context())  # always a new trace
        try:
            with turn("evaluate", attributes=scope.attributes()) as span:
                ctx = span.get_span_context()
                yield span, trace_id_hex(ctx.trace_id) if ctx.is_valid else None
        finally:
            otel_context.detach(token)


@contextmanager
def _evaluator_scope(experiment_id: str, example_key: str, trial_index: int) -> Iterator[None]:
    """Where the client evaluators of one item run: after its turn ended, in an empty context
    (a span they start begins a new trace, never a child of the agent's turn), inside an
    experiment scope that holds those traces under an owner of their own and drops them at the
    end (exported, they would become production traces)."""
    owner = f"{experiment_id}/evaluators/{example_key}/{trial_index}"
    with experiment_scope(experiment_id, example_key, trial_index, owner=owner, stamp=False):
        token = otel_context.attach(otel_context.Context())
        try:
            yield
        finally:
            otel_context.detach(token)
            discard_owner(owner)


def _mark_error(span: Span, error_type: str) -> None:
    span.set_attribute(semconv.ERROR_TYPE, error_type[: semconv.ERROR_TYPE_MAX])
    span.set_status(Status(StatusCode.ERROR))


def _release_traces(trace_ids: Iterable[str]) -> None:
    """Export the held spans of these items (32-hex trace ids)."""
    release_traces(int(t, 16) for t in trace_ids)


def _flush_telemetry(timeout_s: float, *, experiment_id: str, discard: bool = False) -> bool:
    """Release the spans still held for `experiment_id` (the normal end of a run), or, with
    `discard`, drop them (a stopped run: they belong to items that were never reported, and
    exported they would become production `origin = otlp` traces on the platform); then flush
    spans and logs. Either way the experiment's owner is closed: a trace a late task (one
    abandoned after a timeout) starts from now on is dropped at once. Traces held for other
    experiments are left alone."""
    from insightmon._setup import flush

    if discard:
        discard_owner(experiment_id)
    else:
        release_traces(held_traces(experiment_id))
        close_owner(experiment_id)
    return flush(timeout_s)


# --------------------------------------------------------------------------- options


@dataclass
class _Options:
    """`evaluate()` arguments, validated before any request."""

    task: Task
    dataset: str
    dataset_version: int | None
    example_keys: list[str] | None
    trials: int
    evaluators: list[Evaluator]
    server_evaluators: list[dict[str, Any]]
    name: str
    requested: dict[str, Any] | None
    agent_version: str | None
    components: tuple[Component, ...]
    metadata: dict[str, Any] | None
    max_concurrency: int
    timeout_s: float | None
    wait: bool
    wait_timeout_s: float
    task_args: TaskArgs

    @classmethod
    def build(
        cls,
        task: Task,
        *,
        dataset: str,
        dataset_version: int | None,
        example_keys: Sequence[str] | None,
        trials: int,
        evaluators: Sequence[Evaluator],
        server_evaluators: Sequence[str | Mapping[str, Any]],
        name: str | None,
        requested: Mapping[str, Any] | None,
        metadata: Mapping[str, Any] | None,
        max_concurrency: int,
        timeout_s: float | None,
        wait: bool,
        wait_timeout_s: float,
    ) -> _Options:
        if not callable(task):
            raise ValueError("task must be callable")
        if not isinstance(dataset, str) or not dataset:
            raise ValueError("dataset must be a dataset id")
        if isinstance(trials, bool) or not isinstance(trials, int):
            raise ValueError("trials must be an int")
        if not 1 <= trials <= semconv.TRIALS_MAX:
            raise ValueError(f"trials must be between 1 and {semconv.TRIALS_MAX}")
        if not all(callable(e) for e in evaluators):  # a str fails too
            raise ValueError("evaluators must be a list of callables")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
            raise ValueError("max_concurrency must be an int")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if timeout_s is not None and not (_is_number(timeout_s) and timeout_s > 0):
            raise ValueError("timeout_s must be a positive number or None")
        if not (_is_number(wait_timeout_s) and wait_timeout_s >= 0):
            raise ValueError("wait_timeout_s must be a number >= 0")
        if name is None:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            name = f"evaluate {dataset} {stamp}"[: semconv.EXPERIMENT_NAME_MAX]
        elif not isinstance(name, str) or not 1 <= len(name) <= semconv.EXPERIMENT_NAME_MAX:
            raise ValueError(f"name must be 1 to {semconv.EXPERIMENT_NAME_MAX} characters")
        if example_keys is not None and isinstance(example_keys, str | bytes):
            raise ValueError("example_keys must be a list of keys, not a single string")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata must be a mapping or None")
        settings = get_settings()
        agent_version = settings.agent_version if settings is not None else None
        components = global_components()
        return cls(
            task=task,
            dataset=dataset,
            dataset_version=dataset_version,
            example_keys=list(example_keys) if example_keys is not None else None,
            trials=trials,
            evaluators=list(evaluators),
            server_evaluators=normalize_evaluators(server_evaluators),
            name=name,
            requested=(
                normalize_requested(requested)
                if requested is not None
                else _versions(agent_version, components)
            ),
            agent_version=agent_version,
            components=components,
            metadata=dict(metadata) if metadata is not None else None,
            max_concurrency=max_concurrency,
            timeout_s=float(timeout_s) if timeout_s is not None else None,
            wait=bool(wait),
            wait_timeout_s=float(wait_timeout_s),
            task_args=_task_args(task),
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _versions(agent_version: str | None, components: Sequence[Component]) -> dict[str, Any] | None:
    """`{agent_version?, components}` without hashes, or None when both are empty: the default
    `requested` of the experiment (from `init()` and `set_component()`) and the `receipt` of
    an item (plus the item's own `set_component()` overrides)."""
    refs = component_refs(components)
    if agent_version is None and not refs:
        return None
    out: dict[str, Any] = {"components": refs}
    if agent_version is not None:
        out["agent_version"] = agent_version
    return out


def _task_args(task: Task) -> TaskArgs:
    """What `task` takes after the input: `"initial_state"` when its second positional
    parameter is named `initial_state`; `"example"` when it takes another second positional
    argument (a required one, one named `example`, or `*args`); else `"input"` only."""
    try:
        params = list(inspect.signature(task).parameters.values())
    except (TypeError, ValueError):
        return "input"
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    second = positional[1] if len(positional) >= 2 else None
    if second is not None and second.name == "initial_state":
        return "initial_state"
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return "example"
    if second is None:
        return "input"
    if second.default is inspect.Parameter.empty or second.name == "example":
        return "example"
    return "input"


def _is_async_callable(fn: Any) -> bool:
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(
        getattr(fn, "__call__", None)  # noqa: B004  (async callable objects)
    )


# --------------------------------------------------------------------------- one item


@dataclass
class _Outcome:
    status: ItemStatus
    output: Any = None
    error: dict[str, str] | None = None
    error_type: str | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class _Run:
    index: int
    example: Example
    trial_index: int


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))


def _error_message(exc: BaseException) -> str:
    try:
        detail = str(exc)
    except Exception:  # a broken __str__ must not hide the error
        detail = ""
    text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return clean_text(text)[: semconv.ITEM_ERROR_MESSAGE_MAX]


def _failed(exc: BaseException, latency_ms: int) -> _Outcome:
    return _Outcome(
        "task_error",
        error={"code": ERROR_EXCEPTION, "message": _error_message(exc)},
        error_type=type(exc).__name__,
        latency_ms=latency_ms,
    )


def _timed_out(timeout_s: float) -> _Outcome:
    return _Outcome(
        "timeout",
        error={"code": ERROR_TIMEOUT, "message": f"no result within {timeout_s:g} s"},
        error_type=ERROR_TIMEOUT,
        latency_ms=round(timeout_s * 1000),
    )


def _finished(raw: Any, latency_ms: int) -> _Outcome:
    """The task's return value as a reported output, shaped as the platform shapes target
    outputs (docs/contract.md §12 step 4): a str becomes `{"text": ...}`, a mapping stays an
    object, None becomes `{}`, any other value becomes `{"json": value}` (NaN / Infinity in it
    become null, lone surrogates U+FFFD). A value that is not JSON (an object, a cycle, nesting
    deeper than `MAX_JSON_DEPTH`) or more than 256 KiB of JSON is a task error."""
    try:
        if raw is None:
            output: Any = {}
        elif isinstance(raw, str):
            output = {"text": clean_text(raw)}
        elif isinstance(raw, Mapping):
            output = sanitize_json(raw)
        else:
            output = {"json": sanitize_json(raw)}
        size = len(encode_json(output))
    except (TypeError, ValueError, RecursionError) as exc:
        return _Outcome(
            "task_error",
            error={"code": ERROR_OUTPUT_NOT_JSON, "message": _error_message(exc)},
            error_type=ERROR_OUTPUT_NOT_JSON,
            latency_ms=latency_ms,
        )
    if size > semconv.ITEM_OUTPUT_MAX_BYTES:
        return _Outcome(
            "task_error",
            error={
                "code": ERROR_OUTPUT_TOO_LARGE,
                "message": f"the output is {size} bytes of JSON, more than "
                f"{semconv.ITEM_OUTPUT_MAX_BYTES}",
            },
            error_type=ERROR_OUTPUT_TOO_LARGE,
            latency_ms=latency_ms,
        )
    return _Outcome("ok", output=output, latency_ms=latency_ms)


def _invoke(task: Task, args: tuple[Any, ...]) -> Any:
    """Call a task outside an event loop; an awaitable result is run to completion."""
    raw = task(*args)
    if inspect.isawaitable(raw):
        return asyncio.run(_wait_for_awaitable(raw))
    return raw


async def _wait_for_awaitable(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


class _TaskSlots:
    """The places of task runs executing at once, runs abandoned after a timeout included: at
    most `size`. A thread cannot be killed, so a late task keeps its place until it returns;
    the run waits for a free place before the next task starts (module docstring)."""

    def __init__(self, size: int, experiment_id: str, timeout_s: float | None) -> None:
        self.size = size
        self.experiment_id = experiment_id
        self.patience_s = timeout_s
        self.free = threading.Semaphore(size)
        self.lock = threading.Lock()
        #: Places held by tasks that timed out and still run.
        self.abandoned = 0

    def take(self, stop: threading.Event) -> _Slot | None:
        """Wait for a free place; None when the run is stopping. Raises RuntimeError when no
        place frees up within `timeout_s` (every place is held by a task that timed out)."""
        start = time.monotonic()
        while not self.free.acquire(timeout=SLOT_POLL_S):
            if stop.is_set():
                return None
            self._check_patience(start)
        return self._taken(stop)

    async def take_async(self, stop: threading.Event) -> _Slot | None:
        """`take()` for `aevaluate()`: polls without blocking the event loop."""
        start = time.monotonic()
        while not self.free.acquire(blocking=False):
            if stop.is_set():
                return None
            self._check_patience(start)
            await asyncio.sleep(SLOT_POLL_S)
        return self._taken(stop)

    def _taken(self, stop: threading.Event) -> _Slot | None:
        slot = _Slot(self)
        if stop.is_set():
            slot.release()
            return None
        return slot

    def _check_patience(self, start: float) -> None:
        if self.patience_s is None or time.monotonic() - start < self.patience_s:
            return
        with self.lock:
            abandoned = self.abandoned
        raise RuntimeError(
            f"evaluate() stopped: experiment {self.experiment_id}: {abandoned} of the "
            f"max_concurrency={self.size} task runs timed out and are still running, and no "
            f"place freed up within {self.patience_s:g} s (a task thread cannot be killed; "
            "max_concurrency also bounds the runs that timed out, so a hung service never "
            "gets more calls at once)"
        )


class _Slot:
    """One taken place of `_TaskSlots`; `release()` is idempotent."""

    def __init__(self, slots: _TaskSlots) -> None:
        self._slots = slots
        self._released = False
        self._abandoned = False

    def abandon(self) -> None:
        """The run timed out: its task keeps the place until it returns."""
        with self._slots.lock:
            if not self._released and not self._abandoned:
                self._abandoned = True
                self._slots.abandoned += 1

    def release(self) -> None:
        with self._slots.lock:
            if self._released:
                return
            self._released = True
            if self._abandoned:
                self._slots.abandoned -= 1
        self._slots.free.release()


def _call_task_sync(
    task: Task, args: tuple[Any, ...], timeout_s: float | None, stop: threading.Event, slot: _Slot
) -> _Outcome:
    """Run the task and release `slot` when it returns (after a timeout: whenever it does)."""
    start = time.perf_counter()
    if timeout_s is None:
        try:
            raw = _invoke(task, args)
        except Exception as exc:
            return _failed(exc, _elapsed_ms(start))
        finally:
            slot.release()
        return _finished(raw, _elapsed_ms(start))
    # The task runs in its own daemon thread so that a late one can be abandoned: a thread
    # cannot be killed, its result is discarded (it keeps its slot until it returns).
    box: dict[str, Any] = {}
    done = threading.Event()

    def target() -> None:
        try:
            box["value"] = _invoke(task, args)
        except BaseException as exc:
            box["error"] = exc
        finally:
            slot.release()
            done.set()

    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run, args=(target,), name="insightmon-evaluate-task", daemon=True
    )
    try:
        thread.start()
    except BaseException:
        slot.release()
        raise
    deadline = start + timeout_s
    while not done.wait(min(0.25, max(0.0, deadline - time.perf_counter()))):
        if time.perf_counter() >= deadline or stop.is_set():
            break
    if not done.is_set():
        slot.abandon()
        return _timed_out(timeout_s)
    latency = _elapsed_ms(start)
    if "error" in box:
        error: BaseException = box["error"]
        if not isinstance(error, Exception):
            raise error
        return _failed(error, latency)
    return _finished(box.get("value"), latency)


def _in_thread(
    fn: Callable[..., Any], args: tuple[Any, ...], *, name: str = "insightmon-aevaluate"
) -> asyncio.Future[tuple[bool, Any]]:
    """Start `fn(*args)` in a new daemon thread with the caller's contextvars. The returned
    future (of the running loop) gets `(True, result)` or `(False, exception)`; cancelling it
    does not stop the thread. `aevaluate()` never uses the loop's default executor: it is
    shared with the application, and tasks that hang after a timeout would fill it and block
    the SDK's own calls."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[bool, Any]] = loop.create_future()
    context = contextvars.copy_context()

    def settle(outcome: tuple[bool, Any]) -> None:
        if not future.done():
            future.set_result(outcome)

    def target() -> None:
        try:
            outcome: tuple[bool, Any] = (True, context.run(fn, *args))
        except BaseException as exc:
            outcome = (False, exc)
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits any more
            loop.call_soon_threadsafe(settle, outcome)

    threading.Thread(target=target, name=name, daemon=True).start()
    return future


async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """`await` `fn(*args)` run in a dedicated daemon thread (`_in_thread`)."""
    ok, value = await _in_thread(fn, args)
    if not ok:
        raise value
    return value


async def _to_the_end(
    fn: Callable[..., Any], *args: Any, on_cancel: Callable[[], None] | None = None
) -> Any:
    """`_off_loop`, but a cancellation of the caller waits for `fn` to end (it reports items
    and may finalize; the client must not be closed under it): `on_cancel` is called, and the
    cancellation is raised once `fn` returned."""
    future = asyncio.ensure_future(_off_loop(fn, *args))
    cancelled: asyncio.CancelledError | None = None
    while not future.done():
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError as exc:
            cancelled = exc
            if on_cancel is not None:
                on_cancel()
    if cancelled is not None:
        if not future.cancelled():
            future.exception()  # retrieved: the cancellation wins
        raise cancelled
    return future.result()


async def _call_task_async(
    task: Task, args: tuple[Any, ...], timeout_s: float | None, slot: _Slot | None
) -> _Outcome:
    """Run the task: awaited when async (`slot` None; `asyncio.wait_for` cancels a late one),
    else in a dedicated thread that releases `slot` when the task returns. The timeout clock
    starts when the task starts."""
    start = time.perf_counter()
    waiter: Awaitable[tuple[bool, Any]]
    if slot is None:

        async def run() -> tuple[bool, Any]:
            # The task's own exceptions are returned, so that only our timeout escapes wait_for.
            try:
                raw = task(*args)
                if inspect.isawaitable(raw):
                    raw = await raw
            except Exception as exc:
                return False, exc
            return True, raw

        waiter = run()
    else:
        taken = slot

        def body() -> Any:
            try:
                return _invoke(task, args)
            finally:
                taken.release()

        try:
            waiter = _in_thread(body, (), name="insightmon-evaluate-task")
        except BaseException:
            slot.release()
            raise
    if timeout_s is None:
        ok, value = await waiter
    else:
        try:
            ok, value = await asyncio.wait_for(waiter, timeout_s)
        except asyncio.TimeoutError:
            if slot is not None:
                slot.abandon()
            return _timed_out(timeout_s)
    latency = _elapsed_ms(start)
    if not ok:
        if not isinstance(value, Exception):
            raise value
        return _failed(value, latency)
    return _finished(value, latency)


# --------------------------------------------------------------------------- client scores


def _evaluator_name(evaluator: Evaluator) -> str:
    """The default score name: the function's `__name__` (a callable object: its class)."""
    name = getattr(evaluator, "__name__", None)
    return name if isinstance(name, str) and name else type(evaluator).__name__


def _scores_of(evaluator: Evaluator, result: Any) -> list[dict[str, Any]]:
    """Client scores `{name, value, explanation?, metadata?}` from one evaluator result. Text
    is made encodable (`clean_text`); metadata keys reserved for the platform
    (`RESERVED_SCORE_METADATA_KEYS`, e.g. `adjudicated`) are dropped with a warning."""
    default_name = _evaluator_name(evaluator)
    if result is None:
        return []
    if isinstance(result, Mapping):
        entries: list[Any] = [result]
    elif isinstance(result, list | tuple):
        entries = list(result)
    elif isinstance(result, bool | int | float | str):
        entries = [{"name": default_name, "value": result}]
    else:
        raise TypeError(f"an evaluator returns a mapping, a list, a scalar or None, not {result!r}")
    scores: list[dict[str, Any]] = []
    for entry in entries:
        if entry is None:
            continue
        if not isinstance(entry, Mapping):
            raise TypeError(f"a score is a mapping, not {type(entry).__name__}")
        unknown = set(entry) - {"name", "value", "explanation", "metadata"}
        if unknown:
            raise ValueError(f"unknown score fields: {sorted(unknown)}")
        if "value" not in entry:
            raise ValueError("a score needs a value")
        name = entry.get("name", default_name)
        if not isinstance(name, str) or not 1 <= len(name) <= semconv.ITEM_SCORE_NAME_MAX:
            raise ValueError(f"a score name is 1 to {semconv.ITEM_SCORE_NAME_MAX} characters")
        name = clean_text(name)
        value = entry["value"]
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"score {name!r} is not a finite number")
        if value is None:
            continue  # no score
        score: dict[str, Any] = {"name": name, "value": sanitize_json(value)}
        explanation = entry.get("explanation")
        if explanation is not None:
            score["explanation"] = clean_text(str(explanation))[: semconv.EXPLANATION_MAX]
        metadata = entry.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise TypeError("score metadata must be a mapping")
            cleaned: dict[str, Any] = sanitize_json(metadata)
            reserved = sorted(RESERVED_SCORE_METADATA_KEYS & cleaned.keys())
            if reserved:
                _warn_reserved(name, reserved)
                cleaned = {k: v for k, v in cleaned.items() if k not in reserved}
            score["metadata"] = cleaned
        scores.append(score)
    return scores


_reserved_lock = threading.Lock()
_reserved_warned: set[str] = set()


def _warn_reserved(name: str, keys: Sequence[str]) -> None:
    """Warn once per score name (per process) about dropped reserved metadata keys."""
    with _reserved_lock:
        if name in _reserved_warned or len(_reserved_warned) >= 1000:
            return
        _reserved_warned.add(name)
    log.warning(
        "insightmon: client score %r: metadata keys %s are reserved for the platform (the SDK "
        "never writes adjudications or review markers); they were dropped",
        name,
        list(keys),
    )


class _Scores:
    """Client scores of one item: the first score of each name, at most 20."""

    def __init__(self, run: _Run) -> None:
        self.run = run
        self.scores: list[dict[str, Any]] = []

    def add(self, evaluator: Evaluator, result: Any) -> None:
        try:
            entries = _scores_of(evaluator, result)
        except Exception as exc:
            self.failed(evaluator, exc)
            return
        names = {s["name"] for s in self.scores}
        for score in entries:
            if score["name"] in names:
                log.debug("insightmon: duplicate client score %r ignored", score["name"])
                continue
            if len(self.scores) >= semconv.ITEM_SCORES_MAX:
                log.warning(
                    "insightmon: more than %d client scores for example %r; the rest are dropped",
                    semconv.ITEM_SCORES_MAX,
                    self.run.example.example_key,
                )
                return
            names.add(score["name"])
            self.scores.append(score)

    def failed(self, evaluator: Evaluator, exc: BaseException) -> None:
        # Client scores are diagnostic only (the gating evaluators run on the platform), so a
        # failing evaluator is logged and the item is reported without its score.
        log.warning(
            "insightmon: evaluator %s failed on example %r trial %d: %s",
            _evaluator_name(evaluator),
            self.run.example.example_key,
            self.run.trial_index,
            _error_message(exc),
        )


def _run_evaluators_sync(
    evaluators: Sequence[Evaluator], run: _Run, input_: Any, output: Any
) -> list[dict[str, Any]]:
    collected = _Scores(run)
    for evaluator in evaluators:
        try:
            result = evaluator(input_, output, run.example.reference)
            if inspect.isawaitable(result):
                result = asyncio.run(_wait_for_awaitable(result))
        except Exception as exc:
            collected.failed(evaluator, exc)
            continue
        collected.add(evaluator, result)
    return collected.scores


async def _run_evaluators_async(
    evaluators: Sequence[Evaluator], run: _Run, input_: Any, output: Any
) -> list[dict[str, Any]]:
    collected = _Scores(run)
    for evaluator in evaluators:
        try:
            if _is_async_callable(evaluator):
                result = evaluator(input_, output, run.example.reference)
            else:
                result = await _off_loop(evaluator, input_, output, run.example.reference)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            collected.failed(evaluator, exc)
            continue
        collected.add(evaluator, result)
    return collected.scores


# --------------------------------------------------------------------------- one experiment


class _Session:
    """State of one `evaluate()` / `aevaluate()` call after the experiment exists."""

    def __init__(self, client: Client, options: _Options, experiment: dict[str, Any]) -> None:
        self.client = client
        self.options = options
        self.experiment = experiment
        self.experiment_id: str = experiment["id"]
        self.runs: list[_Run] = []
        self.results: dict[int, ItemResult] = {}
        self._by_ref: dict[tuple[str, int], ItemResult] = {}
        self._lock = threading.Lock()
        self.stop = threading.Event()
        #: `aevaluate()` was cancelled while `complete()` ran: do not finalize.
        self.cancelled = threading.Event()
        self.slots = _TaskSlots(options.max_concurrency, self.experiment_id, options.timeout_s)
        self._holding_checked = False
        self.batcher = ItemBatcher(
            client,
            self.experiment_id,
            max_items=BATCH_MAX_ITEMS,
            max_interval_s=BATCH_MAX_INTERVAL_S,
            on_reported=self._reported,
        )

    # ------------------------------------------------------------------ setup

    def plan(self, examples: Sequence[Example]) -> None:
        trials = self.experiment.get("trials")
        default_trials = trials if isinstance(trials, int) and trials >= 1 else self.options.trials
        for example in examples:
            count = example.trials if example.trials >= 1 else default_trials
            for trial_index in range(count):
                self.runs.append(_Run(len(self.runs), example, trial_index))
        with_state = sum(1 for example in examples if example.initial_state)
        if with_state and self.options.task_args == "input":
            log.warning(
                "insightmon: experiment %s: %d examples have an initial_state, but the task "
                "takes only the input; declare task(input, initial_state) or "
                "task(input, example) to receive it",
                self.experiment_id,
                with_state,
            )
        log.info(
            "insightmon: experiment %s: %d examples, %d runs",
            self.experiment_id,
            len(examples),
            len(self.runs),
        )

    # ------------------------------------------------------------------ items

    def args(self, run: _Run) -> tuple[Any, ...]:
        input_ = copy.deepcopy(run.example.input)  # trials of one example never share state
        if self.options.task_args == "initial_state":
            return (input_, copy.deepcopy(run.example.initial_state))
        if self.options.task_args == "example":
            return (input_, run.example)
        return (input_,)

    def check_holding(self, span: Span) -> None:
        """Warn once per run when the item spans are exported as they end instead of being
        held until their items are reported (no `InsightMonSpanProcessor` with an export
        processor: e.g. collector mode without `init(export_processor=...)`)."""
        with self._lock:
            if self._holding_checked:
                return
            self._holding_checked = True
        if span.is_recording() and not has_release_listeners():
            log.warning(
                "insightmon: experiment %s: the spans of evaluate() are exported as they end, "
                "not held until their items are reported, so the platform may store them as "
                "production traces; pass the span processor that exports them to "
                "init(export_processor=...)",
                self.experiment_id,
            )

    def receipt(self) -> dict[str, Any] | None:
        """The item's receipt: `init()` agent version and components merged with the
        `set_component()` overrides of the current experiment scope (call inside the item)."""
        scope = current_experiment()
        overrides = scope.component_overrides() if scope is not None else ()
        components = merge_components(self.options.components, overrides)
        return _versions(self.options.agent_version, components)

    def report(
        self,
        run: _Run,
        outcome: _Outcome,
        trace_id: str | None,
        scores: list[dict[str, Any]],
        receipt: dict[str, Any] | None = None,
    ) -> None:
        """Record the local result and queue the item report (unless the run is stopping)."""
        result = ItemResult(
            example_key=run.example.example_key,
            trial_index=run.trial_index,
            status=outcome.status,
            output=outcome.output,
            error=outcome.error,
            latency_ms=outcome.latency_ms,
            otel_trace_id=trace_id,
            scores=list(scores),
        )
        with self._lock:
            self.results[run.index] = result
            self._by_ref[(result.example_key, result.trial_index)] = result
        item: dict[str, Any] = {
            "example_key": run.example.example_key,
            "trial_index": run.trial_index,
            "status": outcome.status,
        }
        if outcome.status == "ok":
            item["output"] = outcome.output
        if outcome.error is not None:
            item["error"] = outcome.error
        if outcome.latency_ms is not None:
            item["latency_ms"] = outcome.latency_ms
        if trace_id is not None:
            item["otel_trace_id"] = trace_id
        if receipt is not None:
            item["receipt"] = receipt
        if scores:
            item["scores"] = scores
        if self.stop.is_set():
            return  # stopping: an item that finished after the stop is not reported
        self.batcher.add(item)

    def _reported(self, items: Sequence[Mapping[str, Any]]) -> None:
        trace_ids: list[str] = []
        with self._lock:
            for item in items:
                result = self._by_ref.get((str(item["example_key"]), int(item["trial_index"])))
                if result is not None:  # the platform may have refused some client scores
                    result.scores = list(item.get("scores") or [])
                trace_id = item.get("otel_trace_id")
                if isinstance(trace_id, str):
                    trace_ids.append(trace_id)
        if trace_ids:
            _release_traces(trace_ids)

    def run_one_sync(self, run: _Run) -> None:
        options = self.options
        key = run.example.example_key
        slot = self.slots.take(self.stop)
        if slot is None:
            return  # stopping
        handed = False
        try:
            with _item_span(self.experiment_id, key, run.trial_index) as (span, trace_id):
                self.check_holding(span)
                args = self.args(run)
                handed = True
                outcome = _call_task_sync(options.task, args, options.timeout_s, self.stop, slot)
                if outcome.status != "ok":
                    _mark_error(span, outcome.error_type or outcome.status)
                receipt = self.receipt()
        finally:
            if not handed:
                slot.release()
        scores: list[dict[str, Any]] = []
        if outcome.status == "ok" and options.evaluators:
            with _evaluator_scope(self.experiment_id, key, run.trial_index):
                scores = _run_evaluators_sync(options.evaluators, run, args[0], outcome.output)
        self.report(run, outcome, trace_id, scores, receipt)

    async def run_one_async(self, run: _Run, task_is_async: bool) -> None:
        options = self.options
        key = run.example.example_key
        slot: _Slot | None = None
        if not task_is_async:
            slot = await self.slots.take_async(self.stop)
            if slot is None:
                return  # stopping
        handed = False
        try:
            with _item_span(self.experiment_id, key, run.trial_index) as (span, trace_id):
                self.check_holding(span)
                args = self.args(run)
                handed = True
                outcome = await _call_task_async(options.task, args, options.timeout_s, slot)
                if outcome.status != "ok":
                    _mark_error(span, outcome.error_type or outcome.status)
                receipt = self.receipt()
        finally:
            if slot is not None and not handed:
                slot.release()
        scores: list[dict[str, Any]] = []
        if outcome.status == "ok" and options.evaluators:
            with _evaluator_scope(self.experiment_id, key, run.trial_index):
                scores = await _run_evaluators_async(
                    options.evaluators, run, args[0], outcome.output
                )
        await _off_loop(self.report, run, outcome, trace_id, scores, receipt)

    # ------------------------------------------------------------------ ending

    def abort(self, error: BaseException) -> BaseException:
        """Stop early: report what finished (unless reporting itself failed), export the spans
        of reported items, drop the others, never finalize. Returns the exception to raise.
        The spans are dropped and the warning logged even when a second interruption ends the
        reporting."""
        self.stop.set()
        try:
            if self.batcher.error is None:
                try:
                    self.batcher.close()
                    self.batcher.flush()  # items an interrupted last request put back
                except Exception:
                    log.warning("insightmon: could not report the finished items", exc_info=True)
            else:
                self.batcher.stop()
        finally:
            _flush_quietly(self.experiment_id, discard=True)
            how = (
                "interrupted"
                if isinstance(error, KeyboardInterrupt | asyncio.CancelledError)
                else "stopped"
            )
            note = (
                f"evaluate() {how}: experiment {self.experiment_id} was not finalized "
                f"({self.batcher.reported} of {len(self.runs)} items reported); finalize it "
                f"with Client().finalize_experiment({self.experiment_id!r}) or let the platform "
                "finish it 24 hours after creation"
            )
            log.warning("insightmon: %s", note)
        if isinstance(error, InsightMonError):
            return with_message(error, f"{error.message or error.code} ({note})")
        return error

    def complete(self) -> None:
        """Report the rest, export the spans, finalize. An error or an interruption (Ctrl-C)
        while the last items are reported stops the run like one during the runs (`abort`),
        and so does a cancellation of `aevaluate()` (`cancelled`) before `:finalize`."""
        try:
            self.batcher.close()
            if self.cancelled.is_set():
                raise asyncio.CancelledError
        except BaseException as err:
            error = self.abort(err)
            if error is err:
                raise
            raise error from err
        log.info(
            "insightmon: experiment %s: %d items reported (%d duplicates)",
            self.experiment_id,
            self.batcher.reported,
            self.batcher.duplicates,
        )
        _flush_quietly(self.experiment_id)
        if self.cancelled.is_set():
            cancelled = asyncio.CancelledError()
            self.abort(cancelled)
            raise cancelled
        try:
            finalized = self.client.finalize_experiment(self.experiment_id)
        except InsightMonError as err:
            if is_already_finalized(err):
                log.warning(
                    "insightmon: experiment %s was already finalized (by the platform's "
                    "24-hour sweep or another caller)",
                    self.experiment_id,
                )
                return
            note = (
                f"every item of experiment {self.experiment_id} was reported but the "
                "experiment was not finalized; call Client().finalize_experiment("
                f"{self.experiment_id!r}) or let the platform finish it 24 hours after creation"
            )
            raise with_message(err, f"{err.message or err.code} ({note})") from err
        self.experiment = finalized["experiment"]

    def poll_delay(self, previous: float | None) -> float:
        return POLL_INITIAL_S if previous is None else min(previous * 2, POLL_MAX_S)

    def result(self) -> ExperimentResult:
        experiment = self.experiment
        try:
            url = self.client.experiment_url(experiment)
        except ValueError:
            url = self.client.base_url
        summary = experiment.get("summary")
        return ExperimentResult(
            experiment_id=self.experiment_id,
            url=url,
            status=str(experiment.get("status") or ""),
            summary=summary if isinstance(summary, dict) else None,
            items=[self.results[i] for i in sorted(self.results)],
            experiment=experiment,
        )


def _flush_quietly(experiment_id: str, *, discard: bool = False) -> None:
    """Telemetry is best effort: a failing flush never fails the experiment."""
    try:
        ok = _flush_telemetry(
            TELEMETRY_FLUSH_TIMEOUT_S, experiment_id=experiment_id, discard=discard
        )
    except Exception:
        log.warning("insightmon: could not flush spans and logs", exc_info=True)
        return
    if not ok:
        log.warning("insightmon: spans and logs were not all exported in time")


def _open_client(client: Client | None, api_key: str | None, base_url: str | None) -> Client:
    return client if client is not None else Client(base_url=base_url, api_key=api_key)


def _create(client: Client, options: _Options) -> dict[str, Any]:
    try:
        experiment = client.create_experiment(
            name=options.name,
            dataset_id=options.dataset,
            dataset_version=options.dataset_version,
            example_keys=options.example_keys,
            trials=options.trials,
            requested=options.requested,
            evaluators=options.server_evaluators,
            triggered_by="sdk",
            metadata=options.metadata,
        )
    except InsightMonError as err:
        if err.code == "permission_denied":
            raise with_message(err, f"{err.message or err.code} {_scope_hint(client)}") from err
        raise
    log.info("insightmon: experiment %s created", experiment["id"])
    return experiment


def _scope_hint(client: Client) -> str:
    needed = "evaluate() needs an API key with the experiment_write scope (and trace_ingest to "
    needed += "report spans)"
    try:
        info = client.api_key_info()
    except InsightMonError:
        return needed + "."
    return f"{needed}; the key {info.name!r} has {sorted(info.scopes)}."


def _run_all_sync(session: _Session) -> None:
    """Run every planned item on `max_concurrency` worker threads. Stops scheduling on the
    first error (or KeyboardInterrupt in the calling thread) and raises it."""
    runs = iter(session.runs)
    lock = threading.Lock()
    errors: list[BaseException] = []

    def next_run() -> _Run | None:
        with lock:
            if session.stop.is_set():
                return None
            return next(runs, None)

    def worker() -> None:
        while True:
            run = next_run()
            if run is None:
                return
            try:
                session.run_one_sync(run)
            except BaseException as exc:
                with lock:
                    errors.append(exc)
                session.stop.set()
                return

    count = min(session.options.max_concurrency, len(session.runs))
    threads = [
        threading.Thread(
            target=contextvars.copy_context().run,
            args=(worker,),
            name=f"insightmon-evaluate-{i}",
            daemon=True,
        )
        for i in range(count)
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(0.1)
    except BaseException:
        session.stop.set()
        raise
    if errors:
        raise errors[0]


async def _run_all_async(session: _Session) -> None:
    """Run every planned item with at most `max_concurrency` at once; cancels the rest on the
    first error or on cancellation."""
    runs = iter(session.runs)
    task_is_async = _is_async_callable(session.options.task)

    async def worker() -> None:
        for run in runs:
            if session.stop.is_set():
                return
            await session.run_one_async(run, task_is_async)

    count = min(session.options.max_concurrency, len(session.runs))
    workers = [asyncio.ensure_future(worker()) for _ in range(count)]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        session.stop.set()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise


def _wait_sync(session: _Session) -> None:
    deadline = _monotonic() + session.options.wait_timeout_s
    delay: float | None = None
    while str(session.experiment.get("status")) not in TERMINAL_STATUSES:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            log.info(
                "insightmon: experiment %s still running; not waiting longer", session.experiment_id
            )
            return
        delay = session.poll_delay(delay)
        _sleep(min(delay, remaining))
        try:
            session.experiment = session.client.get_experiment(session.experiment_id)
        except InsightMonError as err:
            log.warning("insightmon: could not read experiment %s: %s", session.experiment_id, err)
            return


async def _wait_async(session: _Session) -> None:
    deadline = _monotonic() + session.options.wait_timeout_s
    delay: float | None = None
    while str(session.experiment.get("status")) not in TERMINAL_STATUSES:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            log.info(
                "insightmon: experiment %s still running; not waiting longer", session.experiment_id
            )
            return
        delay = session.poll_delay(delay)
        await _async_sleep(min(delay, remaining))
        try:
            session.experiment = await _off_loop(
                session.client.get_experiment, session.experiment_id
            )
        except InsightMonError as err:
            log.warning("insightmon: could not read experiment %s: %s", session.experiment_id, err)
            return


# --------------------------------------------------------------------------- public


def evaluate(
    task: Task,
    *,
    dataset: str,
    dataset_version: int | None = None,
    example_keys: Sequence[str] | None = None,
    trials: int = 1,
    evaluators: Sequence[Evaluator] = (),
    server_evaluators: Sequence[str | Mapping[str, Any]] = (),
    name: str | None = None,
    requested: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_concurrency: int = 4,
    timeout_s: float | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    wait: bool = True,
    wait_timeout_s: float = 1800.0,
    client: Client | None = None,
) -> ExperimentResult:
    """Run `task` on every (example, trial) of `dataset` and let the platform judge the outputs.

    Args:
        task: `task(input)`, `task(input, initial_state)` (a second positional parameter named
            `initial_state` gets a copy of the example's `initial_state`) or
            `task(input, example)` (an `Example`). Returns the output: a str becomes
            `{"text": ...}`, a mapping stays an object, None becomes `{}`, any other JSON value
            becomes `{"json": value}`.
        dataset: the dataset id.
        dataset_version: frozen version (default: current).
        example_keys: subset of examples (default: the version's ready dev examples; holdout
            examples are refused by the platform).
        trials: runs per example, 1 to 10.
        evaluators: client-side evaluators `(input, output, reference)`, given the reported
            output; their scores are diagnostic only and their names must match published
            score configs. They run after the item's turn, outside its trace; spans they start
            are dropped.
        server_evaluators: evaluator version ids (gating), or mappings
            `{evaluator_version_id, role: "gating" | "diagnostic"}`, at most 20.
        name: experiment name (default: `evaluate <dataset> <UTC timestamp>`).
        requested: `{agent_version?, components?}` (default: from `init()`, without hashes).
        metadata: experiment metadata.
        max_concurrency: task runs at once (worker threads), runs that timed out and still
            run included.
        timeout_s: per-item timeout; a late item is reported as `timeout`. Its thread cannot
            be killed: it keeps its place among the `max_concurrency` until the task returns,
            and the run stops (not finalized) when no place frees up within another
            `timeout_s`.
        api_key / base_url: override `init()` settings and the environment.
        wait: poll until the platform finishes the evaluation.
        wait_timeout_s: give up waiting after this long (returns the current status).
        client: a `Client` to use (tests); closed only when evaluate created it.

    Returns:
        ExperimentResult.

    Raises:
        InsightMonError: platform errors (e.g. `permission_denied` without
            `experiment_write`, `examples_not_ready`); the experiment is not finalized then
            and the error message names its id when one was created.
        ValueError: invalid arguments (checked before any request).
        KeyboardInterrupt: Ctrl-C stops scheduling (or the last report); the finished items
            are reported, the experiment is not finalized (see the module docstring).
        RuntimeError: every place of `max_concurrency` is held by a task that timed out and
            still runs, for another `timeout_s`.

    Task errors never raise: an exception is reported as `task_error` (`error.code =
    "exception"`), a late run as `timeout`. A failing client evaluator is logged and the item
    is reported without that score. Client scores are diagnostic only; their names must be
    published score configs (otherwise the platform refuses them, and the SDK reports the item
    without them and logs a warning).
    """
    options = _Options.build(
        task,
        dataset=dataset,
        dataset_version=dataset_version,
        example_keys=example_keys,
        trials=trials,
        evaluators=evaluators,
        server_evaluators=server_evaluators,
        name=name,
        requested=requested,
        metadata=metadata,
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        wait=wait,
        wait_timeout_s=wait_timeout_s,
    )
    active = _open_client(client, api_key, base_url)
    try:
        session = _Session(active, options, _create(active, options))
        try:
            session.plan(list(active.iter_examples(session.experiment_id)))
            _run_all_sync(session)
        except BaseException as exc:
            error = session.abort(exc)
            if error is exc:
                raise
            raise error from exc
        session.complete()
        if options.wait:
            _wait_sync(session)
        return session.result()
    finally:
        if client is None:
            active.close()


async def aevaluate(
    task: Task,
    *,
    dataset: str,
    dataset_version: int | None = None,
    example_keys: Sequence[str] | None = None,
    trials: int = 1,
    evaluators: Sequence[Evaluator] = (),
    server_evaluators: Sequence[str | Mapping[str, Any]] = (),
    name: str | None = None,
    requested: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_concurrency: int = 4,
    timeout_s: float | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    wait: bool = True,
    wait_timeout_s: float = 1800.0,
    client: Client | None = None,
) -> ExperimentResult:
    """Async `evaluate()`: an async `task` is awaited (at most `max_concurrency` at once, with
    `asyncio.wait_for` for `timeout_s`, which cancels a late run); a sync one runs in dedicated
    daemon threads (their timeout clock starts when the task starts; `max_concurrency` bounds
    them, runs that timed out included, as in `evaluate()`). Async evaluators are awaited,
    sync ones run in dedicated threads. REST calls run in dedicated threads on the same
    synchronous `Client`; nothing uses the event loop's default executor. Cancelling the call
    stops scheduling, reports the finished items and does not finalize (see the module
    docstring); a cancellation while the last items are reported waits for that report to
    end, then raises without finalizing.
    """
    options = _Options.build(
        task,
        dataset=dataset,
        dataset_version=dataset_version,
        example_keys=example_keys,
        trials=trials,
        evaluators=evaluators,
        server_evaluators=server_evaluators,
        name=name,
        requested=requested,
        metadata=metadata,
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        wait=wait,
        wait_timeout_s=wait_timeout_s,
    )
    active = _open_client(client, api_key, base_url)
    try:
        experiment = await _off_loop(_create, active, options)
        session = _Session(active, options, experiment)
        try:
            examples = await _off_loop(lambda: list(active.iter_examples(session.experiment_id)))
            session.plan(examples)
            await _run_all_async(session)
        except BaseException as exc:
            error = await _to_the_end(session.abort, exc)
            if error is exc:
                raise
            raise error from exc
        await _to_the_end(session.complete, on_cancel=session.cancelled.set)
        if options.wait:
            await _wait_async(session)
        return session.result()
    finally:
        if client is None:
            await _to_the_end(active.close)
