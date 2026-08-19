"""PostgreSQL proof for the bounded Milestone 11 schedule worker."""

import asyncio
from dataclasses import replace
from uuid import uuid4

from agent_core.adapters.schedule_wakeup import PostgresScheduleWakeup
from agent_core.bootstrap import build, build_schedule_worker
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import DurableWorker
from agent_core.scheduling.worker import ScheduleWorker
from tests.contract.support import agent
from tests.integration.m2_support import database_settings
from tests.integration.test_schedule_materializer_m11 import (
    NOW,
    _create_due_schedule,
    _materializer,
)


async def test_postgres_schedule_wakeup_crosses_process_connections() -> None:
    settings = database_settings()
    listener = PostgresScheduleWakeup(settings.database_url)
    publisher = PostgresScheduleWakeup(settings.database_url)
    try:
        waiting = asyncio.create_task(listener.wait(5))
        await asyncio.sleep(0.1)
        await publisher.notify()
        await asyncio.wait_for(waiting, timeout=1)
    finally:
        await listener.close()
        await publisher.close()


async def test_lean_production_schedule_role_constructs_without_execution_credentials() -> None:
    settings = replace(
        database_settings(),
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
    async with build_schedule_worker(settings=settings) as worker:
        assert isinstance(worker, ScheduleWorker)


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
