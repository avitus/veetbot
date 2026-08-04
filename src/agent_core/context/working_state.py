"""Typed, bounded transitions and carry rules for run working state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from agent_core.context.rendering import envelope_items, working_state_items
from agent_core.domain.context import Fact, TaskState, TaskStatus, WorkingState
from agent_core.domain.policies import TrustLevel
from agent_core.ports.context import TokenEstimator
from agent_core.ports.determinism import Clock

STRUCTURED_STATE_KEY = "context"


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4096)
    status: TaskStatus = TaskStatus.OPEN
    source_event_ids: list[PositiveInt] = Field(default_factory=list)


class FactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=4096)
    source_event_ids: list[PositiveInt] = Field(min_length=1)


class WorkingStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str | None = Field(default=None, max_length=4096)
    add_constraints: list[str] = Field(default_factory=list)
    upsert_tasks: list[TaskUpdate] = Field(default_factory=list)
    add_facts: list[FactUpdate] = Field(default_factory=list)
    resolve_questions: list[str] = Field(default_factory=list)
    next_action: str | None = Field(default=None, max_length=4096)


class WorkingStateLimits(BaseModel):
    max_constraints: int = Field(gt=0)
    max_open_tasks: int = Field(gt=0)
    max_established_facts: int = Field(gt=0)
    max_open_questions: int = Field(gt=0)
    block_ceiling_tokens: int = Field(gt=0)


class WorkingStateLimitError(ValueError):
    pass


class WorkingStateManager:
    def __init__(
        self,
        clock: Clock,
        config: Mapping[str, object],
        estimator: TokenEstimator,
    ) -> None:
        self._clock = clock
        self._limits = WorkingStateLimits.model_validate(config)
        self._estimator = estimator

    @staticmethod
    def load(container: Mapping[str, Any]) -> WorkingState:
        raw = container.get(STRUCTURED_STATE_KEY)
        return WorkingState() if raw is None else WorkingState.model_validate(raw)

    @staticmethod
    def store(container: dict[str, Any], state: WorkingState) -> None:
        container[STRUCTURED_STATE_KEY] = state.model_dump(mode="json")

    @staticmethod
    def carry(state: WorkingState) -> WorkingState:
        return state.model_copy(
            update={
                "tasks": [
                    task.model_copy(deep=True)
                    for task in state.tasks
                    if task.status is not TaskStatus.COMPLETED
                ],
                "next_action": None,
            },
            deep=True,
        )

    def _ensure_block_ceiling(self, state: WorkingState) -> WorkingState:
        tokens = self._estimator.estimate(
            envelope_items(working_state_items(state)),
            "working-state",
        )
        if tokens > self._limits.block_ceiling_tokens:
            raise WorkingStateLimitError("working-state token ceiling reached")
        return state

    def transition(self, current: WorkingState, raw_update: Mapping[str, Any]) -> WorkingState:
        update = WorkingStateUpdate.model_validate(raw_update)
        objective = current.objective
        next_action = current.next_action
        if "objective" in update.model_fields_set:
            objective = update.objective
        if "next_action" in update.model_fields_set:
            next_action = update.next_action

        constraints = list(current.constraints)
        for constraint in update.add_constraints:
            value = constraint.strip()
            if value and value not in constraints:
                constraints.append(value)
        if len(constraints) > self._limits.max_constraints:
            raise WorkingStateLimitError("working-state constraint cap reached")

        tasks = {task.task_id: task.model_copy(deep=True) for task in current.tasks}
        for task_update in update.upsert_tasks:
            source_event_ids = sorted(set(task_update.source_event_ids))
            existing = tasks.get(task_update.task_id)
            if existing is not None and (
                existing.description == task_update.description
                and existing.status is task_update.status
                and existing.source_event_ids == source_event_ids
                and existing.trust_level is TrustLevel.EXTERNAL_UNTRUSTED
            ):
                continue
            tasks[task_update.task_id] = TaskState(
                task_id=task_update.task_id,
                description=task_update.description,
                status=task_update.status,
                source_event_ids=source_event_ids,
                trust_level=TrustLevel.EXTERNAL_UNTRUSTED,
                updated_at=self._clock.now(),
            )
        open_tasks = [task for task in tasks.values() if task.status is not TaskStatus.COMPLETED]
        if len(open_tasks) > self._limits.max_open_tasks:
            raise WorkingStateLimitError("working-state open-task cap reached")

        facts = [fact.model_copy(deep=True) for fact in current.established_facts]
        known_facts = {(fact.statement, tuple(fact.source_event_ids)) for fact in facts}
        for fact_update in update.add_facts:
            key = (fact_update.statement, tuple(sorted(set(fact_update.source_event_ids))))
            if key in known_facts:
                continue
            facts.append(
                Fact(
                    statement=fact_update.statement,
                    source_event_ids=list(key[1]),
                    # Model-authored state never upgrades its own trust label.
                    trust_level=TrustLevel.EXTERNAL_UNTRUSTED,
                    established_at=self._clock.now(),
                )
            )
            known_facts.add(key)
        if len(facts) > self._limits.max_established_facts:
            raise WorkingStateLimitError("working-state fact cap reached")

        resolved = set(update.resolve_questions)
        questions = [question for question in current.open_questions if question not in resolved]
        if len(questions) > self._limits.max_open_questions:
            raise WorkingStateLimitError("working-state question cap reached")

        state = WorkingState(
            objective=objective,
            constraints=constraints,
            tasks=sorted(tasks.values(), key=lambda task: task.task_id),
            established_facts=facts,
            open_questions=questions,
            next_action=next_action,
        )
        return self._ensure_block_ceiling(state)

    def add_question(self, current: WorkingState, question: str) -> WorkingState:
        if not question.strip() or len(question) > 4096:
            raise WorkingStateLimitError("working-state question is empty or too long")
        questions = list(current.open_questions)
        if question not in questions:
            questions.append(question)
        if len(questions) > self._limits.max_open_questions:
            raise WorkingStateLimitError("working-state question cap reached")
        return self._ensure_block_ceiling(
            current.model_copy(update={"open_questions": questions}, deep=True)
        )

    @staticmethod
    def resolve_question(current: WorkingState, question: str | None) -> WorkingState:
        if question is None:
            return current.model_copy(deep=True)
        return current.model_copy(
            update={
                "open_questions": [item for item in current.open_questions if item != question]
            },
            deep=True,
        )
