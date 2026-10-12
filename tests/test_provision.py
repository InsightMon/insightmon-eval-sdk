"""scripts/provision_platform.py and scripts/integration.sh: the integration key is revoked
when the run that created it ends (a shared platform keeps no usable key behind)."""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "provision_platform", ROOT / "scripts" / "provision_platform.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAdmin:
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.project_id: str | None = None
        FakeAdmin.calls.append(("sign in", base_url))

    def projects(self) -> list[dict[str, Any]]:
        return [{"id": "proj-1", "role": "admin"}]

    def find_dataset(self, min_ready: int = 2) -> tuple[str, list[str]]:
        return "ds-1", ["a", "b"]

    def create_api_key(self, name: str, scopes: list[str]) -> dict[str, Any]:
        FakeAdmin.calls.append(("create", scopes))
        return {"api_key": {"id": "key-7"}, "secret": "imk_test_secret"}

    def revoke_api_key(self, key_id: str) -> dict[str, Any]:
        FakeAdmin.calls.append(("revoke", (self.project_id, key_id)))
        return {"id": key_id}

    def close(self) -> None:
        pass


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_script()
    FakeAdmin.calls = []
    monkeypatch.setattr(module, "AdminSession", FakeAdmin)
    monkeypatch.setattr(module, "responds", lambda url: True)
    monkeypatch.setenv("INSIGHTMON_IT_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("INSIGHTMON_IT_ADMIN_PASSWORD", "not-a-real-password")
    monkeypatch.delenv("INSIGHTMON_IT_PROJECT_ID", raising=False)
    return module


def exports(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        name, _, value = line.removeprefix("export ").partition("=")
        out[name] = shlex.split(value)[0]
    return out


def test_provisioning_prints_the_key_id_to_revoke_it_later(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["provision", "--base-url", "http://127.0.0.1:8790"])
    assert script.main() == 0
    printed = exports(capsys.readouterr().out)
    assert printed == {
        "INSIGHTMON_IT_BASE_URL": "http://127.0.0.1:8790",
        "INSIGHTMON_IT_API_KEY": "imk_test_secret",
        "INSIGHTMON_IT_API_KEY_ID": "key-7",
        "INSIGHTMON_IT_PROJECT_ID": "proj-1",
    }


def test_revoke_mode_revokes_the_key(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["provision", "--base-url", "http://127.0.0.1:8790", "--project-id", "proj-1"]
    monkeypatch.setattr("sys.argv", [*argv, "--revoke", "key-7"])
    assert script.main() == 0
    assert ("revoke", ("proj-1", "key-7")) in FakeAdmin.calls
    assert not any(call[0] == "create" for call in FakeAdmin.calls)
    assert capsys.readouterr().out == ""  # nothing to eval


def test_revoke_mode_needs_the_project(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["provision", "--base-url", "http://127.0.0.1:8790", "--revoke", "key-7"]
    monkeypatch.setattr("sys.argv", argv)
    assert script.main() == 2
    assert not any(call[0] == "revoke" for call in FakeAdmin.calls)


def test_integration_script_revokes_the_key_it_created() -> None:
    text = (ROOT / "scripts" / "integration.sh").read_text(encoding="utf-8")
    cleanup = text[text.index("cleanup() {") :]
    assert cleanup.index("revoke_key") < cleanup.index("stopping the platform")
    assert '--revoke "$CREATED_KEY_ID"' in text
    assert 'CREATED_KEY_ID="$INSIGHTMON_IT_API_KEY_ID"' in text
