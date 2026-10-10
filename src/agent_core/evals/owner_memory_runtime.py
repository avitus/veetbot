"""A disposable store around the existing M32 pipeline; no production connection."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.memory import (
    InMemoryEventRepository,
    InMemorySessionRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType, MemoryAuthority, MemoryRecord, MemoryStatus
from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel
from agent_core.domain.reconsolidation import ReconsolidationJob
from agent_core.domain.reconsolidation_execution import AppliedGroup, ReconsolidationExecution
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.evals.owner_memory_budget import ExperimentBudget
from agent_core.evals.owner_memory_fixture import (
    FixtureConfirmation,
    FixturePacket,
    validate_confirmation,
)
from agent_core.evals.owner_memory_preflight import preview_selection
from agent_core.memory.reconsolidation import ReconsolidationInventoryPass
from agent_core.memory.reconsolidation_apply import apply_review
from agent_core.memory.reconsolidation_evidence import implementation_digest, model_digest
from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
from agent_core.memory.reconsolidation_policy import local_egress_policy
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.models import ModelProvider


@dataclass(frozen=True)
class FixtureResources:
    factory: MemoryUnitOfWorkFactory
    sessions: InMemorySessionRepository
    events: InMemoryEventRepository
    event_clock: FixedClock
    ids: IdFactory


async def _seed(
    packet: FixturePacket,
    confirmations: tuple[FixtureConfirmation, ...],
    resources: FixtureResources,
) -> tuple[MemoryUnitOfWorkFactory, Principal, list[dict[str, str]]]:
    # Only the standalone launcher constructs these volatile resources. There is
    # no Settings, production database URL or synthetic evidence builder here.
    owner = Principal(
        tenant_id="offline-fixture",
        principal_id=str(packet.experiment_id),
        roles=set(),
        scopes=set(),
    )
    factory = resources.factory
    sessions = resources.sessions
    events = resources.events
    event_clock = resources.event_clock
    ids = resources.ids
    confirmed = {c.reference_id: c for c in confirmations}
    inputs = []
    # Repeated identical statements share one new event: copies add no evidence.
    evidence: dict[str, tuple[UUID, int, str]] = {}
    selected = (s for s in packet.sources if s.reference_id in confirmed)
    for source in sorted(selected, key=lambda s: confirmed[s.reference_id].confirmed_at):
        consent = confirmed[source.reference_id]
        key = source.statement
        if key not in evidence:
            session_id = ids.new_id()
            await sessions.create(
                Session(
                    id=session_id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    agent_id=ids.new_id(),
                    agent_version="1.0.0",
                    status=SessionStatus.ACTIVE,
                    created_at=consent.confirmed_at,
                    updated_at=consent.confirmed_at,
                )
            )
            event_clock.advance(consent.confirmed_at - event_clock.now())
            event = await events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={
                        "content": source.statement,
                        "evaluation_only": True,
                        "input_kind": packet.version,
                        "confirmation_sha256": packet.digest,
                    },
                )
            )
            evidence[key] = (session_id, event.sequence, consent.confirmed_at.isoformat())
        session_id, sequence, occurred = evidence[key]
        # Evidence time is the actual new confirmation, never an old record date.
        at = datetime.fromisoformat(occurred)
        identifier = ids.new_id()
        async with factory() as uow:
            await uow.memories.upsert_belief(
                MemoryRecord(
                    id=identifier,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    subject=source.subject,
                    statement=source.statement,
                    scope=source.scope,
                    portability=source.portability,
                    sensitivity=source.sensitivity,
                    belief_type=BeliefType.FACT,
                    authority=MemoryAuthority.USER,
                    confidence=0.9,
                    status=MemoryStatus.ACTIVE,
                    origin_scopes=[source.scope],
                    source_session_id=session_id,
                    source_event_ids=[sequence],
                    valid_from=at,
                    last_evidence_at=at,
                    last_reinforced_at=at,
                    consolidation_policy_version="owner-confirmed-fixture@1",
                    formation_run_id=ids.new_id(),
                    created_at=at,
                    updated_at=at,
                    store_position=await uow.memories.next_position(),
                )
            )
        inputs.append(
            {
                "reference_id": str(source.reference_id),
                "belief_id": str(identifier),
                "event_id": f"{session_id}:{sequence}",
                "confirmed_at": occurred,
            }
        )
    return factory, owner, inputs


class _FixturePass(ReconsolidationInventoryPass):
    def __init__(
        self,
        factory: MemoryUnitOfWorkFactory,
        clock: Clock,
        owner: Principal,
        *,
        packet: FixturePacket,
        provider: ModelProvider,
        admitted: Callable[[], bool],
        ids: IdFactory,
    ) -> None:
        super().__init__(factory, clock, owner, worker_id="owner-fixture", admitted=admitted)
        self.packet = packet
        self.ids = ids
        self.provider = provider
        self.execution = ReconsolidationExecution(reason="deferred")
        self.applied: tuple[AppliedGroup, ...] = ()
        self.groups = 0

    async def _process(self, lease: ReconsolidationJob) -> int:
        selected = []
        async with self._uow_factory() as uow:
            for _ in range(4):
                group = await uow.reconsolidation.claim_group(
                    self._principal, lease.lease_token, self._clock.now()
                )
                if group is None:
                    break
                selected.append(group.id)
        self.groups = len(selected)
        if not selected:
            return 0
        executor = ReconsolidationBatchExecutor(
            self._uow_factory,
            self._clock,
            self._principal,
            admitted=self._admitted,
            egress_policy=local_egress_policy(self.packet.residency_provider),
        )
        self.execution = await executor.run(
            lease,
            tuple(selected),
            model=self.packet.model,
            provider=self.provider,
            batch_id=self.ids.new_id(),
        )
        if self.execution.prepared is not None and self.execution.review is not None:
            self.applied = await apply_review(
                self._uow_factory,
                self._clock,
                self._principal,
                self.execution.prepared,
                self.execution.review,
                admitted=self._admitted,
            )
        finished = {g.group_id for g in self.applied if g.outcome != "deferred"}
        # Like the maintenance worker, settle every claim before releasing its
        # lease. This disposable experiment never executes the queued retry.
        for group_id in selected:
            if group_id in finished:
                continue
            try:
                async with self._uow_factory() as uow:
                    await uow.reconsolidation.finish_group(
                        self._principal, lease.lease_token, group_id, "retry", self._clock.now()
                    )
            except ConflictError:
                break
        return sum(len(g.operation_ids) for g in self.applied)


class _CountedProvider:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.name = provider.name
        self.calls = 0

    async def stream(
        self, request: ModelRequest, model: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        if self.calls >= 2 or request.maximum_provider_attempts != 1:
            raise ValueError("experiment call bound exceeded")
        self.calls += 1
        async for event in self.provider.stream(request, model, attempt):
            yield event

    async def close(self) -> None:
        await self.provider.close()


async def run_fixture(
    packet: FixturePacket,
    confirmations: tuple[FixtureConfirmation, ...],
    *,
    owner_digest: str,
    model: ResolvedModel,
    submitted_at: datetime,
    provider: ModelProvider,
    budget: ExperimentBudget,
    clock: Clock,
    resources: Callable[[datetime], FixtureResources],
    admitted: Callable[[], bool] = lambda: True,
) -> dict[str, Any]:
    """One explicit attempt; only the returned transient result can contain prose."""
    packet = packet.model_copy(deep=True)
    validate_confirmation(
        packet,
        confirmations,
        owner_digest=owner_digest,
        model=model,
        submitted_at=submitted_at,
        now=clock.now(),
    )
    if not admitted() or provider.name != model.provider:
        raise ValueError("confirmation cancelled or provider changed")
    preview = preview_selection(packet, {c.reference_id for c in confirmations}, clock.now())
    if not preview.ready:
        raise ValueError(preview.reason)
    root = Path(__file__).resolve().parents[3]
    code_digest = implementation_digest(root)
    consent_digest = hashlib.sha256(
        (
            submitted_at.isoformat() + "\n" + "\n".join(c.model_dump_json() for c in confirmations)
        ).encode()
    ).hexdigest()
    budget.reserve(
        packet.experiment_id,
        owner_digest,
        packet.digest,
        clock.now(),
        identities=(model_digest(model), code_digest, consent_digest),
    )
    result: dict[str, Any] = {
        "outcome": "failed",
        "reason": "runtime_failure",
        "calls": 0,
        "operations": [],
        "reviews": [],
        "inputs": [],
        "activation_evidence": False,
        "apply_changes": False,
        "reserved_usd": "0.25",
        "experiment_id": str(packet.experiment_id),
        "packet_digest": packet.digest,
        "model_digest": model_digest(model),
        "implementation_digest": code_digest,
        "consent_digest": consent_digest,
    }
    counted = _CountedProvider(provider)
    sweep = None
    try:
        async with asyncio.timeout(120):
            isolated = resources(min(c.confirmed_at for c in confirmations))
            factory, owner, inputs = await _seed(packet, confirmations, isolated)
            result["inputs"] = inputs
            sweep = _FixturePass(
                factory,
                clock,
                owner,
                packet=packet,
                provider=counted,
                admitted=admitted,
                ids=isolated.ids,
            )
            await sweep.run_once()
            result["groups"] = sweep.groups
            result["application"] = [g.outcome for g in sweep.applied]
            result["reason"] = sweep.execution.reason if sweep.groups else "no_related_groups"
            async with factory() as uow:
                operations = await uow.reconsolidation.operation_page(
                    owner, kind=None, state="committed", before=None, limit=100
                )
                by_id = {row["belief_id"]: row["reference_id"] for row in inputs}
                for operation in operations:
                    original = await uow.memories.get(operation.plan.member_ids[0], owner)
                    if operation.kind in {"summary", "hypothesis"}:
                        value = await uow.reconsolidation.get_summary(
                            owner,
                            operation.id,
                            clock.now(),
                            ceiling=original.sensitivity,
                            current_scope=original.scope,
                        )
                        if value is None:
                            raise ValueError("accepted output unavailable")
                        text = value.content.rendered
                    elif operation.kind == "merge":
                        text = (
                            await uow.memories.get(operation.plan.canonical_id, owner)
                        ).statement
                    else:
                        text = "These statements may conflict; neither was chosen as true."
                    result["operations"].append(
                        {
                            "kind": operation.kind,
                            "text": text,
                            "source_ids": [by_id[str(key)] for key in operation.plan.member_ids],
                        }
                    )
            if sweep.execution.review is not None:
                result["reviews"] = [
                    {
                        "kind": c.operation.kind,
                        "reason": c.reason,
                        "requires_local_validation": c.requires_local_validation,
                    }
                    for c in sweep.execution.review.candidates
                ]
            result["outcome"] = "complete"
    except TimeoutError:
        result.update(outcome="timeout", reason="timeout")
    except asyncio.CancelledError:
        result.update(outcome="cancelled", reason="cancelled", operations=[])
        raise
    except Exception:
        # Provider/storage exceptions can contain private text. Preserve finite failure only.
        result.update(outcome="failed", reason="runtime_failure", operations=[])
    finally:
        result["calls"] = counted.calls
        if sweep is not None:
            result["usage"] = [
                {
                    "stage": c.stage,
                    "reason": c.reason,
                    "charged_usd": str(c.spend.charged_usd),
                    "state": c.spend.state,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                }
                for c in sweep.execution.calls
            ]
        receipt_digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        budget.finish(
            packet.experiment_id,
            outcome=result["outcome"],
            calls=result["calls"],
            receipt_digest=receipt_digest,
            model_digest=result["model_digest"],
            implementation_digest=code_digest,
            consent_digest=consent_digest,
            call_receipts=sweep.execution.calls if sweep is not None else (),
        )
    return result
