"""The README code samples run.

- README.md and README.en.md have the same Python blocks, in the same order;
- the quick start block is examples/quickstart.py (from `import uuid` on);
- every Python block runs in-process (unless the line before it is `<!-- no-run -->`), with
  nothing leaving the process: `init()` gets in-memory providers, `Client()` talks to an
  `httpx.MockTransport`, `evaluate()` to the fake platform of tests/fake_platform.py, and the
  placeholders (`<dataset id>` ...) are filled in. Some blocks get extra checks (the thread key
  reaches the spans of the thread pool and asyncio samples, ...);
- the relative links of both READMEs point at files that exist.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import insightmon
from insightmon import _http, semconv
from insightmon._config import get_runtime
from insightmon.client import Client
from tests.conftest import API_KEY, BASE_URL, MockApi
from tests.fake_platform import EXP_ID, FakePlatform

evaluate_mod = importlib.import_module("insightmon.evaluate")

ROOT = Path(__file__).resolve().parents[1]
READMES = (ROOT / "README.md", ROOT / "README.en.md")
QUICKSTART = ROOT / "examples" / "quickstart.py"
NO_RUN = "<!-- no-run -->"

PLACEHOLDERS = {
    "<dataset id>": "ds-1",
    "<evaluator version id>": "ev-1",
    "<experiment id>": EXP_ID,
}


@dataclass(frozen=True)
class Block:
    index: int
    code: str
    run: bool

    @property
    def id(self) -> str:
        first = next(line for line in self.code.splitlines() if line.strip())
        return f"{self.index}: {first[:40]}"


def python_blocks(path: Path) -> list[Block]:
    text = path.read_text(encoding="utf-8")
    blocks: list[Block] = []
    for match in re.finditer(r"^```python\n(.*?)^```$", text, re.S | re.M):
        before = text[: match.start()].rstrip().splitlines()
        run = not (before and before[-1].strip() == NO_RUN)
        blocks.append(Block(len(blocks), match.group(1), run))
    return blocks


BLOCKS = python_blocks(READMES[0])


def test_both_readmes_have_the_same_python_blocks() -> None:
    zh, en = (python_blocks(path) for path in READMES)
    assert len(zh) >= 10
    assert [b.code for b in zh] == [b.code for b in en]
    assert [b.run for b in zh] == [b.run for b in en]


@pytest.mark.parametrize("block", BLOCKS, ids=lambda b: b.id)
def test_every_block_compiles(block: Block) -> None:
    compile(block.code, f"README block {block.index}", "exec")


def test_the_quick_start_is_the_example_file() -> None:
    example = QUICKSTART.read_text(encoding="utf-8")
    body = example[example.index("import uuid") :]
    assert body in [b.code for b in BLOCKS]


@pytest.mark.parametrize("path", READMES, ids=lambda p: p.name)
def test_relative_links_point_at_files(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text)
    local = [t for t in targets if not re.match(r"[a-z]+:", t)]
    assert local
    missing = [t for t in local if not (ROOT / t).exists()]
    assert missing == []


# --------------------------------------------------------------------------- running blocks


@dataclass
class Sandbox:
    spans: InMemorySpanExporter
    logs: Any
    api: MockApi
    platform: FakePlatform


def _scores_answer(request: httpx.Request) -> httpx.Response:
    scores = json.loads(request.content)["scores"]
    return httpx.Response(
        200,
        json={
            "items": [
                {
                    "index": i,
                    "id": f"score-{i}",
                    "created": True,
                    "idempotency_key": s["idempotency_key"],
                }
                for i, s in enumerate(scores)
            ]
        },
    )


@pytest.fixture
def sandbox(
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
    tracer_provider: TracerProvider,
    log_exporter: Any,
    logger_provider: LoggerProvider,
    mock_api: MockApi,
) -> Iterator[Sandbox]:
    real_init = insightmon.init

    def init(**kwargs: Any) -> insightmon.InsightMonHandle:
        kwargs.pop("base_url", None)
        kwargs.pop("api_key", None)
        kwargs.setdefault("tracer_provider", tracer_provider)
        kwargs.setdefault("logger_provider", logger_provider)
        return real_init(**kwargs)

    def client(*args: Any, **kwargs: Any) -> Client:
        return Client(BASE_URL, API_KEY, transport=mock_api.transport)

    platform = FakePlatform(["k1", "k2", "k3"], score_names=frozenset({"mentions_rain"}))
    platform.install(mock_api)
    mock_api.add("POST", "/api/v1/scores:batch", _scores_answer)

    async def no_async_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(insightmon, "init", init)
    monkeypatch.setattr(insightmon, "Client", client)
    monkeypatch.setattr(evaluate_mod, "_open_client", lambda c, k, u: client())
    monkeypatch.setattr(_http, "_sleep", lambda seconds: None)
    monkeypatch.setattr(evaluate_mod, "_sleep", lambda seconds: None)
    monkeypatch.setattr(evaluate_mod, "_async_sleep", no_async_sleep)
    yield Sandbox(span_exporter, log_exporter, mock_api, platform)
    insightmon.shutdown()


def _run(block: Block) -> dict[str, Any]:
    code = block.code
    for placeholder, value in PLACEHOLDERS.items():
        code = code.replace(f'"{placeholder}"', repr(value))
    if "insightmon.init(" not in code:
        insightmon.init()  # the samples that skip init() assume it ran before
    namespace: dict[str, Any] = {"__name__": "__main__"}
    exec(compile(code, f"README block {block.index}", "exec"), namespace)
    insightmon.flush()
    return namespace


def _keys_of(sandbox: Sandbox, name: str) -> set[Any]:
    return {
        (s.attributes or {}).get(semconv.GEN_AI_CONVERSATION_ID)
        for s in sandbox.spans.get_finished_spans()
        if s.name == name
    }


def _check_quickstart(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    spans = sandbox.spans.get_finished_spans()
    assert [s.name for s in spans].count("turn") == 2
    keys = {(s.attributes or {}).get(semconv.GEN_AI_CONVERSATION_ID) for s in spans}
    assert len(keys) == 1
    assert next(iter(keys)).startswith("chat-")
    assert any((s.attributes or {}).get(semconv.THREAD_END) for s in spans)


def _check_threads(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    turns = [s for s in sandbox.spans.get_finished_spans() if s.name == "turn"]
    assert len(turns) == 2
    first, second = turns
    assert (first.attributes or {})[semconv.RESPONSE_ID] == "resp-0001"
    assert (first.attributes or {})[semconv.component_attribute("prompt", "system")] == "p8"
    assert (second.attributes or {})[semconv.THREAD_END] is True


def _check_asyncio(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    assert _keys_of(sandbox, "lookup") == {"chat-43"}
    assert len([s for s in sandbox.spans.get_finished_spans() if s.name == "lookup"]) == 2


def _check_thread_pool(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    assert namespace["forecasts"] == ["forecast for Oslo", "forecast for Lima"]
    assert _keys_of(sandbox, "lookup") == {"chat-44"}  # the copied context reached the workers
    lookups = [s for s in sandbox.spans.get_finished_spans() if s.name == "lookup"]
    (turn,) = [s for s in sandbox.spans.get_finished_spans() if s.name == "turn"]
    assert len(lookups) == 2
    assert all(s.parent is not None and s.parent.span_id == turn.context.span_id for s in lookups)


def _check_post_fork(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    insightmon.shutdown()
    namespace["post_fork"](None, None)
    with insightmon.turn() as span:
        pass
    assert (span.attributes or {})[semconv.AGENT_VERSION] == "1.4.0"


def _check_content(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    (turn,) = sandbox.spans.get_finished_spans()
    assert "Will it rain in Oslo" in str((turn.attributes or {})[semconv.INPUT_MESSAGES])


def _check_log_evaluation(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    records = sandbox.logs.get_finished_logs()
    names = [r.log_record.attributes[semconv.EVALUATION_NAME] for r in records]
    assert names == ["helpfulness", "mentions_city"]


def _check_scores(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    (body,) = sandbox.api.json_bodies("POST", "/api/v1/scores:batch")
    assert [s["source"] for s in body["scores"]] == ["end_user", "outcome"]


def _check_evaluate(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    result = namespace["result"]
    assert result.status == "completed"
    assert len(result.items) == 6  # 3 examples x 2 trials
    assert all(item.status == "ok" for item in result.items)
    assert {s["name"] for item in result.items for s in item.scores} == {"mentions_rain"}
    assert sandbox.platform.create_body is not None
    assert sandbox.platform.create_body["dataset_id"] == "ds-1"


def _check_aevaluate(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    assert namespace["result"].status == "completed"
    assert len(namespace["result"].items) == 3


def _check_finalize(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    assert sandbox.platform.finalized


def _check_middleware(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    assert isinstance(namespace["app"], insightmon.InsightMonASGIMiddleware)


def _check_flush_when(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    assert isinstance(namespace["app"], insightmon.InsightMonASGIMiddleware)
    is_replay = namespace["is_replay"]
    assert is_replay({"headers": [(b"x-replay", b"1")]}) is True
    assert is_replay({"headers": [(b"traceparent", b"00-ab-cd-01")]}) is False


def _check_collector(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    state = get_runtime()
    assert state is not None
    (processor,) = state.span_processors
    # The collector processor sits behind the SDK's processor, so evaluate() spans are held.
    assert isinstance(processor, insightmon.InsightMonSpanProcessor)
    assert isinstance(processor._export, BatchSpanProcessor)


def _check_content_attributes(sandbox: Sandbox, namespace: dict[str, Any]) -> None:
    state = get_runtime()
    assert state is not None
    assert state.settings.content_attributes == ("notes.body", "search.results.*")


#: A marker found only in one block -> the extra checks for that block.
CHECKS: dict[str, Callable[[Sandbox, dict[str, Any]], None]] = {
    "def reply(question": _check_quickstart,
    'set_response_id("resp-0001")': _check_threads,
    "asyncio.gather": _check_asyncio,
    "contextvars.copy_context()": _check_thread_pool,
    "def post_fork": _check_post_fork,
    "insightmon.record_io(": _check_content,
    "insightmon.log_evaluation(": _check_log_evaluation,
    "client.scores_batch(": _check_scores,
    "result = insightmon.evaluate(": _check_evaluate,
    "insightmon.aevaluate(": _check_aevaluate,
    "client.finalize_experiment(": _check_finalize,
    "InsightMonASGIMiddleware(app)": _check_middleware,
    "flush_when=is_replay": _check_flush_when,
    "export_processor=BatchSpanProcessor(": _check_collector,
    "content_attributes=[": _check_content_attributes,
}


def test_every_check_matches_one_block() -> None:
    for marker in CHECKS:
        assert len([b for b in BLOCKS if marker in b.code]) == 1, marker


@pytest.mark.parametrize("block", [b for b in BLOCKS if b.run], ids=lambda b: b.id)
def test_every_block_runs(block: Block, sandbox: Sandbox) -> None:
    namespace = _run(block)
    for marker, check in CHECKS.items():
        if marker in block.code:
            check(sandbox, namespace)
