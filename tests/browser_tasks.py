"""Closed synthetic task manifest and content-free component evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z-]{1,63}$")
    fixture: Literal[
        "sequence",
        "button",
        "dense",
        "hidden",
        "long-text",
        "shadow",
        "text",
        "select-check",
        "slow",
    ]
    target: str
    expected_effects: int = Field(ge=1, le=3)
    budget: int = Field(ge=1024, le=4096)


_MANIFEST = json.loads(Path(__file__).with_name("browser_task_manifest.json").read_text())
SCENARIOS = tuple(Scenario.model_validate(value) for value in _MANIFEST["scenarios"])
assert _MANIFEST["version"] == 1
assert len(SCENARIOS) == len({scenario.id for scenario in SCENARIOS}) == 12


@dataclass(frozen=True)
class TaskOutcome:
    completed: bool
    operations: int
    observed_effects: int
    duration_seconds: float


OUTCOMES: dict[str, TaskOutcome] = {}


def task_report() -> dict[str, Any]:
    cases = []
    for scenario in SCENARIOS:
        result = OUTCOMES.get(scenario.id)
        cases.append(
            {
                "id": scenario.id,
                "outcome": "not_run"
                if result is None
                else (
                    "completed"
                    if result.completed and result.observed_effects == scenario.expected_effects
                    else "failed"
                ),
                "expected_effects": scenario.expected_effects,
                "observed_effects": None if result is None else result.observed_effects,
                "operations": None if result is None else result.operations,
                "duration_seconds": None if result is None else result.duration_seconds,
                "inline_budget_bytes": scenario.budget,
            }
        )
    return {
        "manifest_version": _MANIFEST["version"],
        "kind": "scripted_browser_component_tasks",
        "model_calls": 0,
        "model_cost": None,
        "live_task_quality_measured": False,
        "verified": all(case["outcome"] == "completed" for case in cases),
        "cases": cases,
    }
