"""Quick start: report a two-turn chat of a weather assistant as one platform thread.

    export INSIGHTMON_BASE_URL=https://eval.example.com
    export INSIGHTMON_API_KEY=imk_...        # an API key with scope trace_ingest
    python examples/quickstart.py

Without the two variables nothing is sent (a dry run). The README's quick start is this file.
"""

from __future__ import annotations

import uuid

import insightmon


def forecast(city: str) -> str:
    """A tool call: its own span inside the turn."""
    with insightmon.get_tracer().start_as_current_span(
        "forecast",
        attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "forecast"},
    ):
        return f"Light rain is expected in {city} tomorrow."


def reply(question: str, *, last: bool = False) -> str:
    """One turn of the assistant (one trace on the platform); the last one closes the thread."""
    with insightmon.turn(end_thread=last):
        if "rain" in question.lower():
            return forecast("Oslo")
        return "You're welcome!"


def main() -> None:
    insightmon.init(
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=[{"type": "prompt", "name": "system", "version": "p7"}],
        language="en",
    )
    chat_id = f"chat-{uuid.uuid4().hex[:8]}"  # your own conversation id
    with insightmon.thread(chat_id, user_ref="user-42"):
        print(reply("Will it rain in Oslo tomorrow?"))
        print(reply("Thanks!", last=True))
    insightmon.shutdown()  # export what is left before the process exits
    print(f"reported thread {chat_id}")


if __name__ == "__main__":
    main()
