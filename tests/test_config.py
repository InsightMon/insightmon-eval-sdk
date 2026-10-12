from __future__ import annotations

import pytest

from insightmon import _config
from insightmon._config import (
    Component,
    RuntimeState,
    coerce_component,
    global_components,
    merge_components,
    normalize_base_url,
    normalize_language,
    resolve_settings,
    set_global_component,
    validate_api_key,
)
from insightmon._errors import InsightMonError

from .conftest import API_KEY, BASE_URL


def test_defaults_without_environment() -> None:
    settings = resolve_settings(env={})
    assert settings.base_url is None
    assert settings.api_key is None
    assert not settings.export_enabled
    assert settings.capture_content is False
    assert settings.components == ()


def test_environment_fallback_and_argument_precedence() -> None:
    env = {
        "INSIGHTMON_BASE_URL": "https://from-env.test/",
        "INSIGHTMON_API_KEY": "imk_env",
        "OTEL_SERVICE_NAME": "notes-service",
    }
    settings = resolve_settings(env=env)
    assert settings.base_url == "https://from-env.test"
    assert settings.api_key == "imk_env"
    assert settings.service_name == "notes-service"
    assert settings.export_enabled
    override = resolve_settings(base_url=BASE_URL, api_key=API_KEY, env=env)
    assert (override.base_url, override.api_key) == (BASE_URL, API_KEY)


def test_base_url_and_key_go_together() -> None:
    with pytest.raises(InsightMonError) as info:
        resolve_settings(base_url=BASE_URL, env={})
    assert info.value.code == "config_error"
    assert "INSIGHTMON_API_KEY" in info.value.message


def test_api_key_is_validated_and_hidden() -> None:
    with pytest.raises(InsightMonError, match="imk_"):
        validate_api_key("sk_live")
    with pytest.raises(InsightMonError, match="whitespace"):
        validate_api_key("imk_a b")
    settings = resolve_settings(base_url=BASE_URL, api_key=API_KEY, env={})
    assert API_KEY not in repr(settings)
    assert settings.auth_headers() == {"Authorization": f"Bearer {API_KEY}"}
    assert settings.endpoint(_config.OTLP_TRACES_PATH) == f"{BASE_URL}/otlp/v1/traces"


@pytest.mark.parametrize("url", ["eval.example.test", "ftp://x.test", "https://x.test/?a=1"])
def test_bad_base_url(url: str) -> None:
    with pytest.raises(InsightMonError):
        normalize_base_url(url)


def test_base_url_keeps_a_path_prefix() -> None:
    assert normalize_base_url(" https://x.test/eval/ ") == "https://x.test/eval"


def test_language() -> None:
    assert normalize_language(" ZH ") == "zh"
    assert normalize_language(None) is None
    with pytest.raises(ValueError, match="language"):
        normalize_language("fr")


def test_component_validation_and_shapes() -> None:
    component = Component("prompt", "system", version="v3", content_hash="sha256:ab")
    assert component.attributes() == {
        "insightmon.component.prompt.system.version": "v3",
        "insightmon.component.prompt.system.content_hash": "sha256:ab",
    }
    assert component.as_ref() == {"type": "prompt", "name": "system", "version": "v3"}
    assert component.as_receipt()["content_hash"] == "sha256:ab"
    for bad, message in (
        ({"type": "a.b", "name": "x"}, "type"),
        ({"type": "", "name": "x"}, "type"),
        ({"type": "t" * 65, "name": "x"}, "type"),
        ({"type": "tool", "name": ""}, "name"),
        ({"type": "tool", "name": "x", "version": "v" * 201}, "version"),
    ):
        with pytest.raises(ValueError, match=message):
            coerce_component(bad)
    with pytest.raises(ValueError, match="unknown"):
        coerce_component({"type": "tool", "name": "x", "owner": "me"})


def test_merge_components_later_wins_sorted() -> None:
    a = Component("tool", "geo", version="1")
    b = Component("prompt", "system", version="1")
    c = Component("tool", "geo", version="2")
    assert merge_components([a, b], [c]) == (b, c)


def test_requested_and_receipt() -> None:
    settings = resolve_settings(
        agent_name="weather-assistant",
        agent_version="1.4.0",
        components=[{"type": "tool", "name": "geo", "version": "2", "content_hash": "h"}],
        env={},
    )
    assert settings.requested() == {
        "agent_version": "1.4.0",
        "components": [{"type": "tool", "name": "geo", "version": "2"}],
    }
    assert settings.receipt() == {
        "agent": {"name": "weather-assistant", "version": "1.4.0"},
        "components": [{"type": "tool", "name": "geo", "version": "2", "content_hash": "h"}],
    }


def test_field_limits() -> None:
    with pytest.raises(ValueError, match="agent_version"):
        resolve_settings(agent_version="v" * 201, env={})
    with pytest.raises(ValueError, match="environment"):
        resolve_settings(environment="e" * 101, env={})


def test_runtime_state_and_global_components() -> None:
    assert _config.get_settings() is None
    settings = resolve_settings(components=[Component("tool", "geo", version="1")], env={})
    _config.set_runtime(RuntimeState(settings=settings))
    assert _config.get_settings() is settings
    set_global_component(Component("tool", "geo", version="2"))
    set_global_component(Component("prompt", "system", version="9"))
    assert global_components() == (
        Component("prompt", "system", version="9"),
        Component("tool", "geo", version="2"),
    )
    _config.reset_runtime()
    assert _config.get_settings() is None
    assert global_components() == ()


def test_compared_text_values_are_stripped() -> None:
    settings = resolve_settings(
        agent_name=" weather-assistant ",
        agent_version="1.4.0\n",  # e.g. read from a VERSION file
        environment=" staging ",
        service_name=" weather-api ",
        components=[{"type": " prompt ", "name": " system ", "version": " 7\n"}],
        env={},
    )
    assert settings.agent_name == "weather-assistant"
    assert settings.agent_version == "1.4.0"
    assert settings.environment == "staging"
    assert settings.service_name == "weather-api"
    assert settings.requested() == {
        "agent_version": "1.4.0",
        "components": [{"type": "prompt", "name": "system", "version": "7"}],
    }
    assert settings.receipt()["agent"] == {"name": "weather-assistant", "version": "1.4.0"}
    component = Component("tool", "geo ", version=" 2 ", content_hash=" h ")
    assert (component.name, component.version, component.content_hash) == ("geo", "2", "h")
    with pytest.raises(ValueError, match="version"):
        Component("tool", "geo", version="   ")


@pytest.mark.parametrize(
    "value",
    ["imk_live_SECRET123", " imk_live_SECRET123 ", "https://imk_live_SECRET123@eval.example.test"],
)
def test_bad_base_url_errors_never_repeat_the_value(value: str) -> None:
    with pytest.raises(InsightMonError) as info:
        normalize_base_url(value)
    assert "SECRET123" not in str(info.value)
    assert "SECRET123" not in repr(info.value)
    assert info.value.code == "config_error"


def test_a_key_passed_as_base_url_is_named_as_such() -> None:
    with pytest.raises(InsightMonError, match="looks like an API key"):
        resolve_settings(base_url="imk_live_SECRET123", api_key="https://eval.example.test", env={})


@pytest.mark.parametrize(
    "url", ["https://user:pa55@eval.example.test", "https://user@eval.example.test/eval"]
)
def test_base_url_with_credentials_is_refused(url: str) -> None:
    with pytest.raises(InsightMonError, match="user name or password") as info:
        normalize_base_url(url)
    assert "pa55" not in str(info.value)


def test_base_url_with_a_bad_port_is_refused() -> None:
    with pytest.raises(InsightMonError, match="not a valid URL"):
        normalize_base_url("https://eval.example.test:99999")
