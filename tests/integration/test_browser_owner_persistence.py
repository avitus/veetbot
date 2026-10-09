"""Durable, principal-scoped same-chat website binding."""

from agent_core.bootstrap import build
from tests.contract.support import principal
from tests.contract.test_session_repository_contract import (
    assert_browser_binding_is_scoped_and_preserves_metadata,
)
from tests.integration.m2_support import database_settings


async def test_postgres_browser_binding() -> None:
    async with (
        build(settings=database_settings(), principal=principal(), storage="postgres") as app,
        app.uow_factory() as uow,
    ):
        await assert_browser_binding_is_scoped_and_preserves_metadata(uow.sessions)


async def test_binding_rolls_back_and_admission_serializes_workers() -> None:
    import asyncio
    from uuid import UUID

    import pytest

    from tests.contract.support import session

    owner = principal()
    async with build(settings=database_settings(), principal=owner, storage="postgres") as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        with pytest.raises(RuntimeError, match="abort"):
            async with app.uow_factory() as uow:
                await uow.sessions.bind_browser_profile(session().id, owner, UUID(int=172))
                raise RuntimeError("abort")
        async with app.uow_factory() as uow:
            assert (
                "browser_profile_id" not in (await uow.sessions.get(session().id, owner)).metadata
            )
        entered = asyncio.Event()

        async def contender() -> None:
            async with (
                app.uow_factory() as second,
                second.sessions.admission(session().id, owner) as chat,
            ):
                assert chat.metadata["browser_profile_id"] == str(UUID(int=173))
                entered.set()

        async with app.uow_factory() as first, first.sessions.admission(session().id, owner):
            task = asyncio.create_task(contender())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.wait(), timeout=0.05)
            await first.sessions.bind_browser_profile(session().id, owner, UUID(int=173))
        await asyncio.wait_for(task, timeout=2)
        assert entered.is_set()
