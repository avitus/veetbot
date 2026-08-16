"""PostgreSQL memory-store parity for Milestone 9 write and read paths.

The memory port contracts run only against the in-memory adapter, so every
behavior asserted here targets the PostgreSQL adapters through a real
composition: FTS matching, supersession currency and as-of history, the
sensitivity and local-portability predicates, tombstone replay across
re-derivation, watermark monotonicity, expiry, promotion, rejection linking,
and the recorded trace with its two-ceiling user view.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RecallQuery,
    RejectionKind,
    Sensitivity,
)
from tests.integration.m2_support import database_settings


def _settings(tmp_path: Path) -> Settings:
    return replace(database_settings(), artifact_root=tmp_path / "memory-artifacts")


async def _user_event(composition: Composition, session_id: UUID, text: str) -> int:
    async with composition.uow_factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=composition.principal.principal_id,
                payload={"content": text},
            )
        )
    return event.sequence


def _query(
    composition: Composition,
    *,
    text: str | None,
    subjects: list[str] | None = None,
    scope: str = "integration",
    sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED,
    as_of: datetime | None = None,
) -> RecallQuery:
    return RecallQuery.model_validate(
        {
            "tenant_id": composition.principal.tenant_id,
            "principal_id": composition.principal.principal_id,
            "current_scope": scope,
            "text": text,
            "subjects": subjects or [],
            "as_of": as_of,
            "budget_tokens": 500,
            "max_items": 10,
            "min_score": 0.1,
            "sensitivity_ceiling": sensitivity_ceiling,
        }
    )


async def _remember(
    composition: Composition,
    session_id: UUID,
    statement: str,
    *,
    subject: str,
    scope: str = "integration",
    belief_type: BeliefType = BeliefType.PREFERENCE,
    portability: Portability | None = None,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> MemoryRecord:
    sequence = await _user_event(composition, session_id, statement)
    return await composition.memory.remember(
        session_id=session_id,
        run_id=None,
        statement=statement,
        subject=subject,
        scope=scope,
        belief_type=belief_type,
        portability=portability,
        sensitivity=sensitivity,
        source_event_ids=[sequence],
    )


async def test_postgres_supersession_serves_current_and_historical_beliefs(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memfts{uuid4().hex[:10]}"
        old = await _remember(
            composition,
            session_id,
            f"{marker} answers should be concise",
            subject=f"style-{marker}",
        )
        await asyncio.sleep(0.002)
        historical_at = composition.clock.now()
        await asyncio.sleep(0.002)
        new = await _remember(
            composition,
            session_id,
            f"{marker} answers should be detailed",
            subject=f"style-{marker}",
        )

        current = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} answers"), session_id=session_id
        )
        assert [item.belief_id for item in current.items] == [new.id]

        historical = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} answers", as_of=historical_at),
            session_id=session_id,
        )
        assert [item.belief_id for item in historical.items] == [old.id]

        async with composition.uow_factory() as uow:
            stored_old = await uow.memories.get(old.id, composition.principal)
        assert stored_old.status is MemoryStatus.SUPERSEDED
        assert stored_old.superseded_by == new.id
        assert stored_old.valid_to is not None

        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        assert "memory.superseded" in {event.event_type for event in events}


async def test_postgres_fts_matches_terms_in_any_order_and_requires_all(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memfts{uuid4().hex[:10]}"
        belief = await _remember(
            composition,
            session_id,
            f"Grafana dashboards use the emerald theme {marker}",
            subject=f"dash-{marker}",
            belief_type=BeliefType.FACT,
        )

        reordered = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} theme emerald"), session_id=session_id
        )
        assert [item.belief_id for item in reordered.items] == [belief.id]

        missing_term = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} nonexistentterm"), session_id=session_id
        )
        assert missing_term.items == []

        structured = await composition.memory_retriever.recall(
            _query(composition, text="no lexical overlap", subjects=[f"DASH-{marker}"]),
            session_id=session_id,
        )
        assert [item.belief_id for item in structured.items] == [belief.id]


async def test_postgres_sensitivity_ceiling_and_local_portability_predicates(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memsec{uuid4().hex[:10]}"
        restricted = await _remember(
            composition,
            session_id,
            f"The {marker} incident channel is private",
            subject=f"incident-{marker}",
            sensitivity=Sensitivity.RESTRICTED,
        )
        local = await _remember(
            composition,
            session_id,
            f"Staging endpoint for {marker} is svc.internal:8443",
            subject=f"endpoint-{marker}",
            scope="another-project",
            belief_type=BeliefType.FACT,
            portability=Portability.LOCAL,
        )

        below_ceiling = await composition.memory_retriever.recall(
            _query(
                composition,
                text=f"{marker} incident channel",
                sensitivity_ceiling=Sensitivity.INTERNAL,
            ),
            session_id=session_id,
        )
        assert below_ceiling.items == []

        at_ceiling = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} incident channel"), session_id=session_id
        )
        assert [item.belief_id for item in at_ceiling.items] == [restricted.id]

        cross_project_text = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} staging endpoint"), session_id=session_id
        )
        assert cross_project_text.items == []

        named_subject = await composition.memory_retriever.recall(
            _query(composition, text=None, subjects=[f"endpoint-{marker}"]),
            session_id=session_id,
        )
        assert [item.belief_id for item in named_subject.items] == [local.id]
        assert named_subject.items[0].carried is True
        assert "(learned in another-project)" in named_subject.rendered


async def test_postgres_rederivation_replays_outstanding_rejections(tmp_path: Path) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memrej{uuid4().hex[:10]}"
        await _user_event(composition, session_id, f"I prefer {marker} pipelines")
        formed = await composition.memory.run(
            trigger="session_close", scope="integration", session_id=session_id
        )
        (belief,) = formed.beliefs
        await composition.memory.reject(belief.id, RejectionKind.UNTRUE)

        rederived = await composition.memory.run(
            trigger="policy_upgrade_rederive",
            scope="integration",
            session_id=session_id,
            since_watermark=0,
        )
        assert rederived.beliefs == []
        assert rederived.run.candidates_proposed == 1
        assert rederived.run.rejected == 1

        recalled = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} pipelines"), session_id=session_id
        )
        assert recalled.items == []
        async with composition.uow_factory() as uow:
            rejections = await uow.memories.outstanding_rejections(
                composition.principal.tenant_id, composition.principal.principal_id
            )
        assert [rejection.kind for rejection in rejections] == [RejectionKind.UNTRUE]
        assert rejections[0].statement == belief.statement


async def test_postgres_delete_leaves_a_hash_only_tombstone_that_blocks_reformation(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memdel{uuid4().hex[:10]}"
        statement = f"The {marker} playbook lives in the wiki"
        belief = await _remember(composition, session_id, statement, subject=f"playbook-{marker}")
        await composition.memory.delete(belief.id)

        sequence = await _user_event(composition, session_id, "Please remember it again")
        with pytest.raises(ConflictError):
            await composition.memory.remember(
                session_id=session_id,
                run_id=None,
                statement=statement.upper(),
                subject=f"playbook-{marker}",
                scope="integration",
                source_event_ids=[sequence],
            )

        assert await composition.memory.list_memories(include_inactive=True) == []
        async with composition.uow_factory() as uow:
            rejections = await uow.memories.outstanding_rejections(
                composition.principal.tenant_id, composition.principal.principal_id
            )
        assert [rejection.kind for rejection in rejections] == [RejectionKind.DELETED]
        assert rejections[0].statement is None
        expected_hash = hashlib.sha256(statement.casefold().encode()).hexdigest()
        assert rejections[0].statement_sha256 == expected_hash


async def test_postgres_watermarks_are_monotonic_and_consolidation_incremental(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memwm{uuid4().hex[:10]}"
        await _user_event(composition, session_id, f"Remember that {marker} is the marker")
        first = await composition.memory.run(
            trigger="session_close", scope="integration", session_id=session_id
        )
        assert len(first.beliefs) == 1
        assert first.run.watermark_after >= 1

        second = await composition.memory.run(
            trigger="session_close", scope="integration", session_id=session_id
        )
        assert second.beliefs == []
        assert second.run.candidates_proposed == 0

        async with composition.uow_factory() as uow:
            await uow.memories.set_consolidation_watermark(session_id, composition.principal, 1)
        async with composition.uow_factory() as uow:
            watermark = await uow.memories.consolidation_watermark(
                session_id, composition.principal
            )
        assert watermark >= first.run.watermark_after


async def test_postgres_expire_retires_only_past_expiry_beliefs(tmp_path: Path) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memexp{uuid4().hex[:10]}"
        now = composition.clock.now()

        async def _seed(belief_id: UUID, subject: str, expires_at: datetime) -> MemoryRecord:
            async with composition.uow_factory() as uow:
                record = MemoryRecord.model_validate(
                    {
                        "id": belief_id,
                        "tenant_id": composition.principal.tenant_id,
                        "principal_id": composition.principal.principal_id,
                        "scope": "integration",
                        "subject": subject,
                        "statement": f"{subject} statement {marker}",
                        "source_session_id": session_id,
                        "source_event_ids": [1],
                        "confidence": 0.9,
                        "sensitivity": Sensitivity.INTERNAL,
                        "valid_from": now - timedelta(hours=1),
                        "expires_at": expires_at,
                        "status": MemoryStatus.ACTIVE,
                        "belief_type": BeliefType.FACT,
                        "polarity": Polarity.ASSERT,
                        "portability": Portability.CONTEXTUAL,
                        "origin_scopes": ["integration"],
                        "last_reinforced_at": now - timedelta(hours=1),
                        "formation_run_id": uuid4(),
                        "consolidation_policy_version": "formation@1",
                        "authority": MemoryAuthority.USER,
                        "store_position": await uow.memories.next_position(),
                        "created_at": now - timedelta(hours=1),
                        "updated_at": now - timedelta(hours=1),
                    }
                )
                return await uow.memories.upsert_belief(record)

        past = await _seed(uuid4(), f"stale-{marker}", now - timedelta(minutes=1))
        fresh = await _seed(uuid4(), f"fresh-{marker}", now + timedelta(hours=1))

        expired = await composition.memory.expire()
        assert [record.id for record in expired] == [past.id]
        assert expired[0].status is MemoryStatus.EXPIRED
        assert expired[0].valid_to is not None

        recalled = await composition.memory_retriever.recall(
            _query(composition, text=f"statement {marker}"), session_id=session_id
        )
        assert [item.belief_id for item in recalled.items] == [fresh.id]


async def test_postgres_cross_project_corroboration_promotes_to_user_scope(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memprom{uuid4().hex[:10]}"
        statement = f"Reviews precede merges for {marker}"
        formed = await _remember(
            composition, session_id, statement, subject=f"reviews-{marker}", scope="proj-one"
        )
        promoted = await _remember(
            composition, session_id, statement, subject=f"reviews-{marker}", scope="proj-two"
        )

        assert promoted.id == formed.id
        assert promoted.scope == "user"
        assert promoted.origin_scopes == ["proj-one", "proj-two"]
        assert promoted.corroboration_count == 2
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        assert "memory.promoted" in {event.event_type for event in events}


async def test_postgres_changed_rejection_links_belief_and_replacement(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memchg{uuid4().hex[:10]}"
        belief = await _remember(
            composition,
            session_id,
            f"The {marker} deploy day is Tuesday",
            subject=f"deploys-{marker}",
        )

        replacement = await composition.memory.reject(
            belief.id,
            RejectionKind.CHANGED,
            replacement_statement=f"The {marker} deploy day is Thursday",
        )

        async with composition.uow_factory() as uow:
            stored_old = await uow.memories.get(belief.id, composition.principal)
            rejections = await uow.memories.outstanding_rejections(
                composition.principal.tenant_id, composition.principal.principal_id
            )
        assert stored_old.status is MemoryStatus.SUPERSEDED
        assert stored_old.superseded_by == replacement.id
        assert [rejection.replacement_id for rejection in rejections] == [replacement.id]

        recalled = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} deploy day"), session_id=session_id
        )
        assert [item.belief_id for item in recalled.items] == [replacement.id]


async def test_postgres_trace_round_trip_user_view_and_conflict_detection(
    tmp_path: Path,
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memtrc{uuid4().hex[:10]}"
        internal = await _remember(
            composition,
            session_id,
            f"The {marker} weekly digest is public",
            subject=f"digest-{marker}",
        )
        restricted = await _remember(
            composition,
            session_id,
            f"The {marker} incident notes are restricted",
            subject=f"incident-{marker}",
            sensitivity=Sensitivity.RESTRICTED,
        )
        turn_id = uuid4()

        result = await composition.memory_retriever.recall(
            _query(
                composition,
                text=None,
                subjects=[f"digest-{marker}", f"incident-{marker}"],
            ),
            session_id=session_id,
            turn_id=turn_id,
        )
        assert {item.belief_id for item in result.items} == {internal.id, restricted.id}

        async with composition.uow_factory() as uow:
            trace = await uow.traces.get(result.trace_id, composition.principal)
            view = await uow.traces.user_view(
                turn_id, viewing_surface_id="shared", viewing_ceiling="internal"
            )
        assert trace.rendered == result.rendered
        assert trace.rendered_sha256 == hashlib.sha256(result.rendered.encode()).hexdigest()
        assert [belief.belief_id for belief in view.beliefs] == [internal.id]

        async with composition.uow_factory() as uow:
            await uow.traces.record(trace)
        drifted = trace.model_copy(
            update={
                "rendered": "<memory>drift</memory>",
                "rendered_sha256": hashlib.sha256(b"<memory>drift</memory>").hexdigest(),
            }
        )
        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.traces.record(drifted)
