"""Milestone 11 bounded schedule-worker gates."""

import ast
import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.schedule_wakeup import InMemoryScheduleWakeup
from agent_core.bootstrap import _validate_schedule_role, build, build_schedule_worker
from agent_core.config import ConfigurationError, load_schedule_worker_settings
from agent_core.domain.schedules import ScheduleOccurrence
from agent_core.runtime.worker import DurableWorker
from agent_core.scheduling.worker import ScheduleWorker
from tests.contract.support import NOW
from tests.contract.test_schedule_repository_contract import revision, schedule
from tests.integration.m2_support import memory_settings

ROOT = Path(__file__).resolve().parents[2]


def _production_schedule_environment(*, scopes: str) -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://schedule@127.0.0.1:5432/agent",
        "DEPLOYMENT_MODE": "production",
        "AUTH_MODE": "token",
        "AUTH_TENANT_ID": "tenant-production",
        "AUTH_PRINCIPAL_ID": "operator",
        "AUTH_ROLES": "user",
        "AUTH_SCOPES": scopes,
        "AGENT_SCHEDULE_API_ENABLED": "1",
        "AGENT_SCHEDULE_WORKER_ENABLED": "1",
    }


def test_lean_schedule_role_derives_manifest_gmail_authority_without_credentials(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "gmail-accounts.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "default_account": "personal",
                "accounts": [
                    {
                        "account_id": "personal",
                        "read_credential_file": "/credentials/personal/read.json",
                        "write_credential_file": "/credentials/personal/write.json",
                        "send_credential_file": "/credentials/personal/send.json",
                    },
                    {
                        "account_id": "work",
                        "read_credential_file": "/credentials/work/read.json",
                        "write_credential_file": "/credentials/work/write.json",
                        "send_credential_file": "/credentials/work/send.json",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    configured_scopes = {
        "session.read",
        "schedule.read",
        "schedule.write",
        "schedule.cancel",
    }
    gmail_scopes = {
        "mcp.gmail_read.use",
        "mcp.gmail_write.use",
        "mcp.gmail_send.use",
        "mcp.gmail_work_read.use",
        "mcp.gmail_work_write.use",
        "mcp.gmail_work_send.use",
    }
    settings = load_schedule_worker_settings(
        {
            **_production_schedule_environment(scopes=",".join(sorted(configured_scopes))),
            "AGENT_EMAIL_ENABLED": "1",
            "GMAIL_ACCOUNTS_FILE": str(manifest),
        }
    )

    assert settings.credentials == {}
    assert settings.email_enabled is True
    assert settings.email_account_ids == ("personal", "work")
    principal = _validate_schedule_role(settings)
    assert principal.scopes == configured_scopes | gmail_scopes


def test_lean_schedule_role_derives_legacy_gmail_authority_without_credentials() -> None:
    configured_scopes = {
        "session.read",
        "schedule.read",
        "schedule.write",
        "schedule.cancel",
    }
    settings = load_schedule_worker_settings(
        {
            **_production_schedule_environment(scopes=",".join(sorted(configured_scopes))),
            "AGENT_EMAIL_ENABLED": "1",
        }
    )

    assert settings.credentials == {}
    assert settings.email_account_ids == ()
    principal = _validate_schedule_role(settings)
    assert principal.scopes == configured_scopes | {
        "mcp.gmail_read.use",
        "mcp.gmail_write.use",
        "mcp.gmail_send.use",
    }


@pytest.mark.parametrize(
    "scope",
    [
        "mcp.gmail_read.admin",
        "mcp.gmail_work.use",
        "mcp.gmail_Work_read.use",
        "mcp.unrelated.use",
    ],
)
def test_lean_schedule_role_rejects_non_gmail_extension_scopes(scope: str) -> None:
    settings = load_schedule_worker_settings(
        _production_schedule_environment(scopes=f"schedule.read,{scope}")
    )

    with pytest.raises(ConfigurationError, match="unknown platform scopes"):
        _validate_schedule_role(settings)


async def test_schedule_worker_batches_and_isolates_definition_failures() -> None:
    visited: list[UUID] = []
    schedule_ids = [UUID(int=900 + index) for index in range(3)]

    async with build(
        settings=memory_settings(), storage="memory", sequential_ids=True
    ) as composition:
        async with composition.uow_factory() as uow:
            for schedule_id in schedule_ids:
                await uow.schedules.create(schedule(schedule_id=schedule_id), revision(schedule_id))

        async def materialize(schedule_id: UUID) -> ScheduleOccurrence | None:
            visited.append(schedule_id)
            if schedule_id == schedule_ids[0]:
                raise RuntimeError("one corrupt definition")
            return None

        worker = ScheduleWorker(
            uow_factory=composition.uow_factory,
            materialize=materialize,
            clock=composition.clock,
            scan_batch=2,
            fallback_poll_seconds=30,
            admission_backoff_seconds=5,
        )

        assert await worker.run_once() == 1
        assert visited == schedule_ids[:2]
        with pytest.raises(ConfigurationError, match="disabled"):
            composition.schedule_worker_factory()

    async with build(
        settings=replace(memory_settings(), schedule_worker_enabled=True),
        storage="memory",
    ) as enabled:
        assert isinstance(enabled.schedule_worker_factory(), ScheduleWorker)
        interactive = enabled.worker_factory("interactive-reserved")
        asynchronous = enabled.async_worker_factory("async-reserved")
        assert isinstance(interactive, DurableWorker)
        assert isinstance(asynchronous, DurableWorker)
        assert interactive._eligible_classes == (0,)
        assert asynchronous._eligible_classes == (10,)


async def test_schedule_worker_waits_for_next_fire_with_bounded_fallback_and_backoff() -> None:
    future = NOW.replace(microsecond=0)
    schedule_id = UUID(int=910)

    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=future,
        sequential_ids=True,
    ) as composition:
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        due_at = future + timedelta(seconds=7)
        async with composition.uow_factory() as uow:
            await uow.schedules.create(
                schedule(schedule_id=schedule_id, next_fire_at=due_at),
                revision(schedule_id),
            )

        worker = ScheduleWorker(
            uow_factory=composition.uow_factory,
            materialize=lambda _schedule_id: _no_occurrence(),
            clock=clock,
            scan_batch=2,
            fallback_poll_seconds=30,
            admission_backoff_seconds=5,
        )

        assert await worker.wait_seconds() == 7
        clock.advance(due_at - future)
        assert await worker.wait_seconds() == 5


async def test_schedule_worker_cancellation_cleans_up_child_waits() -> None:
    child_finished = asyncio.Event()
    never = asyncio.Event()

    async def wait_forever() -> None:
        try:
            await never.wait()
        finally:
            child_finished.set()

    async with build(settings=memory_settings(), storage="memory") as composition:
        worker = ScheduleWorker(
            uow_factory=composition.uow_factory,
            materialize=lambda _schedule_id: _no_occurrence(),
            clock=composition.clock,
            scan_batch=1,
            fallback_poll_seconds=30,
            admission_backoff_seconds=5,
        )
        waiting = asyncio.create_task(worker._wait_or_stop(wait_forever()))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    assert child_finished.is_set()


async def test_schedule_worker_uses_poll_fallback_when_wait_calculation_fails() -> None:
    slept: list[float] = []

    async with build(settings=memory_settings(), storage="memory") as composition:
        async with composition.uow_factory() as uow:

            async def fail_next_fire_at():  # type: ignore[no-untyped-def]
                raise RuntimeError("temporary schedule read failure")

            uow.schedules.next_fire_at = fail_next_fire_at  # type: ignore[method-assign]

        worker = ScheduleWorker(
            uow_factory=composition.uow_factory,
            materialize=lambda _schedule_id: _no_occurrence(),
            clock=composition.clock,
            scan_batch=1,
            fallback_poll_seconds=30,
            admission_backoff_seconds=5,
        )
        original_sleep = composition.clock.sleep

        async def stop_after_sleep(seconds: float) -> None:
            slept.append(seconds)
            worker.stop()
            await original_sleep(0)

        composition.clock.sleep = stop_after_sleep  # type: ignore[method-assign]
        await worker.run_forever()

    assert slept == [30]


async def _no_occurrence() -> ScheduleOccurrence | None:
    return None


async def test_schedule_wakeup_interrupts_the_bounded_poll_wait() -> None:
    wakeup = InMemoryScheduleWakeup()
    waiting = asyncio.create_task(wakeup.wait(30))
    await asyncio.sleep(0)
    await wakeup.notify()
    await asyncio.wait_for(waiting, timeout=1)


async def test_lean_schedule_role_is_default_off_and_has_no_execution_capabilities() -> None:
    with pytest.raises(ConfigurationError, match="disabled"):
        async with build_schedule_worker(settings=memory_settings()):
            pass

    tree = ast.parse((ROOT / "src/agent_core/bootstrap.py").read_text(encoding="utf-8"))
    schedule_wiring = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name
        in {
            "_ScheduleUnitOfWork",
            "_ScheduleUnitOfWorkFactory",
            "_validate_schedule_role",
            "build_schedule_worker",
        }
    }
    assert set(schedule_wiring) == {
        "_ScheduleUnitOfWork",
        "_ScheduleUnitOfWorkFactory",
        "_validate_schedule_role",
        "build_schedule_worker",
    }
    referenced_names = {
        child.id
        for node in schedule_wiring.values()
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }
    assert {
        "PostgresScheduleRepository",
        "PostgresScheduleOccurrenceRepository",
        "PostgresScheduleAdmissionController",
        "PostgresRunQueue",
    } <= referenced_names
    assert not referenced_names & {
        "MappingCredentialResolver",
        "AnthropicMessagesProvider",
        "ChatCompletionsProvider",
        "OpenAIResponsesProvider",
        "SandboxManager",
        "ToolPipeline",
        "RunExecutor",
    }
