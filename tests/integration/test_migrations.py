"""Migration acceptance checks against a disposable or CI PostgreSQL database."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.revision import EXPECTED_REVISION
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleMergeSuggestion, Person, PersonIdentifier
from agent_core.domain.people_views import ResolveMergeSuggestion
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings

ROOT = Path(__file__).resolve().parents[2]
# ADR-0125's migration. Later migrations follow it, so its downgrade tests
# name the revision before it rather than stepping back one from head.
BEFORE_MERGE_SUGGESTIONS = "524f16dfc8f9-1"


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


def _people_owner() -> Principal:
    return principal().model_copy(
        update={
            "principal_id": f"downgrade-{uuid4().hex[:12]}",
            "scopes": {"people.read", "people.write"},
        }
    )


async def _two_sabinas(app: Any, owner: Principal) -> UUID:
    """Seed two correspondents the duplicate pass asks about; return an audit session."""
    audit = uuid4()
    fields: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW - timedelta(days=10),
        "updated_at": NOW - timedelta(days=10),
    }
    async with app.uow_factory() as uow:
        await uow.sessions.create(
            session().model_copy(update={"id": audit, "principal_id": owner.principal_id})
        )
        for address in ("sabina@home.test", "sabina@work.test"):
            person = Person(id=uuid4(), display_name="Sabina Smith", **fields)
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value=address,
                    context="owner",
                    verification="channel_observed",
                    valid_from=NOW - timedelta(days=10),
                    **fields,
                ),
                expected_revision=0,
            )
    return audit


async def test_merge_suggestion_downgrade_never_erases_an_owner_answer() -> None:
    """ADR-0125: a downgrade drops derived suggestions but refuses to lose an answer."""
    owner = _people_owner()
    settings = replace(database_settings(), people_enabled=True)
    try:
        async with build(settings=settings, storage="postgres", principal=owner) as app:
            service = app.services.people
            assert service is not None
            audit = await _two_sabinas(app, owner)

            async def listed() -> list[Any]:
                return list(
                    (await service.merge_suggestions(owner, ceiling=Sensitivity.SENSITIVE)).items
                )

            assert len((await service.dedupe(owner, apply=True)).suggestions) == 1
            # An open suggestion is derived: it goes, and the next pass asks again.
            _alembic("downgrade", BEFORE_MERGE_SUGGESTIONS)
            _alembic("upgrade", "head")
            assert await listed() == []
            assert len((await service.dedupe(owner, apply=True)).suggestions) == 1
            [asked] = await listed()
            await service.resolve_merge_suggestion(
                owner,
                asked.id,
                ResolveMergeSuggestion(
                    session_id=audit, expected_revision=asked.revision, decision="separate"
                ),
                key="keep-apart",
                ceiling=Sensitivity.SENSITIVE,
            )
            # The owner's answer is final, so the downgrade refuses to erase it.
            with pytest.raises(subprocess.CalledProcessError) as refused:
                _alembic("downgrade", BEFORE_MERGE_SUGGESTIONS)
            assert "answered" in refused.value.stderr
            assert EXPECTED_REVISION in _alembic("current")
            assert (await service.dedupe(owner, apply=True)).suggestions == []
    finally:
        _alembic("upgrade", "head")


async def _until_the_downgrade_waits_for_people_heads(engine: Any, downgrade: Any) -> None:
    for _ in range(300):
        async with engine.connect() as connection:
            waiting = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE relation = 'people_heads'::regclass "
                        "AND mode = 'AccessExclusiveLock' AND NOT granted"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        assert downgrade.returncode is None, "the downgrade ended before it waited"
        await asyncio.sleep(0.1)
    raise AssertionError("the downgrade never waited for the answer in flight")


async def test_merge_suggestion_downgrade_waits_for_an_answer_in_flight() -> None:
    """An answer that commits while the downgrade runs is never lost (ADR-0125)."""
    owner = _people_owner()
    settings = replace(database_settings(), people_enabled=True)
    engine = create_engine(settings.database_url)
    try:
        async with build(settings=settings, storage="postgres", principal=owner) as app:
            service = app.services.people
            assert service is not None
            await _two_sabinas(app, owner)
            assert len((await service.dedupe(owner, apply=True)).suggestions) == 1
            [asked] = (await service.merge_suggestions(owner, ceiling=Sensitivity.SENSITIVE)).items
            async with app.uow_factory() as uow:
                current = await uow.people.get(owner, asked.id, ceiling=Sensitivity.RESTRICTED)
                assert isinstance(current, PeopleMergeSuggestion)
                await uow.people.put(
                    current.model_copy(
                        update={
                            "state": "separated",
                            "revision": current.revision + 1,
                            "updated_at": current.updated_at + timedelta(seconds=1),
                        }
                    ),
                    expected_revision=current.revision,
                )
                downgrade = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "alembic",
                    "downgrade",
                    BEFORE_MERGE_SUGGESTIONS,
                    cwd=ROOT,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await _until_the_downgrade_waits_for_people_heads(engine, downgrade)
            # The answer commits here, while the downgrade waits on it.
            _output, errors = await downgrade.communicate()
            assert downgrade.returncode != 0, "the downgrade erased an answer in flight"
            assert b"answered" in errors
            async with app.uow_factory() as uow:
                kept = await uow.people.get(owner, asked.id, ceiling=Sensitivity.RESTRICTED)
            assert isinstance(kept, PeopleMergeSuggestion) and kept.state == "separated"
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()
