"""Dedicated notification worker role and bounded-wait behavior."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.notification_wakeup import InMemoryNotificationWakeup
from agent_core.application.notification_worker import NotificationWorker
from agent_core.bootstrap import build, build_notification_worker
from agent_core.cli.main import WorkerRole
from agent_core.config import ConfigurationError
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings

ROOT = Path(__file__).resolve().parents[2]


async def test_notification_worker_dispatches_and_wakeup_interrupts_bounded_poll() -> None:
    calls = 0

    async def dispatch() -> int:
        nonlocal calls
        calls += 1
        return 3

    wakeup = InMemoryNotificationWakeup()
    worker = NotificationWorker(
        dispatch_once=dispatch,
        clock=FixedClock(NOW),
        fallback_poll_seconds=30,
        wait_for_wakeup=wakeup.wait,
    )

    assert await worker.run_once() == 3
    waiting = asyncio.create_task(wakeup.wait(30))
    await asyncio.sleep(0)
    await wakeup.notify()
    await asyncio.wait_for(waiting, timeout=1)
    assert calls == 1
    await wakeup.close()


async def test_notification_worker_stops_during_fallback_sleep() -> None:
    clock = FixedClock(NOW)
    worker = NotificationWorker(
        dispatch_once=_nothing_due,
        clock=clock,
        fallback_poll_seconds=30,
    )

    running = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0)
    worker.stop()
    await asyncio.wait_for(running, timeout=1)


async def test_lean_notification_role_is_default_off() -> None:
    with pytest.raises(ConfigurationError, match="disabled"):
        async with build_notification_worker(settings=memory_settings()):
            pass

    tree = ast.parse((ROOT / "src/agent_core/bootstrap.py").read_text(encoding="utf-8"))
    notification_wiring = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name
        in {
            "_NotificationUnitOfWork",
            "_NotificationUnitOfWorkFactory",
            "_validate_notification_role",
            "build_notification_worker",
        }
    }
    assert set(notification_wiring) == {
        "_NotificationUnitOfWork",
        "_NotificationUnitOfWorkFactory",
        "_validate_notification_role",
        "build_notification_worker",
    }
    referenced_names = {
        child.id
        for node in notification_wiring.values()
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }
    assert {
        "PostgresDeviceRegistry",
        "PostgresNotificationOutbox",
        "NotificationDispatcher",
        "APNsPushTransport",
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


async def test_notification_production_is_composed_only_when_enabled() -> None:
    async with build(settings=memory_settings(), storage="memory") as disabled:
        assert disabled.executor._notification_producer is None

    settings = replace(
        memory_settings(),
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    async with build(settings=settings, storage="memory") as enabled:
        assert enabled.executor._notification_producer is not None


async def _nothing_due() -> int:
    return 0


def test_notification_worker_has_a_dedicated_cli_role() -> None:
    assert WorkerRole.NOTIFY.value == "notify"
