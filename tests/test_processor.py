"""InsightMonSpanProcessor: stamping rules, experiment scopes and held traces."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

import insightmon
from insightmon import _context, processor, semconv
from insightmon._context import experiment_scope, release_traces
from insightmon.processor import InsightMonSpanProcessor

CONV = semconv.GEN_AI_CONVERSATION_ID
PROMPT_KEY = semconv.component_attribute("prompt", "system")


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


class Pipeline:
    """A provider whose only processor is an InsightMonSpanProcessor exporting to memory."""

    def __init__(self, max_held_spans: int = 10_000, resource: Resource | None = None) -> None:
        self.exporter = InMemorySpanExporter()
        self.processor = InsightMonSpanProcessor(
            SimpleSpanProcessor(self.exporter), max_held_spans=max_held_spans
        )
        self.provider = TracerProvider(resource=resource or Resource.create({}))
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer("app")

    def finished(self) -> dict[str, ReadableSpan]:
        return {span.name: span for span in self.exporter.get_finished_spans()}


@pytest.fixture
def pipeline() -> Iterator[Pipeline]:
    built = Pipeline()
    yield built
    built.provider.shutdown()


# --------------------------------------------------------------------------- stamping


def test_processor_works_without_init(pipeline: Pipeline) -> None:
    insightmon.set_component("prompt", "system", version="v2")
    with (
        insightmon.thread("chat-1", language="ja"),
        pipeline.tracer.start_as_current_span("root"),
        pipeline.tracer.start_as_current_span("child"),
    ):
        pass
    spans = pipeline.finished()
    root = attrs(spans["root"])
    assert root[CONV] == "chat-1"
    assert root[semconv.LANGUAGE] == "ja"
    assert root[PROMPT_KEY] == "v2"
    assert semconv.AGENT_NAME not in root  # nothing from init()
    assert attrs(spans["child"])[CONV] == "chat-1"
    assert PROMPT_KEY not in attrs(spans["child"])


def test_agent_identity_wins_on_anchors_only(pipeline: Pipeline) -> None:
    insightmon.init(
        tracer_provider=pipeline.provider, agent_name="weather-assistant", agent_version="2.0"
    )
    with pipeline.tracer.start_as_current_span(
        "framework root", attributes={semconv.AGENT_NAME: "Planner"}
    ):
        with pipeline.tracer.start_as_current_span(
            "sub agent", attributes={semconv.AGENT_NAME: "Geocoder"}
        ):
            pass
        with pipeline.tracer.start_as_current_span("plain"):
            pass
    spans = pipeline.finished()
    assert attrs(spans["framework root"])[semconv.AGENT_NAME] == "weather-assistant"
    assert attrs(spans["sub agent"])[semconv.AGENT_NAME] == "Geocoder"
    assert attrs(spans["sub agent"])[semconv.AGENT_VERSION] == "2.0"
    assert attrs(spans["plain"])[semconv.AGENT_NAME] == "weather-assistant"


def test_remote_parent_makes_a_local_root(pipeline: Pipeline) -> None:
    insightmon.init(
        tracer_provider=pipeline.provider,
        components=[{"type": "tool", "name": "geo", "version": "5", "content_hash": "h1"}],
    )
    remote = SpanContext(
        trace_id=0xABC,
        span_id=0x123,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    parent = trace.set_span_in_context(NonRecordingSpan(remote))
    with (
        pipeline.tracer.start_as_current_span("handler", context=parent),
        pipeline.tracer.start_as_current_span("inner"),
    ):
        pass
    spans = pipeline.finished()
    handler = attrs(spans["handler"])
    assert handler[semconv.component_attribute("tool", "geo")] == "5"
    assert handler[semconv.component_attribute("tool", "geo", "content_hash")] == "h1"
    assert semconv.component_attribute("tool", "geo") not in attrs(spans["inner"])
    assert spans["handler"].context.trace_id == 0xABC


def test_environment_is_stamped_when_the_resource_lacks_it() -> None:
    plain = Pipeline()
    tagged = Pipeline(resource=Resource.create({semconv.DEPLOYMENT_ENVIRONMENT_NAME: "staging"}))
    for built in (plain, tagged):
        insightmon.init(tracer_provider=built.provider, environment="staging")
        with (
            built.tracer.start_as_current_span("root"),
            built.tracer.start_as_current_span("child"),
        ):
            pass
        insightmon.shutdown()
    assert attrs(plain.finished()["root"])[semconv.DEPLOYMENT_ENVIRONMENT_NAME] == "staging"
    assert semconv.DEPLOYMENT_ENVIRONMENT_NAME not in attrs(plain.finished()["child"])
    assert semconv.DEPLOYMENT_ENVIRONMENT_NAME not in attrs(tagged.finished()["root"])


def test_component_attribute_already_on_a_span_wins(pipeline: Pipeline) -> None:
    insightmon.set_component("prompt", "system", version="v2")
    with pipeline.tracer.start_as_current_span("root", attributes={PROMPT_KEY: "pinned"}):
        pass
    assert attrs(pipeline.finished()["root"])[PROMPT_KEY] == "pinned"


def test_stamping_failures_never_break_spans(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(span: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(processor, "stamp_attributes", broken)
    with caplog.at_level(logging.WARNING, logger="insightmon.processor"):
        with pipeline.tracer.start_as_current_span("a"):
            pass
        with pipeline.tracer.start_as_current_span("b"):
            pass
    assert names(pipeline.exporter) == ["a", "b"]
    assert len([r for r in caplog.records if "stamp" in r.getMessage()]) == 1


# --------------------------------------------------------------------------- experiments


def test_experiment_scope_suppresses_the_thread_and_holds_spans(pipeline: Pipeline) -> None:
    with insightmon.thread("chat-1"), experiment_scope("exp-1", "k1", 2):
        with (
            pipeline.tracer.start_as_current_span("evaluate") as root,
            insightmon.thread("inner-chat"),  # a thread opened by the task itself
            pipeline.tracer.start_as_current_span("step"),
        ):
            pass
        trace_id = root.get_span_context().trace_id
    assert _context.is_held(trace_id)
    assert names(pipeline.exporter) == []
    assert pipeline.processor.held_span_count == 2
    release_traces([trace_id])
    spans = pipeline.finished()
    assert set(spans) == {"evaluate", "step"}
    assert pipeline.processor.held_span_count == 0
    root_attrs = attrs(spans["evaluate"])
    assert root_attrs[semconv.EXPERIMENT_ID] == "exp-1"
    assert root_attrs[semconv.EXAMPLE_KEY] == "k1"
    assert root_attrs[semconv.TRIAL_INDEX] == 2
    for span in spans.values():
        assert not set(semconv.THREAD_KEY_ATTRIBUTES) & set(attrs(span))
    assert semconv.EXPERIMENT_ID not in attrs(spans["step"])


def test_spans_of_a_held_trace_from_other_threads_are_held(pipeline: Pipeline) -> None:
    with (
        experiment_scope("exp-1", "k1", 0),
        pipeline.tracer.start_as_current_span("evaluate") as root,
    ):
        parent = otel_context.get_current()

        def worker() -> None:  # no copy_context(): only the parent span is passed on
            with pipeline.tracer.start_as_current_span("background", context=parent):
                pass

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert names(pipeline.exporter) == []
    release_traces([root.get_span_context().trace_id])
    assert sorted(names(pipeline.exporter)) == ["background", "evaluate"]


def test_release_is_per_trace_and_late_spans_pass_through(pipeline: Pipeline) -> None:
    roots = []
    for key in ("k1", "k2"):
        with (
            experiment_scope("exp-1", key, 0),
            pipeline.tracer.start_as_current_span(f"evaluate-{key}") as root,
        ):
            roots.append(root)
    late = pipeline.tracer.start_span(
        "late", context=trace.set_span_in_context(roots[0]), attributes={}
    )
    release_traces([roots[0].get_span_context().trace_id])
    assert names(pipeline.exporter) == ["evaluate-k1"]
    late.end()  # its trace is no longer held: exported at once
    assert names(pipeline.exporter) == ["evaluate-k1", "late"]
    release_traces(None)
    assert names(pipeline.exporter) == ["evaluate-k1", "late", "evaluate-k2"]


def test_held_traces_carry_the_experiment_as_owner(pipeline: Pipeline) -> None:
    with (
        experiment_scope("exp-1", "k1", 0),
        pipeline.tracer.start_as_current_span("evaluate") as root,
    ):
        pass
    assert _context.held_traces("exp-1") == frozenset({root.get_span_context().trace_id})
    assert _context.held_traces("exp-2") == frozenset()


def test_discarded_traces_are_dropped_even_when_spans_end_later(pipeline: Pipeline) -> None:
    with experiment_scope("exp-1", "k1", 0):
        root = pipeline.tracer.start_span("evaluate")
        with pipeline.tracer.start_as_current_span("step", context=trace.set_span_in_context(root)):
            pass
    assert pipeline.processor.held_span_count == 1
    _context.discard_traces(_context.held_traces("exp-1"))
    assert pipeline.processor.held_span_count == 0
    root.end()  # an abandoned run ends after the discard
    assert names(pipeline.exporter) == []


def test_experiment_component_overrides_are_stamped_on_anchors(pipeline: Pipeline) -> None:
    insightmon.set_component("prompt", "system", version="v1")
    with experiment_scope("exp-1", "k1", 0) as scope:
        insightmon.set_component("prompt", "system", version="v9")
        with pipeline.tracer.start_as_current_span("evaluate"):
            pass
        assert scope.component_overrides()[0].version == "v9"
    with pipeline.tracer.start_as_current_span("later"):
        pass
    release_traces(None)
    spans = pipeline.finished()
    assert attrs(spans["evaluate"])[PROMPT_KEY] == "v9"
    assert attrs(spans["later"])[PROMPT_KEY] == "v1"  # the process default is unchanged


def test_experiment_scope_without_hold_exports_at_once(pipeline: Pipeline) -> None:
    with (
        experiment_scope("exp-1", "k1", 0, hold=False),
        pipeline.tracer.start_as_current_span("evaluate"),
    ):
        pass
    assert names(pipeline.exporter) == ["evaluate"]
    assert _context.held_traces() == frozenset()


def test_held_buffer_is_bounded(caplog: pytest.LogCaptureFixture) -> None:
    built = Pipeline(max_held_spans=2)
    with (
        caplog.at_level(logging.WARNING, logger="insightmon.processor"),
        experiment_scope("exp-1", "k1", 0),
        built.tracer.start_as_current_span("evaluate"),
    ):
        for index in range(4):
            with built.tracer.start_as_current_span(f"step-{index}"):
                pass
    assert names(built.exporter) == ["step-2", "step-3", "evaluate"]
    assert built.processor.held_span_count == 2
    assert any("waiting" in record.getMessage() for record in caplog.records)
    release_traces(None)
    assert names(built.exporter)[-2:] == ["step-0", "step-1"]


def test_shutdown_releases_held_spans_and_stops_stamping(pipeline: Pipeline) -> None:
    with (
        experiment_scope("exp-1", "k1", 0),
        pipeline.tracer.start_as_current_span("evaluate"),
    ):
        pass
    pipeline.processor.shutdown()
    assert names(pipeline.exporter) == ["evaluate"]
    pipeline.processor.shutdown()  # idempotent
    assert pipeline.processor.force_flush() is True
    with insightmon.thread("chat-1"), pipeline.tracer.start_as_current_span("after"):
        pass
    assert names(pipeline.exporter) == ["evaluate"]  # the exporter is shut down


def test_stamp_only_processor_forwards_nothing() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    stamp_only = InsightMonSpanProcessor()
    provider.add_span_processor(stamp_only)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("app")
    with insightmon.thread("chat-1"), tracer.start_as_current_span("root"):
        pass
    assert stamp_only.export_processor is None
    assert stamp_only.force_flush() is True
    (span,) = exporter.get_finished_spans()
    assert attrs(span)[CONV] == "chat-1"
    provider.shutdown()


def test_invalid_buffer_bound() -> None:
    with pytest.raises(ValueError, match="max_held_spans"):
        InsightMonSpanProcessor(max_held_spans=-1)
    with pytest.raises(ValueError, match="release_chunk"):
        InsightMonSpanProcessor(release_chunk=0)


def test_released_spans_reach_a_default_batch_queue_in_chunks() -> None:
    exporter = InMemorySpanExporter()
    batch = BatchSpanProcessor(exporter)  # OpenTelemetry's default queue: 2048 spans
    holder = InsightMonSpanProcessor(batch)
    provider = TracerProvider()
    provider.add_span_processor(holder)
    tracer = provider.get_tracer("app")
    roots = []
    for item in range(50):
        with (
            experiment_scope("exp-1", f"k{item}", 0),
            tracer.start_as_current_span("evaluate") as root,
        ):
            for step in range(59):
                with tracer.start_as_current_span(f"step-{step}"):
                    pass
        roots.append(root.get_span_context().trace_id)
    assert holder.held_span_count == 3000
    release_traces(roots)  # one report of 50 items: 3000 spans at once
    assert batch.force_flush()
    assert len(exporter.get_finished_spans()) == 3000
    provider.shutdown()


def test_a_span_ending_while_its_trace_is_discarded_is_dropped(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    with experiment_scope("exp-1", "k1", 0):
        root = pipeline.tracer.start_span("evaluate")
    trace_id = root.get_span_context().trace_id
    real = processor.trace_status
    discarder: list[threading.Thread] = []

    def racing_status(tid: int) -> Any:
        status = real(tid)
        if not discarder:  # the run is stopped while on_end decides
            thread = threading.Thread(target=_context.discard_traces, args=([trace_id],))
            discarder.append(thread)
            thread.start()
            thread.join(0.2)
        return status

    monkeypatch.setattr(processor, "trace_status", racing_status)
    root.end()
    discarder[0].join(5)
    assert names(pipeline.exporter) == []
    assert pipeline.processor.held_span_count == 0


def test_evaluator_scope_holds_under_its_own_owner_without_experiment_attributes(
    pipeline: Pipeline,
) -> None:
    owner = "exp-1/evaluators/k1/0"
    with (
        experiment_scope("exp-1", "k1", 0, owner=owner, stamp=False),
        pipeline.tracer.start_as_current_span("judge") as root,
    ):
        pass
    trace_id = root.get_span_context().trace_id
    assert _context.held_traces(owner) == frozenset({trace_id})
    assert _context.held_traces("exp-1") == frozenset()
    _context.release_traces([trace_id])
    assert semconv.EXPERIMENT_ID not in attrs(pipeline.finished()["judge"])


def test_closed_owners_hold_nothing_new(pipeline: Pipeline) -> None:
    with experiment_scope("exp-1", "k1", 0):
        first = pipeline.tracer.start_span("evaluate")
    _context.discard_owner("exp-1")
    with (
        experiment_scope("exp-1", "k2", 0),  # an abandoned task starts a new trace
        pipeline.tracer.start_as_current_span("late-root"),
    ):
        pass
    first.end()
    assert names(pipeline.exporter) == []
    assert _context.held_traces() == frozenset()
    release_traces(None)  # shutdown
    assert names(pipeline.exporter) == []


def test_fork_child_forgets_held_spans(pipeline: Pipeline) -> None:
    with (
        experiment_scope("exp-1", "k1", 0),
        pipeline.tracer.start_as_current_span("evaluate"),
    ):
        pass
    assert pipeline.processor.held_span_count == 1
    pipeline.processor._reinit_after_fork()
    assert pipeline.processor.held_span_count == 0
