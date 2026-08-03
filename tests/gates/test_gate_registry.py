"""Milestone 0 checks over the hard-gate registry and scheduling corpus."""

import re
from collections import Counter
from pathlib import Path

from scripts.gate_registry import (
    GATE_ID,
    hard_gate_items,
    load_registry,
    map_entries,
    registry_errors,
)

ROOT = Path(__file__).resolve().parents[2]


def test_registry_complete() -> None:
    assert registry_errors(ROOT, current_milestone=2) == []


def test_no_stale_active_gate() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    active = [entry for entry in entries if entry.milestone <= 2]
    assert len(active) == 57
    assert all(entry.check != "tests/gates/pending.py::pending_gate" for entry in active)
    assert all(not entry.optional for entry in active)


def test_milestone_tokens_present() -> None:
    for filename in (
        "runtime-loop.md",
        "tool-system.md",
        "builtin-tools.md",
        "model-gateway.md",
        "policy-and-approvals.md",
        "event-log-and-persistence.md",
        "context-engine.md",
        "memory-formation-and-consolidation.md",
        "memory-retrieval-and-ranking.md",
        "evaluation-harness.md",
        "http-api-and-streaming.md",
        "sandbox-isolation.md",
        "skills.md",
        "knowledge-documents.md",
        "milestone-map.md",
    ):
        items = hard_gate_items(ROOT / "docs" / "plan" / filename)
        assert items
        assert all(len(re.findall(r"\*\*M\d+\.\*\*", item)) == 1 for item in items)


def test_map_bijection() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    assert {entry.id for entry in entries} == set(map_entries(ROOT))


def test_alias_arithmetic() -> None:
    assert registry_errors(ROOT, current_milestone=0) == []


def test_spec_anchors_resolve() -> None:
    assert registry_errors(ROOT, current_milestone=0) == []


def test_identifier_grammar() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    assert len(entries) == 172
    assert all(GATE_ID.fullmatch(entry.id) for entry in entries)


def test_census_is_derived() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    assert Counter(entry.milestone for entry in entries) == {
        0: 13,
        1: 28,
        2: 16,
        3: 15,
        4: 22,
        5: 11,
        6: 11,
        7: 7,
        8: 17,
        9: 26,
        10: 6,
    }
