"""Context planning, budgeting, compaction, and structured working-state values."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from agent_core.domain.messages import CacheBreakpoint, ModelRequest
from agent_core.domain.policies import TrustLevel
from agent_core.domain.provenance import ElidedSpan
from agent_core.domain.skills import CatalogMetadata, SkillPin
from agent_core.domain.tools import ToolSpec


class ContextRegion(StrEnum):
    PREFIX = "A"
    BODY = "B"


class ContextClass(StrEnum):
    PLATFORM_POLICY = "platform_policy"
    FRAMING = "framing"
    AGENT_INSTRUCTIONS = "agent_instructions"
    TOOL_DEFINITIONS = "tool_definitions"
    SKILL_CATALOG = "skill_catalog"
    MEMORY_SNAPSHOT = "memory_snapshot"
    COMPACTED_SUMMARY = "compacted_summary"
    HISTORY = "history"
    SKILL_BODIES = "skill_bodies"
    WORKING_STATE = "working_state"
    KNOWLEDGE = "knowledge"
    RECALL = "recall"
    RUNTIME_METADATA = "runtime_metadata"
    CURRENT_USER = "current_user"


REGION_ASSIGNMENTS: dict[ContextClass, ContextRegion] = {
    ContextClass.PLATFORM_POLICY: ContextRegion.PREFIX,
    ContextClass.FRAMING: ContextRegion.PREFIX,
    ContextClass.AGENT_INSTRUCTIONS: ContextRegion.PREFIX,
    ContextClass.TOOL_DEFINITIONS: ContextRegion.PREFIX,
    ContextClass.SKILL_CATALOG: ContextRegion.PREFIX,
    ContextClass.MEMORY_SNAPSHOT: ContextRegion.PREFIX,
    ContextClass.COMPACTED_SUMMARY: ContextRegion.BODY,
    ContextClass.HISTORY: ContextRegion.BODY,
    ContextClass.SKILL_BODIES: ContextRegion.BODY,
    ContextClass.WORKING_STATE: ContextRegion.BODY,
    ContextClass.KNOWLEDGE: ContextRegion.BODY,
    ContextClass.RECALL: ContextRegion.BODY,
    ContextClass.RUNTIME_METADATA: ContextRegion.BODY,
    ContextClass.CURRENT_USER: ContextRegion.BODY,
}


class ContextBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_tokens: int = Field(gt=0)
    reserve_output_tokens: int = Field(gt=0)
    platform_tokens: int = Field(ge=0)
    agent_tokens: int = Field(ge=0)
    tool_tokens: int = Field(ge=0)
    skill_catalog_tokens: int = Field(ge=0)
    skill_body_tokens: int = Field(ge=0)
    retrieved_context_tokens: int = Field(ge=0)
    history_tokens: int = Field(ge=0)
    working_state_tokens: int = Field(ge=0)
    tool_result_tokens: int = Field(ge=0)
    knowledge_tokens: int = Field(ge=0)
    safety_margin_ratio: float = Field(default=0.05, ge=0, lt=1)

    @model_validator(mode="after")
    def reserve_fits_total(self) -> ContextBudget:
        if self.reserve_output_tokens > self.total_tokens:
            raise ValueError("context output reserve exceeds the total token budget")
        return self

    @property
    def input_capacity(self) -> int:
        return self.total_tokens - self.reserve_output_tokens


class ContextPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    epoch: int = Field(ge=1)
    prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prefix_tokens: int = Field(ge=0)
    model_id: str
    tool_names: tuple[str, ...]
    tool_specs: tuple[ToolSpec, ...]
    tool_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: UUID | None = None
    snapshot_watermark: int = Field(default=0, ge=0)
    memory_snapshot: str = ""
    skill_pins: tuple[SkillPin, ...] = ()
    skill_catalog: tuple[CatalogMetadata, ...] = ()
    cache_breakpoints: tuple[CacheBreakpoint, ...] = ()
    policy_version: str
    builder_version: str
    budget: ContextBudget
    created_at: datetime

    @model_validator(mode="after")
    def tools_match_names(self) -> ContextPlan:
        if tuple(spec.name for spec in self.tool_specs) != self.tool_names:
            raise ValueError("context plan tool names do not match its pinned specifications")
        return self


class ContextPressure(BaseModel):
    model_config = ConfigDict(frozen=True)

    fits: bool
    compactable: bool
    reason: str
    total_tokens: int = Field(ge=0)
    prefix_tokens: int = Field(ge=0)
    body_tokens: int = Field(ge=0)
    reserve_output_tokens: int = Field(ge=0)
    capacity_tokens: int = Field(ge=0)
    history_cut: int = Field(ge=0)
    history_budget_tokens: int = Field(ge=0)
    yield_steps: tuple[str, ...] = ()


class ContextAssembly(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: ModelRequest
    pressure: ContextPressure


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class TaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4096)
    status: TaskStatus = TaskStatus.OPEN
    source_event_ids: list[PositiveInt] = Field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED
    updated_at: datetime


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=4096)
    source_event_ids: list[PositiveInt] = Field(min_length=1)
    trust_level: TrustLevel
    established_at: datetime


class WorkingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str | None = Field(default=None, max_length=4096)
    constraints: list[str] = Field(default_factory=list)
    tasks: list[TaskState] = Field(default_factory=list)
    established_facts: list[Fact] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_action: str | None = Field(default=None, max_length=4096)


class CompactionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    source_event_ids: tuple[PositiveInt, ...]
    elided: tuple[ElidedSpan, ...]
    replaced_through_sequence: int = Field(ge=0)
    depth: int = Field(ge=1, le=2)
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    compactor_version: str
