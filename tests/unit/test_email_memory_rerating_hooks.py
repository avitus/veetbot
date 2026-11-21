"""ADR-0340: where an email memory's retention and standing are recomputed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.email_memory import EMAIL_RETENTION_POLICY, EmailRerateReport
from agent_core.domain.errors import AuthorizationError, ConflictError, NotFoundError
from agent_core.domain.memory import MemoryDerivation, MemoryRecord
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.memory.email_people import EmailPeopleFormationService
from tests.contract.memory_fixtures import browse_query
from tests.gates.test_email_m18 import _email_settings
from tests.unit.test_email_refresh_bulk_formation import EVIDENCE, _mail


def _assessment(importance: float) -> ScriptedTurn:
    return ScriptedTurn(
        text=json.dumps(
            {
                "summary": "Board request",
                "reason": "Direct request",
                "topics": ["board"],
                "content_importance": importance,
                "relationship_importance": 0,
                "urgency": 0,
                "needs_reply": False,
                "bulk": False,
                "attention_expires_at": None,
                "reply_blocked_reason": None,
                "supported_evidence": [EVIDENCE],
                "relationship_memory_ids": [],
                "semantic_facts": [],
                "people_facts": [
                    {
                        "message_id": "m1",
                        "quote": EVIDENCE,
                        "belief_type": "fact",
                        "subject": "Board materials",
                        "predicate": "requested_action",
                        "value": "approve the board materials",
                        "confidence": 0.4,
                        "people": None,
                    }
                ],
            }
        )
    )


# Three days after the fixture's message, so no test here depends on today's date.
NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


async def _memories(app: Any) -> list[MemoryRecord]:
    async with app.uow_factory() as uow:
        return list(
            await uow.memories.browse(
                browse_query(
                    tenant_id=app.principal.tenant_id, principal_id=app.principal.principal_id
                )
            )
        )


@pytest.mark.parametrize(
    ("content_importance", "days"),
    # The saved rating is 0.9 times the content importance here.
    [(0.2, 30), (0.6, 90), (0.8, 365), (1.0, None)],
)
async def test_a_refresh_keeps_a_new_memory_by_its_threads_saved_importance(
    content_importance: float, days: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rerated: list[tuple[str, str, bool]] = []
    original = EmailPeopleFormationService.rerate_thread

    async def recording(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        rerated.append((account_id, thread_id, kwargs.get("run") is not None))
        return await original(self, account_id, thread_id, **kwargs)

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", recording)
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(content_importance)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        [memory] = await _memories(app)
        assert memory.lifecycle_policy_version == EMAIL_RETENTION_POLICY
        assert memory.expires_at == (
            None if days is None else memory.last_evidence_at + timedelta(days=days)
        )
        # An unknown sender stays a guess whatever the thread is rated.
        assert memory.derivation is MemoryDerivation.HYPOTHESIS
        # The rating is kept with the assessment, for when a new message resets the thread's.
        async with app.uow_factory() as uow:
            [rated] = await uow.email.list(app.principal, "thread")
            assessment = await uow.email.get(app.principal, "assessment", str(rated.payload["id"]))
        assert assessment is not None
        assert assessment.payload["priority"] == rated.payload["priority"]
        assert float(str(rated.payload["priority"])) > 0
        # Each saved assessment re-rates the thread's memories under the run's lease.
        [(account_id, thread_id, under_run)] = rerated
        assert thread_id == "thread-1" and under_run

        # The owner marks the thread Important: its memory follows at once.
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        marked = await app.services.email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        assert rerated[-1] == (account_id, "thread-1", False)
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at is None

        # Undoing the mark returns the memory to its thread's own rating.
        await app.services.email.undo_feedback(app.principal, UUID(str(marked["feedback_id"])))
        [returned] = await _memories(app)
        assert returned.expires_at == memory.expires_at


async def test_feedback_succeeds_when_the_memory_rerating_cannot_run() -> None:
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")

        async def refused(*_: object) -> None:
            raise ConflictError("People email source was erased")

        app.services.email.rerate_memories = refused
        marked = await app.services.email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        thread_view = marked["thread"]
        assert isinstance(thread_view, dict) and thread_view["priority"] == 1.0


async def test_a_retried_feedback_request_resumes_a_rerating_that_did_not_run() -> None:
    """The feedback commits before its memories follow. A retry with the same key, and a
    repeated undo, re-rate again instead of returning early."""
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        [memory] = await _memories(app)
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=90)
        rerate = app.services.email.rerate_memories

        async def interrupted(*_: object) -> None:
            raise ConflictError("the request ended before its memories followed")

        async def mark() -> dict[str, object]:
            return await app.services.email.feedback(
                app.principal,
                thread_id=UUID(str(thread.payload["id"])),
                target="thread",
                judgment="important",
                idempotency_key="mark-once",
            )

        app.services.email.rerate_memories = interrupted
        marked = await mark()
        assert (await _memories(app))[0].expires_at == memory.expires_at
        app.services.email.rerate_memories = rerate
        assert (await mark())["feedback_id"] == marked["feedback_id"]
        assert (await _memories(app))[0].expires_at is None
        async with app.uow_factory() as uow:
            assert len(await uow.email.list(app.principal, "feedback")) == 1

        feedback_id = UUID(str(marked["feedback_id"]))

        async def feedback_record() -> Any:
            async with app.uow_factory() as uow:
                return await uow.email.get(app.principal, "feedback", str(feedback_id))

        app.services.email.rerate_memories = interrupted
        await app.services.email.undo_feedback(app.principal, feedback_id)
        # The undo itself committed; only its re-rating did not run.
        undone = await feedback_record()
        assert undone is not None and undone.payload["undone_at"] is not None
        assert (await _memories(app))[0].expires_at is None
        app.services.email.rerate_memories = rerate
        await app.services.email.undo_feedback(app.principal, feedback_id)
        # The repeated undo wrote nothing to the feedback and ran the re-rating.
        assert await feedback_record() == undone
        assert (await _memories(app))[0].expires_at == memory.expires_at


async def _second_thread(app: Any, memory: MemoryRecord) -> MemoryRecord:
    """Another conversation from the same sender, holding its own memory under the table."""
    from agent_core.domain.email_semantics import semantic_source_key, thread_source_key

    other_id, copy_id = UUID(int=7_702), UUID(int=7_703)
    async with app.uow_factory() as uow:
        [thread] = await uow.email.list(app.principal, "thread")
        [source] = [
            row
            for row in await uow.email.list(app.principal, "semantic_source")
            if str(memory.id) in row.payload["memory_ids"]
        ]
        account_id = str(thread.payload["account_id"])
        index = await uow.email.get(
            app.principal, "thread_source", thread_source_key(account_id, "thread-1")
        )
        assert index is not None
        copy: MemoryRecord = await uow.memories.upsert_belief(
            memory.model_copy(
                update={
                    "id": copy_id,
                    "statement": memory.statement.replace("thread-1", "thread-2"),
                    "store_position": await uow.memories.next_position(),
                }
            )
        )
        for record, key, payload in (
            (thread, str(other_id), {"id": str(other_id), "provider_thread_id": "thread-2"}),
            (
                index,
                thread_source_key(account_id, "thread-2"),
                {"thread_id": str(other_id)},
            ),
            (
                source,
                semantic_source_key(account_id, "thread-2", "m1"),
                {
                    "provider_thread_id": "thread-2",
                    "memory_ids": [str(copy.id)],
                    "facts": {f"formed:{copy.id}": str(copy.id)},
                },
            ),
        ):
            await uow.email.put(
                record.model_copy(
                    update={"key": key, "revision": 1, "payload": {**record.payload, **payload}}
                ),
                expected_revision=0,
            )
    return copy


async def test_a_mark_on_a_person_lengthens_their_memories_in_other_threads_at_once() -> None:
    """Person and topic feedback rates every thread it matches. Lengthening is always
    safe, so the other threads' memories gain it at once; shortening them still waits
    for their own next assessment or the owner's command."""
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        [memory] = await _memories(app)
        ninety = memory.last_evidence_at + timedelta(days=90)
        assert memory.expires_at == ninety
        other = await _second_thread(app, memory)

        async def expiries() -> tuple[datetime | None, datetime | None]:
            kept = {row.id: row.expires_at for row in await _memories(app)}
            return kept[memory.id], kept[other.id]

        async def feedback(target: str, judgment: str) -> UUID:
            marked = await app.services.email.feedback(
                app.principal,
                thread_id=UUID(str(thread.payload["id"])),
                target=target,  # type: ignore[arg-type]
                judgment=judgment,  # type: ignore[arg-type]
            )
            return UUID(str(marked["feedback_id"]))

        # A mark on the thread itself touches only that thread.
        marked = await feedback("thread", "important")
        assert await expiries() == (None, ninety)
        await app.services.email.undo_feedback(app.principal, marked)
        assert await expiries() == (ninety, ninety)

        # Less important on the person lowers this thread's memory and shortens
        # nothing in the person's other threads.
        lowered = await feedback("person", "less_important")
        assert await expiries() == (memory.last_evidence_at + timedelta(days=30), ninety)
        await app.services.email.undo_feedback(app.principal, lowered)
        assert await expiries() == (ninety, ninety)

        # Important on the person removes the expiry in the other thread at once.
        marked = await feedback("person", "important")
        assert await expiries() == (None, None)
        # Undoing it lowers this thread's memory; the other thread's waits.
        await app.services.email.undo_feedback(app.principal, marked)
        assert await expiries() == (ninety, None)


async def test_a_failed_pass_over_other_threads_never_fails_the_feedback() -> None:
    """The feedback is committed first. The same retry that resumes this thread's
    re-rating resumes the pass over the person's other threads."""
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        [memory] = await _memories(app)
        other = await _second_thread(app, memory)
        email = app.services.email
        lengthen = email.lengthen_memories
        asked: list[list[tuple[str, str]]] = []

        async def unavailable(_: object, threads: Any) -> None:
            asked.append(list(threads))
            raise RuntimeError("the store went away")

        async def mark() -> dict[str, object]:
            return await email.feedback(
                app.principal,
                thread_id=UUID(str(thread.payload["id"])),
                target="person",
                judgment="important",
                idempotency_key="person-once",
            )

        email.lengthen_memories = unavailable
        marked = await mark()
        # Only the other matching thread is asked for; this one has its own pass.
        assert asked == [[(str(thread.payload["account_id"]), "thread-2")]]
        kept = {row.id: row.expires_at for row in await _memories(app)}
        assert kept == {memory.id: None, other.id: other.expires_at}

        email.lengthen_memories = lengthen
        assert (await mark())["feedback_id"] == marked["feedback_id"]
        kept = {row.id: row.expires_at for row in await _memories(app)}
        assert kept == {memory.id: None, other.id: None}

        # Feedback about replies changes no importance, so no other thread is visited.
        email.lengthen_memories = unavailable
        await email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="person",
            judgment="needs_reply",
        )
        assert len(asked) == 1


async def test_an_abstaining_assessment_leaves_the_threads_memories_as_they_were(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result that cannot be grounded is saved as zero features, and rates the thread zero."""
    ungrounded = json.loads(_assessment(0.8).text or "")
    ungrounded.update(supported_evidence=["A sentence the message does not hold."], people_facts=[])
    rerated: list[tuple[str, Any]] = []
    original = EmailPeopleFormationService.rerate_thread

    async def recording(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        run = kwargs.get("run")
        rerated.append((thread_id, None if run is None else run.id))
        return await original(self, account_id, thread_id, **kwargs)

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", recording)
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.8), ScriptedTurn(text=json.dumps(ungrounded))]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        [memory] = await _memories(app)
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=365)
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        # The owner's reply judgment changes what the assessment sees, so it runs again.
        await app.services.email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="no_reply_needed",
        )
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        async with app.uow_factory() as uow:
            [rated] = await uow.email.list(app.principal, "thread")
            assessment = await uow.email.get(app.principal, "assessment", str(rated.payload["id"]))
        assert assessment is not None and assessment.payload["grounded"] is False
        assert rated.payload["priority"] == 0
        # The abstention was followed by a re-rating, under this refresh's run, and the
        # re-rating is what left the retention alone.
        assert rerated[-1] == ("thread-1", operation.run_id)
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at == memory.expires_at


async def test_a_junk_first_pass_assessment_rerates_the_threads_memories_too(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every saved assessment is followed by a re-rating, whichever path saved it."""
    from agent_core.adapters.judgment import FakeJudgmentProvider
    from tests.unit.test_email_junk_pilot import pilot_decision, pilot_settings, work_factory

    rerated: list[tuple[str, bool]] = []
    original = EmailPeopleFormationService.rerate_thread

    async def recording(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        rerated.append((account_id, kwargs.get("run") is not None))
        return await original(self, account_id, thread_id, **kwargs)

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", recording)
    settings = replace(pilot_settings(tmp_path), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        judgment_provider_override=FakeJudgmentProvider([pilot_decision()]),
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await work_factory(),
    ) as app:
        saves: list[tuple[str, int]] = []
        for saved in (1, 2):
            # The second refresh reuses the first pass's cached verdict and saves it again.
            operation = await app.services.email.submit_task(app.principal, kind="refresh")
            run = await app.runs.get(operation.run_id)
            assert run.status is RunStatus.COMPLETED, run.failure
            async with app.uow_factory() as uow:
                [assessment] = await uow.email.list(app.principal, "assessment")
            assert assessment.payload["junk_bypassed"] and run.usage.model_calls == 0
            assert rerated == [("work", True)] * saved
            saves.append((assessment.key, assessment.revision))
        # The second refresh saved the same assessment again; it did not only re-rate.
        (first_key, first_revision), (second_key, second_revision) = saves
        assert second_key == first_key and second_revision > first_revision


async def test_a_refresh_that_stopped_after_saving_an_assessment_is_followed_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved assessment owes its thread's memories a re-rating. If the refresh stops
    in between, the assessment is already current and is never visited again, so the
    next refresh looks for the debt itself, whatever the assessment's freshness."""
    rerated: list[tuple[str, Any]] = []
    stopped = True
    original = EmailPeopleFormationService.rerate_thread

    async def recording(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        if stopped:
            # The refresh ended here: the assessment is saved, its memories not followed.
            return EmailRerateReport()
        run = kwargs.get("run")
        rerated.append((thread_id, None if run is None else run.id))
        return await original(self, account_id, thread_id, **kwargs)

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", recording)
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:

        async def refresh() -> UUID:
            operation = await app.services.email.submit_task(app.principal, kind="refresh")
            run = await app.runs.get(operation.run_id)
            assert run.status is RunStatus.COMPLETED, run.failure
            assert run.usage.model_calls <= 1
            return operation.run_id

        async def owed() -> object:
            async with app.uow_factory() as uow:
                [assessment] = await uow.email.list(app.principal, "assessment")
            return assessment.payload.get("rerating_pending")

        # The first assessment, by the model: saved, and the refresh stopped.
        await refresh()
        assert rerated == [] and await owed() is True
        stopped = False
        second = await refresh()
        assert rerated == [("thread-1", second)] and await owed() is None
        await refresh()
        assert len(rerated) == 1

        # The same on a reused assessment, saved again under a new profile revision.
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await app.services.email._bump_profile(uow.email, app.principal)
        stopped = True
        await refresh()
        assert len(rerated) == 1 and await owed() is True
        stopped = False
        fifth = await refresh()
        assert rerated[1:] == [("thread-1", fifth)] and await owed() is None


async def test_assessments_saved_while_learning_was_paused_are_followed_up_afterwards() -> None:
    """A pause holds the memories still, not the ratings. Resumed, each assessment
    saved meanwhile is followed by the re-rating it still owes."""
    higher = json.loads(_assessment(1.0).text or "")
    higher["people_facts"] = []
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6), ScriptedTurn(text=json.dumps(higher))]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        [memory] = await _memories(app)
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=90)
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        await app.services.email.pause_learning(app.principal, True)
        # The owner's reply judgment changes what the assessment sees, so it runs again.
        await app.services.email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="no_reply_needed",
        )
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [rated] = await uow.email.list(app.principal, "thread")
        assert rated.payload["priority"] == pytest.approx(0.9)
        assert (await _memories(app))[0].expires_at == memory.expires_at

        await app.services.email.pause_learning(app.principal, False)
        await app.services.email.submit_task(app.principal, kind="refresh")
        [followed] = await _memories(app)
        assert followed.id == memory.id and followed.expires_at is None


async def test_the_owners_mark_reaches_a_memory_at_once_while_learning_is_paused() -> None:
    """A pause holds what Veetbot learns on its own, not the owner's own act: Important
    given during a pause keeps the thread's memory from expiring, and its undo returns
    the memory to its thread's rating, both at once."""
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        service = app.services.email
        await service.submit_task(app.principal, kind="refresh")
        [memory] = await _memories(app)
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=90)
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        await service.pause_learning(app.principal, True)

        marked = await service.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at is None

        await service.undo_feedback(app.principal, UUID(str(marked["feedback_id"])))
        [returned] = await _memories(app)
        assert returned.expires_at == memory.expires_at
        assert (await service.learning(app.principal)).paused is True


async def _owed(app: Any) -> object:
    """Whether the one saved assessment still owes its thread's memories a re-rating."""
    async with app.uow_factory() as uow:
        [assessment] = await uow.email.list(app.principal, "assessment")
    return assessment.payload.get("rerating_pending")


async def test_feedback_about_replies_cannot_apply_a_pending_assessment_during_a_pause() -> None:
    """Only an importance judgment is the owner's word on what a thread's memories are
    worth. Needs reply says nothing about that, so it re-rates nothing: a low assessment
    saved during a pause stays unapplied, with its mark, until the pause is over."""
    lower = json.loads(_assessment(0.2).text or "")
    lower["people_facts"] = []
    low = ScriptedTurn(text=json.dumps(lower))
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.8), low, low]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        service = app.services.email
        await service.submit_task(app.principal, kind="refresh")
        [memory] = await _memories(app)
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=365)
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        thread_id = UUID(str(thread.payload["id"]))
        await service.pause_learning(app.principal, True)
        # A reply judgment changes what the assessment sees, so the thread is assessed again.
        await service.feedback(
            app.principal, thread_id=thread_id, target="thread", judgment="no_reply_needed"
        )
        await service.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [rated] = await uow.email.list(app.principal, "thread")
        assert rated.payload["priority"] == pytest.approx(0.18)
        assert (await _memories(app))[0].expires_at == memory.expires_at
        assert await _owed(app) is True

        marked = await service.feedback(
            app.principal, thread_id=thread_id, target="thread", judgment="needs_reply"
        )
        assert (await _memories(app))[0].expires_at == memory.expires_at
        assert await _owed(app) is True
        await service.undo_feedback(app.principal, UUID(str(marked["feedback_id"])))
        assert (await _memories(app))[0].expires_at == memory.expires_at
        assert await _owed(app) is True

        await service.pause_learning(app.principal, False)
        operation = await service.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        [followed] = await _memories(app)
        assert followed.expires_at == memory.last_evidence_at + timedelta(days=30)
        assert await _owed(app) is None


async def test_only_an_importance_judgment_rerates_and_a_persons_rerates_its_thread_twice() -> None:
    """The feedback hook is for importance judgments and their undo. On a person or a
    topic it re-rates the feedback's own thread, then the other threads it matches, then
    its own thread once more: a memory the other threads' pass put back takes its
    standing from the message that formed it, which may sit in the feedback's thread."""
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        fixed_clock_at=NOW,
    ) as app:
        email = app.services.email
        await email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        [memory] = await _memories(app)
        await _second_thread(app, memory)
        calls: list[str] = []

        async def own(*_: object) -> None:
            calls.append("own")

        async def others(*_: object) -> None:
            calls.append("others")

        email.rerate_memories, email.lengthen_memories = own, others

        async def given(target: str, judgment: str) -> list[str]:
            calls.clear()
            marked = await email.feedback(
                app.principal,
                thread_id=UUID(str(thread.payload["id"])),
                target=target,  # type: ignore[arg-type]
                judgment=judgment,  # type: ignore[arg-type]
            )
            first = list(calls)
            calls.clear()
            await email.undo_feedback(app.principal, UUID(str(marked["feedback_id"])))
            assert calls == first, "an undo follows the same path as the judgment it undoes"
            return first

        assert await given("thread", "important") == ["own"]
        assert await given("thread", "less_important") == ["own"]
        assert await given("person", "important") == ["own", "others", "own"]
        assert await given("person", "less_important") == ["own", "others", "own"]
        for judgment in ("needs_reply", "no_reply_needed"):
            assert await given("thread", judgment) == []
            assert await given("person", judgment) == []


async def test_a_follow_up_that_a_pause_holds_is_not_counted_as_a_failure() -> None:
    """The refresh reads the pause before it follows up a mark, and the owner can pause
    in between. The re-rating then writes nothing, and nothing was tried: the mark keeps
    its place and its time, so the follow-up runs as soon as learning resumes."""
    from unittest.mock import AsyncMock

    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.email import EmailLearningState, EmailRecord, EmailThread
    from agent_core.runtime.email_tasks import _TaskIO
    from tests.contract.people_mail_fixtures import correspondence_stack
    from tests.contract.support import principal

    factory, semantics = await correspondence_stack()
    owner = principal()
    thread = EmailThread(
        id=UUID(int=5_100),
        account_id="work",
        provider_thread_id="t-mail",
        subject="Atlas board",
        updated_at=NOW,
        last_accessed_at=NOW,
    )
    marked: dict[str, Any] = {"priority": 0.5, "rerating_pending": True}
    rows = {
        ("thread", str(thread.id)): thread.model_dump(mode="json"),
        ("assessment", str(thread.id)): marked,
        ("learning", "shared"): EmailLearningState(paused=True).model_dump(mode="json"),
    }
    async with factory() as uow:
        for (kind, key), payload in rows.items():
            await uow.email.put(
                EmailRecord(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kind=kind,
                    key=key,
                    revision=1,
                    payload=payload,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
    io = _TaskIO.__new__(_TaskIO)
    vars(io).update(
        context=SimpleNamespace(
            run=None,
            lease=None,
            uow_factory=factory,
            principal=owner,
            clock=FixedClock(NOW),
        ),
        task=SimpleNamespace(account_ids=["work"]),
        semantics=semantics,
        # What the refresh read before the owner paused learning.
        service=SimpleNamespace(learning_context=AsyncMock(return_value={"paused": False})),
        found_work=False,
        _write_fence=AsyncMock(),
    )
    await _TaskIO._resume_reratings(io)

    async with factory() as uow:
        row = await uow.email.get(owner, "assessment", str(thread.id))
    assert row is not None and (row.revision, row.payload) == (1, marked)


async def test_the_refresh_continues_when_a_memory_rerating_is_refused() -> None:
    from unittest.mock import AsyncMock

    from agent_core.runtime.email_tasks import _TaskIO

    semantics = SimpleNamespace(rerate_thread=AsyncMock(side_effect=ConflictError("erased")))
    run, lease = SimpleNamespace(session_id=UUID(int=1)), object()
    io = _TaskIO.__new__(_TaskIO)
    vars(io).update(context=SimpleNamespace(run=run, lease=lease), semantics=semantics)
    thread = SimpleNamespace(account_id="work", provider_thread_id="t1")
    await _TaskIO._rerate_memories(io, thread)  # type: ignore[arg-type]
    semantics.rerate_thread.assert_awaited_once_with("work", "t1", run=run, lease=lease)


async def test_pending_reratings_that_keep_failing_do_not_hold_back_the_rest() -> None:
    """A slice follows up twenty debts. When the same twenty are refused every time, each
    is put off for a while, so the twenty-first has its turn in the next slice. The
    marks are found among all saved assessments, archived conversations included, and
    only this task's accounts' are followed up."""
    from unittest.mock import AsyncMock

    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.email import EmailRecord, EmailThread
    from agent_core.runtime.email_tasks import RERATING_RESUME_LIMIT, _TaskIO
    from tests.contract.people_mail_fixtures import correspondence_stack
    from tests.contract.support import principal

    factory, _ = await correspondence_stack()
    owner = principal()

    def conversation(number: int, account_id: str, provider_thread_id: str) -> EmailThread:
        return EmailThread(
            id=UUID(int=5_000 + number),
            account_id=account_id,
            provider_thread_id=provider_thread_id,
            subject="Atlas board",
            updated_at=NOW,
            last_accessed_at=NOW,
            # Archived: in no shortlist of arriving mail.
            in_inbox=False,
        )

    threads = [conversation(n, "work", f"t-{n:02}") for n in range(RERATING_RESUME_LIMIT + 1)]
    # Another mailbox's debt, which this task does not cover, sorts before them all.
    elsewhere = conversation(-1, "home", "t-home")
    async with factory() as uow:
        for thread in (elsewhere, *threads):
            seeded: tuple[tuple[str, dict[str, object]], ...] = (
                ("thread", thread.model_dump(mode="json")),
                ("assessment", {"priority": 0.5, "rerating_pending": True}),
            )
            for kind, payload in seeded:
                await uow.email.put(
                    EmailRecord(
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        kind=kind,
                        key=str(thread.id),
                        revision=1,
                        payload=payload,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    expected_revision=0,
                )
    last = threads[-1].provider_thread_id
    attempted: list[str] = []

    async def refusing(account_id: str, thread_id: str, **kwargs: Any) -> EmailRerateReport:
        assert account_id == "work", "a task follows up only its own accounts' debts"
        attempted.append(thread_id)
        if thread_id != last:
            raise ConflictError("erased")
        return EmailRerateReport()

    clock = FixedClock(NOW)
    io = _TaskIO.__new__(_TaskIO)
    vars(io).update(
        context=SimpleNamespace(
            run=SimpleNamespace(id=UUID(int=2), session_id=UUID(int=1)),
            lease=object(),
            uow_factory=factory,
            principal=owner,
            clock=clock,
        ),
        task=SimpleNamespace(account_ids=["work"]),
        semantics=SimpleNamespace(people_enabled=True, rerate_thread=refusing),
        service=SimpleNamespace(learning_context=AsyncMock(return_value={})),
        found_work=False,
        # The lease fence is the worker's own and is not what this test is about.
        _write_fence=AsyncMock(),
    )

    async def slice_() -> list[str]:
        attempted.clear()
        await _TaskIO._resume_reratings(io)
        assert len(attempted) <= RERATING_RESUME_LIMIT
        return list(attempted)

    assert await slice_() == [thread.provider_thread_id for thread in threads[:-1]]
    # The refused twenty are put off; the one they held back is followed up now.
    assert await slice_() == [last]
    assert await slice_() == []
    # Their time comes again, and debts with fewer failures behind them go first.
    clock.advance(timedelta(minutes=2))
    assert last not in await slice_()
    clock.advance(timedelta(minutes=2))
    assert await slice_() == [last]
    async with factory() as uow:
        rows = await uow.email.list(owner, "assessment", limit=100)
    assert all(row.payload["rerating_pending"] is True for row in rows)
    assert {row.updated_at for row in rows} == {NOW}
    # The other mailbox's debt was never tried and never counted.
    [untried] = [row for row in rows if row.key == str(elsewhere.id)]
    assert untried.revision == 1


async def test_a_new_mail_slice_follows_up_a_rerating_owed_by_an_archived_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new-mail slice assesses only what recently arrived in the Inbox. A conversation
    archived after its assessment was saved is in no such shortlist, and above the
    learning share no full slice runs; its mark is found all the same."""
    from decimal import Decimal

    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.email import EmailBudgetLimits
    from tests.gates.test_email_arrival_checks_m26 import _seed_spend

    stopped = True
    original = EmailPeopleFormationService.rerate_thread

    async def stopping(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        if stopped:
            return EmailRerateReport()
        return await original(self, account_id, thread_id, **kwargs)

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", stopping)
    clock = FixedClock(NOW)
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        clock=clock,
    ) as app:
        service = app.services.email
        await service.submit_task(app.principal, kind="refresh")
        [memory] = await _memories(app)
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        await service.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        assert (await _memories(app))[0].expires_at == memory.expires_at
        assert await _owed(app) is True
        # The owner archives the conversation: it leaves the Inbox.
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            [row] = await uow.email.list(app.principal, "thread")
            await uow.email.put(
                row.model_copy(
                    update={
                        "revision": row.revision + 1,
                        "payload": {**row.payload, "in_inbox": False},
                    }
                ),
                expected_revision=row.revision,
            )
        stopped = False

        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("40"), monthly_cost=Decimal("600")
        )
        await _seed_spend(app, "460", NOW - timedelta(days=10))
        clock.advance(timedelta(minutes=1))
        operation = await service.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        task = await service.get_task(app.principal, run.id)
        assert task is not None and task.scope == "new_mail" and run.usage.model_calls == 0
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at is None
        assert await _owed(app) is None and task.empty_check is False


def test_a_put_off_rerating_waits_longer_each_time_and_a_new_save_starts_afresh() -> None:
    from agent_core.domain.email_memory import (
        rerating_attempts,
        rerating_due,
        rerating_put_off,
        rerating_settled,
    )
    from agent_core.runtime.email_tasks import _TaskIO

    marked: dict[str, Any] = {"priority": 0.5, "rerating_pending": True}
    assert rerating_due(marked, NOW) and rerating_attempts(marked) == 0
    once = rerating_put_off(marked, NOW)
    assert rerating_attempts(once) == 1
    assert not rerating_due(once, NOW + timedelta(minutes=1))
    assert rerating_due(once, NOW + timedelta(minutes=2))
    often = marked
    for _ in range(40):
        often = rerating_put_off(often, NOW)
    # However often it failed, a debt is tried again within a day.
    assert not rerating_due(often, NOW + timedelta(hours=23))
    assert rerating_due(often, NOW + timedelta(days=1))
    # A time that cannot be read holds nothing back.
    assert rerating_due({**once, "rerating_retry_at": "soon"}, NOW)
    assert rerating_settled(often) == {"priority": 0.5}

    # An assessment saved again reuses the stored one; its debt is a new one.
    io = _TaskIO.__new__(_TaskIO)
    vars(io).update(semantics=SimpleNamespace(people_enabled=True))
    assert _TaskIO._owing_rerating(io, often) == marked
    vars(io).update(semantics=SimpleNamespace(people_enabled=False))
    assert _TaskIO._owing_rerating(io, often) == {"priority": 0.5}


async def test_a_slice_that_only_resumed_a_rerating_is_not_an_empty_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new-mail slice can find no mail and still change memories, by following up a
    re-rating an earlier refresh left owing. Its run holds the audit event of that
    change, so it is no empty check and its session is not removed with them."""
    from decimal import Decimal

    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.email import EmailBudgetLimits
    from tests.gates.test_email_arrival_checks_m26 import _seed_spend

    stopped = True
    original = EmailPeopleFormationService.rerate_thread

    async def stopping(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        if stopped:
            return EmailRerateReport()
        return await original(self, account_id, thread_id, **kwargs)

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", stopping)
    clock = FixedClock(NOW)
    settings = replace(_email_settings(), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_assessment(0.6)]),
        mcp_client_factory=await _mail(census=False),
        clock=clock,
    ) as app:
        service = app.services.email

        async def rerated(run: Any) -> bool:
            async with app.uow_factory() as uow:
                events = await uow.events.list_after(run.session_id, 0, app.principal)
            return "email.memory.rerated" in [event.event_type for event in events]

        await service.submit_task(app.principal, kind="refresh")
        [memory] = await _memories(app)
        assert memory.expires_at == memory.last_evidence_at + timedelta(days=90)
        async with app.uow_factory() as uow:
            [thread] = await uow.email.list(app.principal, "thread")
        # The owner marks the thread Important while no re-rating can run.
        await service.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="thread",
            judgment="important",
        )
        assert (await _memories(app))[0].expires_at == memory.expires_at
        stopped = False

        # Above the learning share a refresh reads only new mail, and none arrived.
        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("40"), monthly_cost=Decimal("600")
        )
        await _seed_spend(app, "460", NOW - timedelta(days=10))
        clock.advance(timedelta(minutes=1))
        operation = await service.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        task = await service.get_task(app.principal, run.id)
        assert task is not None and task.scope == "new_mail"
        assert run.usage.model_calls == 0
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at is None
        assert await rerated(run)
        assert task.empty_check is False

        clock.advance(timedelta(days=7, minutes=1))
        assert await service.prune_empty_checks(app.principal) == 0
        assert await rerated(run)

        # A slice that finds nothing, and follows up nothing, is still an empty check.
        operation = await service.submit_task(app.principal, kind="refresh")
        quiet = await service.get_task(app.principal, operation.run_id)
        assert quiet is not None and quiet.scope == "new_mail" and quiet.empty_check is True


async def test_the_owner_command_previews_unless_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core.cli import main
    from tests.contract.support import principal

    owner = principal()
    calls: list[tuple[object, bool]] = []

    async def rerating(who: object, confirm: bool) -> EmailRerateReport:
        calls.append((who, confirm))
        return EmailRerateReport(applied=confirm, examined=3, changed=2)

    available: Any = rerating

    @asynccontextmanager
    async def fake_build(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        assert kwargs == {"storage": "postgres"}
        yield SimpleNamespace(principal=owner, email_memory_rerating=available)

    monkeypatch.setattr(main, "build", fake_build)
    identity = f"{owner.tenant_id}/{owner.principal_id}"
    with pytest.raises(AuthorizationError):
        await main._memory_rerate_email("other/owner", True)
    preview = await main._memory_rerate_email(identity, False)
    assert preview["applied"] is False and preview["changed"] == 2
    applied = await main._memory_rerate_email(identity, True)
    assert applied["applied"] is True and calls == [(owner, False), (owner, True)]
    available = None
    with pytest.raises(NotFoundError, match="disabled"):
        await main._memory_rerate_email(identity, False)


def test_the_owner_command_is_registered_under_memory() -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    result = CliRunner().invoke(app, ["memory", "rerate-email", "--help"])
    assert result.exit_code == 0
    assert "--confirm" in result.output and "--owner" in result.output
