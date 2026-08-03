"""PostgreSQL-backed Milestone 3 trajectory materialization and retention."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from agent_core.bootstrap import build
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings


async def test_postgres_export_persists_artifact_metadata_and_bytes(tmp_path: Path) -> None:
    settings = replace(
        database_settings(),
        trajectory_export_enabled=True,
        artifact_root=tmp_path,
    )
    async with build(settings=settings, storage="postgres") as composition:
        await composition.trajectories.grant_consent()
        run_id = await composition.runs.submit("export a durable calculator trajectory")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="m3-export-worker",
        )
        assert await worker.run_once()
        run = await composition.runs.get(run_id)
        artifact = await composition.trajectories.export(run_id)
        content = await composition.trajectories.read(run_id)
        async with composition.uow_factory() as uow:
            export = await uow.trajectory_exports.get_for_run(run_id)

    document = json.loads(content)
    assert run.export_consent
    assert document["run_id"] == str(run_id)
    assert document["messages"]
    assert export is not None
    assert export.artifact == artifact
    assert export.artifact.expires_at is not None
    assert (tmp_path / artifact.storage_uri).read_bytes() == content
