"""Memory conflict-resolution contract."""

from datetime import timedelta
from uuid import UUID

from agent_core.domain.memory import MemoryAuthority, Polarity
from agent_core.memory.formation import DeterministicConflictResolver
from tests.contract.memory_fixtures import memory
from tests.contract.support import NOW, SESSION_ID


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


def test_conflict_resolver_reports_conflicts_for_lower_authority_and_unordered_equal_authority() -> (  # noqa: E501
    None
):
    """Authority first, then recency; what neither orders is a conflict.

    The resolver never lets weaker evidence overwrite stronger evidence, and it
    never guesses between two statements of equal standing that nothing places
    in time. Polarity is carried for the caller's record and never decides the
    outcome on its own.
    """

    resolver = DeterministicConflictResolver()
    stated = memory()
    assert stated.authority is MemoryAuthority.USER
    later = NOW + timedelta(seconds=1)
    other_session = UUID(int=0x5E55)

    # Lower authority never overwrites, however late its evidence is.
    for authority in (MemoryAuthority.INFERRED, MemoryAuthority.AFFIRMED):
        assert (
            resolver.relationship(
                stated,
                "User prefers detailed answers",
                [2],
                authority=authority,
                session_id=SESSION_ID,
                at=later,
            )
            == "conflict"
        )

    # Equal authority with a later source in the same session is still ordered.
    assert (
        resolver.relationship(
            stated,
            "User prefers detailed answers",
            [2],
            authority=MemoryAuthority.USER,
            session_id=SESSION_ID,
            at=later,
        )
        == "contradiction"
    )

    # Equal authority, same session, no later source: nothing orders them.
    inferred = memory().model_copy(
        update={"authority": MemoryAuthority.INFERRED, "source_event_ids": [4]}
    )
    assert (
        resolver.relationship(
            inferred,
            "User prefers detailed answers",
            [3],
            authority=MemoryAuthority.INFERRED,
            session_id=SESSION_ID,
            at=later,
        )
        == "conflict"
    )

    # Equal authority across sessions: only a later instant orders them.
    assert (
        resolver.relationship(
            stated,
            "User prefers detailed answers",
            [1],
            authority=MemoryAuthority.USER,
            session_id=other_session,
            at=NOW,
        )
        == "conflict"
    )
    assert (
        resolver.relationship(
            stated,
            "User prefers detailed answers",
            [1],
            authority=MemoryAuthority.USER,
            session_id=other_session,
            at=later,
        )
        == "contradiction"
    )

    # Higher authority supersedes lower authority whatever the sequences say.
    assert (
        resolver.relationship(
            inferred,
            "User prefers detailed answers",
            [3],
            authority=MemoryAuthority.USER,
            session_id=SESSION_ID,
            at=NOW,
        )
        == "contradiction"
    )

    # Polarity alone never conflicts: a later retraction supersedes.
    assert (
        resolver.relationship(
            inferred,
            "User no longer prefers concise answers",
            [5],
            authority=MemoryAuthority.INFERRED,
            polarity=Polarity.RETRACT,
            session_id=SESSION_ID,
            at=later,
        )
        == "contradiction"
    )

    # Replay and duplicate detection still run ahead of the conflict rule.
    assert (
        resolver.relationship(
            stated,
            "User prefers detailed answers",
            [1],
            authority=MemoryAuthority.INFERRED,
            session_id=SESSION_ID,
            at=later,
        )
        == "same_source"
    )
    assert (
        resolver.relationship(
            stated,
            stated.statement,
            [2],
            authority=MemoryAuthority.INFERRED,
            session_id=SESSION_ID,
            at=later,
        )
        == "duplicate"
    )
