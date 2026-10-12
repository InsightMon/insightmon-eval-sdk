"""init() / flush() / shutdown(): attaching vs creating providers, export wiring, flush order."""

from __future__ import annotations

import logging
import os
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from typing import Any, ClassVar

import pytest
from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    ConcurrentMultiSpanProcessor,
    ReadableSpan,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

import insightmon
from insightmon import _config, _context, _export, _setup, semconv
from insightmon._errors import InsightMonError
from insightmon.logs import EventFilterProcessor
from insightmon.processor import InsightMonSpanProcessor

from .collector import Collector
from .conftest import API_KEY, BASE_URL

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fresh_global(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A process without a global tracer provider; records `set_tracer_provider` calls."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.delenv("OTEL_PYTHON_TRACER_PROVIDER", raising=False)
    calls: list[Any] = []
    real = trace.set_tracer_provider

    def spy(provider: Any) -> None:
        calls.append(provider)
        real(provider)

    monkeypatch.setattr(trace, "set_tracer_provider", spy)
    return calls


class FakeOTLP:
    """Stands in for the platform exporters: records its arguments and the batches."""

    instances: ClassVar[list[FakeOTLP]] = []

    def __init__(self, endpoint: str, api_key: str, **kwargs: Any) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.kwargs = kwargs
        self.batches: list[list[Any]] = []
        self.shut_down = False
        FakeOTLP.instances.append(self)

    def export(self, batch: Sequence[Any]) -> Any:
        self.batches.append(list(batch))
        if "traces" in self.endpoint:
            EXPORT_ORDER.append("traces")
            return SpanExportResult.SUCCESS
        EXPORT_ORDER.append("logs")
        return _export.LogResult.SUCCESS

    def shutdown(self) -> None:
        self.shut_down = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    @property
    def items(self) -> list[Any]:
        return [item for batch in self.batches for item in batch]


EXPORT_ORDER: list[str] = []


@pytest.fixture
def fake_otlp(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, FakeOTLP]]:
    FakeOTLP.instances = []
    EXPORT_ORDER.clear()
    monkeypatch.setattr(_export, "PlatformSpanExporter", FakeOTLP)
    monkeypatch.setattr(_export, "PlatformLogExporter", FakeOTLP)

    class Lookup(dict[str, FakeOTLP]):
        def __missing__(self, kind: str) -> FakeOTLP:
            for instance in FakeOTLP.instances:
                if kind in instance.endpoint:
                    return instance
            raise KeyError(kind)

    yield Lookup()
    insightmon.shutdown()


# --------------------------------------------------------------------------- providers


def test_init_attaches_to_a_given_provider(
    tracer_provider: TracerProvider, span_exporter: InMemorySpanExporter, fresh_global: list[Any]
) -> None:
    handle = insightmon.init(tracer_provider=tracer_provider, agent_name="notes-assistant")
    state = _config.get_runtime()
    assert state is not None
    assert handle.tracer_provider is tracer_provider
    assert state.owns_tracer_provider is False
    assert handle.logger_provider is None  # export disabled and none given
    assert fresh_global == []
    insightmon.shutdown()
    assert _config.get_runtime() is None
    # The application's own pipeline still works after the SDK shut down.
    with tracer_provider.get_tracer("app").start_as_current_span("after"):
        pass
    (span,) = span_exporter.get_finished_spans()
    assert semconv.AGENT_NAME not in (span.attributes or {})


def test_init_attaches_to_the_global_sdk_provider(
    fresh_global: list[Any], span_exporter: InMemorySpanExporter
) -> None:
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    fresh_global.clear()
    handle = insightmon.init(agent_name="notes-assistant")
    assert handle.tracer_provider is provider
    assert fresh_global == []  # never set twice
    assert _config.get_runtime().owns_tracer_provider is False  # type: ignore[union-attr]
    insightmon.shutdown()


def test_init_creates_and_sets_the_global_provider_once(fresh_global: list[Any]) -> None:
    handle = insightmon.init(agent_name="weather-assistant", environment="staging")
    provider = handle.tracer_provider
    assert isinstance(provider, TracerProvider)
    assert fresh_global == [provider]
    assert trace.get_tracer_provider() is provider
    resource = provider.resource.attributes
    assert resource[semconv.SERVICE_NAME] == "weather-assistant"
    assert resource[semconv.DEPLOYMENT_ENVIRONMENT_NAME] == "staging"
    assert semconv.SERVICE_VERSION not in resource
    insightmon.shutdown()
    again = insightmon.init(agent_name="weather-assistant")
    assert again.tracer_provider is provider  # attached to the provider it created before
    assert fresh_global == [provider]
    assert _config.get_runtime().owns_tracer_provider is False  # type: ignore[union-attr]
    insightmon.shutdown()


def test_service_name_wins_over_agent_name(fresh_global: list[Any]) -> None:
    handle = insightmon.init(service_name="notes-api", agent_name="notes-assistant")
    assert handle.tracer_provider is not None
    assert handle.tracer_provider.resource.attributes[semconv.SERVICE_NAME] == "notes-api"
    insightmon.shutdown()


def test_set_global_false_keeps_the_provider_private(
    fresh_global: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = insightmon.init(set_global=False)
    provider = handle.tracer_provider
    assert provider is not None
    assert fresh_global == []
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
    with insightmon.get_tracer().start_as_current_span("private") as span:
        assert span.is_recording()
    shut: list[bool] = []
    monkeypatch.setattr(provider, "shutdown", lambda: shut.append(True))
    insightmon.shutdown()
    assert shut == [True]  # a private provider the SDK created is shut down


def test_non_sdk_global_provider_is_left_alone(
    fresh_global: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    foreign = trace.NoOpTracerProvider()
    trace.set_tracer_provider(foreign)
    fresh_global.clear()
    with caplog.at_level(logging.WARNING, logger="insightmon._setup"):
        handle = insightmon.init()
    assert isinstance(handle.tracer_provider, TracerProvider)
    assert trace.get_tracer_provider() is foreign
    assert fresh_global == []
    assert any("not an OpenTelemetry SDK" in r.getMessage() for r in caplog.records)
    insightmon.shutdown()


def test_second_init_warns_and_keeps_the_first(tracer_provider: TracerProvider) -> None:
    first = insightmon.init(tracer_provider=tracer_provider, agent_version="1")
    with pytest.warns(RuntimeWarning, match="already called"):
        second = insightmon.init(agent_version="2")
    assert second is first
    assert _config.get_settings().agent_version == "1"  # type: ignore[union-attr]
    insightmon.shutdown()


def test_invalid_arguments_set_nothing_up(tracer_provider: TracerProvider) -> None:
    with pytest.raises(InsightMonError) as info:
        insightmon.init(base_url=BASE_URL, tracer_provider=tracer_provider)
    assert info.value.code == "config_error"
    with pytest.raises(ValueError, match="language"):
        insightmon.init(language="fr", tracer_provider=tracer_provider)
    with pytest.raises(TypeError, match="TracerProvider"):
        insightmon.init(tracer_provider=object())  # type: ignore[arg-type]
    assert _config.get_runtime() is None


def test_handle_is_a_context_manager(tracer_provider: TracerProvider) -> None:
    with insightmon.init(tracer_provider=tracer_provider) as handle:
        assert _config.get_runtime() is not None
        assert "imk_" not in repr(handle)
    assert _config.get_runtime() is None
    insightmon.shutdown()  # idempotent


def test_flush_before_init_is_trivially_true() -> None:
    assert insightmon.flush() is True
    insightmon.shutdown()


def test_atexit_is_registered_once(
    tracer_provider: TracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[Any] = []
    monkeypatch.setattr(_setup, "_atexit_registered", False)
    monkeypatch.setattr(_setup.atexit, "register", registered.append)
    insightmon.init(tracer_provider=tracer_provider)
    insightmon.shutdown()
    insightmon.init(tracer_provider=tracer_provider)
    assert registered == [_setup._shutdown_at_exit]
    _setup._shutdown_at_exit()
    assert _config.get_runtime() is None


# --------------------------------------------------------------------------- export wiring


def test_export_pipeline_wiring(
    fake_otlp: dict[str, FakeOTLP], tracer_provider: TracerProvider
) -> None:
    handle = insightmon.init(
        base_url=BASE_URL + "/", api_key=API_KEY, tracer_provider=tracer_provider
    )
    traces, logs = fake_otlp["traces"], fake_otlp["logs"]
    assert traces.endpoint == f"{BASE_URL}/otlp/v1/traces"
    assert logs.endpoint == f"{BASE_URL}/otlp/v1/logs"
    state = _config.get_runtime()
    assert state is not None
    for exporter in (traces, logs):
        assert exporter.api_key == API_KEY
        assert exporter.kwargs["control"] is state.export_control  # one control per init()
    (span_processor,) = state.span_processors
    assert isinstance(span_processor, InsightMonSpanProcessor)
    assert isinstance(handle.logger_provider, LoggerProvider)
    assert state.owns_logger_provider is True
    (log_processor,) = state.log_processors
    assert isinstance(log_processor, EventFilterProcessor)
    assert log_processor.allowed == {semconv.EVALUATION_RESULT_EVENT}
    insightmon.shutdown()
    assert traces.shut_down
    assert logs.shut_down


def test_environment_variables_enable_export(
    fake_otlp: dict[str, FakeOTLP], monkeypatch: pytest.MonkeyPatch, tracer_provider: TracerProvider
) -> None:
    monkeypatch.setenv("INSIGHTMON_BASE_URL", BASE_URL)
    monkeypatch.setenv("INSIGHTMON_API_KEY", API_KEY)
    insightmon.init(tracer_provider=tracer_provider)
    assert fake_otlp["traces"].endpoint == f"{BASE_URL}/otlp/v1/traces"


def test_capture_content_forwards_content_events(
    fake_otlp: dict[str, FakeOTLP], tracer_provider: TracerProvider
) -> None:
    insightmon.init(
        base_url=BASE_URL, api_key=API_KEY, tracer_provider=tracer_provider, capture_content=True
    )
    state = _config.get_runtime()
    assert state is not None
    assert state.log_processors[0].allowed == {
        semconv.EVALUATION_RESULT_EVENT,
        *semconv.CONTENT_EVENTS,
    }


def test_given_logger_provider_gets_the_filter(
    fake_otlp: dict[str, FakeOTLP],
    tracer_provider: TracerProvider,
    logger_provider: LoggerProvider,
    log_exporter: Any,
) -> None:
    handle = insightmon.init(
        base_url=BASE_URL,
        api_key=API_KEY,
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
    )
    assert handle.logger_provider is logger_provider
    app_logger = logger_provider.get_logger("app")
    from insightmon.logs import emit_event

    emit_event(app_logger, trace.set_span_in_context(trace.INVALID_SPAN), "app.audit", {})
    with insightmon.turn():
        insightmon.log_evaluation("helpfulness", value=1)
    insightmon.flush()
    assert len(log_exporter.get_finished_logs()) == 2  # the application's own pipeline
    exported = fake_otlp["logs"].items
    assert [item.log_record.event_name for item in exported] == ["gen_ai.evaluation.result"]
    insightmon.shutdown()
    # a provider the application owns is not shut down
    emit_event(app_logger, trace.set_span_in_context(trace.INVALID_SPAN), "app.audit", {})
    assert len(log_exporter.get_finished_logs()) == 3


def test_flush_exports_traces_before_logs(fake_otlp: dict[str, FakeOTLP]) -> None:
    insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False)
    with insightmon.thread("chat-1"), insightmon.turn():
        insightmon.log_evaluation("helpfulness", value=1)
    assert EXPORT_ORDER == []  # both batch processors are still waiting
    assert insightmon.flush() is True
    assert EXPORT_ORDER == ["traces", "logs"]
    (record,) = fake_otlp["logs"].items
    (span,) = fake_otlp["traces"].items
    assert record.log_record.span_id == span.context.span_id


def test_content_is_stripped_when_capture_is_off(fake_otlp: dict[str, FakeOTLP]) -> None:
    insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False)
    tracer = insightmon.get_tracer()
    with tracer.start_as_current_span(
        "chat model",
        attributes={
            semconv.INPUT_MESSAGES: '[{"role":"user","content":"hi"}]',
            "llm.input_messages.0.message.content": "hi",
            "input.value": "hi",
            semconv.REQUEST_MODEL: "small-model",
        },
    ) as span:
        assert insightmon.record_io(output_messages="ho") is False
        span.add_event(semconv.DETAILS_EVENT, {semconv.OUTPUT_MESSAGES: "[]"})
    insightmon.flush()
    (exported,) = fake_otlp["traces"].items
    attributes = dict(exported.attributes or {})
    assert semconv.REQUEST_MODEL in attributes
    assert not any(semconv.is_content_attribute(key) for key in attributes)
    assert exported.events == ()


def test_content_is_kept_when_capture_is_on(fake_otlp: dict[str, FakeOTLP]) -> None:
    insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False, capture_content=True)
    with insightmon.turn():
        assert insightmon.record_io(input_messages="hi", output_messages="ho") is True
    insightmon.flush()
    (exported,) = fake_otlp["traces"].items
    assert semconv.INPUT_MESSAGES in (exported.attributes or {})


def test_shutdown_releases_held_spans_and_flushes(fake_otlp: dict[str, FakeOTLP]) -> None:
    insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False)
    with (
        _context.experiment_scope("exp-1", "k1", 0),
        insightmon.get_tracer().start_as_current_span("evaluate") as root,
    ):
        pass
    assert _context.is_held(root.get_span_context().trace_id)
    insightmon.flush()
    assert fake_otlp["traces"].items == []  # held spans stay held on flush
    traces = fake_otlp["traces"]
    insightmon.shutdown()
    assert [span.name for span in traces.items] == ["evaluate"]
    assert _context.held_traces() == frozenset()


def test_flush_reports_failures(
    tracer_provider: TracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    insightmon.init(tracer_provider=tracer_provider)
    state = _config.get_runtime()
    assert state is not None

    class Failing:
        def force_flush(self, timeout_millis: int = 30000) -> bool:
            raise RuntimeError("boom")

        def shutdown(self) -> None:
            raise RuntimeError("boom")

    state.log_processors.append(Failing())
    assert insightmon.flush(0.5) is False
    insightmon.shutdown()  # a failing processor does not stop the shutdown
    assert _config.get_runtime() is None


def test_resource_of_a_created_provider_is_shared_with_logs(fake_otlp: dict[str, FakeOTLP]) -> None:
    handle = insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False, agent_name="x")
    assert handle.tracer_provider is not None
    assert handle.logger_provider is not None
    assert handle.logger_provider.resource == handle.tracer_provider.resource
    assert isinstance(handle.tracer_provider.resource, Resource)


def test_exported_spans_are_readable(fake_otlp: dict[str, FakeOTLP]) -> None:
    insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False)
    with insightmon.turn():
        pass
    insightmon.flush()
    assert all(isinstance(span, ReadableSpan) for span in fake_otlp["traces"].items)


# --------------------------------------------------------------------------- deadlines


def test_flush_returns_false_when_exports_fail(collector: Collector) -> None:
    collector.statuses["/otlp/v1/traces"] = 400  # refused: OpenTelemetry would report success
    insightmon.init(base_url=collector.base_url, api_key=API_KEY, set_global=False)
    with insightmon.turn():
        pass
    assert insightmon.flush(5.0) is False
    with insightmon.turn():
        pass
    collector.statuses.clear()
    assert insightmon.flush(5.0) is True  # only failures during this flush count


def test_flush_and_shutdown_keep_their_deadline_when_the_platform_hangs(
    collector: Collector, caplog: pytest.LogCaptureFixture
) -> None:
    collector.hang = threading.Event()
    insightmon.init(base_url=collector.base_url, api_key=API_KEY, set_global=False)
    for _ in range(3):
        with insightmon.turn():
            pass
    started = time.monotonic()
    assert insightmon.flush(0.5) is False
    assert time.monotonic() - started < 1.5
    with insightmon.turn():
        pass
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="insightmon._setup"):
        insightmon.shutdown(0.5)
    assert time.monotonic() - started < 0.5 + _setup.SHUTDOWN_GRACE_S + 1.0
    assert _config.get_runtime() is None


class BlockingProcessor(SpanProcessor):
    """An export processor of the application whose flush and shutdown never return on
    their own (until `release` is set)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.flushes = 0
        self.ended: list[ReadableSpan] = []

    def on_end(self, span: ReadableSpan) -> None:
        self.ended.append(span)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushes += 1
        self.release.wait(30)
        return True

    def shutdown(self) -> None:
        self.release.wait(30)


@pytest.fixture
def blocking() -> Iterator[BlockingProcessor]:
    processor = BlockingProcessor()
    yield processor
    processor.release.set()


def test_flush_and_shutdown_never_wait_past_the_deadline(
    tracer_provider: TracerProvider, blocking: BlockingProcessor
) -> None:
    insightmon.init(tracer_provider=tracer_provider, export_processor=blocking)
    started = time.monotonic()
    assert insightmon.flush(0.3) is False
    assert time.monotonic() - started < 1.0
    started = time.monotonic()
    insightmon.shutdown(0.3)
    assert time.monotonic() - started < 1.0
    assert _config.get_runtime() is None


def test_concurrent_flushes_share_one_run(
    tracer_provider: TracerProvider, blocking: BlockingProcessor
) -> None:
    insightmon.init(tracer_provider=tracer_provider, export_processor=blocking)
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(insightmon.flush(0.3))) for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results == [False] * 5
    assert blocking.flushes == 1  # the others joined the running flush
    blocking.release.set()
    assert insightmon.flush(2.0) is True  # a new flush once the old one is done


def test_shutdown_never_blocks_span_starts(
    tracer_provider: TracerProvider, blocking: BlockingProcessor
) -> None:
    insightmon.init(
        tracer_provider=tracer_provider,
        export_processor=blocking,
        components=[{"type": "prompt", "name": "system", "version": "v1"}],
    )
    stopper = threading.Thread(target=lambda: insightmon.shutdown(2.0))
    stopper.start()
    time.sleep(0.2)  # shutdown is now waiting for the processor
    started = time.monotonic()
    with tracer_provider.get_tracer("app").start_as_current_span("request"):
        pass
    assert time.monotonic() - started < 0.2
    blocking.release.set()
    stopper.join(5)


def test_global_components_are_read_without_the_lock() -> None:
    insightmon.set_component("prompt", "system", version="v3")
    taken = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with _config.runtime_lock():
            taken.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    taken.wait(5)
    try:
        started = time.monotonic()
        (component,) = _config.global_components()
        assert time.monotonic() - started < 0.1
        assert component.version == "v3"
    finally:
        release.set()
        holder.join(5)


def test_atexit_shutdown_has_a_short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[float] = []
    monkeypatch.setattr(_setup, "shutdown", budgets.append)
    _setup._shutdown_at_exit()
    assert budgets == [_setup.ATEXIT_TIMEOUT_S]
    assert _setup.ATEXIT_TIMEOUT_S <= 5.0


# --------------------------------------------------------------------------- export processors


def test_export_processor_holds_experiment_spans_in_collector_mode(
    tracer_provider: TracerProvider,
) -> None:
    collected = InMemorySpanExporter()
    insightmon.init(
        tracer_provider=tracer_provider, export_processor=SimpleSpanProcessor(collected)
    )
    with (
        _context.experiment_scope("exp-1", "k1", 0),
        insightmon.get_tracer().start_as_current_span("evaluate") as root,
    ):
        pass
    with insightmon.turn():
        pass
    assert [s.name for s in collected.get_finished_spans()] == ["turn"]
    _context.release_traces([root.get_span_context().trace_id])
    assert [s.name for s in collected.get_finished_spans()] == ["turn", "evaluate"]
    insightmon.shutdown()


def test_export_processor_next_to_platform_export(
    fake_otlp: dict[str, FakeOTLP], tracer_provider: TracerProvider
) -> None:
    collected = InMemorySpanExporter()
    insightmon.init(
        base_url=BASE_URL,
        api_key=API_KEY,
        tracer_provider=tracer_provider,
        export_processor=SimpleSpanProcessor(collected),
    )
    with insightmon.turn():
        pass
    assert insightmon.flush() is True
    assert [s.name for s in collected.get_finished_spans()] == ["turn"]
    assert [s.name for s in fake_otlp["traces"].items] == ["turn"]


def test_export_processor_must_be_a_span_processor() -> None:
    with pytest.raises(TypeError, match="SpanProcessor"):
        insightmon.init(export_processor=object())  # type: ignore[arg-type]
    assert _config.get_runtime() is None


def test_concurrent_span_processor_warns(caplog: pytest.LogCaptureFixture) -> None:
    provider = TracerProvider(active_span_processor=ConcurrentMultiSpanProcessor())
    with caplog.at_level(logging.WARNING, logger="insightmon._setup"):
        insightmon.init(tracer_provider=provider)
    assert any("ConcurrentMultiSpanProcessor" in r.getMessage() for r in caplog.records)
    insightmon.shutdown()
    provider.shutdown()


def test_releasing_more_spans_than_a_default_queue_loses_none(
    fake_otlp: dict[str, FakeOTLP],
) -> None:
    insightmon.init(base_url=BASE_URL, api_key=API_KEY, set_global=False)
    tracer = insightmon.get_tracer()
    roots = []
    for item in range(50):
        with (
            _context.experiment_scope("exp-1", f"k{item}", 0),
            tracer.start_as_current_span("evaluate") as root,
        ):
            for step in range(59):
                with tracer.start_as_current_span(f"step-{step}"):
                    pass
        roots.append(root.get_span_context().trace_id)
    _context.release_traces(roots)  # 3000 spans at once, more than 2048
    assert insightmon.flush(30.0) is True
    assert len(fake_otlp["traces"].items) == 3000


# --------------------------------------------------------------------------- fork


def _fork_and_check(check: Callable[[], bool]) -> int:
    """Exit status of a forked child that runs `check` (0 when it returns True in time)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads (3.12+)
        pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        try:
            code = 0 if check() else 1
        except BaseException:
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork()")
def test_forked_children_get_fresh_locks_and_no_held_spans(tracer_provider: TracerProvider) -> None:
    from insightmon import logs

    insightmon.init(
        tracer_provider=tracer_provider,
        export_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    state = _config.get_runtime()
    assert state is not None
    (span_processor,) = state.span_processors
    with (
        _context.experiment_scope("exp-parent", "k1", 0),
        insightmon.get_tracer().start_as_current_span("evaluate"),
    ):
        pass
    assert span_processor.held_span_count == 1
    locks = {
        "context": lambda: _context._held_lock,
        "config": lambda: _config._lock,
        "export": lambda: _export._rejections_lock,
        "logs": lambda: logs._warned_lock,
        "processor": lambda: span_processor._lock,
    }
    for name, current in locks.items():
        taken = threading.Event()
        release = threading.Event()

        def hold(
            current: Callable[[], Any] = current,
            taken: threading.Event = taken,
            release: threading.Event = release,
        ) -> None:
            with current():
                taken.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        taken.wait(5)

        def check(current: Callable[[], Any] = current) -> bool:
            lock = current()
            if not lock.acquire(timeout=2):
                return False
            lock.release()
            return _context.held_traces() == frozenset() and span_processor.held_span_count == 0

        try:
            code = _fork_and_check(check)
        finally:
            release.set()
            holder.join(5)
        assert code == 0, f"{name} lock still held in the child"
    assert len(_context.held_traces()) == 1  # the parent keeps its state
    assert span_processor.held_span_count == 1
