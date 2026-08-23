"""Memory conflict-resolution contract."""

from uuid import UUID

from agent_core.memory.formation import DeterministicConflictResolver
from tests.contract.memory_fixtures import memory
from tests.contract.support import SESSION_ID


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


def test_conflict_resolver_reads_a_replay_only_inside_the_belief_source_session() -> None:
    """Sequences are per session, so a replay is only a replay in its session."""

    resolver = DeterministicConflictResolver()
    value = memory()
    another_session = UUID(int=0x5E55)
    assert value.source_session_id == SESSION_ID

    assert resolver.relationship(value, value.statement, [1], session_id=SESSION_ID) == (
        "same_source"
    )
    assert (
        resolver.relationship(
            value, "User prefers detailed answers", [1], session_id=another_session
        )
        == "contradiction"
    )
    assert resolver.relationship(value, value.statement, [1], session_id=another_session) == (
        "duplicate"
    )
    # A caller that cannot name the session keeps the sequence-only comparison.
    assert resolver.relationship(value, value.statement, [1]) == "same_source"
