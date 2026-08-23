"""The recall delta and the correction lines the builder assembles beside it.

These pin the Region B behavior specified in
docs/plan/memory-evaluation-and-lifecycle.md#the-recall-delta-and-correction-lines
and docs/plan/context-engine.md: a session whose snapshot is frozen takes a
second, position-bounded recall, drops the beliefs the base recall already
carries, and places the corrections to its own snapshot in the fixed body where
budget pressure cannot yield them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextBudget, ContextPlan
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryCorrection,
    MemoryStatus,
    Portability,
    RecalledBelief,
    RecallProfile,
    RecallQuery,
    RecallResult,
    Sensitivity,
)
from agent_core.domain.messages import ModelRequest, TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.domain.sessions import Session
from tests.contract.support import NOW, agent, principal, run, session

SNAPSHOT_TRACE = UUID(int=970)
BASE_BELIEF = UUID(int=971)
DELTA_BELIEF = UUID(int=972)
CORRECTED_BELIEF = UUID(int=973)
REPLACEMENT_BELIEF = UUID(int=974)


def _budget(*, total: int = 32_768) -> ContextBudget:
    return ContextBudget(
        total_tokens=total,
        reserve_output_tokens=4_096,
        platform_tokens=2_000,
        agent_tokens=4_000,
        tool_tokens=6_000,
        skill_catalog_tokens=1_500,
        skill_body_tokens=6_000,
        retrieved_context_tokens=3_500,
        history_tokens=18_000,
        working_state_tokens=1_000,
        tool_result_tokens=4_000,
        knowledge_tokens=3_000,
    )


def _working_state() -> WorkingStateManager:
    return WorkingStateManager(
        FixedClock(NOW),
        {
            "max_constraints": 20,
            "max_open_tasks": 30,
            "max_established_facts": 40,
            "max_open_questions": 20,
            "block_ceiling_tokens": 1_000,
        },
        ConservativeTokenEstimator(),
    )


class _PlanStore:
    def __init__(self, plan: ContextPlan) -> None:
        self.value = plan

    async def current(self, session_id: UUID) -> ContextPlan | None:
        assert session_id == self.value.session_id
        return self.value.model_copy(deep=True)

    async def plan(
        self,
        session: Session,
        agent: AgentSpec,
        principal: Principal,
        model: object,
    ) -> ContextPlan:
        del session, agent, principal, model
        return self.value.model_copy(deep=True)

    async def rotate(self, session_id: UUID, reason: str) -> ContextPlan:
        del session_id, reason
        return self.value.model_copy(deep=True)


class _QueryFormer:
    def form(
        self,
        active_run: object,
        working_state: object,
        message: str | None,
        *,
        current_scope: str | None = None,
    ) -> list[RecallQuery]:
        del active_run, working_state, message
        return [
            RecallQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                current_scope=current_scope or "project-a",
                text="concise answers",
                subjects=["answer style"],
                budget_tokens=500,
                max_items=5,
                min_score=0.1,
            )
        ]


def _belief(belief_id: UUID, statement: str) -> RecalledBelief:
    return RecalledBelief(
        belief_id=belief_id,
        subject="answer style",
        statement=statement,
        belief_type=BeliefType.PREFERENCE,
        status=MemoryStatus.ACTIVE,
        confidence_band="high",
        authority=MemoryAuthority.USER,
        origin_scope="project-a",
        portability=Portability.PORTABLE,
        sensitivity=Sensitivity.INTERNAL,
        valid_from=NOW,
        score=1.0,
        arms=["structured"],
        source_event_ids=[1],
    )


class _Retriever:
    """A retriever that answers the base and delta queries differently."""

    def __init__(self) -> None:
        self.calls = 0
        self.queries: list[RecallQuery] = []
        self.correction_calls: list[tuple[UUID, int]] = []

    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
    ) -> RecallResult:
        del session_id, run_id, turn_id, surface_id
        self.calls += 1
        self.queries.append(query)
        assert moment == "in_turn"
        if query.min_store_position:
            # The delta restates one belief the base already carries, which the
            # builder must drop, and one the base never reached.
            return RecallResult(
                items=[
                    _belief(BASE_BELIEF, "User prefers concise answers"),
                    _belief(DELTA_BELIEF, "User deploys on Fridays"),
                ],
                rendered="<memory>delta</memory>",
                tokens=12,
                truncated=False,
                trace_id=UUID(int=976),
                watermark=12,
            )
        return RecallResult(
            items=[_belief(BASE_BELIEF, "User prefers concise answers")],
            rendered="<memory>base</memory>",
            tokens=12,
            truncated=False,
            trace_id=UUID(int=975),
            watermark=12,
        )

    async def corrections(
        self,
        *,
        snapshot_id: UUID,
        watermark: int,
        as_of: datetime | None = None,
    ) -> list[MemoryCorrection]:
        del as_of
        self.correction_calls.append((snapshot_id, watermark))
        return [
            MemoryCorrection(
                belief_id=CORRECTED_BELIEF,
                replacement_id=REPLACEMENT_BELIEF,
                ended_at=NOW,
            )
        ]


def _builder(
    retriever: _Retriever,
    *,
    snapshot_id: UUID | None = SNAPSHOT_TRACE,
    watermark: int = 7,
    total_tokens: int = 32_768,
) -> BudgetedContextBuilder:
    configured = agent()
    estimator = ConservativeTokenEstimator()
    prefix = build_prefix(configured, [], memory_snapshot="")
    plan = ContextPlan(
        session_id=session().id,
        epoch=1,
        prefix_sha256=hashlib.sha256(prefix_bytes(prefix, [])).hexdigest(),
        prefix_tokens=estimator.estimate(prefix, "fake:scripted"),
        model_id="fake:scripted",
        tool_names=(),
        tool_specs=(),
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        snapshot_id=snapshot_id,
        snapshot_watermark=watermark,
        memory_snapshot="",
        policy_version="milestone-16-test",
        builder_version="context-builder@2",
        budget=_budget(total=total_tokens),
        created_at=NOW,
    )
    return BudgetedContextBuilder(
        _PlanStore(plan),
        estimator,
        FixedClock(NOW),
        _working_state(),
        retriever,
        _QueryFormer(),
    )


def _checkpoint(text: str = "How should you answer?") -> RunCheckpoint:
    active = run(status=RunStatus.RUNNING)
    return RunCheckpoint(
        run_id=active.id,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text=text)])],
        created_at=NOW,
    )


def _correction_line() -> str:
    return (
        f"correction: [m:{str(CORRECTED_BELIEF)[:8]}] no longer holds as of "
        f"{NOW.isoformat().replace('+00:00', 'Z')}; "
        f"superseded by [m:{str(REPLACEMENT_BELIEF)[:8]}]."
    )


def _memory_texts(request: ModelRequest) -> list[str]:
    """The memory-trust body messages, still inside their trust envelopes."""

    return [
        "\n".join(part.text for part in item.content if isinstance(part, TextPart))
        for item in request.conversation
        if isinstance(item, UserMessage) and item.trust is TrustLevel.MEMORY
    ]


async def test_builder_injects_delta_and_correction_lines_without_yielding_them() -> None:
    """A frozen snapshot buys a second recall, deduped, plus its corrections.

    The delta is the base query taken again with no text over positions past
    the snapshot watermark, so the belief the base already carries is dropped
    rather than stated twice. The corrections sit in their own memory-trust
    message before the current user turn, and budget pressure that drops both
    recall blocks leaves them exactly where they were.
    """

    retriever = _Retriever()
    builder = _builder(retriever)
    active = run(status=RunStatus.RUNNING)
    checkpoint = _checkpoint()

    request = await builder.build(active, checkpoint, agent(), principal())

    assert retriever.calls == 2
    base_query, delta_query = retriever.queries
    assert (base_query.text, base_query.subjects) == ("concise answers", ["answer style"])
    assert base_query.min_store_position == 0
    assert (delta_query.text, delta_query.subjects) == (None, [])
    assert delta_query.profile is RecallProfile.CORE
    assert delta_query.min_store_position == 7
    assert retriever.correction_calls == [(SNAPSHOT_TRACE, 7)]

    texts = _memory_texts(request)
    assert len(texts) == 3
    assert "<memory>base</memory>" in texts[0]
    # The delta block is re-rendered without the belief the base already holds.
    assert "User deploys on Fridays" in texts[1]
    assert "User prefers concise answers" not in texts[1]
    assert _correction_line() in texts[2]
    assert request.metadata["context_origin_trust"] == TrustLevel.MEMORY.value


async def test_budget_pressure_drops_both_recall_blocks_and_keeps_the_corrections() -> None:
    """Recall yields; the correction lines are fixed body and never do."""

    roomy = _builder(_Retriever())
    assembled = await roomy.assemble(
        run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
    )
    # One token less than the roomy assembly needs is exactly the pressure the
    # recall blocks exist to absorb.
    squeezed_builder = _builder(_Retriever(), total_tokens=assembled.pressure.total_tokens - 1)

    squeezed = await squeezed_builder.assemble(
        run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
    )

    assert assembled.pressure.yield_steps == ()
    assert squeezed.pressure.yield_steps[0] == "recall"
    assert squeezed.pressure.fits
    texts = _memory_texts(squeezed.request)
    assert len(texts) == 1
    assert _correction_line() in texts[0]
    assert "<memory>" not in texts[0]
    assert squeezed.request.metadata["context_origin_trust"] == TrustLevel.MEMORY.value


async def test_a_session_without_a_snapshot_takes_one_recall_and_no_corrections() -> None:
    """The delta belongs to a frozen snapshot; a plan without one keeps its single call."""

    retriever = _Retriever()
    builder = _builder(retriever, snapshot_id=None, watermark=0)

    request = await builder.build(
        run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
    )

    assert retriever.calls == 1
    assert retriever.correction_calls == []
    texts = _memory_texts(request)
    assert len(texts) == 1
    assert "<memory>base</memory>" in texts[0]
