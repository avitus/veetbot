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

from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.errors import ConflictError, NotFoundError
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


async def test_postgres_fts_matches_any_term_in_any_order_and_ranks_full_matches_first(
    tmp_path: Path,
) -> None:
    """PostgreSQL answers a text query with the same any-term set as memory.

    Lexical recall is a ranking arm rather than a hard filter, so a belief
    sharing one term is a candidate the ranker demotes, a belief sharing none
    is absent, and naming a subject reaches a belief no term reaches.
    """

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
        partial = await _remember(
            composition,
            session_id,
            f"Runbooks call the {marker} palette burgundy",
            subject=f"runbook-{marker}",
            belief_type=BeliefType.FACT,
        )

        reordered = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} theme emerald"), session_id=session_id
        )
        assert [item.belief_id for item in reordered.items] == [belief.id, partial.id]

        zero_overlap = await composition.memory_retriever.recall(
            _query(composition, text=f"absent{marker} missing{marker}"), session_id=session_id
        )
        assert zero_overlap.items == []

        structured = await composition.memory_retriever.recall(
            _query(composition, text=f"absent{marker}", subjects=[f"DASH-{marker}"]),
            session_id=session_id,
        )
        assert [item.belief_id for item in structured.items] == [belief.id]


_PARITY_BELIEFS = (
    ("Dashboards use the emerald themes", "dashboard palette"),
    ("Apple Watch charges overnight", "wearables"),
    ("The theme is emerald", "editor colours"),
    ("The e-mail digest is weekly", "digest cadence"),
    ("Staging endpoint is svc.internal:8443", "endpoint"),
    ("The user's runbook lives in the wiki", "runbook"),
    ("Reviews land in tabs/spaces order", "review order"),
    ("Release 3.14 ships on Tuesday", "release train"),
)
_PARITY_TEXTS = (
    "theme",
    "themes",
    "app",
    "apple",
    "mail",
    "e-mail",
    "svc.internal:8443",
    "internal",
    "user's",
    "users",
    "wiki runbook",
    "3.14",
    "tabs/spaces",
    "spaces",
    "...",
    "emerald digest",
)


async def test_postgres_and_memory_stores_agree_on_lexical_matching(tmp_path: Path) -> None:
    """The two belief stores answer the same text query with the same set.

    The benchmark measures the in-memory tier, so a predicate more permissive
    than PostgreSQL's would record a baseline the production store cannot
    reproduce. Both are asked the same fixtures over the same beliefs.
    """

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memlex{uuid4().hex[:10]}"
        for statement, subject in _PARITY_BELIEFS:
            await _remember(
                composition,
                session_id,
                statement,
                subject=f"{subject} {marker}",
                belief_type=BeliefType.FACT,
            )

        mirror = InMemoryMemoryStore(composition.clock)
        for record in await composition.memory.list_memories():
            await mirror.upsert_belief(record)

        for text in _PARITY_TEXTS:
            query = _query(composition, text=text)
            async with composition.uow_factory() as uow:
                stored = await uow.memories.query(query)
            assert {record.id for record in stored} == {
                record.id for record in await mirror.query(query)
            }, f"stores disagree on {text!r}"


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

        # Terms the local belief alone carries: any-term recall would return
        # it on this text if project-local portability did not hold it back.
        cross_project_text = await composition.memory_retriever.recall(
            _query(composition, text="staging endpoint"), session_id=session_id
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


async def test_postgres_expires_operator_trace_fields_and_keeps_the_user_view(
    tmp_path: Path,
) -> None:
    """The JSONB rewrite nulls the operator tier and preserves the user tier."""

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memexp{uuid4().hex[:10]}"
        first = await _remember(
            composition,
            session_id,
            f"The {marker} standup is at nine",
            subject=f"standup-{marker}",
        )
        await _remember(
            composition,
            session_id,
            f"The {marker} retro is on Friday",
            subject=f"retro-{marker}",
        )
        turn_id = uuid4()
        # Both beliefs carry the marker and only the first carries "standup",
        # so the ranking is a fact of the query rather than of identifier order.
        result = await composition.memory_retriever.recall(
            _query(composition, text=f"{marker} standup").model_copy(update={"max_items": 1}),
            session_id=session_id,
            turn_id=turn_id,
        )
        assert [item.belief_id for item in result.items] == [first.id]

        async with composition.uow_factory() as uow:
            recorded = await uow.traces.get(result.trace_id, composition.principal)
        assert len(recorded.dropped_for_budget) == 1

        expiry = recorded.operator_fields_expire_at + timedelta(seconds=1)
        async with composition.uow_factory() as uow:
            assert await uow.traces.expire_operator_fields(expiry, 10) == 1
        async with composition.uow_factory() as uow:
            swept = await uow.traces.get(result.trace_id, composition.principal)
            view = await uow.traces.user_view(
                turn_id, viewing_surface_id="private", viewing_ceiling="restricted"
            )
            assert await uow.traces.expire_operator_fields(expiry, 10) == 0
        assert swept.arm_latencies_ms == {}
        assert swept.candidates == 0
        assert swept.dropped_for_budget == []
        assert swept.dropped_for_budget_count == 1
        assert swept.returned == recorded.returned
        assert swept.beliefs == recorded.beliefs
        assert swept.rendered == recorded.rendered
        assert [belief.belief_id for belief in view.beliefs] == [first.id]
        assert view.considered_not_shown == 1


async def test_postgres_mark_cited_unions_into_the_trace_and_feeds_usage_back(
    tmp_path: Path,
) -> None:
    """The locked JSONB rewrite unions citations and moves utility, not confidence.

    Two beliefs are recalled into one turn and the answer cites one of them, so
    the row-locked rewrite is observed marking exactly that belief used in the
    user view, raising its utility while its confidence stands, and lowering
    the other's. A later mark widens the set instead of replacing it, repeating
    it changes nothing, and a foreign principal cannot reach the trace at all.
    """

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memcite{uuid4().hex[:10]}"
        cited = await _remember(
            composition,
            session_id,
            f"The {marker} standup is at nine",
            subject=f"standup-{marker}",
        )
        uncited = await _remember(
            composition,
            session_id,
            f"The {marker} retro is on Friday",
            subject=f"retro-{marker}",
        )
        # The trace and the citation event both reference a real run, which is
        # what the events foreign key requires of anything the hook writes.
        turn_id = await composition.runs.submit(f"What about {marker}?", session_id)
        result = await composition.memory_retriever.recall(
            _query(composition, text=marker),
            session_id=session_id,
            run_id=turn_id,
            turn_id=turn_id,
        )
        assert {item.belief_id for item in result.items} == {cited.id, uncited.id}

        feedback = await composition.memory.record_usage(
            session_id=session_id,
            run_id=turn_id,
            final_text=f"[m:{str(cited.id)[:8]}] answers it.",
        )

        assert (feedback.cited, feedback.uncited, feedback.traces) == (1, 1, 1)
        async with composition.uow_factory() as uow:
            stored = await uow.traces.get(result.trace_id, composition.principal)
            view = await uow.traces.user_view(turn_id, "private", "restricted")
            foreign = composition.principal.model_copy(update={"principal_id": f"other-{marker}"})
            with pytest.raises(NotFoundError):
                await uow.traces.mark_cited(result.trace_id, foreign, [cited.id])
        assert stored.cited == [cited.id]
        assert {belief.belief_id: belief.used for belief in view.beliefs} == {
            cited.id: True,
            uncited.id: False,
        }
        beliefs = {belief.id: belief for belief in await composition.memory.list_memories()}
        assert beliefs[cited.id].utility > 0
        assert beliefs[cited.id].confidence == cited.confidence
        assert beliefs[cited.id].last_reinforced_at > cited.last_reinforced_at
        assert beliefs[uncited.id].utility < 0
        assert beliefs[uncited.id].confidence == uncited.confidence

        async with composition.uow_factory() as uow:
            widened = await uow.traces.mark_cited(
                result.trace_id, composition.principal, [uncited.id]
            )
        assert widened.cited == [cited.id, uncited.id]
        async with composition.uow_factory() as uow:
            repeated = await uow.traces.mark_cited(
                result.trace_id, composition.principal, [cited.id, uncited.id]
            )
            assert repeated == widened
            assert await uow.traces.get(result.trace_id, composition.principal) == widened


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


async def test_postgres_list_idle_orders_by_reinforcement_and_bounds_the_window(
    tmp_path: Path,
) -> None:
    """The decay window is the least recently reinforced live beliefs.

    The shared database carries rows from other cases, so the ordering is
    asserted over whatever the window returns and membership over this case's
    own beliefs: written newest-first, only the two past the cutoff come back,
    oldest first, and neither the freshly reinforced nor the retired one does.
    """

    async with build(settings=_settings(tmp_path), storage="postgres") as composition:
        session_id = await composition.sessions.create()
        marker = f"memidle{uuid4().hex[:10]}"
        now = composition.clock.now()

        async def _seed(subject: str, idle_days: int, status: MemoryStatus) -> MemoryRecord:
            reinforced_at = now - timedelta(days=idle_days)
            async with composition.uow_factory() as uow:
                record = MemoryRecord.model_validate(
                    {
                        "id": uuid4(),
                        "tenant_id": composition.principal.tenant_id,
                        "principal_id": composition.principal.principal_id,
                        "scope": "integration",
                        "subject": f"{subject}-{marker}",
                        "statement": f"{subject} statement {marker}",
                        "source_session_id": session_id,
                        "source_event_ids": [1],
                        "confidence": 0.5,
                        "sensitivity": Sensitivity.INTERNAL,
                        "valid_from": now - timedelta(days=idle_days + 1),
                        "status": status,
                        "valid_to": None if status is MemoryStatus.PROVISIONAL else now,
                        "belief_type": BeliefType.FACT,
                        "polarity": Polarity.ASSERT,
                        "portability": Portability.CONTEXTUAL,
                        "origin_scopes": ["integration"],
                        "last_reinforced_at": reinforced_at,
                        "formation_run_id": uuid4(),
                        "consolidation_policy_version": "formation@1",
                        "authority": MemoryAuthority.INFERRED,
                        "store_position": await uow.memories.next_position(),
                        "created_at": reinforced_at,
                        "updated_at": reinforced_at,
                    }
                )
                return await uow.memories.upsert_belief(record)

        fresh = await _seed("fresh", 1, MemoryStatus.PROVISIONAL)
        newer_idle = await _seed("newer-idle", 100, MemoryStatus.PROVISIONAL)
        oldest_idle = await _seed("oldest-idle", 400, MemoryStatus.PROVISIONAL)
        retired = await _seed("retired", 500, MemoryStatus.RETIRED)

        async with composition.uow_factory() as uow:
            window = await uow.memories.list_idle(
                composition.principal,
                reinforced_before=now - timedelta(days=50),
                limit=500,
            )
            bounded = await uow.memories.list_idle(
                composition.principal,
                reinforced_before=now - timedelta(days=50),
                limit=1,
            )

        stamps = [(record.last_reinforced_at, str(record.id)) for record in window]
        mine = [record.id for record in window if marker in record.subject]
        assert stamps == sorted(stamps)
        assert mine == [oldest_idle.id, newer_idle.id]
        assert fresh.id not in {record.id for record in window}
        assert retired.id not in {record.id for record in window}
        assert len(bounded) == 1
        assert bounded[0].id == window[0].id
