"""PostgreSQL migration, episode, and erasure coverage for Milestone 21."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.memory.distillation import deterministic_integrated_episode
from tests.integration.m2_support import database_settings

ROOT = Path(__file__).resolve().parents[2]


def _alembic(*arguments: str) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Alembic command failed: {exc.stderr}") from exc


def test_alembic_failure_reports_captured_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            1,
            [sys.executable, "-m", "alembic"],
            stderr="migration exploded",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="migration exploded"):
        _alembic("upgrade", "head")


async def _seed_legacy_row(tmp_path: Path) -> tuple[UUID, UUID, str]:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with build(settings=settings, storage="postgres") as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=composition.principal.principal_id,
                    payload={"content": "User prefers concise answers."},
                )
            )
        belief = await composition.memory.remember(
            session_id=session_id,
            run_id=None,
            statement="User prefers concise answers.",
            subject="answer style",
            scope="general",
            source_event_ids=[event.sequence],
        )
        return session_id, belief.id, composition.principal.tenant_id


async def _null_lifecycle_fields(belief_id: UUID, tenant_id: str) -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "UPDATE memories SET claim_kind = NULL, derivation = NULL, "
                    "longevity = NULL, last_evidence_at = NULL, evidence_count = NULL, "
                    "lifecycle_policy_version = NULL WHERE id = :belief_id"
                ),
                {"belief_id": belief_id},
            )
    finally:
        await engine.dispose()


async def _verify_backfill_and_erasure(tmp_path: Path, session_id: UUID, belief_id: UUID) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts-after")
    async with build(settings=settings, storage="postgres") as composition:
        belief = await composition.memory.get_memory(belief_id)
        assert belief.statement == "User prefers concise answers."
        assert belief.authority.value == "user"
        assert belief.consolidation_policy_version == "formation@7"
        assert belief.claim_kind.value == "project_fact"
        assert belief.derivation.value == "direct"
        assert belief.longevity.value == "durable"
        assert belief.last_evidence_at == belief.valid_from
        assert belief.evidence_count == len(set(belief.source_event_ids))
        assert belief.lifecycle_policy_version == "lifecycle@1-backfill"

        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
            episode = deterministic_integrated_episode(
                events,
                principal=composition.principal,
                episode_id=composition.ids.new_id(),
                created_at=composition.clock.now(),
            )
            await uow.episodes.put(episode)
            conflicting = episode.model_copy(update={"derivation_key": "f" * 64})
            with pytest.raises(ConflictError, match="episode id identifies different content"):
                await uow.episodes.put(conflicting)
            assert await uow.episodes.get(episode.id, composition.principal) == episode
            assert (
                await uow.episodes.get_by_derivation(episode.derivation_key, composition.principal)
                == episode
            )
            foreign = composition.principal.model_copy(update={"principal_id": "other"})
            assert await uow.episodes.get_by_derivation(episode.derivation_key, foreign) is None
        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(
                session_id, composition.principal, composition.clock.now()
            )
        async with composition.uow_factory() as uow:
            try:
                await uow.episodes.get(episode.id, composition.principal)
            except NotFoundError:
                pass
            else:
                raise AssertionError("session erasure left an integrated episode")


def test_schema_backfill_preserves_history_and_erasure(tmp_path: Path) -> None:
    """A stepwise upgrade preserves belief history and erases derived episodes."""

    session_id, belief_id, tenant_id = asyncio.run(_seed_legacy_row(tmp_path))
    _alembic("downgrade", "b1d9e3f5a720")
    try:
        asyncio.run(_null_lifecycle_fields(belief_id, tenant_id))
        _alembic("upgrade", "head")
        asyncio.run(_verify_backfill_and_erasure(tmp_path, session_id, belief_id))
    finally:
        _alembic("upgrade", "head")
