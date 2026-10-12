"""Purity: the SDK is generic; no domain vocabulary in code, tests, examples or docs.

The term list is copied verbatim from the platform's own purity test
(`backend/tests/test_purity.py` of InsightMon/insightmon-ai-eval). Matching is the same:
case-insensitive substring matching, so pick neutral names and examples (a weather or notes
assistant).

Scanned: `src/`, `tests/`, `examples/`, `scripts/`, `docs/` (including docs/contract.md), the
READMEs, `CHANGELOG.md`, `pyproject.toml`, `Makefile` and `.github/`. Only this file is excluded
(it has to spell the terms out).

docs/contract.md describes the platform, and a few platform identifiers contain listed terms
(e.g. the name of the platform's bundled domain pack). The contract paraphrases them instead of
allowlisting them; `ALLOWLIST` stays available for a case that cannot be paraphrased, and every
entry needs a justification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DOMAIN_TERMS: tuple[str, ...] = (
    # English
    "handoff",
    "hand-off",
    "hand off",
    "customer service",
    "customer_service",
    "customer-service",
    "customerservice",
    "customer support",
    "call center",
    "call centre",
    "resolution",
    "cs_agent",
    "cs_default",
    # Chinese
    "客服",
    "转人工",
    "轉人工",
    "坐席",
    "自主解决",
    "有效接待",
    "客户服务",
    # Japanese
    "有人対応",
    "オペレーター",
    "カスタマーサポート",
    "カスタマーサービス",
    "有人エスカレーション",
    "コールセンター",
    "顧客サポート",
)

SCAN_DIRS: tuple[str, ...] = ("src", "tests", "examples", "scripts", "docs", ".github")
SCAN_FILES: tuple[str, ...] = (
    "README.md",
    "README.en.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "Makefile",
)
SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".txt",
        ".cfg",
        ".ini",
        ".typed",
    }
)
SKIP_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build"}
)
SELF = Path(__file__).resolve()

#: (path relative to the repository root, term) -> justification. Keep empty when possible.
ALLOWLIST: dict[tuple[str, str], str] = {}


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        root = REPO_ROOT / name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if (
                path.is_file()
                and (path.suffix in SUFFIXES or path.name == "Makefile")
                and not SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts)
                and path.resolve() != SELF
            ):
                files.append(path)
    files.extend(REPO_ROOT / name for name in SCAN_FILES if (REPO_ROOT / name).is_file())
    return sorted(set(files))


def _terms_in(text: str) -> list[str]:
    lowered = text.lower()
    return [term for term in DOMAIN_TERMS if term.lower() in lowered]


def test_scan_covers_code_tests_and_docs() -> None:
    rels = {str(p.relative_to(REPO_ROOT)) for p in _scanned_files()}
    assert "src/insightmon/__init__.py" in rels
    assert "docs/contract.md" in rels
    assert "tests/conftest.py" in rels
    assert "README.md" in rels
    assert "pyproject.toml" in rels
    assert "scripts/integration.sh" in rels
    assert "examples/http_agent.py" in rels
    assert "tests/integration/test_replay.py" in rels


@pytest.mark.parametrize("term", DOMAIN_TERMS)
def test_no_domain_term(term: str) -> None:
    offenders = []
    for path in _scanned_files():
        rel = str(path.relative_to(REPO_ROOT))
        if (rel, term) in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if term.lower() in text:
            offenders.append(rel)
    assert not offenders, f"domain term {term!r} found in: {offenders}"


def test_this_file_is_not_scanned() -> None:
    assert SELF not in {p.resolve() for p in _scanned_files()}


def test_allowlist_entries_are_justified() -> None:
    for (rel, term), why in ALLOWLIST.items():
        assert term in DOMAIN_TERMS, (rel, term)
        assert (REPO_ROOT / rel).is_file(), rel
        assert why.strip(), f"allowlist entry {(rel, term)} needs a justification"


def test_matching_is_case_insensitive_substring() -> None:
    assert _terms_in("A HandOff step") == ["handoff"]
    assert "客服" in _terms_in("在线客服中心")
    assert "コールセンター" in _terms_in("大規模コールセンター")
    assert _terms_in("a weather assistant that resolves locations") == []
