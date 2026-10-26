"""Learned-memory shape v2 and the reshape of v1 rows (ADR-0250, plan ADR-B).

A website-learned belief is the grounded claim with its provenance beside it,
not inside it: the statement is the claim, the subject is the model's grounded
subject or the authorized host, the host is an attribution column the renderer
prints as "(learned from <host>)", the belief is contextual rather than local.
Claim identity, once-per-host corroboration, deletion barriers and source
erasure keep working across the two versions, and the v1 rows are reshaped
through governed supersession, never rewritten.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest

from agent_core.bootstrap import build
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    MemoryAuthority,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecalledBelief,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.people import SourceMemoryLink
from agent_core.domain.people_sources import source_id
from agent_core.domain.policies import TrustLevel
from agent_core.domain.sessions import project_scope
from agent_core.domain.tools import ToolExecutionContext
from tests.contract.support import tool_context
from tests.gates.test_runtime_tool_budget import _settings, _tool_turn
from tests.unit.test_source_learning import (
    QUOTE,
    URL,
    ArchiveProvider,
    learn,
    observation,
    read_archive,
)

HOST = "family.example.org"
LEGACY_VERSION = "owner-source-learning@1"


def _service(app: Any) -> Any:
    return app.source_learning


@asynccontextmanager
async def _chats(count: int) -> AsyncIterator[tuple[Any, list[ToolExecutionContext]]]:
    """One owner, ``count`` chats, each asking to learn from the same site and reading it."""
    script = FakeModelScript(
        turns=[
            turn
            for _ in range(count)
            for turn in (
                _tool_turn(
                    ScriptedToolCall(name="web.fetch", call_id="read-album", arguments={"url": URL})
                ),
                ScriptedTurn(text="Read the archive."),
            )
        ]
    )
    async with build(
        settings=_settings(), script=script, web_fetch_provider_override=ArchiveProvider()
    ) as app:
        contexts = []
        for _ in range(count):
            run = await app.runs.wait_terminal(
                await app.runs.submit(f"Study {URL} and learn about our family.")
            )
            contexts.append(
                replace(
                    tool_context(),
                    run_id=run.id,
                    session_id=run.session_id,
                    principal=app.principal,
                    origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                )
            )
        yield app, contexts


async def _expire(app: Any, belief_id: str) -> None:
    async with app.uow_factory() as uow:
        record = await uow.memories.get(UUID(belief_id), app.principal)
        await uow.memories.reinforce(
            record.model_copy(
                update={"expires_at": app.clock.now() - timedelta(seconds=1)}, deep=True
            )
        )


async def _legacy_row(app: Any, run: Any, *, claim: str = QUOTE, url: str = URL) -> MemoryRecord:
    """Write a v1 row the way owner-source-learning@1 did, with its receipts."""
    async with app.uow_factory() as uow:
        session = await uow.sessions.get(run.session_id, app.principal)
        events = await uow.events.list_after(run.session_id, 0, app.principal)
        sources = [events[0].sequence, events[-1].sequence]
        record = MemoryRecord(
            id=uuid4(),
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            scope=project_scope(session.metadata),
            subject=claim,
            statement=f"Source suggests: {claim} [Source: {url}]",
            source_session_id=run.session_id,
            source_event_ids=sources,
            confidence=0.35,
            sensitivity=Sensitivity.SENSITIVE,
            valid_from=app.clock.now(),
            expires_at=app.clock.now() + timedelta(days=30),
            status=MemoryStatus.PROVISIONAL,
            belief_type=__import__(
                "agent_core.domain.memory", fromlist=["BeliefType"]
            ).BeliefType.FACT,
            portability=Portability.LOCAL,
            origin_scopes=[project_scope(session.metadata)],
            derivation=MemoryDerivation.HYPOTHESIS,
            longevity=MemoryLongevity.TENTATIVE,
            last_evidence_at=app.clock.now(),
            last_reinforced_at=app.clock.now(),
            flagged_for_review=True,
            formation_run_id=uuid4(),
            consolidation_policy_version=LEGACY_VERSION,
            authority=MemoryAuthority.INFERRED,
            store_position=await uow.memories.next_position(),
            created_at=app.clock.now(),
            updated_at=app.clock.now(),
        )
        stored: MemoryRecord = await uow.memories.upsert_belief(record)
        normalized = " ".join(claim.casefold().replace(".", "").split())
        identity = hashlib.sha256(
            (
                app.principal.tenant_id + "\0" + app.principal.principal_id + "\0" + normalized
            ).encode()
        ).hexdigest()
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="memory.source_claim",
                actor_type="memory",
                actor_id=app.principal.principal_id,
                derivation_key=f"{LEGACY_VERSION}:claim:{identity}",
                payload={"belief_id": str(stored.id), "policy_version": LEGACY_VERSION},
            )
        )
    return stored


async def test_a_learned_belief_is_the_claim_with_its_host_beside_it() -> None:
    async with read_archive() as (app, context, _run):
        (result,) = await learn(app, context, observation(subject=None))
        assert result["status"] == "committed", result
        (belief,) = await app.memory.list_memories()
    assert belief.statement == QUOTE
    assert belief.subject == HOST
    assert belief.attribution_host == HOST
    assert belief.portability is Portability.CONTEXTUAL
    assert belief.sensitivity is Sensitivity.SENSITIVE
    assert belief.consolidation_policy_version == "owner-source-learning@2"
    assert belief.authority is MemoryAuthority.INFERRED
    assert belief.derivation is MemoryDerivation.HYPOTHESIS
    assert belief.confidence == 0.35 and belief.flagged_for_review
    assert belief.expires_at is not None


async def test_a_grounded_subject_is_kept_and_a_long_one_is_refused() -> None:
    async with read_archive() as (app, context, _run):
        (kept,) = await learn(app, context, observation(subject="Erin and Andy"))
        assert kept["status"] == "committed"
        (belief,) = await app.memory.list_memories()
        assert belief.subject == "Erin and Andy"
        (refused,) = await learn(
            app, context, observation(subject="Erin and Andy " * 8, claim=QUOTE)
        )
        assert refused["status"] == "rejected" and refused["reason"] == "observation_too_long"


async def test_recall_renders_the_host_the_belief_was_learned_from() -> None:
    async with read_archive() as (app, context, run):
        await learn(app, context, observation(subject=None))
        result = await app.memory_retriever.recall(
            RecallQuery(
                tenant_id=app.principal.tenant_id,
                principal_id=app.principal.principal_id,
                current_scope="general",
                text="Erin married Andy",
                budget_tokens=600,
                max_items=5,
                min_score=0.0,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            ),
            session_id=run.session_id,
            turn_id=UUID(int=901),
        )
    (item,) = [item for item in result.items if isinstance(item, RecalledBelief)]
    assert item.attribution_host == HOST
    assert f"(learned from {HOST})" in result.rendered
    assert "Source suggests" not in result.rendered


async def test_v1_receipts_still_count_as_the_same_claim_and_host() -> None:
    """Once per host: a claim a v1 row already holds from this host is unchanged."""
    async with read_archive() as (app, context, run):
        legacy = await _legacy_row(app, run)
        (result,) = await learn(app, context, observation())
        assert result == {"status": "unchanged", "belief_id": str(legacy.id)}
        assert [b.id for b in await app.memory.list_memories()] == [legacy.id]


async def test_a_v1_deletion_still_blocks_the_v2_identity() -> None:
    async with read_archive() as (app, context, run):
        legacy = await _legacy_row(app, run)
        await app.memory.delete(legacy.id)
        (result,) = await learn(app, context, observation())
        assert result["status"] == "rejected", result
        assert await app.memory.list_memories() == []


async def test_an_expired_claim_can_be_learned_again_from_a_fresh_read() -> None:
    async with read_archive() as (app, context, _run):
        (first,) = await learn(app, context, observation())
        assert first["status"] == "committed"
        async with app.uow_factory() as uow:
            record = await uow.memories.get(UUID(first["belief_id"]), app.principal)
            await uow.memories.reinforce(
                record.model_copy(
                    update={"expires_at": app.clock.now() - timedelta(seconds=1)}, deep=True
                )
            )
        (again,) = await learn(app, context, observation())
        assert again["status"] == "committed", again
        assert again["belief_id"] != first["belief_id"]


async def test_reshape_supersedes_v1_rows_with_v2_beliefs_through_formation() -> None:
    async with read_archive() as (app, context, run):
        first = await _legacy_row(app, run)
        second = await _legacy_row(app, run, claim="Andy plays trombone in two bands.")
        service = _service(app)

        dry = await service.reshape(policy=LEGACY_VERSION, confirm=False)
        assert (dry.examined, dry.reshaped, dry.dry_run) == (2, 2, True)
        assert {b.id for b in await app.memory.list_memories()} == {first.id, second.id}

        done = await service.reshape(policy=LEGACY_VERSION, confirm=True)
        assert (done.examined, done.reshaped, done.dry_run) == (2, 2, False)
        live = {b.statement: b for b in await app.memory.list_memories()}
        assert set(live) == {QUOTE, "Andy plays trombone in two bands."}
        originals = {QUOTE: first, "Andy plays trombone in two bands.": second}
        for statement, belief in live.items():
            assert belief.consolidation_policy_version == "owner-source-learning@2"
            assert belief.attribution_host == HOST
            assert belief.subject == HOST
            assert belief.portability is Portability.CONTEXTUAL
            assert belief.source_event_ids == originals[statement].source_event_ids
            assert belief.confidence == originals[statement].confidence
            assert belief.expires_at == originals[statement].expires_at
        async with app.uow_factory() as uow:
            old = await uow.memories.get(first.id, app.principal)
        assert old.status is MemoryStatus.SUPERSEDED
        assert old.superseded_by == live[QUOTE].id
        # Receipts follow superseded_by: the same claim from this host is unchanged.
        (result,) = await learn(app, context, observation())
        assert result == {"status": "unchanged", "belief_id": str(live[QUOTE].id)}
        again = await service.reshape(policy=LEGACY_VERSION, confirm=True)
        assert (again.examined, again.reshaped) == (0, 0)


async def test_each_relearn_of_an_expired_claim_still_counts_its_host_once() -> None:
    """A relearn chains its receipt to the newest receipt, never to the first one,
    so the host stays once-per-claim however many times the claim expires."""
    async with _chats(3) as (app, (first_chat, second_chat, third_chat)):
        (first,) = await learn(app, first_chat, observation())
        await _expire(app, first["belief_id"])
        (second,) = await learn(app, second_chat, observation())
        assert second["status"] == "committed", second
        # The same host again while the relearned belief is live: unchanged.
        assert await learn(app, second_chat, observation()) == [
            {"status": "unchanged", "belief_id": second["belief_id"]}
        ]
        await _expire(app, second["belief_id"])
        (third,) = await learn(app, third_chat, observation())
        assert third["status"] == "committed", third
        assert third["belief_id"] not in {first["belief_id"], second["belief_id"]}
        assert await learn(app, third_chat, observation()) == [
            {"status": "unchanged", "belief_id": third["belief_id"]}
        ]
        async with app.uow_factory() as uow:
            belief = await uow.memories.get(UUID(third["belief_id"]), app.principal)
        assert belief.confidence == 0.35


async def test_reshape_reads_only_the_source_events_of_each_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with read_archive() as (app, _context, run):
        rows = [
            await _legacy_row(app, run),
            await _legacy_row(app, run, claim="Andy plays trombone in two bands."),
        ]
        async with app.uow_factory() as uow:
            repository = type(uow.events)
        original = repository.list_after
        loaded: list[int] = []

        async def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
            events = await original(self, *args, **kwargs)
            loaded.extend(event.sequence for event in events)
            return events

        monkeypatch.setattr(repository, "list_after", counting)
        done = await _service(app).reshape(policy=LEGACY_VERSION, confirm=True)
    assert done.reshaped == 2
    assert sorted(loaded) == sorted(seq for row in rows for seq in row.source_event_ids)


async def test_linking_a_belief_again_keeps_every_supporting_event() -> None:
    """A link that already exists takes the new support rather than dropping it,
    so erasing the chat behind either read still reaches the belief."""
    async with read_archive() as (app, context, run):
        (result,) = await learn(app, context, observation())
        service = _service(app)
        async with app.uow_factory() as uow:
            belief = await uow.memories.get(UUID(result["belief_id"]), app.principal)
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        first, second = events[:2], events[1:3]
        for supporting in (first, second):
            async with app.uow_factory() as uow, uow.people.lock(app.principal):
                await service._link_sources(uow, app.principal, belief, supporting, "probe")
        async with app.uow_factory() as uow:
            link = await uow.people.get(
                app.principal, uuid5(belief.id, "probe"), ceiling=Sensitivity.RESTRICTED
            )
    assert isinstance(link, SourceMemoryLink)
    assert link.support_ids == [
        source_id(app.principal, run.session_id, event.sequence) for event in events[:3]
    ]
    assert link.revision == 2
