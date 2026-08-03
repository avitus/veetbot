"""Migration acceptance checks against a disposable or CI PostgreSQL database."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agent_core.adapters.persistence.revision import EXPECTED_REVISION

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


def test_initial_migration_upgrades_and_round_trips() -> None:
    assert EXPECTED_REVISION in _alembic("current")
    _alembic("downgrade", "base")
    try:
        _alembic("upgrade", "head")
    finally:
        _alembic("upgrade", "head")
    assert EXPECTED_REVISION in _alembic("current")
