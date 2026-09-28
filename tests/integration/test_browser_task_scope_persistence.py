"""Real transaction checks for the owner-managed browser task scope list."""

import asyncio

import pytest
from sqlalchemy import text

from agent_core.bootstrap import build
from agent_core.domain.browser_task_grants import BrowserTaskScopePolicy
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import ProcessEvent
from tests.contract.test_browser_task_scope_repository_contract import (
    SCOPES,
    assert_scope_policy_contract,
)
from tests.integration.m2_support import database_settings


async def test_postgres_scope_policy_contract() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as app,
        app.uow_factory() as uow,
    ):
        await assert_scope_policy_contract(uow.browser_task_grants, app.principal)


async def test_policy_survives_new_connections_and_rollback_does_not_widen_it() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:
        async with app.uow_factory() as uow, uow.browser_task_grants.locked_scopes(app.principal):
            await uow.browser_task_grants.replace_scopes(
                app.principal, BrowserTaskScopePolicy(revision=1)
            )
        with pytest.raises(RuntimeError, match="audit failure"):
            async with (
                app.uow_factory() as uow,
                uow.browser_task_grants.locked_scopes(app.principal),
            ):
                await uow.browser_task_grants.replace_scopes(
                    app.principal, BrowserTaskScopePolicy(revision=2, scopes=SCOPES)
                )
                raise RuntimeError("audit failure")
        async with (
            app.uow_factory() as uow,
            uow.browser_task_grants.locked_scopes(app.principal, defaults=SCOPES) as policy,
        ):
            assert policy == BrowserTaskScopePolicy(revision=1)


async def test_competing_clients_cannot_overwrite_each_other() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:

        async def edit_scope() -> str:
            async with (
                app.uow_factory() as uow,
                uow.browser_task_grants.locked_scopes(app.principal),
            ):
                try:
                    await uow.browser_task_grants.replace_scopes(
                        app.principal, BrowserTaskScopePolicy(revision=1, scopes=SCOPES)
                    )
                except ConflictError:
                    return "conflict"
                return "saved"

        assert sorted(await asyncio.gather(edit_scope(), edit_scope())) == ["conflict", "saved"]


async def test_policy_lock_serializes_removal_with_worker_authorization() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:
        attempted = asyncio.Event()
        entered = asyncio.Event()

        async def worker() -> BrowserTaskScopePolicy:
            attempted.set()
            async with (
                app.uow_factory() as uow,
                uow.browser_task_grants.locked_scopes(app.principal) as policy,
            ):
                entered.set()
                return policy

        async with (
            app.uow_factory() as uow,
            uow.browser_task_grants.locked_scopes(app.principal, defaults=SCOPES),
        ):
            await uow.browser_task_grants.replace_scopes(
                app.principal, BrowserTaskScopePolicy(revision=1)
            )
            task = asyncio.create_task(worker())
            await attempted.wait()
            await asyncio.sleep(0.05)
            assert not entered.is_set()
        assert await asyncio.wait_for(task, 5) == BrowserTaskScopePolicy(revision=1)


async def test_scope_policy_has_forced_tenant_rls() -> None:
    from agent_core.adapters.persistence.database import create_engine

    engine = create_engine(database_settings().database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'browser_task_scope_policies'"
                    )
                )
            ).one()
            assert tuple(row) == (True, True)
    finally:
        await engine.dispose()


async def test_scope_edit_and_grant_removal_roll_back_if_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.persistence.repositories import PostgresProcessEventRepository
    from agent_core.application.browser_task_grants import PublicBrowserTaskGrantService
    from tests.contract.support import NOW
    from tests.contract.test_browser_task_grant_repository_contract import grant
    from tests.integration.test_browser_task_grant_persistence import _seeded

    async with _seeded() as (app, owner):
        owner = owner.model_copy(update={"scopes": {"browser.grant.write", "browser.grant.read"}})
        async with (
            app.uow_factory() as uow,
            uow.browser_task_grants.locked_scopes(owner, defaults=SCOPES),
        ):
            created = await uow.browser_task_grants.create(grant(owner=owner))
        service = PublicBrowserTaskGrantService(uow_factory=app.uow_factory, clock=FixedClock(NOW))
        original = PostgresProcessEventRepository.append

        async def fail_audit(
            self: PostgresProcessEventRepository, event: ProcessEvent
        ) -> ProcessEvent:
            if event.event_type == "browser.task_scopes.updated":
                raise RuntimeError("audit unavailable")
            return await original(self, event)

        with monkeypatch.context() as patch:
            patch.setattr(PostgresProcessEventRepository, "append", fail_audit)
            with pytest.raises(RuntimeError, match="audit unavailable"):
                await service.update_scopes(owner, BrowserTaskScopePolicy(revision=0))
        assert (await service.get_scopes(owner)).scopes == SCOPES
        async with app.uow_factory() as uow:
            assert (await uow.browser_task_grants.get(created.id, owner)).ended_at is None
            assert not await uow.events.list_after(created.session_id, 0, owner)
        assert (
            await service.update_scopes(owner, BrowserTaskScopePolicy(revision=0))
        ).revision == 1
        async with app.uow_factory() as uow:
            assert (
                await uow.browser_task_grants.get(created.id, owner)
            ).end_reason == "scope_removed"
            assert len(await uow.events.list_after(created.session_id, 0, owner)) == 1
