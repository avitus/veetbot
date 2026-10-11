"""Shared reviewed dreaming store contracts."""

from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from tests.contract.dreaming_cases import SCENARIOS
from tests.contract.reconsolidation_cases import Factory
from tests.contract.support import memory_uow_factory


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_dreaming(scenario: Callable[[Factory], Awaitable[None]]) -> None:
    _, factory = await memory_uow_factory()
    await scenario(cast(Factory, factory))
