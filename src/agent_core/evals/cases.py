"""Declarative evaluation case schema and collection-time loader."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.runs import FailureReason, RunStatus

CASE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
FIXTURE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalClock(StrictModel):
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    @model_validator(mode="after")
    def aware(self) -> EvalClock:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("evaluation clock start must include an offset")
        return self


class EvalInput(StrictModel):
    text: str = Field(min_length=1)


class EvalRunLimits(StrictModel):
    max_steps: int = Field(default=32, ge=1)
    max_model_calls: int = Field(default=16, ge=1)
    max_tool_calls: int = Field(default=32, ge=0)


class EvalFixtures(StrictModel):
    tools: list[str] = Field(default_factory=lambda: ["math.calculate", "system.current_time"])
    run_limits: EvalRunLimits = Field(default_factory=EvalRunLimits)


class EvalExpected(StrictModel):
    terminal_status: RunStatus
    final_text: str | None = None
    failure_reason: FailureReason | None = None
    model_calls: int | None = Field(default=None, ge=0)
    tool_started_count: int | None = Field(default=None, ge=0)
    reason_codes: list[str] = Field(default_factory=list)
    event_order: list[str] = Field(default_factory=list)
    maximum_steps: int | None = Field(default=None, ge=0)


class EvalCase(StrictModel):
    name: str
    milestone: int = Field(ge=1, le=10)
    source: Literal["authored", "trajectory"] = "authored"
    source_export_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)
    agent_id: str = "general"
    principal: str = "eval.standard"
    policy_profile: str = "eval.default"
    clock: EvalClock = Field(default_factory=EvalClock)
    input: EvalInput
    model_fixture: str
    fixtures: EvalFixtures = Field(default_factory=EvalFixtures)
    expected: EvalExpected

    @model_validator(mode="after")
    def stable_names(self) -> EvalCase:
        if CASE_NAME.fullmatch(self.name) is None:
            raise ValueError("case name must use lower snake case")
        if FIXTURE_NAME.fullmatch(self.model_fixture) is None:
            raise ValueError("model_fixture must be a lower snake case stem")
        if self.source == "trajectory" and self.source_export_id is None:
            raise ValueError("trajectory cases must carry their source export id")
        if self.source == "authored" and self.source_export_id is not None:
            raise ValueError("authored cases cannot carry a trajectory export id")
        return self


def load_cases(case_root: Path) -> list[EvalCase]:
    """Load every case in stable path order, rejecting duplicate names."""

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for path in sorted(case_root.glob("*.yaml")):
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        case = EvalCase.model_validate(loaded)
        if case.name in seen:
            raise ValueError(f"duplicate evaluation case name {case.name!r}")
        seen.add(case.name)
        cases.append(case)
    if not cases:
        raise ValueError(f"no evaluation cases found under {case_root}")
    return cases
