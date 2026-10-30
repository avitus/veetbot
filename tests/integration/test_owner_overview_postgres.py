"""The owner overview production path against PostgreSQL."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.bootstrap import build
from tests.contract.owner_overview_cases import overview_run
from tests.integration.test_memory_postgres_m9 import _settings
from tests.unit.test_owner_overview import script


@pytest.mark.parametrize("family", [False, True])
async def test_production_owner_overview_postgres(tmp_path: Path, family: bool) -> None:
    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=script(family),
    ) as app:
        await overview_run(app, family=family)


@pytest.mark.parametrize("restriction", ["local", "expired", "retracted", "erased", "corrected"])
async def test_production_overview_visibility_postgres(tmp_path: Path, restriction: str) -> None:
    from tests.contract.owner_overview_cases import restricted_overview_run

    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=script(True),
    ) as app:
        await restricted_overview_run(app, restriction)


async def test_production_overview_continues_postgres(tmp_path: Path) -> None:
    from tests.contract.owner_overview_cases import paged_overview_run
    from tests.unit.test_owner_overview import page_script

    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=page_script(),
    ) as app:
        await paged_overview_run(app)


async def test_production_overview_first_page_is_the_closest_family_postgres(
    tmp_path: Path,
) -> None:
    from tests.contract.owner_overview_cases import closeness_overview_run

    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=script(True),
    ) as app:
        await closeness_overview_run(app)


async def test_production_overview_first_page_reaches_every_family_tier_postgres(
    tmp_path: Path,
) -> None:
    from tests.contract.owner_overview_cases import tiered_overview_run

    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=script(True),
    ) as app:
        await tiered_overview_run(app)


async def test_production_overview_says_when_a_group_exceeds_what_it_reads_postgres(
    tmp_path: Path,
) -> None:
    from tests.contract.owner_overview_cases import overflow_overview_run

    async with build(
        settings=replace(_settings(tmp_path), people_enabled=True),
        storage="postgres",
        script=script(True),
    ) as app:
        await overflow_overview_run(app)


@pytest.mark.parametrize(
    "restriction", ["missing", "retracted", "expired", "future", "sensitive", "future_edge"]
)
async def test_owner_name_resolution_requires_current_support_postgres(restriction: str) -> None:
    from tests.contract.support import NOW, agent, principal, session
    from tests.integration.m2_support import database_settings
    from tests.unit.test_people_resolution import owner_tie_support_contract

    async with build(
        settings=database_settings(), storage="postgres", principal=principal(), fixed_clock_at=NOW
    ) as app:
        async with app.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(session())
        await owner_tie_support_contract(app.uow_factory, restriction)


async def test_owner_name_resolution_qualifies_display_name_postgres() -> None:
    from tests.contract.support import NOW, agent, principal, session
    from tests.integration.m2_support import database_settings
    from tests.unit.test_people_resolution import owner_tie_display_name_contract

    async with build(
        settings=database_settings(), storage="postgres", principal=principal(), fixed_clock_at=NOW
    ) as app:
        async with app.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(session())
        await owner_tie_display_name_contract(app.uow_factory)
