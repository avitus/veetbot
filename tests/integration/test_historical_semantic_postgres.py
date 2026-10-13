"""The shared historical recall contract uses real migrated PostgreSQL."""

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.historical_semantic_cases import (
    historical_attribution_revision_survives_later_text_change,
    historical_revision_is_not_current_evidence,
    historical_revision_restrictions_precede_limit,
    historical_selection_precedes_similarity,
    historical_visibility_and_stale_writes,
)
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings


async def test_historical_revision_is_not_current_evidence() -> None:
    clock = FixedClock(NOW)
    async with build(
        settings=database_settings(), storage="postgres", principal=principal(), clock=clock
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await historical_revision_is_not_current_evidence(app.uow_factory, clock.advance)


async def test_historical_people_identity_precedes_candidate_limits() -> None:
    from tests.contract.semantic_memory_cases import people_identity_precedes_dense_limit

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await people_identity_precedes_dense_limit(app.uow_factory, historical=True)


@pytest.mark.parametrize(
    "scenario",
    [
        historical_attribution_revision_survives_later_text_change,
        historical_visibility_and_stale_writes,
        historical_selection_precedes_similarity,
        historical_revision_restrictions_precede_limit,
    ],
    ids=lambda case: case.__name__,
)
async def test_historical_boundaries(
    scenario: Callable[[UnitOfWorkFactory, Callable[[timedelta], None]], Awaitable[None]],
) -> None:
    clock = FixedClock(NOW)
    async with build(
        settings=database_settings(), storage="postgres", principal=principal(), clock=clock
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await scenario(app.uow_factory, clock.advance)
