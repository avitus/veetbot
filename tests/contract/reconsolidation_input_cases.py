"""Identical source-only request snapshots from each authenticated repository."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefRejection, RejectionKind, Sensitivity
from agent_core.domain.messages import ModelPricing, ResolvedModel
from agent_core.domain.people import PeopleSource
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation_inputs import EgressSubject, ReconsolidationEgressDecision
from agent_core.memory.reconsolidation_inputs import prepare_proposal_request
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, SESSION_ID, principal


async def complete_original_parts(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    async with factory() as uow:
        result = await uow.reconsolidation.original_input(
            principal(), job.lease_token, group.id, NOW
        )
        assert result is not None, (
            "a claimed group must expose its complete authenticated originals"
        )
        assert result.group == group
        assert tuple(s.version for s in result.sources) == group.sources
        assert len(result.excerpts) == 1  # Two copies of one event remain one event.
        assert result.excerpts[0].text == "I prefer concise answers."
        assert result.excerpts[0].occurred_at == NOW
        assert result.excerpts[0].event_sequence == 1
        inspected = []

        def permit(
            owner: Principal, model: ResolvedModel, subject: EgressSubject, now: datetime
        ) -> ReconsolidationEgressDecision:
            inspected.append(subject)
            return ReconsolidationEgressDecision(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                provider=model.provider,
                model=model.model,
                policy_version="fixture-egress@1",
                assessed_at=now,
                permitted=True,
                sensitivity=Sensitivity.INTERNAL,
                residency="allowed",
            )

        preparation = prepare_proposal_request(
            principal(),
            (result,),
            model=ResolvedModel(
                provider="fake",
                model="test-model",
                resolved_at=NOW,
                pricing=ModelPricing(input_per_mtok=Decimal(1), output_per_mtok=Decimal(2)),
            ),
            egress_policy=permit,
            batch_id=UUID(int=98),
            now=NOW,
        )
        assert preparation.prepared is not None and not preparation.deferred
        assert [(s.kind, s.id) for s in inspected] == [
            *(("memory", s.record.id) for s in result.sources),
            ("excerpt", result.excerpts[0].id),
        ]
        offered = preparation.prepared.context.groups[0]
        assert offered.id == group.id
        assert {s.source.belief_id for s in offered.sources} == {s.belief_id for s in group.sources}
        assert all(s.excerpt_ids == (result.excerpts[0].id,) for s in offered.sources)
        # A caller cannot mutate the store through the returned preview.
        result.sources[0].record.source_event_ids.append(999)
        assert (await uow.memories.get(memory().id, principal())).source_event_ids == [1]


async def text_boundaries(
    factory: Factory, *, content: object, expected: tuple[str, ...] | None
) -> None:
    async with factory() as uow:
        job, group = await seed(
            uow,
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": content, "gold_answer": "NEVER_SERIALIZE"},
            ),
        )
    async with factory() as uow:
        result = await uow.reconsolidation.original_input(
            principal(), job.lease_token, group.id, NOW
        )
        if expected is None:
            assert result is None
        else:
            assert result is not None
            assert tuple(e.text for e in result.excerpts) == expected
            assert len({e.id for e in result.excerpts}) == len(expected)
            assert len({e.event_id for e in result.excerpts}) == 1


async def input_visibility(factory: Factory, *, change: str) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    async with factory() as uow:
        record = await uow.memories.get(memory().id, principal())
        if change == "erased":
            await uow.memories.fence_for_erasure(principal(), [record.id])
        else:
            edits: dict[str, dict[str, Any]] = {
                "sensitive": {"sensitivity": Sensitivity.SENSITIVE},
                "revision": {"statement": "A changed claim"},
                "missing_event": {"source_event_ids": [999]},
            }
            await uow.memories.reinforce(record.model_copy(update=edits[change]))
    async with factory() as uow:
        assert (
            await uow.reconsolidation.original_input(principal(), job.lease_token, group.id, NOW)
            is None
        )


async def input_owner_and_lease(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    async with factory() as uow:
        other = principal().model_copy(update={"principal_id": "other-owner"})
        with pytest.raises(NotFoundError):
            await uow.reconsolidation.original_input(other, job.lease_token, group.id, NOW)
        with pytest.raises(ConflictError):
            await uow.reconsolidation.original_input(principal(), UUID(int=999), group.id, NOW)
        with pytest.raises(ConflictError):
            await uow.reconsolidation.original_input(
                principal(), job.lease_token, group.id, NOW + timedelta(seconds=120)
            )
        assert (
            await uow.reconsolidation.original_input(principal(), job.lease_token, group.id, NOW)
            is not None
        )


async def input_event_actor(factory: Factory, *, actor: str) -> None:
    async with factory() as uow:
        job, group = await seed(
            uow,
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type=actor,
                actor_id=principal().principal_id,
                payload={"content": "Do not treat this as original owner speech."},
            ),
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.original_input(principal(), job.lease_token, group.id, NOW)
            is None
        )


async def input_people_exclusion(factory: Factory) -> None:
    owner = principal()
    async with factory() as uow:
        job, group = await seed(uow)
        async with uow.people.lock(owner):
            await uow.people.put(
                PeopleSource(
                    id=source_id(owner, SESSION_ID, 1),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    sensitivity=Sensitivity.INTERNAL,
                    session_id=SESSION_ID,
                    event_sequence=1,
                    source_kind="owner",
                    evidence_at=NOW,
                    source_revision="test@1",
                    excluded=True,
                ),
                expected_revision=0,
            )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.original_input(owner, job.lease_token, group.id, NOW) is None
        )


async def input_owner_rejection(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        rejected = memory(belief_id=900).model_copy(update={"store_position": 3})
        await uow.memories.upsert_belief(rejected)
        await uow.memories.reject(
            BeliefRejection(
                id=UUID(int=901),
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                belief_id=rejected.id,
                kind=RejectionKind.UNTRUE,
                statement=rejected.statement,
                subject=rejected.subject,
                created_at=NOW,
                statement_sha256="0" * 64,
                belief_type=rejected.belief_type,
                scope=rejected.scope,
            ),
            rejected,
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.original_input(principal(), job.lease_token, group.id, NOW)
            is None
        )


def named(
    function: Callable[..., Awaitable[None]], name: str, **kwargs: object
) -> Callable[[Factory], Awaitable[None]]:
    # Construct named parametrized cases with the same signatures in both adapters.
    async def case(factory: Factory) -> None:
        await function(factory, **kwargs)

    case.__name__ = name
    return case


INPUT_SCENARIOS = [
    complete_original_parts,
    input_owner_and_lease,
    input_people_exclusion,
    input_owner_rejection,
    named(
        text_boundaries,
        "complete_multipart",
        content=[{"kind": "text", "text": "  First\n"}, {"kind": "text", "text": "Second café"}],
        expected=("  First\n", "Second café"),
    ),
    named(text_boundaries, "at_excerpt_byte_limit", content="é" * 1024, expected=("é" * 1024,)),
    named(text_boundaries, "over_excerpt_byte_limit", content="é" * 1025, expected=None),
    named(text_boundaries, "no_mid_sentence_cut", content="x" * 2049, expected=None),
    named(
        text_boundaries,
        "no_attachment_admission",
        content=[
            {"kind": "text", "text": "Owner text"},
            {"kind": "file", "artifact_id": str(UUID(int=90))},
        ],
        expected=None,
    ),
    named(
        text_boundaries,
        "too_many_text_parts",
        content=[{"kind": "text", "text": "Part"}] * 33,
        expected=None,
    ),
    *[
        named(input_visibility, f"input_{change}", change=change)
        for change in ("erased", "sensitive", "revision", "missing_event")
    ],
    *[
        named(input_event_actor, f"input_actor_{actor}", actor=actor)
        for actor in ("assistant", "scheduler", "tool", "external")
    ],
]
