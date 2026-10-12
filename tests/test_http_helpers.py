"""`insightmon._http` helpers."""

from __future__ import annotations

import json
import math

import pytest

from insightmon._http import USER_AGENT, backoff_delay, encode_json, sanitize_json


def test_sanitize_json_replaces_non_finite_numbers() -> None:
    value = {"a": math.nan, "b": [math.inf, 1.5, (1, 2)], 3: {"c": -math.inf}, "d": {2, 1}}
    assert sanitize_json(value) == {
        "a": None,
        "b": [None, 1.5, [1, 2]],
        "3": {"c": None},
        "d": [1, 2],
    }
    with pytest.raises(TypeError):
        sanitize_json(object())


def test_encode_json_is_compact_utf8() -> None:
    body = encode_json({"text": "晴れ", "x": math.nan})
    assert body == '{"text":"晴れ","x":null}'.encode()
    assert json.loads(body) == {"text": "晴れ", "x": None}


def test_backoff_delay() -> None:
    assert backoff_delay(1, rng=lambda: 1.0) == 0.5
    assert backoff_delay(3, rng=lambda: 1.0) == 2.0
    assert backoff_delay(10, rng=lambda: 1.0) == 8.0
    assert backoff_delay(1, retry_after_s=3.0) == 3.0
    assert backoff_delay(1, retry_after_s=600.0) == 60.0


def test_user_agent() -> None:
    assert USER_AGENT.startswith("insightmon-eval-sdk/")
