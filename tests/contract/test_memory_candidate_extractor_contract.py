"""Memory candidate extractor contract: bounded, structured, trusted proposals."""

import pytest

from agent_core.memory.formation import MAX_EXTRACTOR_PROPOSALS, DeterministicCandidateExtractor
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import principal


async def test_extractor_returns_separate_provenance_bound_candidate_proposals() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    source = await user_event(factory, "I have an Apple Watch and a BMW X3.")
    events = await session_events(factory)

    candidates = await DeterministicCandidateExtractor().extract(
        events,
        principal=principal(),
        scope="project-a",
    )

    assert {candidate.subject for candidate in candidates} == {"Apple Watch", "BMW X3"}
    assert all(candidate.source_event_ids == [source] for candidate in candidates)
    assert all(candidate.proposed_scope == "project-a" for candidate in candidates)
    assert all(candidate.model_confidence > 0 for candidate in candidates)


async def test_extractor_enforces_its_candidate_volume_cap() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "I have an Apple Watch and a BMW X3.")

    candidates = await DeterministicCandidateExtractor(maximum_candidates=1).extract(
        await session_events(factory),
        principal=principal(),
        scope="project-a",
    )

    assert len(candidates) == 1

    with pytest.raises(ValueError, match="must not exceed"):
        DeterministicCandidateExtractor(maximum_candidates=MAX_EXTRACTOR_PROPOSALS + 1)
