"""`ItemBatcher`: groups experiment item reports into `items:batch` requests.

Flush rules (docs/contract.md §12 step 5): when `max_items` items are pending, when the oldest
pending item is `max_interval_s` old (checked on `add()` and by a timer thread), on `flush()`
and on `close()`. Pending items are cut into requests of at most `max_items` items and
`max_body_bytes` bytes (`client.plan_requests`), and each request goes through
`Client.report_items`, which encodes the body once and re-sends the same bytes on retry (the
platform deduplicates by report content). After each successful request `on_reported` is
called with the reported items (evaluate releases their held spans there).

400 `external_item_invalid {reason: "score_invalid"}` (a client score whose name is not a
published score config, or whose value does not fit it): nothing of the request was written,
so the batcher drops the refused item's client scores and sends the request again. The
platform names the item by `example_key` (and `trial_index` when it says so). When several
items of that example carry client scores (trials), the batcher does not guess: it sends one
of them with the rest of the request and the others alone, so that each refusal names exactly
one item and valid scores are never dropped for another trial's value.

Bounds: after `REFUSALS_PER_REQUEST` refusals within one request, the items that still carry
client scores are sent one at a time (a refusal then re-sends one small item, never the whole
request again). After `REFUSALS_BEFORE_DROP` refusals of items carrying the same set of score
names, a set the platform never accepted is dropped from every other item of the run up front
(a misnamed evaluator would otherwise cost one extra request per item). One warning per set
of names, one more when the set is dropped for the rest of the run.

Thread-safe: `add()` is called from evaluate's worker threads; requests are sent one at a
time. A failed request is kept as the first error; later `add()` / `flush()` calls raise it
again (evaluate stops on it).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any

from insightmon import semconv
from insightmon._errors import EXTERNAL_ITEM_INVALID, InsightMonError
from insightmon.client import plan_requests

if TYPE_CHECKING:
    from insightmon.client import Client, ReportResult

__all__ = ["ItemBatcher"]

log = logging.getLogger("insightmon")

ReportedCallback = Callable[[Sequence[Mapping[str, Any]]], None]

#: Refusals of items carrying the same set of client score names after which the batcher drops
#: that set from the remaining items of the run without asking the platform again (unless the
#: platform accepted that set before: its refusals are then about values, not names).
REFUSALS_BEFORE_DROP = 3
#: `score_invalid` refusals within one request after which the items that still carry client
#: scores are sent one at a time.
REFUSALS_PER_REQUEST = 3


class _Chunk:
    """One planned request being sent: the current version of each item (client scores may
    be dropped), the items the platform confirmed, and the refusals so far."""

    def __init__(self, items: list[Mapping[str, Any]]) -> None:
        self.items = items
        self.confirmed: set[int] = set()
        self.refusals = 0

    def unconfirmed(self) -> list[Mapping[str, Any]]:
        return [item for i, item in enumerate(self.items) if i not in self.confirmed]


class ItemBatcher:
    """Batch item reports of one external experiment.

    Args:
        client: the REST client.
        experiment_id: the experiment the items belong to.
        max_items: pending items that trigger a request (1 to 200).
        max_interval_s: age of the oldest pending item that triggers a request.
        max_body_bytes: body size limit of one request (passed to `Client.report_items`).
        on_reported: called after each successful request with its items (as sent).
        clock: monotonic clock (tests pass a fake).
        timer: run the timer thread that flushes items older than `max_interval_s` while no
            `add()` comes (tests turn it off and call `flush_if_due()`).
    """

    def __init__(
        self,
        client: Client,
        experiment_id: str,
        *,
        max_items: int = 50,
        max_interval_s: float = 5.0,
        max_body_bytes: int = semconv.TARGET_BODY_BYTES,
        on_reported: ReportedCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
        timer: bool = True,
    ) -> None:
        if not 1 <= max_items <= semconv.ITEMS_BATCH_MAX:
            raise ValueError(f"max_items must be between 1 and {semconv.ITEMS_BATCH_MAX}")
        if not max_interval_s > 0:
            raise ValueError("max_interval_s must be > 0")
        self.client = client
        self.experiment_id = experiment_id
        self.max_items = max_items
        self.max_interval_s = max_interval_s
        self.max_body_bytes = max_body_bytes
        self.on_reported = on_reported
        self._clock = clock
        self.accepted = 0
        self.duplicates = 0
        #: Items reported successfully (accepted or duplicate), in reporting order.
        self.reported = 0
        self._timer_enabled = timer
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._pending: list[dict[str, Any]] = []
        self._oldest: float | None = None
        self._error: Exception | None = None
        self._closed = False
        self._timer: threading.Thread | None = None
        #: set of client score names -> refusals (`score_invalid`); only touched while sending
        self._refusals: dict[frozenset[str], int] = {}
        #: sets of client score names the platform accepted at least once
        self._accepted_names: set[frozenset[str]] = set()

    # ------------------------------------------------------------------ public

    @property
    def error(self) -> Exception | None:
        """The first failed request's error, if any."""
        return self._error

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def add(self, item: Mapping[str, Any]) -> None:
        """Queue one `ReportedItem` mapping; may send a request (see flush rules)."""
        self._raise_if_failed()
        with self._lock:
            if self._closed:
                raise RuntimeError("the item batcher is closed")
            self._pending.append(dict(item))
            if self._oldest is None:
                self._oldest = self._clock()
            due = self._is_due()
            self._start_timer()
        if due:
            self._send_pending()
        self._raise_if_failed()

    def flush_if_due(self) -> bool:
        """Send the pending items if the count or age threshold is reached; True if sent."""
        with self._lock:
            due = self._is_due()
        if due:
            self._send_pending()
        return due

    def flush(self) -> None:
        """Send every pending item now."""
        self._raise_if_failed()
        self._send_pending()
        self._raise_if_failed()

    def stop(self) -> None:
        """Stop the timer thread without sending anything (pending items stay pending)."""
        self._stop.set()
        timer = self._timer
        if timer is not None and timer is not threading.current_thread():
            timer.join()

    def close(self) -> None:
        """Flush and stop the timer thread. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stop()
        self.flush()

    def __enter__(self) -> ItemBatcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is None:
            self.close()
            return
        try:  # do not hide the exception that is already on its way
            self.close()
        except Exception:
            log.warning("insightmon: could not report the pending experiment items", exc_info=True)

    # ------------------------------------------------------------------ internals

    def _is_due(self) -> bool:  # under self._lock
        if self._oldest is None:
            return False
        return (
            len(self._pending) >= self.max_items
            or self._clock() - self._oldest >= self.max_interval_s
        )

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error

    def _start_timer(self) -> None:  # under self._lock
        if not self._timer_enabled or self._timer is not None or self._stop.is_set():
            return
        self._timer = threading.Thread(
            target=self._run_timer, name="insightmon-item-batcher", daemon=True
        )
        self._timer.start()

    def _run_timer(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if self._error is not None:
                    return
                oldest = self._oldest
            if oldest is None:
                wait = self.max_interval_s
            else:
                wait = oldest + self.max_interval_s - self._clock()
            if wait > 0:
                self._stop.wait(wait)
                continue
            try:
                self.flush_if_due()
            except Exception:  # kept in self._error; add() / flush() raise it
                return

    def _send_pending(self) -> None:
        with self._send_lock:
            if self._error is not None:
                return
            with self._lock:
                items = self._pending
                self._pending = []
                self._oldest = None
            if not items:
                return
            try:
                chunks = [
                    list(chunk)
                    for chunk, _body in plan_requests(
                        "items", items, max_items=self.max_items, max_body_bytes=self.max_body_bytes
                    )
                ]
            except Exception as exc:  # an item that is not JSON or alone too large
                self._fail(exc)
                raise
            for index, chunk in enumerate(chunks):
                state = _Chunk([self._without_dropped_scores(item) for item in chunk])
                try:
                    self._send_items(state, list(range(len(state.items))))
                except Exception as exc:
                    self._fail(exc)
                    raise
                except BaseException:
                    # interrupted (e.g. KeyboardInterrupt): keep what was not confirmed (as last
                    # sent), a later flush may send it again (same content: at worst duplicates)
                    unsent = state.unconfirmed()
                    unsent += [item for rest in chunks[index + 1 :] for item in rest]
                    with self._lock:
                        self._pending[:0] = [dict(item) for item in unsent]
                        if self._oldest is None:
                            self._oldest = self._clock()
                    raise

    def _fail(self, error: Exception) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def _dropped(self, names: frozenset[str]) -> bool:
        return (
            self._refusals.get(names, 0) >= REFUSALS_BEFORE_DROP
            and names not in self._accepted_names
        )

    def _without_dropped_scores(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        if item.get("scores") and self._dropped(_score_names(item)):
            return _without_scores(item)
        return item

    def _refused(self, item: Mapping[str, Any]) -> None:
        names = _score_names(item)
        count = self._refusals.get(names, 0) + 1
        self._refusals[names] = count
        level = logging.WARNING if count == 1 else logging.DEBUG
        log.log(
            level,
            "insightmon: the platform refused the client scores %s of example %r trial %s "
            "(each name must be a published score config of the project and each value must "
            "fit it); reporting the item without client scores",
            sorted(names),
            item.get("example_key"),
            item.get("trial_index"),
        )
        if count == REFUSALS_BEFORE_DROP and self._dropped(names):
            log.warning(
                "insightmon: the client scores %s were refused %d times; dropping them from "
                "the other items of experiment %s",
                sorted(names),
                count,
                self.experiment_id,
            )

    def _send_items(self, state: _Chunk, indexes: list[int]) -> None:
        """Send `state.items[indexes]` until the platform accepts them, dropping refused
        client scores (module docstring)."""
        while True:
            if state.refusals >= REFUSALS_PER_REQUEST and len(indexes) > 1:
                scored = [i for i in indexes if state.items[i].get("scores")]
                if len(scored) > 1:
                    self._send_apart(state, indexes, scored[1:])
                    return
            items = [state.items[i] for i in indexes]
            try:
                result = self.client.report_items(
                    self.experiment_id, items, max_body_bytes=self.max_body_bytes
                )
            except InsightMonError as err:
                refused = _score_invalid_items(err, indexes, state.items)
                if not refused:
                    raise
                if len(refused) > 1:  # several trials of that example carry client scores
                    self._send_apart(state, indexes, refused[1:])
                    return
                index = refused[0]
                self._refused(state.items[index])
                state.items[index] = _without_scores(state.items[index])
                state.refusals += 1
                for i in indexes:
                    state.items[i] = self._without_dropped_scores(state.items[i])
                continue
            self._confirmed(state, indexes, result)
            return

    def _send_apart(self, state: _Chunk, indexes: list[int], apart: list[int]) -> None:
        """Send `indexes` without `apart` in one request, then each of `apart` alone."""
        alone = set(apart)
        self._send_items(state, [i for i in indexes if i not in alone])
        for index in apart:
            self._send_items(state, [index])

    def _confirmed(self, state: _Chunk, indexes: list[int], result: ReportResult) -> None:
        items = [state.items[i] for i in indexes]
        state.confirmed.update(indexes)
        for item in items:
            if item.get("scores"):
                self._accepted_names.add(_score_names(item))
        with self._lock:
            self.accepted += result.accepted
            self.duplicates += result.duplicates
            self.reported += len(items)
        if self.on_reported is not None:
            try:
                self.on_reported(items)
            except Exception:
                log.warning("insightmon: on_reported callback failed", exc_info=True)


def _score_names(item: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        str(score.get("name")) for score in item.get("scores") or () if isinstance(score, Mapping)
    )


def _without_scores(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "scores"}


def _score_invalid_items(
    error: InsightMonError, indexes: Sequence[int], items: Sequence[Mapping[str, Any]]
) -> list[int]:
    """The items (among `indexes`) a 400 `external_item_invalid {reason: score_invalid}` may
    name: those of its `example_key` (and `trial_index`, when given) that still carry client
    scores. Empty for any other error."""
    if error.code != EXTERNAL_ITEM_INVALID or error.params.get("reason") != "score_invalid":
        return []
    key = error.params.get("example_key")
    trial = error.params.get("trial_index")
    by_trial = isinstance(trial, int) and not isinstance(trial, bool)
    return [
        i
        for i in indexes
        if items[i].get("example_key") == key
        and items[i].get("scores")
        and (not by_trial or items[i].get("trial_index") == trial)
    ]
