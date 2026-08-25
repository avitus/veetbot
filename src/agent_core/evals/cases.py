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
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    run_limits: EvalRunLimits = Field(default_factory=EvalRunLimits)


class EvalSession(StrictModel):
    turns: int = Field(ge=2, le=100)
    advance_seconds: int = Field(default=0, ge=0)
    prompt_padding_bytes: int = Field(default=0, ge=0, le=16_384)
    revoke_scope: str | None = None
    revoke_scope_turn: int | None = Field(default=None, ge=2)
    memory_write_turn: int | None = Field(default=None, ge=2)
    memory_correction_turn: int | None = Field(default=None, ge=2)

    @model_validator(mode="after")
    def intervention_turns_fit(self) -> EvalSession:
        for name in (
            "revoke_scope_turn",
            "memory_write_turn",
            "memory_correction_turn",
        ):
            value = getattr(self, name)
            if value is not None and value > self.turns:
                raise ValueError(f"{name} cannot exceed session turns")
        if (self.revoke_scope is None) != (self.revoke_scope_turn is None):
            raise ValueError("scope revocation requires both a scope and a turn")
        return self


class EvalExpected(StrictModel):
    terminal_status: RunStatus
    final_text: str | None = None
    failure_reason: FailureReason | None = None
    model_calls: int | None = Field(default=None, ge=0)
    tool_started_count: int | None = Field(default=None, ge=0)
    reason_codes: list[str] = Field(default_factory=list)
    event_order: list[str] = Field(default_factory=list)
    maximum_steps: int | None = Field(default=None, ge=0)
    pending_approvals: int | None = Field(default=None, ge=0)
    distinct_prefixes: int | None = Field(default=None, ge=1)
    minimum_compactions: int | None = Field(default=None, ge=0)
    absent_strings: list[str] = Field(default_factory=list)


class EvalArm(StrictModel):
    name: str
    skills: list[str] = Field(default_factory=list)
    skill_source: Literal["operator", "agent"] = "operator"
    carry: list[Literal["memory"]] = Field(default_factory=list)
    tools: list[str] | None = None
    expected: EvalExpected

    @model_validator(mode="after")
    def stable_name(self) -> EvalArm:
        if CASE_NAME.fullmatch(self.name) is None:
            raise ValueError("arm name must use lower snake case")
        if len(set(self.carry)) != len(self.carry):
            raise ValueError("arm carry subjects must be unique")
        return self


class EvalDelta(StrictModel):
    policy_failures: Literal["same", "not_worse"] = "same"
    outcome: Literal["improves", "not_worse"] = "improves"


class EvalCase(StrictModel):
    name: str
    milestone: int = Field(ge=1, le=13)
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
    session: EvalSession | None = None
    expected: EvalExpected | None = None
    arms: list[EvalArm] = Field(default_factory=list)
    delta: EvalDelta | None = None
    approval_resolution: Literal["approve_once", "deny"] | None = None
    cancel_after_submission: bool = False

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
        if (self.expected is None) == (not self.arms):
            raise ValueError("a case must declare either expected or arms")
        if self.arms:
            if len(self.arms) != 2:
                raise ValueError("comparison cases require exactly two arms")
            if len({arm.name for arm in self.arms}) != len(self.arms):
                raise ValueError("comparison arm names must be unique")
            if self.delta is None:
                raise ValueError("comparison cases require delta assertions")
        elif self.delta is not None:
            raise ValueError("single-run cases cannot declare delta assertions")
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
