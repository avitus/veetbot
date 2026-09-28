"""The citation ledger detects drift, repairs only unambiguous moves, and rejects prose.

AGENTS.md makes `make citations-fix` and `make docs-check` the guard on every
`file.md:LINE` reference. These tests run the checker over a throwaway corpus
so its repair rules are pinned without touching the real documents.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

# Imported by name so the type checker, which covers tests but not this
# untyped maintenance script, does not follow into it.
check_citations: ModuleType = importlib.import_module("scripts.check_citations")

CITED = ["# B", "", "The cited rule.", "Its second line.", "Its third line."]


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Point the checker at a temporary corpus; return a runner yielding its errors."""

    plan = tmp_path / "docs" / "plan"
    plan.mkdir(parents=True)
    (tmp_path / "docs" / "status").mkdir()
    monkeypatch.setattr(check_citations, "ROOT", tmp_path)
    monkeypatch.setattr(check_citations, "LEDGER", tmp_path / "docs/status/citation-ledger.yaml")

    def run(*, update: bool = False, prose: bool = False) -> list[str]:
        monkeypatch.setattr(check_citations, "errors", [])
        monkeypatch.setattr(check_citations, "notes", [])
        check_citations.run_check(update)
        if prose:
            check_citations.check_bare_references()
        return [str(error) for error in check_citations.errors]

    return run


def _write(root: Path, name: str, lines: list[str]) -> Path:
    path = root / "docs" / "plan" / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _root() -> Path:
    root: Path = check_citations.ROOT
    return root


def test_an_unrecorded_citation_is_an_error_until_update_adopts_it(
    corpus: Callable[..., list[str]],
) -> None:
    _write(_root(), "b.md", CITED)
    _write(_root(), "a.md", ["See `b.md:3` for the rule."])

    [error] = corpus()
    assert "is not in the ledger" in error
    assert corpus(update=True) == []
    assert corpus() == []


def test_a_moved_line_is_reported_then_repaired_to_its_new_number(
    corpus: Callable[..., list[str]],
) -> None:
    _write(_root(), "b.md", CITED)
    citing = _write(_root(), "a.md", ["See `b.md:3` and again `b.md:3-4`."])
    assert corpus(update=True) == []

    _write(_root(), "b.md", ["# B", "An inserted paragraph.", *CITED[1:]])
    errors = corpus()
    assert len(errors) == 2
    assert all("has drifted" in error and "now at line 4" in error for error in errors)

    assert corpus(update=True) == []
    assert citing.read_text(encoding="utf-8") == "See `b.md:4` and again `b.md:4-5`.\n"
    assert corpus() == []


def test_an_edit_inside_a_cited_range_is_drift_even_when_its_first_line_holds(
    corpus: Callable[..., list[str]],
) -> None:
    _write(_root(), "b.md", CITED)
    citing = _write(_root(), "a.md", ["See `b.md:3-5`."])
    assert corpus(update=True) == []

    _write(_root(), "b.md", [*CITED[:4], "A rewritten third line."])
    [error] = corpus(update=True)
    assert "no longer holds the text it cited" in error
    assert citing.read_text(encoding="utf-8") == "See `b.md:3-5`.\n", "never guessed"


def test_an_ambiguous_move_is_left_for_a_human(corpus: Callable[..., list[str]]) -> None:
    _write(_root(), "b.md", CITED)
    citing = _write(_root(), "a.md", ["See `b.md:3`."])
    assert corpus(update=True) == []

    _write(_root(), "b.md", ["# B", "The cited rule.", "", "Other.", "The cited rule."])
    [error] = corpus(update=True)
    assert "appears 2 times" in error
    assert citing.read_text(encoding="utf-8") == "See `b.md:3`.\n"


@pytest.mark.parametrize(
    ("citation", "message"),
    [
        ("`b.md:0`", "not a line number"),
        ("`b.md:5-4`", "runs backwards"),
        ("`b.md:40`", "past the end"),
        ("`b.md:2`", "blank"),
        ("`missing.md:1`", "not a document in this corpus"),
    ],
)
def test_a_citation_that_is_not_a_real_span_is_refused(
    corpus: Callable[..., list[str]], citation: str, message: str
) -> None:
    _write(_root(), "b.md", CITED)
    _write(_root(), "a.md", [f"See {citation}."])
    errors = corpus(update=True)
    assert any(message in error for error in errors), errors


@pytest.mark.parametrize(
    "prose",
    ["as line 1408 says", "see lines\n42 onward", "per b.md 12"],
)
def test_a_line_number_written_as_prose_is_rejected(
    corpus: Callable[..., list[str]], prose: str
) -> None:
    _write(_root(), "b.md", CITED)
    _write(_root(), "a.md", [f"The rule, {prose}, holds."])
    [error] = corpus(prose=True)
    assert "which the ledger cannot check" in error
