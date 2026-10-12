from collections.abc import Awaitable, Callable

import pytest

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.semantic_query_work_cases import SCENARIOS
from tests.contract.support import memory_uow_factory


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda fn: fn.__name__)
async def test_semantic_query_work(
    scenario: Callable[[UnitOfWorkFactory], Awaitable[None]],
) -> None:
    _, factory = await memory_uow_factory()
    await scenario(factory)
