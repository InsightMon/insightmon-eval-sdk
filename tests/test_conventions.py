"""docs/conventions.md matches `insightmon.semconv`.

- every attribute or event name in `semconv` (a string constant with a dot), every member of the
  name collections and every allowed value appears in the document;
- the first column of every attribute or event table holds only names `semconv` knows (or their
  patterns: `<type>.<name>` components, `insightmon.metadata.<key>`, `prefix*`);
- the limits written next to the names agree with the `semconv` constants.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from insightmon import semconv

DOC = Path(__file__).resolve().parents[1] / "docs" / "conventions.md"
TEXT = DOC.read_text(encoding="utf-8")

#: Table header first cells that mark a table of names.
NAME_HEADERS = ("属性", "事件")

COLLECTIONS: dict[str, tuple[str, ...]] = {
    "THREAD_KEY_ATTRIBUTES": tuple(semconv.THREAD_KEY_ATTRIBUTES),
    "CONTENT_ATTRIBUTES": tuple(semconv.CONTENT_ATTRIBUTES),
    "THIRD_PARTY_CONTENT_ATTRIBUTES": tuple(semconv.THIRD_PARTY_CONTENT_ATTRIBUTES),
    "CONTENT_EVENTS": tuple(sorted(semconv.CONTENT_EVENTS)),
    "SERVER_ATTRIBUTES": tuple(sorted(semconv.SERVER_ATTRIBUTES)),
}
VALUE_SETS: dict[str, tuple[str, ...]] = {
    "OPERATIONS": tuple(sorted(semconv.OPERATIONS)),
    "CONTENT_LANGUAGES": tuple(semconv.CONTENT_LANGUAGES),
    "DEFAULT_COMPONENT_TYPES": tuple(semconv.DEFAULT_COMPONENT_TYPES),
    "TRUE_LABELS": tuple(sorted(semconv.TRUE_LABELS)),
    "FALSE_LABELS": tuple(sorted(semconv.FALSE_LABELS)),
}
PREFIXES = (semconv.COMPONENT_PREFIX, semconv.METADATA_PREFIX)

#: attribute -> its limit in characters, written as `≤ N` in the attribute's row.
LIMITS: dict[str, int] = {
    semconv.GEN_AI_CONVERSATION_ID: semconv.THREAD_KEY_MAX,
    semconv.SESSION_ID: semconv.THREAD_KEY_MAX,
    semconv.INSIGHTMON_THREAD_ID: semconv.THREAD_KEY_MAX,
    semconv.ENDUSER_ID: semconv.ENDUSER_ID_MAX,
    semconv.AGENT_NAME: semconv.AGENT_FIELD_MAX,
    semconv.AGENT_VERSION: semconv.AGENT_FIELD_MAX,
    semconv.component_attribute("<type>", "<name>"): semconv.COMPONENT_VALUE_MAX,
    semconv.DEPLOYMENT_ENVIRONMENT_NAME: semconv.ENVIRONMENT_MAX,
    semconv.RESPONSE_ID: semconv.RESPONSE_ID_MAX,
    semconv.ERROR_TYPE: semconv.ERROR_TYPE_MAX,
    semconv.EVALUATION_NAME: semconv.EVALUATION_NAME_MAX,
    semconv.EVALUATION_EXPLANATION: semconv.EVALUATION_EXPLANATION_MAX,
    semconv.EXAMPLE_KEY: semconv.EXAMPLE_KEY_MAX,
}


def dotted_names() -> list[str]:
    """Every string constant of `semconv` that is an attribute or event name (has a dot)."""
    return sorted(
        value
        for name, value in vars(semconv).items()
        if name.isupper() and isinstance(value, str) and "." in value
    )


def known_names() -> set[str]:
    names = {n for n in dotted_names() if not n.endswith(".")}
    for members in COLLECTIONS.values():
        names.update(members)
    return names


def name_tables() -> list[list[list[str]]]:
    """The rows (cells) of every table whose first header cell is a name column."""
    tables: list[list[list[str]]] = []
    rows: list[list[str]] | None = None  # the rows of the current table (None outside one)
    for line in TEXT.splitlines():
        if not line.startswith("|"):
            rows = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if rows is None:  # the header row
            rows = []
            if cells[0] in NAME_HEADERS:
                tables.append(rows)
        elif not set(cells[0]) <= {"-", ":"}:  # not the separator row
            rows.append(cells)
    return tables


def test_the_document_has_name_tables() -> None:
    tables = name_tables()
    assert len(tables) >= 8
    assert sum(len(rows) for rows in tables) >= 50


@pytest.mark.parametrize("name", dotted_names())
def test_every_semconv_name_is_documented(name: str) -> None:
    if name.endswith("."):  # a prefix: documented as part of its pattern
        assert f"`{name}" in TEXT
    else:
        assert f"`{name}`" in TEXT


@pytest.mark.parametrize("collection", sorted(COLLECTIONS))
def test_every_name_collection_is_documented(collection: str) -> None:
    missing = [n for n in COLLECTIONS[collection] if f"`{n}`" not in TEXT]
    assert missing == []


def test_third_party_prefixes_are_documented_as_patterns() -> None:
    for prefix in semconv.THIRD_PARTY_CONTENT_PREFIXES:
        assert f"`{prefix}*`" in TEXT


@pytest.mark.parametrize("values", sorted(VALUE_SETS))
def test_every_allowed_value_is_documented(values: str) -> None:
    missing = [v for v in VALUE_SETS[values] if f"`{v}`" not in TEXT]
    assert missing == []


def _is_known(token: str, known: set[str]) -> bool:
    if token in known:
        return True
    if token.endswith("*"):
        return token[:-1] in semconv.THIRD_PARTY_CONTENT_PREFIXES
    if token.startswith(semconv.METADATA_PREFIX):
        return token == f"{semconv.METADATA_PREFIX}<key>"
    if token.startswith(semconv.COMPONENT_PREFIX):
        return token in {
            semconv.component_attribute("<type>", "<name>", field)
            for field in (semconv.COMPONENT_VERSION, semconv.COMPONENT_CONTENT_HASH)
        }
    return False


def test_table_rows_name_only_known_attributes_and_events() -> None:
    known = known_names()
    unknown: list[str] = []
    for rows in name_tables():
        for cells in rows:
            tokens = re.findall(r"`([^`]+)`", cells[0])
            assert tokens, f"a row without a name: {cells}"
            unknown.extend(t for t in tokens if not _is_known(t, known))
    assert unknown == []


def test_every_documented_attribute_has_one_row() -> None:
    first_cells = [re.findall(r"`([^`]+)`", cells[0]) for rows in name_tables() for cells in rows]
    tokens = [t for cell in first_cells for t in cell]
    # `error.type` and `gen_ai.response.id` are span attributes and evaluation log attributes
    repeated = {t for t in tokens if tokens.count(t) > 1}
    assert repeated <= {semconv.ERROR_TYPE, semconv.RESPONSE_ID}


@pytest.mark.parametrize("name", sorted(LIMITS))
def test_limits_agree_with_semconv(name: str) -> None:
    rows = [cells for rows in name_tables() for cells in rows if f"`{name}`" in cells[0]]
    assert rows, f"no row for {name}"
    for cells in rows:
        limits = {int(n) for n in re.findall(r"≤ (\d+)", " ".join(cells[1:]))}
        assert limits == {LIMITS[name]}, (name, cells)


def test_content_field_limit_agrees_with_semconv() -> None:
    kib = semconv.CONTENT_FIELD_MAX_BYTES // semconv.KIB
    for attribute in semconv.CONTENT_ATTRIBUTES:
        (cells,) = [c for rows in name_tables() for c in rows if f"`{attribute}`" in c[0]]
        assert f"≤ {kib} KiB" in " ".join(cells)
