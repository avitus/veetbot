"""The synthetic owner-memory incident against actual PostgreSQL."""

from pathlib import Path

import pytest

from agent_core.bootstrap import build
from tests.contract.owner_memory_incident_cases import (
    deleted_correction_does_not_restore_old_claim,
    owner_formation_lifecycle,
    owner_question_recall,
)
from tests.integration.test_memory_postgres_m9 import _settings


async def test_owner_question_recall_postgres(tmp_path: Path) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as app:
        await owner_question_recall(app)


async def test_owner_formation_lifecycle_postgres(tmp_path: Path) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as app:
        await owner_formation_lifecycle(app)


@pytest.mark.parametrize("session_delete", [False, True])
async def test_deleted_correction_does_not_restore_old_claim_postgres(
    tmp_path: Path, session_delete: bool
) -> None:
    async with build(settings=_settings(tmp_path), storage="postgres") as app:
        await deleted_correction_does_not_restore_old_claim(app, session_delete=session_delete)
