"""Real PostgreSQL execution of the source erasure contract."""

import pytest

from agent_core.bootstrap import build
from tests.contract.support import principal
from tests.contract.test_email_source_erasure_contract import (
    assert_email_source_erasure,
    assert_erasure_waits_for_potential_producer,
)
from tests.integration.m2_support import database_settings


async def test_postgres_source_revision_erasure_batches_matching_events() -> None:
    import json
    from typing import Any
    from uuid import uuid4

    from sqlalchemy import Engine, event, select

    from agent_core.adapters.persistence.database import create_engine, create_session_factory
    from agent_core.adapters.persistence.sqlalchemy_models import MemoryRevisionRow
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import TextPart, ToolResultItem
    from agent_core.domain.policies import TrustLevel
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW, session

    settings = database_settings()
    first = session().model_copy(
        update={
            "metadata": {
                "email_account_servers": {"work": {"read": "gmail_work_read"}},
            }
        }
    )
    other = session().model_copy(update={"id": uuid4()})
    foreign = session().model_copy(update={"id": uuid4(), "principal_id": "another-owner"})
    result = ToolResultItem(
        call_id="selected",
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        content=[
            TextPart(
                text=json.dumps(
                    {"thread_id": "t1", "messages": [{"id": "m1", "body": "Selected content"}]}
                )
            )
        ],
    )
    survivors = set()
    async with build(settings=settings, storage="postgres", principal=principal()) as app:
        async with app.uow_factory() as uow:
            for source in [first, other, foreign]:
                await uow.sessions.create(source)
            for index in range(3):
                source_event = await uow.events.append(
                    NewEvent(
                        session_id=first.id,
                        run_id=None,
                        event_type="tool.call.completed",
                        actor_type="runtime",
                        payload={
                            "name": "mcp.gmail_work_read.get_thread_page",
                            "result_item": result.model_dump(mode="json"),
                        },
                    )
                )
                await uow.memories.upsert_belief(
                    memory().model_copy(
                        update={
                            "id": uuid4(),
                            "source_session_id": first.id,
                            "source_event_ids": [source_event.sequence],
                            "store_position": index + 1,
                        }
                    )
                )
            for index, (source, sequences) in enumerate(
                [(first, [999]), (other, [1]), (foreign, [1])], start=4
            ):
                belief = memory().model_copy(
                    update={
                        "id": uuid4(),
                        "principal_id": source.principal_id,
                        "source_session_id": source.id,
                        "source_event_ids": sequences,
                        "store_position": index,
                    }
                )
                survivors.add(belief.id)
                await uow.memories.upsert_belief(belief)
        deletes = []

        def count_deletes(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _many: bool,
        ) -> None:
            if statement.startswith("DELETE FROM memory_revisions"):
                deletes.append(statement)

        event.listen(Engine, "before_cursor_execute", count_deletes)
        try:
            async with app.uow_factory() as uow:
                await uow.session_deletions.erase_email_source(
                    principal(), "work", "t1", frozenset({"m1"}), NOW
                )
        finally:
            event.remove(Engine, "before_cursor_execute", count_deletes)
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as db:
                assert set(await db.scalars(select(MemoryRevisionRow.belief_id))) == survivors
        finally:
            await engine.dispose()
        assert len(deletes) == 1, "one source with multiple events should use one revision delete"


async def test_postgres_source_erasure_contract() -> None:
    async with build(
        settings=database_settings(), storage="postgres", principal=principal()
    ) as composition:
        await assert_email_source_erasure(composition.uow_factory)


@pytest.mark.parametrize("kind", ["bound", "chat_scope", "gmail_scope", "invocation", "unrelated"])
async def test_postgres_erasure_waits_for_inflight_source_producer(kind: str) -> None:
    async with build(
        settings=database_settings(), storage="postgres", principal=principal()
    ) as composition:
        await assert_erasure_waits_for_potential_producer(composition.uow_factory, kind)
