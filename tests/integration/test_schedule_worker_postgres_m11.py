"""PostgreSQL proof for the bounded Milestone 11 schedule worker."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.schedule_wakeup import PostgresScheduleWakeup
from agent_core.bootstrap import build, build_schedule_worker
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import DurableWorker
from agent_core.scheduling.worker import ScheduleWorker
from scripts.check_schedule_database_permissions import REQUIRED_TABLE_PRIVILEGES
from tests.contract.support import agent
from tests.contract.test_schedule_unit_of_work_contract import (
    assert_schedule_unit_of_work_contract,
)
from tests.integration.m2_support import database_settings
from tests.integration.test_schedule_materializer_m11 import (
    NOW,
    _assert_complete,
    _create_due_schedule,
    _materializer,
)


def _production_schedule_settings(database_url: str | None = None) -> Settings:
    settings = database_settings()
    return replace(
        settings,
        database_url=database_url or settings.database_url,
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=None,
        sandbox=SandboxMechanism.GVISOR,
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
    )


@asynccontextmanager
async def _release_schedule_role() -> AsyncIterator[str]:
    """Yield a login URL holding exactly the privileges release validation allows."""

    admin_url = make_url(database_settings().database_url)
    role = f"veetbot_schedule_probe_{uuid4().hex[:12]}"
    password = uuid4().hex
    engine = create_engine(admin_url.render_as_string(hide_password=False))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            for table, privileges in REQUIRED_TABLE_PRIVILEGES.items():
                await connection.execute(
                    text(f"GRANT {', '.join(sorted(privileges))} ON {table} TO {role}")
                )
        try:
            yield admin_url.set(username=role, password=password).render_as_string(
                hide_password=False
            )
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await engine.dispose()


async def test_postgres_schedule_wakeup_crosses_process_connections() -> None:
    settings = database_settings()
    listener = PostgresScheduleWakeup(settings.database_url)
    publisher = PostgresScheduleWakeup(settings.database_url)
    try:
        waiting = asyncio.create_task(listener.wait(5))
        for _attempt in range(25):
            if waiting.done():
                break
            await publisher.notify()
            done, _pending = await asyncio.wait({waiting}, timeout=0.2)
            if done:
                break
        await asyncio.wait_for(waiting, timeout=1)
    finally:
        await listener.close()
        await publisher.close()


async def test_lean_production_schedule_role_constructs_without_execution_credentials() -> None:
    async with build_schedule_worker(settings=_production_schedule_settings()) as worker:
        assert isinstance(worker, ScheduleWorker)
        await assert_schedule_unit_of_work_contract(worker._uow_factory)


async def test_release_privileged_schedule_role_materializes_a_due_run() -> None:
    """The production scheduler needs no privilege beyond the release allowlist."""

    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_id = uuid4()
        await _create_due_schedule(composition, schedule_id)

        async with (
            _release_schedule_role() as role_url,
            build_schedule_worker(
                settings=_production_schedule_settings(role_url), clock=FixedClock(NOW)
            ) as worker,
        ):
            assert await worker.run_once() == 1

        async with composition.uow_factory() as uow:
            [occurrence] = await uow.schedule_occurrences.list(
                schedule_id, composition.principal, limit=10
            )
        await _assert_complete(composition, schedule_id, occurrence)


async def test_release_privileged_schedule_role_cannot_project_a_committed_session() -> None:
    """Committed history still takes the erasure lock the scheduler cannot hold."""

    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        pinned_agent = agent()
        session_id = uuid4()
        async with composition.uow_factory() as uow:
            await uow.agents.put(pinned_agent)
            await uow.sessions.create(
                Session(
                    id=session_id,
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    agent_id=pinned_agent.id,
                    agent_version=pinned_agent.version,
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="user",
                    actor_id=composition.principal.principal_id,
                    payload={"content": "Committed before the scheduler looked."},
                )
            )

        async with (
            _release_schedule_role() as role_url,
            build_schedule_worker(
                settings=_production_schedule_settings(role_url), clock=FixedClock(NOW)
            ) as worker,
        ):
            with pytest.raises(DBAPIError, match="permission denied for table events"):
                async with worker._uow_factory() as uow:
                    await uow.history.catch_up(session_id)


async def test_reserved_worker_classes_preserve_interactive_and_async_progress() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        pinned_agent = agent()
        async_run_ids = []
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            await uow.agents.put(pinned_agent)
            for _index in range(8):
                session_id, run_id = uuid4(), uuid4()
                async_run_ids.append(run_id)
                await uow.sessions.create(
                    Session(
                        id=session_id,
                        tenant_id=composition.principal.tenant_id,
                        principal_id=composition.principal.principal_id,
                        agent_id=pinned_agent.id,
                        agent_version=pinned_agent.version,
                        status=SessionStatus.ACTIVE,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                await uow.queue.enqueue(
                    Run(
                        id=run_id,
                        session_id=session_id,
                        tenant_id=composition.principal.tenant_id,
                        agent_id=pinned_agent.id,
                        agent_version=pinned_agent.version,
                        status=RunStatus.QUEUED,
                        limits=RunLimits(),
                        priority=10,
                        scheduled_for=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    priority=10,
                    scheduled_for=NOW,
                )
            interactive_session_id, interactive_run_id = uuid4(), uuid4()
            await uow.sessions.create(
                Session(
                    id=interactive_session_id,
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    agent_id=pinned_agent.id,
                    agent_version=pinned_agent.version,
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await uow.queue.enqueue(
                Run(
                    id=interactive_run_id,
                    session_id=interactive_session_id,
                    tenant_id=composition.principal.tenant_id,
                    agent_id=pinned_agent.id,
                    agent_version=pinned_agent.version,
                    status=RunStatus.QUEUED,
                    limits=RunLimits(),
                    priority=0,
                    scheduled_for=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                priority=0,
                scheduled_for=NOW,
            )

        interactive = composition.worker_factory("interactive-reserved")
        asynchronous = composition.async_worker_factory("async-reserved")
        assert isinstance(interactive, DurableWorker)
        assert isinstance(asynchronous, DurableWorker)
        interactive_claim, async_claim = await asyncio.gather(
            interactive.claim(), asynchronous.claim()
        )
        assert interactive_claim is not None
        assert interactive_claim.run.id == interactive_run_id
        assert async_claim is not None
        assert async_claim.run.id in async_run_ids


async def test_claim_metric_runs_after_commit_and_cannot_rollback_claim() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        pinned_agent = agent()
        session_id, run_id = uuid4(), uuid4()
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            await uow.agents.put(pinned_agent)
            await uow.sessions.create(
                Session(
                    id=session_id,
                    tenant_id=composition.principal.tenant_id,
                    principal_id=composition.principal.principal_id,
                    agent_id=pinned_agent.id,
                    agent_version=pinned_agent.version,
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await uow.queue.enqueue(
                Run(
                    id=run_id,
                    session_id=session_id,
                    tenant_id=composition.principal.tenant_id,
                    agent_id=pinned_agent.id,
                    agent_version=pinned_agent.version,
                    status=RunStatus.QUEUED,
                    limits=RunLimits(),
                    priority=7,
                    scheduled_for=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                priority=7,
                scheduled_for=NOW,
            )

        labels: list[str] = []

        def failing_metric(worker_class: str, duration_seconds: float) -> None:
            assert duration_seconds >= 0
            assert not composition.uow_factory.is_open()
            labels.append(worker_class)
            raise RuntimeError("metric backend unavailable")

        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="metric-contract",
            eligible_classes=(7,),
            interactive_priority=7,
            async_priority=9,
            record_claim_metric=failing_metric,
        )
        claimed = await worker.claim()

        assert claimed is not None
        assert claimed.run.id == run_id
        assert labels == ["interactive"]
        async with composition.uow_factory() as uow:
            stored = await uow.runs.get(run_id, composition.principal)
        assert stored.status is RunStatus.RUNNING


async def test_worker_materializes_one_bounded_postgres_batch() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_ids = sorted(uuid4() for _ in range(3))
        for schedule_id in schedule_ids:
            await _create_due_schedule(composition, schedule_id)

        worker = ScheduleWorker(
            uow_factory=composition.uow_factory,
            materialize=_materializer(composition).materialize,
            clock=composition.clock,
            scan_batch=2,
            fallback_poll_seconds=30,
            admission_backoff_seconds=5,
        )

        assert await worker.run_once() == 2
        async with composition.uow_factory() as uow:
            assert await uow.schedules.due(NOW, 10) == [schedule_ids[2]]
            occurrences = [
                await uow.schedule_occurrences.list(schedule_id, composition.principal, limit=10)
                for schedule_id in schedule_ids
            ]
            assert sum(len(items) for items in occurrences) == 2
