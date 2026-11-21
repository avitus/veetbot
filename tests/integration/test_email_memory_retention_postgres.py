"""PostgreSQL keeps an email memory by its thread's importance and re-rates it (ADR-0340)."""

from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.email_memory import EMAIL_RETENTION_POLICY
from agent_core.domain.memory import MemoryDerivation, MemoryRecord
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.contract.memory_fixtures import browse_query
from tests.gates.test_email_m18 import _email_settings
from tests.integration.m2_support import database_settings
from tests.unit.test_email_memory_rerating_hooks import NOW, _assessment
from tests.unit.test_email_refresh_bulk_formation import _mail


async def _memories(app: Any) -> list[MemoryRecord]:
    async with app.uow_factory() as uow:
        return list(
            await uow.memories.browse(
                browse_query(
                    tenant_id=app.principal.tenant_id, principal_id=app.principal.principal_id
                )
            )
        )


async def test_postgres_keeps_an_email_memory_by_importance_and_follows_owner_feedback() -> None:
    settings = replace(
        _email_settings(),
        email_mode_enabled=True,
        people_enabled=True,
        database_url=database_settings().database_url,
    )
    async with build(
        settings=settings,
        storage="postgres",
        clock=FixedClock(NOW),
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        worker = cast(DurableWorker, app.async_worker_factory("email-memory-test"))
        while (await app.runs.get(operation.run_id)).status is not RunStatus.COMPLETED:
            assert await worker.run_once(), (await app.runs.get(operation.run_id)).model_dump()

        # The saved rating is 0.54: kept ninety days, and an unknown sender stays a guess.
        [memory] = await _memories(app)
        assert memory.consolidation_policy_version == "email-semantic@2"
        assert memory.lifecycle_policy_version == EMAIL_RETENTION_POLICY
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=90)
        assert memory.derivation is MemoryDerivation.HYPOTHESIS

        # The decay window leaves it out in SQL, so its expiry alone ends it.
        async with app.uow_factory() as uow:
            idle = await uow.memories.list_idle(
                app.principal,
                evidence_before=NOW + timedelta(days=1),
                decay_confidence_ceiling=0.55,
                min_unused_deliveries=3,
                limit=50,
            )
        assert memory.id not in {row.id for row in idle}

        rerating = app.email_memory_rerating
        assert rerating is not None
        preview = await rerating(app.principal, False)
        assert (preview.applied, preview.examined, preview.changed) == (False, 1, 0)
        assert preview.by_retention == {"90_days": 1} and preview.guesses == 1

        # The owner marks the thread Important: the stored row follows at once.
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        marked = await app.services.email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at is None
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(memory.source_session_id, 0, app.principal)
        [audit] = [event for event in events if event.event_type == "email.memory.rerated"]
        assert audit.payload == {
            "cause": "feedback",
            "policy_version": EMAIL_RETENTION_POLICY,
            "belief_ids": [str(memory.id)],
        }

        await app.services.email.undo_feedback(app.principal, UUID(str(marked["feedback_id"])))
        [returned] = await _memories(app)
        assert returned.expires_at == memory.expires_at
        applied = await rerating(app.principal, True)
        assert (applied.applied, applied.examined, applied.changed) == (True, 1, 0)


async def test_postgres_merges_a_repeated_email_fact_into_the_earliest_memory() -> None:
    """ADR-0341: the copy points at the memory kept, through the store's own foreign key."""
    from agent_core.domain.email_memory import EmailMergeReport
    from agent_core.domain.memory import MemoryStatus

    settings = replace(
        _email_settings(),
        email_mode_enabled=True,
        people_enabled=True,
        database_url=database_settings().database_url,
    )
    async with build(
        settings=settings,
        storage="postgres",
        clock=FixedClock(NOW),
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        worker = cast(DurableWorker, app.async_worker_factory("email-merge-test"))
        while (await app.runs.get(operation.run_id)).status is not RunStatus.COMPLETED:
            assert await worker.run_once(), (await app.runs.get(operation.run_id)).model_dump()
        [kept] = await _memories(app)
        async with app.uow_factory() as uow:
            copy = await uow.memories.upsert_belief(
                kept.model_copy(
                    update={
                        "id": UUID(int=9_901),
                        "statement": kept.statement.replace("requested_action", "asked_to"),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
            sources = await uow.email.list(app.principal, "semantic_source")
            [source] = [
                row for row in sources if str(kept.id) in cast(list[str], row.payload["memory_ids"])
            ]
            listed = cast(list[str], source.payload["memory_ids"])
            await uow.email.put(
                source.model_copy(
                    update={
                        "revision": source.revision + 1,
                        "payload": {**source.payload, "memory_ids": [*listed, str(copy.id)]},
                    }
                ),
                expected_revision=source.revision,
            )
        merge = app.email_memory_merge
        assert merge is not None
        preview = await merge(app.principal, False)
        assert preview == EmailMergeReport(applied=False, examined=2, facts=1, superseded=1)
        assert len(await _memories(app)) == 2
        applied = await merge(app.principal, True)
        assert (applied.facts, applied.superseded) == (1, 1)
        async with app.uow_factory() as uow:
            merged = await uow.memories.get(copy.id, app.principal)
            events = await uow.events.list_after(kept.source_session_id, 0, app.principal)
        assert merged.status is MemoryStatus.SUPERSEDED and merged.superseded_by == kept.id
        assert [row.id for row in await _memories(app)] == [kept.id]
        [audit] = [event for event in events if event.event_type == "email.memory.merged"]
        assert audit.payload == {"kept": str(kept.id), "superseded": [str(copy.id)]}
        assert (await merge(app.principal, True)).superseded == 0

        # An earlier memory of the same fact is the one kept. It takes the later
        # evidence and retention, and the later message answers for it from then on.
        earlier_at = kept.valid_from - timedelta(days=5)
        async with app.uow_factory() as uow:
            earlier = await uow.memories.upsert_belief(
                kept.model_copy(
                    update={
                        "id": UUID(int=9_902),
                        "statement": kept.statement.replace("requested_action", "first_asked"),
                        "valid_from": earlier_at,
                        "last_evidence_at": earlier_at,
                        "expires_at": earlier_at + timedelta(days=30),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
            record = await uow.email.get(app.principal, "semantic_source", source.key)
            assert record is not None
            listed = cast(list[str], record.payload["memory_ids"])
            await uow.email.put(
                record.model_copy(
                    update={
                        "revision": record.revision + 1,
                        "payload": {**record.payload, "memory_ids": [*listed, str(earlier.id)]},
                    }
                ),
                expected_revision=record.revision,
            )
        assert (await merge(app.principal, True)).superseded == 1
        async with app.uow_factory() as uow:
            survivor = await uow.memories.get(earlier.id, app.principal)
            replaced = await uow.memories.get(kept.id, app.principal)
            linked = await uow.email.get(app.principal, "semantic_source", source.key)
        assert replaced.status is MemoryStatus.SUPERSEDED and replaced.superseded_by == earlier.id
        assert survivor.valid_from == earlier_at
        assert survivor.last_evidence_at == kept.last_evidence_at
        assert survivor.expires_at == kept.expires_at
        assert linked is not None
        assert cast(dict[str, str], linked.payload["facts"])[f"renewed:{earlier.id}"] == str(
            earlier.id
        )
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        await app.services.email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        [current] = await _memories(app)
        assert current.id == earlier.id and current.expires_at is None
