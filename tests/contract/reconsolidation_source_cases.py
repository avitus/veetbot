"""Authenticated, read-only merge preparation through each actual repository."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefRejection, RejectionKind, Sensitivity
from agent_core.domain.people import PeopleSource, Person, PersonMemoryLink, PersonMention
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation import ReconsolidationGroup, ReconsolidationJob
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.support import NOW, SESSION_ID, principal


async def retained_owner_evidence(factory: Factory) -> None:
    originals = (memory(), memory(belief_id=502).model_copy(update={"store_position": 2}))
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": "I prefer concise answers."},
            )
        )
        for original in originals:
            await uow.memories.upsert_belief(original)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "merge-test")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        groups = await uow.reconsolidation.checkpoint(
            principal(), job.lease_token, page, (page.sources,), NOW
        )
        claimed = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
        assert claimed == groups[0].model_copy(
            update={
                "state": "claimed",
                "lease_token": job.lease_token,
                "attempts": 1,
            }
        )
    async with factory() as uow:
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, claimed.id, NOW)
        assert plan is not None, "a claimed equivalent group needs authenticated source planning"
        assert plan.canonical_id == originals[0].id
        assert plan.member_ids == tuple(item.id for item in originals)
        assert {item.source_event_ids for item in plan.dependencies} == {(1,)}
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, claimed.id, NOW)
            == plan
        )


async def seed_sources(
    uow: Stores, event: NewEvent | None = None, *, sequences: list[int] | None = None
) -> None:
    await uow.events.append(
        event
        or NewEvent(
            session_id=SESSION_ID,
            run_id=None,
            event_type="user.message.created",
            actor_type="principal",
            actor_id=principal().principal_id,
            payload={"content": "I prefer concise answers."},
        )
    )
    for i in range(2):
        await uow.memories.upsert_belief(
            memory(belief_id=501 + i).model_copy(
                update={
                    "store_position": await uow.memories.next_position(),
                    "source_event_ids": sequences or [1],
                }
            )
        )


async def queue_sources(uow: Stores) -> tuple[ReconsolidationJob, ReconsolidationGroup]:
    job = await uow.reconsolidation.claim_due(principal(), NOW, "merge-test")
    assert job is not None
    page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
    await uow.reconsolidation.checkpoint(principal(), job.lease_token, page, (page.sources,), NOW)
    group = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
    assert group is not None
    return job, group


async def seed(
    uow: Stores, event: NewEvent | None = None, *, sequences: list[int] | None = None
) -> tuple[ReconsolidationJob, ReconsolidationGroup]:
    await seed_sources(uow, event, sequences=sequences)
    return await queue_sources(uow)


async def event_trust(
    factory: Factory,
    *,
    event_type: str = "user.message.created",
    actor_type: str = "principal",
    actor_id: str | None = "principal-a",
    trust: str = "user",
    content: object = "I prefer concise answers.",
    admitted: bool = False,
) -> None:
    async with factory() as uow:
        job, group = await seed(
            uow,
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                payload={"content": content, "trust": trust},
            ),
        )
    async with factory() as uow:
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert (plan is not None) is admitted


async def source_changes(factory: Factory, *, change: str) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    async with factory() as uow:
        original = await uow.memories.get(memory().id, principal())
        if change == "fenced":
            await uow.memories.fence_for_erasure(principal(), [original.id])
        else:
            options: dict[str, dict[str, object]] = {
                "statement": {"statement": "Changed statement"},
                "missing": {"source_event_ids": [2]},
                "utility": {"utility": 0.25, "last_used_at": NOW},
            }
            await uow.memories.reinforce(original.model_copy(update=options[change]))
    async with factory() as uow:
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert (plan is not None) is (change == "utility")


async def unavailable_original_event(factory: Factory) -> None:
    async with factory() as uow:
        # Claims can inventory old atoms even when their leaf no longer exists.
        for i in range(2):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(update={"store_position": i + 1})
            )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "merge-test")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        await uow.reconsolidation.checkpoint(
            principal(), job.lease_token, page, (page.sources,), NOW
        )
        group = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
        assert group is not None
    async with factory() as uow:
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            is None
        )


async def lease_boundaries(factory: Factory, *, boundary: str) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    token, group_id, now = job.lease_token, group.id, NOW
    if boundary == "token":
        token = UUID(int=777)
    if boundary == "group":
        group_id = UUID(int=777)
    if boundary == "deadline":
        now += timedelta(seconds=120)
    if boundary == "finished":
        async with factory() as uow:
            await uow.reconsolidation.finish_group(principal(), token, group.id, "no_change", NOW)
    async with factory() as uow:
        with pytest.raises(NotFoundError if boundary == "group" else ConflictError):
            await uow.reconsolidation.plan_merge(principal(), token, group_id, now)


async def people_attribution(factory: Factory, *, variant: str) -> None:
    owner = principal()
    source = PeopleSource(
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
        source_revision="source@1",
    )
    person = Person(
        id=UUID(int=801),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        sensitivity=Sensitivity.INTERNAL,
        display_name="Alice",
        state="active",
        support_ids=[source.id],
    )
    async with factory() as uow:
        await seed_sources(uow)
        async with uow.people.lock(owner):
            if variant == "source_identity":
                source = source.model_copy(update={"event_sequence": 2})
            await uow.people.put(source, expected_revision=0)
            await uow.people.put(person, expected_revision=0)
            for i in range(2):
                link = PersonMemoryLink(
                    id=UUID(int=811 + i),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    sensitivity=Sensitivity.INTERNAL,
                    person_id=person.id,
                    belief_id=UUID(int=501 + i),
                    support_ids=[source.id],
                )
                if variant == "different" and i == 1:
                    other = person.model_copy(update={"id": UUID(int=802), "display_name": "Bob"})
                    await uow.people.put(other, expected_revision=0)
                    link = link.model_copy(update={"person_id": other.id})
                if variant == "unbacked_link":
                    link = link.model_copy(update={"support_ids": []})
                if variant == "unresolved":
                    link = link.model_copy(update={"unresolved": True})
                if variant == "speaker":
                    link = link.model_copy(update={"role": "speaker"})
                if variant == "sensitive":
                    link = link.model_copy(update={"sensitivity": Sensitivity.SENSITIVE})
                await uow.people.put(link, expected_revision=0)
            if variant in {"hidden_link", "purged_link"}:
                await uow.people.fence_for_erasure(owner, [UUID(int=811), UUID(int=812)])
                if variant == "purged_link":
                    await uow.people.purge_erased(owner)
            if variant == "excluded_source":
                await uow.people.put(
                    source.model_copy(update={"revision": 2, "excluded": True}), expected_revision=1
                )
            if variant == "erased_source":
                await uow.people.fence_for_erasure(owner, [source.id])
            if variant == "unresolved_mention":
                mention = PersonMention(
                    id=UUID(int=820),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    sensitivity=Sensitivity.INTERNAL,
                    source_id=source.id,
                    person_id=None,
                    start=0,
                    end=1,
                    support_ids=[source.id],
                )
                await uow.people.put(mention, expected_revision=0)
        job, group = await queue_sources(uow)
    async with factory() as uow:
        plan = await uow.reconsolidation.plan_merge(owner, job.lease_token, group.id, NOW)
        assert (plan is not None) is (variant == "same")
        for key in (501, 502):
            assert (await uow.memories.get(UUID(int=key), owner)).statement == memory().statement


async def rejection_survives_renaming(factory: Factory, *, tombstone: bool) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        rejected = memory(belief_id=503).model_copy(
            update={"store_position": 3, "subject": "old-subject"}
        )
        await uow.memories.upsert_belief(rejected)
        rejection = BeliefRejection(
            id=UUID(int=901),
            tenant_id=rejected.tenant_id,
            principal_id=rejected.principal_id,
            belief_id=rejected.id,
            kind=RejectionKind.DELETED if tombstone else RejectionKind.UNTRUE,
            subject=rejected.subject,
            statement=None if tombstone else "  USER prefers   CONCISE answers ",
            statement_sha256=hashlib.sha256(rejected.statement.casefold().encode()).hexdigest()
            if tombstone
            else "0" * 64,
            belief_type=rejected.belief_type,
            scope=rejected.scope,
            created_at=NOW,
        )
        if tombstone:
            await uow.memories.delete(rejected.id, principal(), rejection)
        else:
            await uow.memories.reject(rejection, rejected)
    async with factory() as uow:
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            is None
        )


async def leaf_bound(factory: Factory, *, count: int, missing: bool = False) -> None:
    async with factory() as uow:
        job, group = await seed(uow, sequences=list(range(1, count + 1)))
        if not missing:
            for _ in range(count - 1):
                await uow.events.append(
                    NewEvent(
                        session_id=SESSION_ID,
                        run_id=None,
                        event_type="user.message.created",
                        actor_type="principal",
                        actor_id=principal().principal_id,
                        payload={"content": "I prefer concise answers."},
                    )
                )
    async with factory() as uow:
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert (plan is not None) is (count <= 256 and not missing)
        if plan is not None:
            assert all(len(item.source_event_ids) == count for item in plan.dependencies)


async def fresh_people_resolution(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    async with factory() as uow:
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            is not None
        )
    async with factory() as uow, uow.people.lock(principal()):
        source = PeopleSource(
            id=source_id(principal(), SESSION_ID, 1),
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            created_at=NOW,
            updated_at=NOW,
            sensitivity=Sensitivity.INTERNAL,
            session_id=SESSION_ID,
            event_sequence=1,
            source_kind="owner",
            evidence_at=NOW,
            source_revision="source@1",
        )
        await uow.people.put(source, expected_revision=0)
        await uow.people.put(
            PersonMention(
                id=UUID(int=850),
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                created_at=NOW,
                updated_at=NOW,
                sensitivity=Sensitivity.INTERNAL,
                source_id=source.id,
                start=0,
                end=1,
                support_ids=[source.id],
            ),
            expected_revision=0,
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            is None
        )


async def foreign_group_is_hidden(factory: Factory) -> None:
    async with factory() as uow:
        _, group = await seed(uow)
    other = principal().model_copy(update={"principal_id": "other"})
    async with factory() as uow:
        job = await uow.reconsolidation.claim_due(other, NOW, "foreign")
        assert job is not None
        with pytest.raises(NotFoundError):
            await uow.reconsolidation.plan_merge(other, job.lease_token, group.id, NOW)


def configured(
    name: str, function: Callable[..., Awaitable[None]], **kwargs: object
) -> Callable[[Factory], Awaitable[None]]:
    async def scenario(factory: Factory) -> None:
        await function(factory, **kwargs)

    scenario.__name__ = name
    return scenario


MERGE_SCENARIOS = [
    retained_owner_evidence,
    fresh_people_resolution,
    foreign_group_is_hidden,
    configured("leaf_bound", leaf_bound, count=256),
    configured("leaf_overflow", leaf_bound, count=257),
    configured("partial_leaf_loss", leaf_bound, count=2, missing=True),
    unavailable_original_event,
    configured("external_message", event_trust, trust="external_untrusted"),
    configured("unknown_trust", event_trust, trust="unknown"),
    configured("unknown_actor", event_trust, actor_id=None),
    configured("foreign_actor", event_trust, actor_id="other"),
    configured("scheduled_instruction", event_trust, actor_type="scheduler"),
    configured("assistant_prose", event_trust, event_type="assistant.message.completed"),
    configured("tool_result", event_trust, event_type="tool.call.completed"),
    configured("empty_message", event_trust, content=" "),
    configured(
        "owner_text_parts",
        event_trust,
        content=[{"kind": "text", "text": "I prefer concise answers."}],
        admitted=True,
    ),
    *[
        configured(f"source_{value}", source_changes, change=value)
        for value in ("statement", "missing", "fenced", "utility")
    ],
    *[
        configured(f"lease_{value}", lease_boundaries, boundary=value)
        for value in ("token", "group", "deadline", "finished")
    ],
    *[
        configured(f"people_{value}", people_attribution, variant=value)
        for value in (
            "same",
            "different",
            "unresolved",
            "speaker",
            "sensitive",
            "hidden_link",
            "purged_link",
            "source_identity",
            "unbacked_link",
            "excluded_source",
            "erased_source",
            "unresolved_mention",
        )
    ],
    configured("rejection_text", rejection_survives_renaming, tombstone=False),
    configured("rejection_hash", rejection_survives_renaming, tombstone=True),
]
