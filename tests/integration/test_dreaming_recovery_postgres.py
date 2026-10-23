"""Legacy recovery validates proofs and commits atomically on real PostgreSQL."""

from collections.abc import Awaitable, Callable

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.dreaming_recovery_cases import SCENARIOS
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_dreaming_recovery(scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]) -> None:
    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await scenario(app.uow_factory)
