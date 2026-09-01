"""PostgreSQL persona store parity coverage for Milestone 22."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity
from agent_core.domain.persona import (
    PersonaDocument,
    PersonaEntry,
    PersonaEntrySource,
    PersonaNomination,
    PersonaNominationState,
)
from tests.integration.m2_support import PRINCIPAL, database_settings

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


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


def _document(principal_id: str, *, version: int) -> PersonaDocument:
    return PersonaDocument(
        tenant_id=PRINCIPAL.tenant_id,
        principal_id=principal_id,
        version=version,
        entries=(
            PersonaEntry(
                text=f"Standing truth number {version}.",
                source=PersonaEntrySource.USER_EDIT,
            ),
        ),
        source=PersonaEntrySource.USER_EDIT,
        created_at=NOW + timedelta(seconds=version),
    )


def _nomination(principal_id: str) -> PersonaNomination:
    return PersonaNomination(
        id=uuid4(),
        tenant_id=PRINCIPAL.tenant_id,
        principal_id=principal_id,
        belief_id=uuid4(),
        statement="User prefers concise answers.",
        belief_type=BeliefType.PREFERENCE,
        authority=MemoryAuthority.AFFIRMED,
        confidence=0.9,
        corroboration_count=3,
        sensitivity=Sensitivity.INTERNAL,
        nominated_at=NOW,
    )


async def test_persona_store_round_trips_versions_and_nominations(tmp_path: Path) -> None:
    _alembic("upgrade", "head")
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    principal_id = f"persona-{uuid4().hex[:12]}"
    principal = PRINCIPAL.model_copy(update={"principal_id": principal_id})
    foreign = PRINCIPAL.model_copy(update={"principal_id": f"other-{uuid4().hex[:12]}"})

    async with build(settings=settings, storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            assert await uow.personas.active(principal) is None
            await uow.personas.append_version(
                _document(principal_id, version=1), expected_version=0
            )
            await uow.personas.append_version(
                _document(principal_id, version=2), expected_version=1
            )

        async with composition.uow_factory() as uow:
            head = await uow.personas.active(principal)
            assert head is not None
            assert head.version == 2
            assert head.entries[0].text == "Standing truth number 2."
            versions = [row.version for row in await uow.personas.history(principal)]
            assert versions == [2, 1]
            assert await uow.personas.active(foreign) is None

        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.personas.append_version(
                    _document(principal_id, version=3), expected_version=1
                )

        nomination = _nomination(principal_id)
        async with composition.uow_factory() as uow:
            stored = await uow.personas.nominate(nomination)
            assert stored.state is PersonaNominationState.NOMINATED

        async with composition.uow_factory() as uow:
            replayed = await uow.personas.nominate(nomination.model_copy(update={"id": uuid4()}))
            assert replayed.id == nomination.id
            with pytest.raises(NotFoundError):
                await uow.personas.get_nomination(nomination.id, foreign)

        async with composition.uow_factory() as uow:
            await uow.personas.resolve_nomination(
                nomination.id,
                principal,
                state=PersonaNominationState.DECLINED,
                resolved_at=NOW + timedelta(minutes=1),
            )

        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.personas.nominate(nomination.model_copy(update={"id": uuid4()}))

        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.personas.resolve_nomination(
                    nomination.id,
                    principal,
                    state=PersonaNominationState.AFFIRMED,
                    resolved_at=NOW + timedelta(minutes=2),
                    affirmed_version=2,
                )

        async with composition.uow_factory() as uow:
            declined = await uow.personas.list_nominations(
                principal, state=PersonaNominationState.DECLINED
            )
            assert [row.id for row in declined] == [nomination.id]
