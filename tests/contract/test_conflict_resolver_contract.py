"""Memory conflict-resolution contract."""

from agent_core.memory.formation import DeterministicConflictResolver
from tests.contract.memory_fixtures import memory


def test_conflict_resolver_distinguishes_retry_duplicate_and_change() -> None:
    resolver = DeterministicConflictResolver()
    value = memory()
    assert resolver.relationship(value, value.statement, [1]) == "same_source"
    assert resolver.relationship(value, value.statement, [2]) == "duplicate"
    assert resolver.relationship(value, "User prefers detailed answers", [2]) == "contradiction"
