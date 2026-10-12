"""InsightMon AI Eval SDK.

Report agent sessions to the InsightMon platform over OTLP (traces and evaluation logs), and run
client-side experiments with `evaluate()`.

    import insightmon

    insightmon.init(agent_name="weather-assistant", agent_version="1.4.0")
    with insightmon.thread("chat-42"), insightmon.turn():
        ...

The platform contract the SDK follows is documented in docs/contract.md.
"""

from __future__ import annotations

from insightmon import semconv
from insightmon._config import Component, Settings
from insightmon._context import ThreadScope
from insightmon._errors import InsightMonError
from insightmon._export import Rejections, rejections
from insightmon._setup import InsightMonHandle, flush, init, shutdown
from insightmon._version import __version__
from insightmon.client import ApiKeyInfo, Client, Example, ExamplesPage, ReportResult
from insightmon.content import record_io
from insightmon.evaluate import ExperimentResult, ItemResult, aevaluate, evaluate
from insightmon.logs import log_evaluation
from insightmon.processor import InsightMonSpanProcessor
from insightmon.propagation import (
    InsightMonASGIMiddleware,
    InsightMonWSGIMiddleware,
    extract_context,
    receipt,
)
from insightmon.tracing import (
    current_thread,
    end_thread,
    get_tracer,
    mark_eval_root,
    set_component,
    set_response_id,
    thread,
    turn,
)

__all__ = [
    "ApiKeyInfo",
    "Client",
    "Component",
    "Example",
    "ExamplesPage",
    "ExperimentResult",
    "InsightMonASGIMiddleware",
    "InsightMonError",
    "InsightMonHandle",
    "InsightMonSpanProcessor",
    "InsightMonWSGIMiddleware",
    "ItemResult",
    "Rejections",
    "ReportResult",
    "Settings",
    "ThreadScope",
    "__version__",
    "aevaluate",
    "current_thread",
    "end_thread",
    "evaluate",
    "extract_context",
    "flush",
    "get_tracer",
    "init",
    "log_evaluation",
    "mark_eval_root",
    "receipt",
    "record_io",
    "rejections",
    "semconv",
    "set_component",
    "set_response_id",
    "shutdown",
    "thread",
    "turn",
]
