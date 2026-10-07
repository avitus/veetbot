"""PostgreSQL behavior of generated conversation titles (ADR-0155)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.bootstrap import build
from agent_core.domain.events import NewEvent
from agent_core.domain.sessions import SessionTitleSource
from tests.contract.support import NOW, principal, session
from tests.contract.test_session_repository_contract import assert_generated_title_lifecycle
from tests.integration.m2_support import database_settings
from tests.integration.test_migrations import _alembic

# The revision before the title columns; its downgrade drops them.
BEFORE_TITLE_SOURCE = "d0143a76b201"


async def test_postgres_generated_title_lifecycle() -> None:
    async with (
        build(settings=database_settings(), principal=principal(), storage="postgres") as app,
        app.uow_factory() as uow,
    ):
        await assert_generated_title_lifecycle(uow.sessions)


async def test_a_recovered_legacy_title_is_regenerable() -> None:
    legacy = session().model_copy(update={"id": UUID(int=901), "title": None})
    owner = principal()
    async with build(settings=database_settings(), principal=owner, storage="postgres") as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(legacy)
            await uow.events.append(
                NewEvent(
                    session_id=legacy.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": [{"kind": "text", "text": "Plan the  garden"}]},
                )
            )
        async with app.uow_factory() as uow:
            assert (await uow.sessions.get(legacy.id, owner)).title == "Plan the garden"
            assert await uow.sessions.request_title(legacy.id, owner, NOW)
            [pending] = await uow.sessions.pending_title_requests(owner, limit=10)
            assert pending.title_source is SessionTitleSource.FIRST_MESSAGE


async def test_the_title_source_backfill_follows_session_metadata() -> None:
    owner = principal()
    rows: dict[str, tuple[UUID, str | None, dict[str, Any]]] = {
        "chat": (UUID(int=911), "can you look at this", {}),
        "email": (UUID(int=912), "Re: Board agenda", {"email_thread_id": "thread"}),
        "operational": (UUID(int=913), "Mailbox", {"email_operational": True}),
        "schedule": (UUID(int=914), "Daily review", {"schedule_id": "schedule"}),
        "delegated": (UUID(int=915), "Research", {"run_kind": "delegated"}),
        "triage": (UUID(int=916), "Device triage", {"device_triage": {"channel": "sms"}}),
        "people": (UUID(int=918), "People history import", {"purpose": "people-import"}),
        "untitled": (UUID(int=917), None, {}),
    }
    settings = database_settings()
    async with build(settings=settings, principal=owner, storage="postgres") as app:
        async with app.uow_factory() as uow:
            for session_id, title, metadata in rows.values():
                await uow.sessions.create(
                    session().model_copy(
                        update={"id": session_id, "title": title, "metadata": metadata}
                    )
                )
        _alembic("downgrade", BEFORE_TITLE_SOURCE)
        try:
            engine = create_engine(settings.database_url)
            try:
                async with create_session_factory(engine)() as db:
                    count = (await db.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
                    assert count == len(rows)
            finally:
                await engine.dispose()
        finally:
            _alembic("upgrade", "head")
        async with app.uow_factory() as uow:
            requested = {
                name: await uow.sessions.request_title(session_id, owner, NOW)
                for name, (session_id, _, _) in rows.items()
            }
            pending = await uow.sessions.pending_title_requests(owner, limit=10)
    assert requested == {
        "chat": True,
        "email": False,
        "operational": False,
        "schedule": False,
        "delegated": False,
        "triage": False,
        "people": False,
        "untitled": False,
    }
    assert [(row.session_id, row.title_source) for row in pending] == [
        (rows["chat"][0], SessionTitleSource.FIRST_MESSAGE)
    ]


async def test_the_title_pass_runs_against_postgres() -> None:
    from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
    from agent_core.domain.agents import Principal
    from agent_core.titles.generator import TitleDecision, TitleInput, TitleOutcome
    from agent_core.titles.profiles import TitleGenerationProfile
    from agent_core.titles.titling import TITLE_CHECKED_EVENT, ConversationTitlePass

    class _Titler:
        def __init__(self) -> None:
            self.inputs: list[TitleInput] = []

        async def title(self, title_input: TitleInput, *, principal: Principal) -> TitleOutcome:
            del principal
            self.inputs.append(title_input)
            return TitleOutcome(decision=TitleDecision.REPLACED, title="Garden planting plan")

    owner = principal()
    chat = session().model_copy(update={"id": UUID(int=921), "title": None})
    async with build(settings=database_settings(), principal=owner, storage="postgres") as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(chat)
            await uow.sessions.set_title_if_missing(chat.id, owner, "can you help")
            for text_value in ("can you help", "Which tomatoes grow in shade?"):
                await uow.events.append(
                    NewEvent(
                        session_id=chat.id,
                        run_id=None,
                        event_type="user.message.created",
                        actor_type="principal",
                        actor_id=owner.principal_id,
                        payload={"content": [{"kind": "text", "text": text_value}]},
                    )
                )
            assert await uow.sessions.request_title(chat.id, owner, NOW)
        titler = _Titler()
        title_pass = ConversationTitlePass(
            uow_factory=app.uow_factory,
            clock=FixedClock(NOW),
            ids=SequenceIdFactory([UUID(int=n) for n in range(9300, 9310)]),
            principal=owner,
            profile=TitleGenerationProfile(),
            titler=titler,
        )
        assert await title_pass.run_once() == 1
        async with app.uow_factory() as uow:
            assert (await uow.sessions.get(chat.id, owner)).title == "Garden planting plan"
            assert await uow.sessions.pending_title_requests(owner, limit=10) == []
            audits = await uow.process_events.list(TITLE_CHECKED_EVENT)
    assert [input_.latest_messages for input_ in titler.inputs] == [
        ("Which tomatoes grow in shade?",)
    ]
    assert titler.inputs[0].first_message == "can you help"
    assert [audit.payload["outcome"] for audit in audits] == ["replaced"]
