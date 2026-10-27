"""Owner recall probes through the composed run loop against actual PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.runtime.worker import DurableWorker
from tests.contract.owner_recall_probe_cases import (
    FakeWebProvider,
    delivery_case,
    family_names_case,
    save_case,
)
from tests.integration.test_memory_postgres_m9 import _settings


async def _finish(app: Any, run_id: UUID) -> Any:
    worker = app.worker_factory("owner-recall-probes")
    assert isinstance(worker, DurableWorker)
    assert await asyncio.wait_for(worker.run_once(), 60)
    return await asyncio.wait_for(app.runs.wait_terminal(run_id), 60)


async def test_probes_observe_delivery_and_retirement_postgres(tmp_path: Path) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(settings=_settings(tmp_path), storage="postgres", script=script) as app:
        await delivery_case(app, script, finish=_finish)


async def test_write_probe_reports_the_save_outcome_postgres(tmp_path: Path) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    web = FakeWebProvider()
    async with build(
        settings=_settings(tmp_path),
        storage="postgres",
        script=script,
        web_search_provider_override=web,
    ) as app:
        await save_case(app, script, web, finish=_finish)


async def test_family_probe_checks_delivered_names_postgres(tmp_path: Path) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=script,
    ) as app:
        await family_names_case(app, script, finish=_finish)
