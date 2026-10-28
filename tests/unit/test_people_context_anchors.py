"""Standalone claims keep exact context anchors without widening frozen controls."""

from typing import Any

import pytest

from agent_core.memory.distillation import NemoriAssistedCandidateExtractor
from agent_core.memory.people_context_distillation import (
    ContextualPeopleCandidateExtractor as PeopleAssistedCandidateExtractor,
)
from agent_core.memory.people_context_distillation import (
    _ContextualCandidate as _PeopleDistilledCandidate,
)
from tests.unit.test_people_named_distillation import proposed


@pytest.mark.parametrize(
    "statement",
    [
        "User reports that Aurora takes nearly 18 seconds to set a timer.",
        "User reports that setting a timer takes nearly 18 seconds.",
    ],
)
async def test_context_anchor_must_survive_in_standalone_statement(statement: str) -> None:
    candidate, events = await proposed(
        "My assistant Aurora takes nearly 18 seconds to set a timer.",
        "Aurora",
        statement=statement,
        kind="recurring_state",
    )
    payload = candidate.model_dump(mode="json")
    payload["people"] = None
    payload["context_anchors"] = [
        {"source_event_id": candidate.source_event_ids[0], "text": "Aurora"}
    ]
    candidate = _PeopleDistilledCandidate.model_validate(payload)
    if "Aurora" not in statement:
        with pytest.raises(ValueError, match="context"):
            PeopleAssistedCandidateExtractor._normalize_candidate(
                candidate, by_sequence=events, scope="user"
            )
    else:
        result = PeopleAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence=events, scope="user"
        )
        assert result.statement == statement
        assert result.source_event_ids == candidate.source_event_ids


def test_only_people_policy_requires_context_anchors_in_provider_schema() -> None:
    schema = PeopleAssistedCandidateExtractor._distillation_schema()
    assert "context_anchors" in schema["$defs"]["_ContextualCandidate"]["required"]
    assert (
        "context_anchors"
        not in NemoriAssistedCandidateExtractor._distillation_schema()["$defs"][
            "_DistilledCandidate"
        ]["properties"]
    )


@pytest.mark.parametrize(
    "anchor",
    [
        {"source_event_id": 999, "text": "Aurora"},
        {"source_event_id": 1, "text": "OtherAssistant"},
    ],
)
async def test_context_cannot_introduce_uncited_or_invented_referents(
    anchor: dict[str, Any],
) -> None:
    candidate, events = await proposed(
        "My Aurora assistant is slow.",
        "Aurora",
        statement="User reports Aurora is slow.",
        kind="recurring_state",
    )
    data = candidate.model_dump(mode="json")
    data.update(people=None, context_anchors=[anchor])
    candidate = _PeopleDistilledCandidate.model_validate(data)
    with pytest.raises(ValueError, match="context"):
        PeopleAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence=events, scope="user"
        )
