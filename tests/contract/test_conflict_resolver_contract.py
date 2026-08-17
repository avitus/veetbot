"""Memory conflict-resolution contract."""

from agent_core.memory.formation import DeterministicConflictResolver
from tests.contract.memory_fixtures import memory


def test_conflict_resolver_distinguishes_retry_duplicate_and_change() -> None:
    resolver = DeterministicConflictResolver()
    value = memory()
    assert resolver.relationship(value, value.statement, [1]) == "same_source"
    assert resolver.relationship(value, value.statement, [2]) == "duplicate"
    assert resolver.relationship(value, "User prefers detailed answers", [2]) == "contradiction"


def test_conflict_resolver_prioritizes_replay_over_content_and_normalizes_text() -> None:
    resolver = DeterministicConflictResolver()
    value = memory().model_copy(update={"source_event_ids": [1, 2]})
    # A replay of already-consolidated episodes is a retry even when the
    # proposed wording changed; content comparison never sees it.
    assert resolver.relationship(value, "User prefers detailed answers", [2]) == "same_source"
    # Duplicate detection is whitespace- and case-insensitive.
    assert resolver.relationship(value, "  user   PREFERS Concise answers ", [3]) == "duplicate"
