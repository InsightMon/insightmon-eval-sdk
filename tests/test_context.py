from __future__ import annotations

import pytest

from insightmon import _context
from insightmon._config import Component
from insightmon._context import (
    ThreadScope,
    current_experiment,
    current_thread,
    experiment_scope,
    thread_scope,
)


def test_thread_scope_nests_and_resets() -> None:
    assert current_thread() is None
    outer = ThreadScope("chat-1")
    inner = ThreadScope("chat-2", language="en")
    with thread_scope(outer):
        assert current_thread() is outer
        with thread_scope(inner):
            assert current_thread() is inner
        assert current_thread() is outer
    assert current_thread() is None


def test_thread_component_overrides() -> None:
    scope = ThreadScope("chat-1")
    scope.set_component(Component("prompt", "system", version="1"))
    scope.set_component(Component("prompt", "system", version="2"))
    assert scope.component_overrides() == (Component("prompt", "system", version="2"),)


def test_experiment_scope_hides_the_thread() -> None:
    with thread_scope(ThreadScope("chat-1")):
        with experiment_scope("exp1", "k1", 0) as scope:
            assert current_thread() is None
            assert current_experiment() is scope
            assert scope.attributes() == {
                "insightmon.experiment_id": "exp1",
                "insightmon.example_key": "k1",
                "insightmon.trial_index": 0,
            }
        assert current_thread() is not None
    assert current_experiment() is None


def test_hold_and_release() -> None:
    seen: list[tuple[frozenset[int] | None, bool]] = []

    def listener(trace_ids: frozenset[int] | None, discard: bool) -> None:
        seen.append((trace_ids, discard))

    _context.add_release_listener(listener)
    _context.add_release_listener(listener)  # registered once
    _context.hold_trace(1)
    _context.hold_trace(2)
    assert _context.is_held(1)
    _context.release_traces([1])
    assert not _context.is_held(1)
    assert _context.held_traces() == frozenset({2})
    _context.release_traces(None)
    assert _context.held_traces() == frozenset()
    assert seen == [(frozenset({1}), False), (None, False)]
    _context.remove_release_listener(listener)
    _context.release_traces([5])
    assert len(seen) == 2


def test_held_traces_have_owners() -> None:
    _context.hold_trace(1, "exp-a")
    _context.hold_trace(2, "exp-b")
    _context.hold_trace(3)
    _context.hold_trace(1, "exp-b")  # the first owner stays
    assert _context.held_traces("exp-a") == frozenset({1})
    assert _context.held_traces("exp-b") == frozenset({2})
    assert _context.held_traces() == frozenset({1, 2, 3})


def test_discard_drops_and_remembers() -> None:
    seen: list[tuple[frozenset[int] | None, bool]] = []
    _context.add_release_listener(lambda ids, discard: seen.append((ids, discard)))
    _context.hold_trace(1, "exp-a")
    _context.hold_trace(2, "exp-a")
    _context.discard_traces(_context.held_traces("exp-a"))
    assert seen == [(frozenset({1, 2}), True)]
    assert _context.held_traces() == frozenset()
    assert _context.is_discarded(1)
    assert _context.is_discarded(2)
    _context.discard_traces([])  # nothing to tell
    assert len(seen) == 1
    _context.hold_trace(1)  # a trace held again is no longer discarded
    assert not _context.is_discarded(1)


def test_discarded_ids_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_context, "MAX_DISCARDED", 3)
    _context.discard_traces(range(5))
    assert [t for t in range(5) if _context.is_discarded(t)] == [2, 3, 4]


def test_experiment_component_overrides() -> None:
    with experiment_scope("exp1", "k1", 0) as scope:
        scope.set_component(Component("prompt", "system", version="1"))
        scope.set_component(Component("prompt", "system", version="2"))
        assert scope.component_overrides() == (Component("prompt", "system", version="2"),)
    with experiment_scope("exp1", "k1", 1) as other:
        assert other.component_overrides() == ()


def test_trace_id_hex() -> None:
    assert _context.trace_id_hex(255) == "0" * 30 + "ff"


def test_trace_status_reads_held_and_discarded_in_one_step() -> None:
    _context.hold_trace(1, "exp-a")
    _context.discard_traces([2])
    assert _context.trace_status(1) == "held"
    assert _context.trace_status(2) == "discarded"
    assert _context.trace_status(3) is None


def test_closed_owners_discard_new_traces() -> None:
    _context.hold_trace(1, "exp-a")
    _context.close_owner("exp-a")
    assert _context.is_held(1)  # closing leaves held traces alone
    _context.hold_trace(2, "exp-a")
    assert _context.trace_status(2) == "discarded"
    _context.hold_trace(3, "exp-b")  # other owners are not affected
    assert _context.is_held(3)
    _context.discard_owner("exp-b")
    assert _context.trace_status(3) == "discarded"
    _context.hold_trace(4, "exp-b")
    assert _context.trace_status(4) == "discarded"


def test_release_listeners_are_visible() -> None:
    assert not _context.has_release_listeners()

    def listener(trace_ids: frozenset[int] | None, discard: bool) -> None:
        pass

    _context.add_release_listener(listener)
    assert _context.has_release_listeners()


def test_scope_owner_defaults_to_the_experiment() -> None:
    with experiment_scope("exp1", "k1", 0) as scope:
        assert scope.holder == "exp1"
    with experiment_scope("exp1", "k1", 0, owner="exp1/evaluators/k1/0", stamp=False) as scope:
        assert scope.holder == "exp1/evaluators/k1/0"
        assert scope.stamp is False
