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
        app.services.email.rerate_memories = interrupted
        await app.services.email.undo_feedback(app.principal, feedback_id)
        assert (await _memories(app))[0].expires_at is None
        app.services.email.rerate_memories = rerate
        await app.services.email.undo_feedback(app.principal, feedback_id)
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
        assert {row.expires_at for row in await _memories(app)} == {None}

        # Feedback about replies changes no importance, so no other thread is visited.
        email.lengthen_memories = unavailable
        await email.feedback(
            app.principal,
            thread_id=UUID(str(thread.payload["id"])),
            target="person",
            judgment="needs_reply",
        )
        assert len(asked) == 1


async def test_an_abstaining_assessment_leaves_the_threads_memories_as_they_were() -> None:
    """A result that cannot be grounded is saved as zero features, and rates the thread zero."""
    ungrounded = json.loads(_assessment(0.8).text or "")
    ungrounded.update(supported_evidence=["A sentence the message does not hold."], people_facts=[])
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
        [kept] = await _memories(app)
        assert kept.id == memory.id and kept.expires_at == memory.expires_at


async def test_a_junk_first_pass_assessment_rerates_the_threads_memories_too(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every saved assessment is followed by a re-rating, whichever path saved it."""
    from agent_core.adapters.judgment import FakeJudgmentProvider
    from tests.unit.test_email_junk_pilot import pilot_decision, pilot_settings, work_factory

    rerated: list[tuple[str, bool]] = []

    async def recording(self: Any, account_id: str, thread_id: str, **kwargs: Any) -> Any:
        rerated.append((account_id, kwargs.get("run") is not None))
        return EmailRerateReport()

    monkeypatch.setattr(EmailPeopleFormationService, "rerate_thread", recording)
    settings = replace(pilot_settings(tmp_path), email_mode_enabled=True, people_enabled=True)
    async with build(
        settings=settings,
        judgment_provider_override=FakeJudgmentProvider([pilot_decision()]),
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await work_factory(),
    ) as app:
        for saved in (1, 2):
            # The second refresh reuses the first pass's cached verdict and saves it again.
            operation = await app.services.email.submit_task(app.principal, kind="refresh")
            run = await app.runs.get(operation.run_id)
            assert run.status is RunStatus.COMPLETED, run.failure
            async with app.uow_factory() as uow:
                [assessment] = await uow.email.list(app.principal, "assessment")
            assert assessment.payload["junk_bypassed"] and run.usage.model_calls == 0
            assert rerated == [("work", True)] * saved


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
