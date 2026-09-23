"""PostgreSQL owner model-settings parity (ADR-0119)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_core.bootstrap import build
from agent_core.domain.agents import chat_model_variant
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import ReasoningEffort
from agent_core.domain.model_settings import ModelChoice, ModelSettings
from tests.integration.m2_support import PRINCIPAL, database_settings

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


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


def _settings(principal_id: str, version: int, effort: ReasoningEffort | None) -> ModelSettings:
    return ModelSettings(
        tenant_id=PRINCIPAL.tenant_id,
        principal_id=principal_id,
        version=version,
        chat=ModelChoice(model_policy="fable", reasoning_effort=ReasoningEffort.XHIGH),
        memory=ModelChoice(model_policy="balanced", reasoning_effort=effort),
        created_at=NOW + timedelta(seconds=version),
    )


async def test_model_settings_round_trip_with_version_guard_and_isolation(
    tmp_path: Path,
) -> None:
    _alembic("upgrade", "head")
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    principal_id = f"settings-{uuid4().hex[:12]}"
    principal = PRINCIPAL.model_copy(update={"principal_id": principal_id})
    foreign = PRINCIPAL.model_copy(update={"principal_id": f"other-{uuid4().hex[:12]}"})

    async with build(settings=settings, storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            assert await uow.model_settings.current(principal) is None
            await uow.model_settings.append_version(
                _settings(principal_id, 1, None), expected_version=0
            )
            await uow.model_settings.append_version(
                _settings(principal_id, 2, ReasoningEffort.MEDIUM), expected_version=1
            )

        async with composition.uow_factory() as uow:
            head = await uow.model_settings.current(principal)
            assert head == _settings(principal_id, 2, ReasoningEffort.MEDIUM)
            assert await uow.model_settings.current(foreign) is None

        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.model_settings.append_version(
                    _settings(principal_id, 3, None), expected_version=1
                )


async def test_a_chat_variant_never_becomes_the_deployed_agents_latest_version(
    tmp_path: Path,
) -> None:
    _alembic("upgrade", "head")
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")

    async with build(settings=settings, storage="postgres") as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(session_id, composition.principal)
            deployed = await uow.agents.get_version(session.agent_id, session.agent_version)
            variant = chat_model_variant(deployed, "variant-policy")
            await uow.agents.put(variant)
            await uow.agents.put(variant)
            latest = await uow.agents.latest_version(deployed.id)

    assert isinstance(variant.id, UUID) and variant.id != deployed.id
    assert (latest.id, latest.version) == (deployed.id, deployed.version)
