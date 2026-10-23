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
