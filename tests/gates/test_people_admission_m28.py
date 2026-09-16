"""The owner-authorized People milestone has explicit, non-vacuous contracts."""

from pathlib import Path

import yaml

from scripts.gate_registry import GATE_ID, load_registry

ROOT = Path(__file__).resolve().parents[2]


def test_people_has_thirty_six_registered_milestone_28_requirements() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    people = [entry for entry in entries if entry.milestone == 28]
    assert len(people) == 36
    assert all(entry.id.startswith("gate.people.") for entry in people)
    assert all(GATE_ID.fullmatch(entry.id) for entry in people)
    assert all(entry.spec == "docs/plan/people-and-relationships.md#hard-gates" for entry in people)
    state = yaml.safe_load((ROOT / "docs/status/project-state.yaml").read_text())
    assert 28 in state["project"]["authorized_milestones"]
    assert state["milestones"]["28"]["status"] == "in_progress"
    assert state["project"]["current_milestone"] == 12
