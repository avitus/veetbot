"""The same recovery transaction must hold for memory and PostgreSQL repositories."""

from decimal import Decimal
from typing import cast
from uuid import UUID, uuid5

import pytest
from sqlalchemy import null, update

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.people import InMemoryPeopleStore, PostgresPeopleStore
from agent_core.adapters.persistence.sqlalchemy_models import PeopleHeadRow
from agent_core.application.reconsolidation import PublicReconsolidationService
from agent_core.domain.dreaming_recovery import LegacyAttributionManifest, LegacyAttributionProof
from agent_core.domain.errors import AuthorizationError, ConflictError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import Person, PersonMemoryLink
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Stores
from tests.contract.reconsolidation_source_cases import seed_sources
from tests.contract.support import NOW, principal


async def fixture(
    factory: UnitOfWorkFactory,
) -> tuple[PublicReconsolidationService, LegacyAttributionManifest]:
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    person = Person(
        id=UUID(int=7001),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Fabricated repair",
        created_at=NOW,
        updated_at=NOW,
    )
    link = PersonMemoryLink(
        id=uuid5(UUID(int=503), f"{person.id}:subject"),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        person_id=person.id,
        belief_id=UUID(int=503),
        role="subject",
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory() as uow:
        await seed_sources(cast(Stores, uow))
        await uow.memories.upsert_belief(
            memory(belief_id=503).model_copy(
                update={"store_position": await uow.memories.next_position()}
            )
        )
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(link, expected_revision=0)
        await uow.people.erase(owner, [person.id])
        if isinstance(uow.people, InMemoryPeopleStore):
            for key in (person.id, link.id):
                del uow.people._attributions[(owner.tenant_id, owner.principal_id, key)]
        else:
            assert isinstance(uow.people, PostgresPeopleStore)
            await uow.people._session.execute(
                update(PeopleHeadRow)
                .where(
                    PeopleHeadRow.tenant_id == owner.tenant_id,
                    PeopleHeadRow.principal_id == owner.principal_id,
                    PeopleHeadRow.id.in_([person.id, link.id]),
                )
                .values(memory_attribution=null())
            )
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    boundary = await service.enable_forward_dreaming(owner)
    manifest = LegacyAttributionManifest(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        legacy_digest=boundary.legacy_digest,
        proofs=(
            LegacyAttributionProof(record_id=person.id, revision=1, kind="person"),
            LegacyAttributionProof(
                record_id=link.id,
                revision=1,
                kind="memory_link",
                belief_id=link.belief_id,
                person_id=person.id,
                role="subject",
            ),
        ),
    )
    return service, manifest


async def recover_old_sources(factory: UnitOfWorkFactory) -> None:
    service, manifest = await fixture(factory)
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    before = await service.dreaming_status(owner)
    async with factory() as uow:
        originals = [await uow.memories.get(UUID(int=i), owner) for i in (501, 502, 503)]
        job = await uow.reconsolidation.claim_due(owner, NOW, "before-recovery")
        assert job is not None
        reservation = await uow.reconsolidation.reserve(
            owner, job.lease_token, "a" * 64, Decimal("0.10"), NOW
        )
        spend = await uow.reconsolidation.settle(
            owner, job.lease_token, reservation.id, Decimal("0.04"), NOW
        )
        page = await uow.reconsolidation.inventory(owner, job.lease_token, NOW)
        assert page.sources == ()
        await uow.reconsolidation.checkpoint(owner, job.lease_token, page, (), NOW)
        await uow.reconsolidation.release(owner, job.lease_token, NOW)
    preview = await service.recover_legacy_dreaming(owner, manifest)
    assert preview.records == 2 and not preview.applied
    assert await service.dreaming_status(owner) == before
    result = await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    assert result.applied and not result.already_applied
    status = await service.dreaming_status(owner)
    assert status.forward_only_since is None and status.blockers == ()
    assert status.schedule == before.schedule and status.runs == before.runs
    async with factory() as uow:
        assert await uow.reconsolidation.get_spend(owner, spend.id) == spend
        history = await uow.people.forward_dreaming_boundary(owner, include_recovered=True)
        assert history is not None and history.activated_at == NOW and history.recovered_at == NOW
        assert history.legacy_digest == manifest.legacy_digest
        for proof in manifest.proofs:
            assert await uow.people.is_erased(owner, proof.record_id)
            assert (
                await uow.people.get(owner, proof.record_id, ceiling=Sensitivity.RESTRICTED) is None
            )
        assert [await uow.memories.get(UUID(int=i), owner) for i in (501, 502, 503)] == originals
        job = await uow.reconsolidation.claim_due(owner, NOW, "after-recovery")
        assert job is not None
        page = await uow.reconsolidation.inventory(owner, job.lease_token, NOW)
        assert {s.belief_id for s in page.sources} == {UUID(int=501), UUID(int=502), UUID(int=503)}
        selected = await uow.reconsolidation.selectable_sources(owner, page.sources, NOW)
        assert {s.belief_id for s in selected} == {UUID(int=501), UUID(int=502)}
        assert await uow.reconsolidation.selection_input(owner, selected, NOW) is not None
        await uow.reconsolidation.checkpoint(owner, job.lease_token, page, (), NOW)
        await uow.reconsolidation.release(owner, job.lease_token, NOW)
    again = await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    assert again.already_applied
    # A repeated recovery cannot reopen completed same-day work or mint a new budget.
    async with factory() as uow:
        assert await uow.reconsolidation.claim_due(owner, NOW, "retry") is None
    assert await service.enable_forward_dreaming(owner) == history


async def recovery_refusal_and_rollback(factory: UnitOfWorkFactory) -> None:
    service, manifest = await fixture(factory)
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    before = await service.dreaming_status(owner)
    with pytest.raises(AuthorizationError):
        await service.recover_legacy_dreaming(principal(), manifest, confirm=True)
    with pytest.raises(ConflictError):
        await service.recover_legacy_dreaming(
            owner.model_copy(update={"principal_id": "other"}), manifest, confirm=True
        )
    with pytest.raises(ConflictError, match="cover exactly"):
        await service.recover_legacy_dreaming(
            owner, manifest.model_copy(update={"proofs": manifest.proofs[:1]}), confirm=True
        )
    forged = manifest.proofs[1].model_copy(update={"role": "object"})
    with pytest.raises(ConflictError, match="Memory-link"):
        await service.recover_legacy_dreaming(
            owner,
            manifest.model_copy(update={"proofs": (manifest.proofs[0], forged)}),
            confirm=True,
        )
    async with factory() as uow:
        assert not await uow.people.memory_attribution_complete(owner)
        job = await uow.reconsolidation.claim_due(owner, NOW, "inflight")
        assert job is not None
    with pytest.raises(ConflictError, match="Dreaming is running"):
        await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    async with factory() as uow:
        await uow.reconsolidation.release(owner, job.lease_token, NOW)
    with pytest.raises(RuntimeError, match="rollback"):
        async with factory() as uow, uow.people.lock(owner):
            await uow.people.recover_legacy_attribution(owner, manifest, {}, NOW, confirm=True)
            await uow.reconsolidation.restart_full_inventory(owner)
            raise RuntimeError("rollback")
    assert await service.dreaming_status(owner) == before
    async with factory() as uow:
        assert not await uow.people.memory_attribution_complete(owner)


async def recover_deleted_session_sources(factory: UnitOfWorkFactory) -> None:
    from agent_core.domain.people import PeopleSource, PersonMention
    from agent_core.domain.people_sources import source_id
    from tests.contract.support import session

    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    deleted_session = UUID(int=7550)
    source = source_id(owner, deleted_session, 10)
    mention = uuid5(source, "mention:2:8")
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": deleted_session}))
        await uow.people.put(
            PeopleSource(
                id=source,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                session_id=deleted_session,
                event_sequence=10,
                source_kind="owner",
                evidence_at=NOW,
                source_revision="synthetic",
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
        await uow.people.put(
            PersonMention(
                id=mention,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                source_id=source,
                start=2,
                end=8,
                support_ids=[source],
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
        assert await uow.session_deletions.delete(deleted_session, owner, NOW)
        if isinstance(uow.people, InMemoryPeopleStore):
            for key in (source, mention):
                del uow.people._attributions[(owner.tenant_id, owner.principal_id, key)]
        else:
            assert isinstance(uow.people, PostgresPeopleStore)
            await uow.people._session.execute(
                update(PeopleHeadRow)
                .where(
                    PeopleHeadRow.tenant_id == owner.tenant_id,
                    PeopleHeadRow.principal_id == owner.principal_id,
                    PeopleHeadRow.id.in_([source, mention]),
                )
                .values(memory_attribution=null())
            )
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    boundary = await service.enable_forward_dreaming(owner)
    manifest = LegacyAttributionManifest(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        legacy_digest=boundary.legacy_digest,
        proofs=(
            LegacyAttributionProof(
                record_id=source,
                revision=1,
                kind="source",
                session_id=deleted_session,
                event_sequence=10,
            ),
            LegacyAttributionProof(
                record_id=mention, revision=1, kind="mention", source_id=source, start=2, end=8
            ),
        ),
    )
    preview = await service.recover_legacy_dreaming(owner, manifest)
    assert preview.records == 2 and not preview.applied
    assert (await service.recover_legacy_dreaming(owner, manifest, confirm=True)).applied
    async with factory() as uow:
        assert await uow.people.memory_attribution_complete(owner)
        assert await uow.people.source_suppressed(owner, source)
        assert await uow.people.get(owner, source, ceiling=Sensitivity.RESTRICTED) is None
        assert await uow.people.get(owner, mention, ceiling=Sensitivity.RESTRICTED) is None
        assert await uow.session_deletions.deleted_at(owner, deleted_session) == NOW
        assert (
            await uow.session_deletions.deleted_at(
                owner.model_copy(update={"principal_id": "other"}), deleted_session
            )
            is None
        )
        assert await uow.people.memory_attribution_records(owner, UUID(int=501), (source,)) is None


SCENARIOS = (recover_old_sources, recovery_refusal_and_rollback, recover_deleted_session_sources)
