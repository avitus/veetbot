"""Durable capability-evaluation results and per-criterion observations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class EvalCriterionScore(BaseModel):
    """One untrusted judge observation and its bounded numeric value."""

    id: UUID
    scenario_run_id: UUID
    criterion: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    value: Decimal


class EvalScenarioRun(BaseModel):
    """One repeat of a versioned capability scenario."""

    id: UUID
    scenario_id: str = Field(min_length=1)
    suite: str = Field(min_length=1)
    repeat_index: int = Field(ge=0)
    run_id: UUID
    judge_version: str = Field(min_length=1)
    build_ref: str = Field(min_length=1)
    score: Decimal | None = None
    ceiling_hit: str | None = None
    policy_failures: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(ge=0)
    started_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def ceiling_runs_are_unscored(self) -> EvalScenarioRun:
        if self.ceiling_hit is not None and self.score is not None:
            raise ValueError("a ceiling-hit scenario run must not have a score")
        if self.ceiling_hit is None and self.score is None:
            raise ValueError("a completed scenario result needs a score or a ceiling hit")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


class SavedEvalScenario(BaseModel):
    """The canonical row and criterion records after replacement semantics."""

    run: EvalScenarioRun
    criteria: list[EvalCriterionScore]
    replaced: bool = False
