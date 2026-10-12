"""Run a weather assistant on a platform dataset with `insightmon.evaluate()`.

    export INSIGHTMON_BASE_URL=https://eval.example.com
    export INSIGHTMON_API_KEY=imk_...        # scopes experiment_write (+ trace_ingest for spans)
    python examples/evaluate_example.py <dataset id> [<server evaluator version id> ...]

The task runs here, once per (example, trial); the platform runs the server evaluators (the
gating checks) and gives the verdict. The client evaluator `mentions_rain` is diagnostic only,
and its name must be a published score config of the project (otherwise the platform refuses
the score: the item is reported without it and the SDK logs a warning).
"""

from __future__ import annotations

import sys
from typing import Any

import insightmon


def question_of(input: Any) -> str:
    """The last user message of `{"messages": [...]}`, or the input as text."""
    if isinstance(input, dict):
        for message in reversed(input.get("messages") or []):
            if isinstance(message, dict) and message.get("role", "user") == "user":
                return str(message.get("content"))
    return str(input)


def weather_assistant(input: Any) -> str:
    question = question_of(input)
    with insightmon.get_tracer().start_as_current_span(
        "forecast",
        attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "forecast"},
    ):
        city = "Oslo" if "oslo" in question.lower() else "your city"
    return f"Light rain is expected in {city} tomorrow."


def mentions_rain(input: Any, output: Any, reference: Any) -> dict[str, Any]:
    return {"name": "mentions_rain", "value": 1.0 if "rain" in output["text"].lower() else 0.0}


def main(dataset_id: str, server_evaluators: list[str]) -> insightmon.ExperimentResult:
    insightmon.init(agent_name="weather-assistant", agent_version="1.4.0")
    result = insightmon.evaluate(
        weather_assistant,
        dataset=dataset_id,
        evaluators=[mentions_rain],
        server_evaluators=server_evaluators,
        trials=1,
        max_concurrency=4,
    )
    print(f"{result.status}: pass rate {result.pass_rate}, {len(result.items)} items")
    print(result.url)
    for item in result.items:
        print(f"  {item.example_key} #{item.trial_index}: {item.status} {item.scores}")
    insightmon.shutdown()
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: evaluate_example.py <dataset id> [<server evaluator version id> ...]")
    main(sys.argv[1], sys.argv[2:])
