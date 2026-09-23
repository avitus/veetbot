"""Migration acceptance checks against a disposable or CI PostgreSQL database."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.revision import EXPECTED_REVISION
from agent_core.bootstrap import build
from agent_core.domain.events import NewEvent
from tests.integration.m2_support import database_settings

ROOT = Path(__file__).resolve().parents[2]


def _alembic(*arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_migrations_upgrade_cleanly_and_match_metadata() -> None:
    try:
        _alembic("downgrade", "base")
        _alembic("upgrade", "head")
        assert "No new upgrade operations detected" in _alembic("check")
        assert EXPECTED_REVISION in _alembic("current")
    finally:
        _alembic("upgrade", "head")


def test_migrations_round_trip_each_step_from_its_predecessor() -> None:
    _alembic("upgrade", "head")
    assert EXPECTED_REVISION in _alembic("current")
    _alembic("downgrade", "-1")
    try:
        _alembic("upgrade", "+1")
        _alembic("downgrade", "-1")
    finally:
        _alembic("upgrade", "head")
    assert EXPECTED_REVISION in _alembic("current")


async def test_lease_expiration_backfill_preserves_continuations_and_crash_history() -> None:
    settings = database_settings()
    async with build(settings=settings, storage="postgres") as composition:
        run_ids = [await composition.runs.submit("synthetic migration case") for _ in range(3)]
        checkpoints = []
        for index, run_id in enumerate(run_ids):
            async with composition.uow_factory() as uow:
                run = await uow.runs.get(run_id, composition.principal)
                checkpoints.append(await uow.checkpoints.latest(run_id))
                for epoch in range(index):
                    await uow.events.append(
                        NewEvent(
                            session_id=run.session_id,
                            run_id=run_id,
                            event_type="run.requeued",
                            actor_type="maintenance",
                            payload={"reclaimed_epoch": epoch + 1, "attempts": epoch + 1},
                        )
                    )
                if index == 2:
                    await uow.events.append(
                        NewEvent(
                            session_id=run.session_id,
                            run_id=run_id,
                            event_type="run.failed",
                            actor_type="maintenance",
                            payload={
                                "reclaimed_epoch": 3,
                                "failure": {"reason": "max_attempts_exceeded"},
                            },
                        )
                    )
        _alembic("downgrade", "a4f7c1e9d2b3")
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as session:
                # Every legacy row has three total claims, but only the last
                # two have any crash history. Terminal rows stay terminal.
                await session.execute(text("UPDATE runs SET attempts = 3"))
                await session.execute(
                    text("UPDATE runs SET status = 'FAILED' WHERE id = :id"), {"id": run_ids[2]}
                )
                await session.commit()
            _alembic("upgrade", "head")
        finally:
            await engine.dispose()
            _alembic("upgrade", "head")
        for index, run_id in enumerate(run_ids):
            async with composition.uow_factory() as uow:
                run = await uow.runs.get(run_id, composition.principal)
                assert run.attempts == 3
                assert run.lease_expirations == [0, 1, 3][index]
                assert run.status.value == ("FAILED" if index == 2 else "QUEUED")
                assert await uow.checkpoints.latest(run_id) == checkpoints[index]
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            first = await uow.queue.claim("recovered-continuation", [0])
            second = await uow.queue.claim("recovered-crash", [0])
            assert first is not None and first.run.id == run_ids[0]
            assert second is not None and second.run.id == run_ids[1]
            assert await uow.queue.claim("no-terminal-reopen", [0]) is None
