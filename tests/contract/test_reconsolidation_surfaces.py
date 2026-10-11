"""Shared owner-control contract against the full in-memory composition."""

from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_surface_cases import SURFACE_SCENARIOS
from tests.contract.support import memory_uow_factory


@pytest.mark.parametrize("scenario", SURFACE_SCENARIOS, ids=lambda case: case.__name__)
async def test_owner_controls(scenario: Callable[[Factory], Awaitable[None]]) -> None:
    _, factory = await memory_uow_factory()
    await scenario(cast(Factory, factory))
