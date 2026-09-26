"""Integration tests erase only a database the run has marked disposable.

CI's database URL is the same as the shared local development database's, so
nothing about the URL can tell them apart. The opt-in is the only signal, and
every destructive reset in ``tests/integration`` goes through it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import tests.integration.disposable_database as disposable
from tests.integration.m2_support import database_settings

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"
GUARD_MODULE = INTEGRATION / "disposable_database.py"
DATABASE_URL = "postgresql+asyncpg://" + "agent:agent@localhost:5432/agent"

DESTRUCTIVE_SQL = re.compile(r"\bTRUNCATE\b|\bDROP\s+(?:TABLE|SCHEMA|DATABASE)\b|\bdrop_all\(")
DATABASE_URL_READ = re.compile(r"""["']DATABASE_URL["']""")
RESET_OVERRIDE = re.compile(r"^\s*(?:async\s+)?def\s+isolate_postgres_case\b", re.MULTILINE)
OWN_DESTRUCTION = re.compile(r"\bDROP\s|\bDELETE\s+FROM\b|[\"']downgrade[\"']")


def _refuse_connection(*_arguments: object, **_keywords: object) -> None:
    raise AssertionError("the reset connected before checking the opt-in")


def test_database_url_without_the_opt_in_skips_with_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.delenv(disposable.DISPOSABLE_FLAG, raising=False)

    with pytest.raises(pytest.skip.Exception) as skipped:
        disposable.disposable_database_url()

    message = str(skipped.value)
    assert "VEETBOT_TEST_DATABASE_DISPOSABLE=1" in message
    assert "scratch database" in message
    assert "agent:agent" not in message


@pytest.mark.parametrize("value", ["", "0", "true", "yes", " 1"])
def test_only_the_exact_opt_in_marks_a_database_disposable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv(disposable.DISPOSABLE_FLAG, value)

    with pytest.raises(pytest.skip.Exception, match="VEETBOT_TEST_DATABASE_DISPOSABLE=1"):
        disposable.disposable_database_url()


def test_marked_database_url_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv(disposable.DISPOSABLE_FLAG, "1")

    assert disposable.disposable_database_url() == DATABASE_URL


def test_missing_database_url_skips_before_the_opt_in_matters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(disposable.DISPOSABLE_FLAG, "1")

    with pytest.raises(pytest.skip.Exception, match="DATABASE_URL is required"):
        disposable.disposable_database_url()


def test_reset_refuses_before_connecting_without_the_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.delenv(disposable.DISPOSABLE_FLAG, raising=False)
    monkeypatch.setattr(disposable, "create_engine", _refuse_connection)

    for existing_only in (False, True):
        with pytest.raises(pytest.skip.Exception, match="VEETBOT_TEST_DATABASE_DISPOSABLE=1"):
            asyncio.run(disposable.truncate_application_tables(existing_only=existing_only))


def test_database_settings_require_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.delenv(disposable.DISPOSABLE_FLAG, raising=False)

    with pytest.raises(pytest.skip.Exception, match="VEETBOT_TEST_DATABASE_DISPOSABLE=1"):
        database_settings()

    monkeypatch.setenv(disposable.DISPOSABLE_FLAG, "1")
    assert database_settings().database_url == DATABASE_URL


def test_integration_directory_resets_only_through_the_guard() -> None:
    """A module cannot bypass the opt-in by truncating or reading the URL itself,
    and a module that replaces the directory's reset destroys nothing of its own."""

    modules = sorted(INTEGRATION.rglob("*.py"))
    assert GUARD_MODULE in modules
    violations: list[str] = []
    for path in modules:
        if path == GUARD_MODULE:
            continue
        source = path.read_text(encoding="utf-8")
        name = path.relative_to(ROOT).as_posix()
        if match := DESTRUCTIVE_SQL.search(source):
            violations.append(f"{name}: {match.group(0)!r} outside the disposable-database guard")
        if DATABASE_URL_READ.search(source):
            violations.append(f"{name}: reads DATABASE_URL instead of disposable_database_url()")
        replaces_reset = path.name != "conftest.py" and RESET_OVERRIDE.search(source)
        if replaces_reset and (match := OWN_DESTRUCTION.search(source)):
            violations.append(
                f"{name}: replaces isolate_postgres_case but runs {match.group(0).strip()!r}"
            )
    assert violations == []
