"""The lexical grouper: deterministic token-overlap grouping of conversations."""

from __future__ import annotations

import random
from uuid import UUID

from agent_core.domain.folders import FolderProposalDerivation, FolderProposalKind
from agent_core.folders.clustering import (
    FolderSummary,
    GroupingInput,
    LexicalGrouper,
    ThreadSummary,
    jaccard,
    lexical_groups,
    overlap,
    thread_terms,
)
from tests.contract.support import principal

KITCHEN = UUID("00000000-0000-0000-0000-00000000f001")


def _thread(number: int, title: str, snippet: str = "") -> ThreadSummary:
    return ThreadSummary(session_id=UUID(int=number), title=title, snippet=snippet)


def _grouping(
    threads: list[ThreadSummary],
    folders: list[FolderSummary] | None = None,
    *,
    threshold: int = 4,
    max_members: int = 12,
    similarity: float = 0.2,
) -> GroupingInput:
    return GroupingInput(
        threads=tuple(threads),
        folders=tuple(folders or []),
        threshold=threshold,
        max_members=max_members,
        similarity_threshold=similarity,
    )


LISBON = [
    _thread(1, "Lisbon trip flights", "Looking at flights to Lisbon for the trip in May"),
    _thread(2, "Lisbon trip hotel booking", "Which Lisbon hotel for the trip near Alfama"),
    _thread(3, "Lisbon trip itinerary museums", "Museums to visit on the Lisbon trip"),
    _thread(4, "Lisbon trip restaurant list", "Restaurants for the Lisbon trip"),
]
PYTHON = _thread(9, "Fix python unit test", "The pytest fixture is not found")


def test_thread_terms_drop_stopwords_short_and_numeric_tokens() -> None:
    assert thread_terms("The trip to Lisbon in 2026", "we fly on 12") == {"trip", "lisbon", "fly"}


def test_similarity_measures() -> None:
    left, right = frozenset({"a", "b", "c"}), frozenset({"b", "c", "d", "e"})
    assert jaccard(left, right) == 2 / 5
    assert overlap(left, right) == 2 / 3
    assert jaccard(frozenset(), frozenset()) == 0.0


def test_four_overlapping_conversations_form_one_new_folder() -> None:
    candidates = lexical_groups(_grouping([*LISBON, PYTHON]))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind is FolderProposalKind.NEW_FOLDER
    assert candidate.name == "Lisbon Trip"
    assert candidate.target_folder_id is None
    assert candidate.member_session_ids == tuple(UUID(int=number) for number in (1, 2, 3, 4))
    assert candidate.derivation is FolderProposalDerivation.LEXICAL


def test_three_similar_conversations_stay_below_the_threshold() -> None:
    assert lexical_groups(_grouping(LISBON[:3])) == []
    assert len(lexical_groups(_grouping(LISBON[:3], threshold=3))) == 1


def test_stopwords_never_create_a_link() -> None:
    threads = [
        _thread(1, "How do I fix the build"),
        _thread(2, "How do I plan the trip"),
        _thread(3, "How do I cook the rice"),
        _thread(4, "How do I sleep at the night"),
    ]
    assert lexical_groups(_grouping(threads)) == []


def test_output_is_identical_under_shuffled_input() -> None:
    threads = [*LISBON, PYTHON]
    shuffled = list(threads)
    random.Random(7).shuffle(shuffled)
    assert lexical_groups(_grouping(threads)) == lexical_groups(_grouping(shuffled))


def test_a_colliding_name_becomes_an_addition_to_that_folder() -> None:
    folder = FolderSummary(id=KITCHEN, name="lisbon TRIP", member_titles=())
    candidates = lexical_groups(_grouping(LISBON, [folder]))
    assert [(candidate.kind, candidate.target_folder_id) for candidate in candidates] == [
        (FolderProposalKind.ADD_TO_FOLDER, KITCHEN)
    ]
    assert candidates[0].name is None


def test_an_existing_folder_attracts_matching_unfiled_conversations() -> None:
    folder = FolderSummary(
        id=KITCHEN,
        name="Kitchen Renovation",
        member_titles=("Kitchen renovation budget", "Kitchen renovation contractor quotes"),
    )
    threads = [_thread(5, "Kitchen renovation tile choices"), PYTHON]
    candidates = lexical_groups(_grouping(threads, [folder]))
    assert len(candidates) == 1
    assert candidates[0].kind is FolderProposalKind.ADD_TO_FOLDER
    assert candidates[0].target_folder_id == KITCHEN
    assert candidates[0].member_session_ids == (UUID(int=5),)


def test_components_are_trimmed_to_max_members_and_new_folders_come_first() -> None:
    extra = [
        _thread(5, "Lisbon trip packing list"),
        _thread(6, "Lisbon trip day trips"),
    ]
    folder = FolderSummary(id=KITCHEN, name="Kitchen", member_titles=("Kitchen remodel plan",))
    threads = [*LISBON, *extra, _thread(7, "Kitchen remodel lighting")]
    candidates = lexical_groups(_grouping(threads, [folder], max_members=5))
    assert [candidate.kind for candidate in candidates] == [
        FolderProposalKind.NEW_FOLDER,
        FolderProposalKind.ADD_TO_FOLDER,
    ]
    assert len(candidates[0].member_session_ids) == 5


async def test_lexical_grouper_reports_no_provider() -> None:
    outcome = await LexicalGrouper().group(_grouping([*LISBON, PYTHON]), principal=principal())
    assert [candidate.name for candidate in outcome.candidates] == ["Lisbon Trip"]
    assert outcome.provider == "none"
    assert outcome.fallback_used is False
    assert outcome.usage is None
