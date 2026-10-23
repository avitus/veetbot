"""Legacy recovery crosses the complete in-memory unit of work."""

from collections.abc import Awaitable, Callable

import pytest

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.dreaming_recovery_cases import SCENARIOS
from tests.contract.support import memory_uow_factory


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_dreaming_recovery(scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]) -> None:
    _, factory = await memory_uow_factory()
    await scenario(factory)
