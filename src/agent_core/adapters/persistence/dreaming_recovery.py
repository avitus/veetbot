"""Transactional metadata recovery; both repositories share the same proof validator."""

from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from agent_core.adapters.persistence.reconsolidation_sources import lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRow,
    PeopleHeadRow,
    PeopleRevisionRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import ForwardDreamingBoundary
from agent_core.domain.dreaming_recovery import (
    LegacyAttributionManifest,
    LegacyRecoveryResult,
    RecoveryEvidence,
    RecoveryHead,
    validate_recovery,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.people import PEOPLE_RECORD, PeopleErasure
from agent_core.domain.reconsolidation_attribution import AttributionFootprint

if TYPE_CHECKING:
    from agent_core.adapters.persistence.people import InMemoryPeopleStore, PostgresPeopleStore


def _receipt_evidence(
    receipts: list[PeopleErasure],
) -> tuple[set[UUID], dict[UUID, frozenset[datetime]]]:
    beliefs: set[UUID] = set()
    sources: dict[UUID, set[datetime]] = defaultdict(set)
    for receipt in receipts:
        if receipt.state not in {"completed", "cleanup_pending"}:
            continue
        beliefs.update(receipt.blocked_belief_ids)
        for source in receipt.blocked_source_ids:
            sources[source].add(receipt.created_at)
    return beliefs, {key: frozenset(times) for key, times in sources.items()}


def _result(
    manifest: LegacyAttributionManifest, confirm: bool, already: bool
) -> LegacyRecoveryResult:
    return LegacyRecoveryResult(
        records=len(manifest.proofs),
        applied=confirm or already,
        already_applied=already,
        manifest_digest=manifest.digest(),
    )


async def recover_memory(
    store: "InMemoryPeopleStore",
    principal: Principal,
    manifest: LegacyAttributionManifest,
    deleted_sessions: dict[UUID, datetime],
    now: datetime,
    *,
    confirm: bool,
) -> LegacyRecoveryResult:
    owner = (principal.tenant_id, principal.principal_id)
    boundary = await store.forward_dreaming_boundary(principal, include_recovered=True)
    heads = {
        key[2]: RecoveryHead(
            rows[-1].kind,
            rows[-1].revision,
            key in store._erased,
            store._attributions.get(key),
            True,
        )
        for key, rows in store._records.items()
        if key[:2] == owner and rows
    }
    for key, (revision, kind) in store._erased_heads.items():
        if key[:2] == owner:
            heads[key[2]] = RecoveryHead(
                kind, revision, True, store._attributions.get(key), bool(store._records.get(key))
            )
    if any(key[:2] == owner and key[2] not in heads for key in store._erased):
        raise ConflictError("Recovery requires metadata for every erased head")
    receipts = [
        rows[-1]
        for key, rows in store._records.items()
        if key[:2] == owner
        and key not in store._erased
        and rows
        and isinstance(rows[-1], PeopleErasure)
    ]
    if len(receipts) > 5000:
        raise ConflictError("Recovery receipt bound exceeded")
    beliefs, sources = _receipt_evidence(receipts)
    if store._memories is not None:
        for proof in manifest.proofs:
            if proof.belief_id is not None:
                try:
                    await store._memories.get(proof.belief_id, principal)
                    beliefs.add(proof.belief_id)
                except NotFoundError:
                    pass
    recovered = validate_recovery(
        principal,
        manifest,
        boundary,
        RecoveryEvidence(heads, frozenset(beliefs), sources, deleted_sessions),
    )
    assert boundary is not None
    already = boundary.recovered_at is not None
    if confirm and not already:
        if store._memory_index is None:
            raise ConflictError("Recovery requires the memory index")
        for record_id, footprint in recovered.items():
            store._attributions[(*owner, record_id)] = footprint
        store._memory_index.forward_dreaming[owner] = ForwardDreamingBoundary.model_validate(
            {**boundary.model_dump(), "recovered_at": now, "recovery_digest": manifest.digest()}
        )
    return _result(manifest, confirm, already)


async def recover_postgres(
    store: "PostgresPeopleStore",
    principal: Principal,
    manifest: LegacyAttributionManifest,
    deleted_sessions: dict[UUID, datetime],
    now: datetime,
    *,
    confirm: bool,
) -> LegacyRecoveryResult:
    session = store._session
    owner = await lock_owner(session, principal)
    boundary = await store.forward_dreaming_boundary(principal, include_recovered=True)
    ids = {p.record_id for p in manifest.proofs} | {
        p.person_id for p in manifest.proofs if p.person_id
    }
    rows = list(
        (
            await session.scalars(
                select(PeopleHeadRow)
                .where(
                    PeopleHeadRow.tenant_id == principal.tenant_id,
                    PeopleHeadRow.principal_id == principal.principal_id,
                    PeopleHeadRow.memory_attribution.is_(None) | PeopleHeadRow.id.in_(ids),
                )
                .limit(10001)
            )
        ).all()
    )
    if len(rows) > 10000:
        raise ConflictError("Recovery head bound exceeded")
    payload_ids = set(
        (
            await session.scalars(
                select(PeopleRevisionRow.entity_id)
                .where(
                    PeopleRevisionRow.tenant_id == principal.tenant_id,
                    PeopleRevisionRow.principal_id == principal.principal_id,
                    PeopleRevisionRow.entity_id.in_([p.record_id for p in manifest.proofs]),
                )
                .distinct()
            )
        ).all()
    )
    heads = {
        r.id: RecoveryHead(
            r.kind,
            r.revision,
            r.erased,
            None
            if r.memory_attribution is None
            else AttributionFootprint.model_validate(r.memory_attribution),
            r.id in payload_ids,
        )
        for r in rows
    }
    receipt_rows = (
        await session.scalars(
            select(PeopleRevisionRow)
            .join(
                PeopleHeadRow,
                (PeopleHeadRow.tenant_id == PeopleRevisionRow.tenant_id)
                & (PeopleHeadRow.principal_id == PeopleRevisionRow.principal_id)
                & (PeopleHeadRow.id == PeopleRevisionRow.entity_id)
                & (PeopleHeadRow.revision == PeopleRevisionRow.revision),
            )
            .where(
                PeopleHeadRow.tenant_id == principal.tenant_id,
                PeopleHeadRow.principal_id == principal.principal_id,
                PeopleHeadRow.kind == "erasure",
                ~PeopleHeadRow.erased,
            )
            .limit(5001)
        )
    ).all()
    if len(receipt_rows) > 5000:
        raise ConflictError("Recovery receipt bound exceeded")
    receipts = [PEOPLE_RECORD.validate_python(r.payload) for r in receipt_rows]
    beliefs, sources = _receipt_evidence([r for r in receipts if isinstance(r, PeopleErasure)])
    beliefs.update(
        (
            await session.scalars(
                select(MemoryRow.id).where(
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    MemoryRow.id.in_([p.belief_id for p in manifest.proofs if p.belief_id]),
                )
            )
        ).all()
    )
    recovered = validate_recovery(
        principal,
        manifest,
        boundary,
        RecoveryEvidence(heads, frozenset(beliefs), sources, deleted_sessions),
    )
    assert boundary is not None
    already = boundary.recovered_at is not None
    if confirm and not already:
        for row in rows:
            if row.id in recovered:
                row.memory_attribution = recovered[row.id].model_dump(mode="json")
        owner.forward_dreaming = ForwardDreamingBoundary.model_validate(
            {**boundary.model_dump(), "recovered_at": now, "recovery_digest": manifest.digest()}
        ).model_dump(mode="json")
        await session.flush()
    return _result(manifest, confirm, already)
