"""Milestone 0 checks over the hard-gate registry and scheduling corpus."""

import re
from collections import Counter
from pathlib import Path

import yaml

from scripts.gate_registry import (
    GATE_ID,
    _check_resolves,
    hard_gate_items,
    load_registry,
    map_entries,
    registry_errors,
)

ROOT = Path(__file__).resolve().parents[2]


def test_registry_complete() -> None:
    assert registry_errors(ROOT, current_milestone=11) == []


def test_nested_pytest_method_selector_resolves(tmp_path: Path) -> None:
    check = tmp_path / "tests" / "test_example.py"
    check.parent.mkdir()
    check.write_text(
        "class TestGate:\n    def test_behavior(self) -> None:\n        pass\n",
        encoding="utf-8",
    )

    assert _check_resolves(tmp_path, "tests/test_example.py::TestGate::test_behavior")
    assert not _check_resolves(tmp_path, "tests/test_example.py::TestGate")


def test_no_stale_active_gate() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    active = [entry for entry in entries if entry.milestone <= 11]
    assert len(active) == 223
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
        "web-access.md",
        "browser-automation.md",
        "scheduling.md",
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
    errors = registry_errors(ROOT, current_milestone=0)
    assert not [error for error in errors if "registry entries; expected" in error]
    assert not [error for error in errors if "hard gates; expected" in error]


def test_spec_anchors_resolve() -> None:
    errors = registry_errors(ROOT, current_milestone=0)
    assert not [error for error in errors if "spec anchor does not resolve" in error]
    assert not [error for error in errors if "invalid spec link" in error]


def test_identifier_grammar() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    assert len(entries) == 223
    assert all(GATE_ID.fullmatch(entry.id) for entry in entries)


def test_new_memory_guarantees_are_formal_registry_entries() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    expected_kinds = {
        "gate.memory.inspection_governed": "case",
        "gate.memory.extractor_contract": "structural",
        "gate.memory.provider_activation_bound": "property",
        "gate.memory.provider_boundary": "case",
        "gate.memory.provider_audit_fallback": "case",
        "gate.memory.provider_evidence_publish": "case",
    }
    by_id = {entry.id: entry for entry in entries}
    assert expected_kinds.keys() <= by_id.keys()
    assert all(by_id[gate_id].milestone == 10 for gate_id in expected_kinds)
    assert {gate_id: by_id[gate_id].kind for gate_id in expected_kinds} == expected_kinds


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
        10: 34,
        11: 23,
    }


def test_web_access_has_complete_milestone_10_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    web_entries = [entry for entry in entries if entry.id.startswith("gate.web.")]

    assert len(web_entries) == 7
    assert all(entry.milestone == 10 for entry in web_entries)
    assert all(entry.spec == "docs/plan/web-access.md#hard-gates" for entry in web_entries)
    assert all(entry.check.startswith("tests/gates/test_web_m10.py::") for entry in web_entries)


def test_browser_automation_has_complete_milestone_10_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    browser_entries = [entry for entry in entries if entry.id.startswith("gate.browser.")]

    assert len(browser_entries) == 10
    assert all(entry.milestone == 10 for entry in browser_entries)
    assert all(
        entry.spec == "docs/plan/browser-automation.md#hard-gates" for entry in browser_entries
    )
    assert all(
        entry.check.startswith("tests/gates/test_browser_m10.py::") for entry in browser_entries
    )


def test_malformed_identifier_and_missing_map_are_reported(tmp_path: Path) -> None:
    gates = tmp_path / "evals" / "gates"
    gates.mkdir(parents=True)
    (gates / "runtime.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "gate",
                    "milestone": 0,
                    "kind": "case",
                    "spec": "docs/plan/runtime-loop.md#hard-gates",
                    "statement": "malformed fixture",
                    "check": "tests/gates/pending.py::pending_gate",
                }
            ]
        ),
        encoding="utf-8",
    )
    _entries, errors = load_registry(tmp_path)
    assert any("malformed identifier: gate" in error for error in errors)
    all_errors = registry_errors(tmp_path)
    assert "docs/plan/milestone-map.md is missing" in all_errors
