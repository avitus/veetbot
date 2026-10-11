"""Shared derived memory controls run against the full in-memory composition."""

from collections.abc import Awaitable, Callable
from typing import Literal

import pytest

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.derived_memory_cases import DERIVED_SCENARIOS
from tests.contract.support import memory_uow_factory


@pytest.mark.parametrize("scenario", DERIVED_SCENARIOS, ids=lambda case: case.__name__)
async def test_derived_controls(scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]) -> None:
    _, factory = await memory_uow_factory()
    await scenario(factory)


@pytest.mark.parametrize("mode", ["summary", "rejection", "rollback"])
async def test_derived_snapshot_erasure(mode: Literal["summary", "rejection", "rollback"]) -> None:
    from tests.contract.reconsolidation_summary_context_cases import summary_snapshot_erasure

    _, factory = await memory_uow_factory()
    await summary_snapshot_erasure(factory, mode)
