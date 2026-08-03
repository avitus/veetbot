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
