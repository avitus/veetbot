"""Offline original-only retrieval observations; never answer or activation evidence."""

from __future__ import annotations

import hashlib
import importlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from agent_core.config import (
    AuthMode,
    DeploymentMode,
    MemoryProviderExtractionMode,
    SandboxMechanism,
    Settings,
)
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecalledBelief,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.evals.memory_reconsolidation import Case, Observation, Seed, StrictValue
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION, DeterministicQueryFormer
from agent_core.policy.scopes import PLATFORM_SCOPES

CONTROL_VERSION: Literal["reconsolidation-control@1"] = "reconsolidation-control@1"
CONTROL_AT = datetime(2026, 10, 2, 12, tzinfo=UTC)
PRINCIPAL = Principal(
    tenant_id="evaluation",
    principal_id="reconsolidation-control",
    roles={"evaluator"},
    scopes=set(PLATFORM_SCOPES),
)


class ControlBudget(StrictValue):
    tokens: int = Field(default=2000, ge=1, le=16384)
    items: int = Field(default=20, ge=1, le=100)
    min_score: float = Field(default=0.12, ge=0, le=1)
    scope: str = "general"


class RuntimeProbe(StrictValue):
    id: str
    question: str


class RuntimeCase(StrictValue):
    id: str
    seeds: tuple[Seed, ...]
    probes: tuple[RuntimeProbe, ...]


def runtime_case(case: Case) -> RuntimeCase:
    """Remove all expected answers, surviving IDs, merge and hypothesis labels."""
    return RuntimeCase(
        id=case.id,
        seeds=case.seeds,
        probes=tuple(RuntimeProbe(id=p.id, question=p.question) for p in case.probes),
    )


class OriginalSnapshot(StrictValue):
    seed_id: str
    record: MemoryRecord


class ProbeTrace(StrictValue):
    probe_id: str
    query: RecallQuery
    rendered: str
    rendered_sha256: str
    items: tuple[RecalledBelief, ...]
    returned_ids: tuple[str, ...]
    dropped_ids: tuple[str, ...]
    blocked_ids: tuple[str, ...]
    candidates: int
    tokens: int


class ControlCaseResult(StrictValue):
    case_id: str
    repeat: int = Field(ge=1, le=3)
    observation: Observation
    originals: tuple[OriginalSnapshot, ...] = ()
    traces: tuple[ProbeTrace, ...] = ()
    expired: int = 0
    decayed: int = 0
    retired: int = 0
    failure: Literal["runtime_failure", "invalid_trace", "invalid_seed"] | None = None
    implementation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


async def collect_case(
    case: RuntimeCase, *, repeat: int = 1, budget: ControlBudget | None = None
) -> ControlCaseResult:
    """Seed admitted originals, maintain once, and read actual persisted recall traces.

    Neither gold labels nor an answer model are reachable from this runtime path.
    Every case owns a fresh composition; the caller retains failures in the census.
    """
    budget = budget or ControlBudget()
    try:
        if not case.seeds or any(seed.evidence_at > CONTROL_AT for seed in case.seeds):
            return _failed(case, repeat, "invalid_seed")
        bootstrap = importlib.import_module("agent_core.bootstrap")
        with tempfile.TemporaryDirectory(prefix="agent-recon-control-") as temporary:
            settings = Settings(
                database_url="postgresql+asyncpg://127.0.0.1:1/unused",
                deployment_mode=DeploymentMode.DEVELOPMENT,
                auth_mode=AuthMode.DEV,
                auth_token=None,
                sandbox=SandboxMechanism.FAKE,
                config_dir=None,
                credentials={},
                interpolation={"OPENAI_MODEL": ""},
                memory_provider_extraction_mode=MemoryProviderExtractionMode.OFF,
                artifact_root=Path(temporary) / "artifacts",
            )
            async with bootstrap.build(
                settings=settings,
                storage="memory",
                principal=PRINCIPAL,
                fixed_clock_at=min(seed.evidence_at for seed in case.seeds),
                sequential_ids=True,
                enabled_tools=[],
                enabled_skills=[],
                script=FakeModelScript(turns=[ScriptedTurn(text="unused")]),
            ) as composition:
                clock = composition.clock
                keys = await _seed(composition, case, clock)
                clock.advance(CONTROL_AT - clock.now())
                for seed in case.seeds:
                    if seed.status == "deleted":
                        await composition.memory.delete(_belief_id(case.id, seed.id))
                expired = await composition.memory.expire()
                decay = await composition.memory.decay()
                async with composition.uow_factory() as uow:
                    records = await uow.memories.list_memories(
                        PRINCIPAL, include_inactive=True, limit=129
                    )
                originals = tuple(
                    sorted(
                        (
                            OriginalSnapshot(seed_id=keys[record.id], record=record)
                            for record in records
                        ),
                        key=lambda row: row.seed_id,
                    )
                )
                traces = tuple(
                    [await _probe(composition, probe, keys, budget) for probe in case.probes]
                )
                return ControlCaseResult(
                    case_id=case.id,
                    repeat=repeat,
                    observation=Observation(
                        case_id=case.id,
                        surviving_ids=tuple(
                            row.seed_id
                            for row in originals
                            if row.record.status in LIVE_MEMORY_STATUSES
                        ),
                    ),
                    originals=originals,
                    traces=traces,
                    expired=len(expired),
                    decayed=decay.decayed,
                    retired=decay.retired,
                )
    except _InvalidTraceError:
        return _failed(case, repeat, "invalid_trace")
    except Exception:
        # Exception text may carry source prose or credentials. Cancellation propagates.
        return _failed(case, repeat, "runtime_failure")


def _failed(
    case: RuntimeCase,
    repeat: int,
    failure: Literal["runtime_failure", "invalid_trace", "invalid_seed"],
) -> ControlCaseResult:
    return ControlCaseResult(
        case_id=case.id, repeat=repeat, observation=Observation(case_id=case.id), failure=failure
    )


def _belief_id(case_id: str, seed_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"veetbot:recon-control:{case_id}:{seed_id}")


async def _seed(composition: Any, case: RuntimeCase, clock: Any) -> dict[UUID, str]:
    sessions = {
        key: await composition.sessions.create() for key in sorted({s.session for s in case.seeds})
    }
    events: dict[tuple[str, int], list[Seed]] = {}
    for seed in case.seeds:
        events.setdefault((seed.session, seed.event), []).append(seed)
    source_ids: dict[tuple[str, int], int] = {}
    for key, sources in sorted(events.items(), key=lambda pair: (pair[1][0].evidence_at, pair[0])):
        first = sources[0]
        if any(
            (s.evidence_at, s.attribution) != (first.evidence_at, first.attribution)
            for s in sources
        ):
            raise ValueError("inconsistent original event")
        clock.advance(first.evidence_at - clock.now())
        async with composition.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=sessions[first.session],
                    run_id=None,
                    event_type="user.message.created"
                    if first.attribution == "owner"
                    else "evaluation.source.admitted",
                    actor_type="principal" if first.attribution == "owner" else "external",
                    actor_id=PRINCIPAL.principal_id
                    if first.attribution == "owner"
                    else first.attribution,
                    payload={"content": "\n".join(dict.fromkeys(s.statement for s in sources))},
                )
            )
            source_ids[key] = event.sequence
    keys: dict[UUID, str] = {}
    async with composition.uow_factory() as uow:
        for seed in case.seeds:
            owner = seed.attribution == "owner"
            scope = seed.scope if owner else f"evaluation:source:{seed.session}"
            identifier = _belief_id(case.id, seed.id)
            await uow.memories.upsert_belief(
                MemoryRecord(
                    id=identifier,
                    tenant_id=PRINCIPAL.tenant_id,
                    principal_id=PRINCIPAL.principal_id,
                    scope=scope,
                    subject=seed.subject,
                    statement=seed.statement,
                    source_session_id=sessions[seed.session],
                    source_event_ids=[source_ids[(seed.session, seed.event)]],
                    confidence=0.9 if owner else 0.35,
                    sensitivity=Sensitivity.INTERNAL if owner else Sensitivity.SENSITIVE,
                    valid_from=seed.evidence_at,
                    expires_at=(
                        CONTROL_AT
                        if seed.status == "expired"
                        else None
                        if owner
                        else seed.evidence_at + timedelta(days=30)
                    ),
                    status=MemoryStatus.ACTIVE if owner else MemoryStatus.PROVISIONAL,
                    belief_type=BeliefType.FACT,
                    portability=Portability.PORTABLE
                    if owner and scope == "user"
                    else Portability.LOCAL,
                    origin_scopes=[scope],
                    last_evidence_at=seed.evidence_at,
                    last_reinforced_at=seed.evidence_at,
                    formation_run_id=uuid5(identifier, "formation"),
                    consolidation_policy_version="evaluation-admitted@1",
                    authority=MemoryAuthority.USER if owner else MemoryAuthority.INFERRED,
                    store_position=await uow.memories.next_position(),
                    created_at=seed.evidence_at,
                    updated_at=seed.evidence_at,
                )
            )
            keys[identifier] = seed.id
    return keys


class _InvalidTraceError(ValueError):
    pass


async def _probe(
    composition: Any, probe: RuntimeProbe, keys: dict[UUID, str], budget: ControlBudget
) -> ProbeTrace:
    former = DeterministicQueryFormer(
        PRINCIPAL,
        current_scope=budget.scope,
        budget_tokens=budget.tokens,
        max_items=budget.items,
        min_score=budget.min_score,
    )
    (query,) = former.from_text(probe.question)
    query = query.model_copy(update={"as_of": CONTROL_AT})
    session_id = await composition.sessions.create()
    result = await composition.memory_retriever.recall(query, session_id=session_id)
    async with composition.uow_factory() as uow:
        trace = await uow.traces.get(result.trace_id, PRINCIPAL)
    if (
        trace.query != query
        or trace.session_id != session_id
        or (trace.tenant_id, trace.principal_id) != (PRINCIPAL.tenant_id, PRINCIPAL.principal_id)
        or trace.retrieval_policy_version != RETRIEVAL_POLICY_VERSION
        or trace.rendered != result.rendered
        or trace.rendered_sha256 != hashlib.sha256(trace.rendered.encode()).hexdigest()
        or trace.beliefs != result.items
        or trace.returned != [item.belief_id for item in trace.beliefs]
        or any(not isinstance(item, RecalledBelief) or item.merge_id for item in trace.beliefs)
        or result.tokens > budget.tokens
        or len(trace.returned) > budget.items
    ):
        raise _InvalidTraceError()
    return ProbeTrace(
        probe_id=probe.id,
        query=trace.query,
        rendered=trace.rendered,
        rendered_sha256=trace.rendered_sha256,
        items=tuple(RecalledBelief.model_validate(item.model_dump()) for item in trace.beliefs),
        returned_ids=tuple(keys[key] for key in trace.returned),
        dropped_ids=tuple(keys[key] for key in trace.dropped_for_budget),
        blocked_ids=tuple(keys[key] for key in trace.blocked),
        candidates=trace.candidates,
        tokens=result.tokens,
    )
