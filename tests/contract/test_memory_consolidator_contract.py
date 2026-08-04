"""Memory consolidation interface contract."""

import inspect

from agent_core.memory.formation import GovernedMemoryService


def test_governed_memory_service_exposes_async_consolidation() -> None:
    assert inspect.iscoroutinefunction(GovernedMemoryService.run)
