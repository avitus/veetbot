"""The Cockpit stores pass their contract suites on PostgreSQL (Milestone 35, gate 1).

Every scenario of every Cockpit store suite runs in its own unit of work with
a fresh owner, and the unit of work is rolled back afterwards, so scenarios
never see each other's rows or leave any behind.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_core.bootstrap import build
from agent_core.domain.cockpit import Widget, WidgetState
from tests.contract import (
    test_cockpit_event_log_contract,
    test_feed_store_contract,
    test_widget_commit_outbox_contract,
    test_widget_store_contract,
)
from tests.contract.cockpit_support import Scenario
from tests.contract.support import NOW
from tests.integration.m2_support import PRINCIPAL, database_settings

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "f4b7d1c8a903"
SUITES: list[tuple[str, list[Scenario], Callable[[Any], Any]]] = [
    ("widgets", test_widget_store_contract.SCENARIOS, lambda uow: uow.cockpit_widgets),
    ("outbox", test_widget_commit_outbox_contract.SCENARIOS, lambda uow: uow.cockpit_outbox),
    ("feeds", test_feed_store_contract.SCENARIOS, lambda uow: uow.cockpit_feeds),
    ("events", test_cockpit_event_log_contract.SCENARIOS, lambda uow: uow.cockpit_events),
]


class _RollBack(Exception):  # noqa: N818
    """Leave the unit of work without committing anything the scenario wrote."""


def _alembic(*arguments: str) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Alembic command failed: {error.stderr}") from error


async def test_cockpit_stores_pass_their_contract_suites_on_postgres(tmp_path: Path) -> None:
    _alembic("upgrade", "head")
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    ran: list[str] = []
    async with build(settings=settings, storage="postgres") as composition:
        for suite, scenarios, store_of in SUITES:
            assert scenarios, suite
            for check in scenarios:
                owner = PRINCIPAL.model_copy(update={"principal_id": f"cockpit-{uuid4().hex[:12]}"})
                with pytest.raises(_RollBack):
                    async with composition.uow_factory() as uow:
                        await check(store_of(uow), owner)
                        raise _RollBack
                ran.append(f"{suite}.{check.__name__}")
    assert len(ran) == sum(len(scenarios) for _, scenarios, _ in SUITES)


async def test_cockpit_downgrade_refuses_while_cockpit_data_remains(tmp_path: Path) -> None:
    _alembic("upgrade", "head")
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    owner = PRINCIPAL.model_copy(update={"principal_id": f"cockpit-{uuid4().hex[:12]}"})
    async with build(settings=settings, storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            await uow.cockpit_widgets.create_widget(
                Widget(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    slug="weather",
                    state=WidgetState.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        try:
            with pytest.raises(RuntimeError, match="exported and erased before downgrade"):
                _alembic("downgrade", PREVIOUS_REVISION)
        finally:
            async with composition.uow_factory() as uow:
                await uow.cockpit_widgets.erase_principal(owner)
    _alembic("upgrade", "head")
