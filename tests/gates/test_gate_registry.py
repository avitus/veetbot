"""Milestone 0 checks over the hard-gate registry and scheduling corpus."""

import re
from collections import Counter
from pathlib import Path

import yaml

from scripts.gate_registry import (
    GATE_ID,
    _check_resolves,
    gate_table_arithmetic_errors,
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
    assert len(active) == 227
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
        "notifications-and-devices.md",
        "subagents-and-delegation.md",
        "inbound-surfaces.md",
        "operational-hardening.md",
        "memory-evaluation-and-lifecycle.md",
        "memory-read-api-and-browser.md",
        "email-integration.md",
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
    assert len(entries) == 402
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
        "gate.memory.provider_claim_rendering": "structural",
        "gate.memory.provider_failure_diagnostics": "case",
        "gate.memory.provider_positive_coverage": "case",
        "gate.memory.provider_source_safety": "case",
    }
    by_id = {entry.id: entry for entry in entries}
    assert expected_kinds.keys() <= by_id.keys()
    assert all(by_id[gate_id].milestone == 10 for gate_id in expected_kinds)
    assert {gate_id: by_id[gate_id].kind for gate_id in expected_kinds} == expected_kinds


def test_provider_positive_coverage_is_versioned_across_milestones() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    by_id = {entry.id: entry for entry in entries}

    original = by_id["gate.memory.provider_positive_coverage"]
    assert original.milestone == 10
    assert "sixteen of the twenty labeled positive cases" in original.statement

    expanded = by_id["gate.memory.provider_positive_coverage_v2"]
    assert expanded.milestone == 16
    assert expanded.spec == "docs/plan/memory-evaluation-and-lifecycle.md#hard-gates"
    assert "seventeen of the twenty-one labeled positive cases" in expanded.statement
    assert expanded.check.endswith(
        "TestProviderEvidencePublicationGate::"
        "test_lift_without_minimum_positive_coverage_does_not_publish"
    )


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
        10: 38,
        11: 23,
        12: 20,
        13: 21,
        14: 21,
        15: 16,
        16: 20,
        17: 10,
        18: 13,
        19: 5,
        20: 6,
        21: 29,
        22: 14,
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


_GATE_TABLE_DERIVED = {
    "subject_specs": 23,
    "subject_gates": 329,
    "plan_gates": 2,
    "map_gates": 7,
    "declarations": 338,
    "entries": 335,
    "aliases": 3,
}


def test_gate_table_arithmetic_reports_stale_digits() -> None:
    stale = (
        "## The gate table\n\n"
        "The 15 subject specifications declare 178 gates, the engineering plan\n"
        "declares 2 more, and this document declares 7 over the corpus: 187\n"
        "declarations, 184 registry entries once the 3 aliases are subtracted.\n\n"
        "```text\n"
    )
    assert gate_table_arithmetic_errors(stale, _GATE_TABLE_DERIVED) == [
        "gate table intro says 15 subject specs; registry derives 23",
        "gate table intro says 178 subject gates; registry derives 329",
        "gate table intro says 187 declarations; registry derives 338",
        "gate table intro says 184 entries; registry derives 335",
    ]


def test_gate_table_arithmetic_rejects_compensating_component_edits() -> None:
    shifted = (
        "## The gate table\n\n"
        "The 23 subject specifications declare 329 gates, the engineering plan\n"
        "declares 3 more, and this document declares 6 over the corpus: 338\n"
        "declarations, 335 registry entries once the 3 aliases are subtracted.\n\n"
        "```text\n"
    )
    assert gate_table_arithmetic_errors(shifted, _GATE_TABLE_DERIVED) == [
        "gate table intro says 3 plan gates; registry derives 2",
        "gate table intro says 6 map gates; registry derives 7",
    ]


def test_gate_table_arithmetic_accepts_reconciled_digits() -> None:
    reconciled = (
        "## The gate table\n\n"
        "The 23 subject specifications declare 329 gates, the engineering plan\n"
        "declares 2 more, and this document declares 7 over the corpus: 338\n"
        "declarations, 335 registry entries once the 3 aliases are subtracted.\n\n"
        "```text\n"
    )
    assert gate_table_arithmetic_errors(reconciled, _GATE_TABLE_DERIVED) == []


def test_gate_table_arithmetic_requires_stated_figures() -> None:
    silent = "## The gate table\n\nProse that states no figures.\n\n```text\n"
    assert gate_table_arithmetic_errors(silent, _GATE_TABLE_DERIVED)


def test_gate_table_prose_matches_registry() -> None:
    errors = registry_errors(ROOT, current_milestone=11)
    assert [error for error in errors if "gate table" in error] == []


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


def test_registry_bound_follows_the_authorized_milestones(tmp_path: Path) -> None:
    """Milestone 22 is authorized; the registry admits it and stops there."""
    import scripts.gate_registry as gate_registry

    assert getattr(gate_registry, "MAX_MILESTONE", None) == 22

    gates = tmp_path / "evals" / "gates"
    gates.mkdir(parents=True)
    plan_dir = tmp_path / "docs" / "plan"
    plan_dir.mkdir(parents=True)
    for filename in gate_registry.DECLARING_SPECS:
        (plan_dir / filename).write_text("## Hard gates\n", encoding="utf-8")
    (plan_dir / "milestone-map.md").write_text(
        "## The gate table\n\n```text\n"
        "gate.schedule.roadmap_probe   case   22\n"
        "gate.schedule.beyond_probe    case   23\n"
        "```\n\n## The census\n\n```text\n```\n",
        encoding="utf-8",
    )

    def entry(slug: str, milestone: int) -> dict[str, object]:
        return {
            "id": f"gate.schedule.{slug}",
            "milestone": milestone,
            "kind": "case",
            "spec": "docs/plan/scheduling.md#hard-gates",
            "statement": "bound fixture",
            "check": "tests/gates/pending.py::pending_gate",
        }

    (gates / "schedule.yaml").write_text(
        yaml.safe_dump([entry("roadmap_probe", 22), entry("beyond_probe", 23)]),
        encoding="utf-8",
    )
    errors = registry_errors(tmp_path)
    assert "gate.schedule.roadmap_probe has invalid milestone 22" not in errors
    assert "gate.schedule.beyond_probe has invalid milestone 23" in errors


def test_notifications_and_devices_have_complete_milestone_12_gate_areas() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    device_entries = [entry for entry in entries if entry.id.startswith("gate.device.")]
    notify_entries = [entry for entry in entries if entry.id.startswith("gate.notify.")]

    assert len(device_entries) == 6
    assert len(notify_entries) == 14
    assert all(entry.milestone == 12 for entry in device_entries + notify_entries)
    assert all(
        entry.spec == "docs/plan/notifications-and-devices.md#hard-gates"
        for entry in device_entries + notify_entries
    )
    assert all(GATE_ID.fullmatch(entry.id) for entry in device_entries + notify_entries)


def test_delegation_has_complete_milestone_13_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    delegate_entries = [entry for entry in entries if entry.id.startswith("gate.delegate.")]

    assert len(delegate_entries) == 21
    assert all(entry.milestone == 13 for entry in delegate_entries)
    assert all(
        entry.spec == "docs/plan/subagents-and-delegation.md#hard-gates"
        for entry in delegate_entries
    )
    assert all(GATE_ID.fullmatch(entry.id) for entry in delegate_entries)


def test_surfaces_have_complete_milestone_14_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    surface_entries = [entry for entry in entries if entry.id.startswith("gate.surface.")]

    assert len(surface_entries) == 21
    assert all(entry.milestone == 14 for entry in surface_entries)
    assert all(
        entry.spec == "docs/plan/inbound-surfaces.md#hard-gates" for entry in surface_entries
    )
    assert all(GATE_ID.fullmatch(entry.id) for entry in surface_entries)


def test_operational_hardening_has_complete_milestone_15_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    ops_entries = [entry for entry in entries if entry.id.startswith("gate.ops.")]

    assert len(ops_entries) == 16
    assert all(entry.milestone == 15 for entry in ops_entries)
    assert all(
        entry.spec == "docs/plan/operational-hardening.md#hard-gates" for entry in ops_entries
    )
    assert all(GATE_ID.fullmatch(entry.id) for entry in ops_entries)


def test_memory_evaluation_and_lifecycle_has_complete_milestone_16_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    memory_entries = [entry for entry in entries if entry.milestone == 16]

    assert len(memory_entries) == 20
    assert all(entry.id.startswith("gate.memory.") for entry in memory_entries)
    assert all(
        entry.spec == "docs/plan/memory-evaluation-and-lifecycle.md#hard-gates"
        for entry in memory_entries
    )
    assert all(GATE_ID.fullmatch(entry.id) for entry in memory_entries)


def test_memory_read_api_and_browser_has_complete_milestone_17_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    read_api_entries = [entry for entry in entries if entry.milestone == 17]

    assert len(read_api_entries) == 10
    assert all(entry.id.startswith("gate.memory.") for entry in read_api_entries)
    assert all(
        entry.spec == "docs/plan/memory-read-api-and-browser.md#hard-gates"
        for entry in read_api_entries
    )
    assert all(GATE_ID.fullmatch(entry.id) for entry in read_api_entries)


def test_email_integration_has_complete_milestone_18_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    email_entries = [entry for entry in entries if entry.milestone == 18]

    assert len(email_entries) == 13
    assert all(entry.id.startswith("gate.email.") for entry in email_entries)
    assert all(entry.spec == "docs/plan/email-integration.md#hard-gates" for entry in email_entries)
    assert all(GATE_ID.fullmatch(entry.id) for entry in email_entries)


def test_conversational_scheduling_has_complete_milestone_19_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    schedule_entries = [entry for entry in entries if entry.milestone == 19]

    assert len(schedule_entries) == 5
    assert all(entry.id.startswith("gate.schedule.model_create_") for entry in schedule_entries)
    assert all(entry.spec == "docs/plan/scheduling.md#hard-gates" for entry in schedule_entries)
    assert all(GATE_ID.fullmatch(entry.id) for entry in schedule_entries)


def test_calendar_scheduling_has_complete_milestone_20_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    schedule_entries = [entry for entry in entries if entry.milestone == 20]

    assert len(schedule_entries) == 6
    assert all(entry.id.startswith("gate.schedule.") for entry in schedule_entries)
    assert all(entry.spec == "docs/plan/scheduling.md#hard-gates" for entry in schedule_entries)
    assert all(GATE_ID.fullmatch(entry.id) for entry in schedule_entries)


def test_persona_has_complete_milestone_22_gate_area() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    persona_entries = [entry for entry in entries if entry.milestone == 22]

    assert len(persona_entries) == 14
    assert all(entry.id.startswith("gate.persona.") for entry in persona_entries)
    assert all(entry.spec == "docs/plan/persona-surface.md#hard-gates" for entry in persona_entries)
    assert all(GATE_ID.fullmatch(entry.id) for entry in persona_entries)
