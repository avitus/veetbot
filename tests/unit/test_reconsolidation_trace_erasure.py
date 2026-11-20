"""Email exclusion removes entire summaries, including unrendered support."""

import pytest

from agent_core.adapters.persistence.email_erasure import erase_trace


@pytest.mark.parametrize("kind", ["summary", "hypothesis"])
def test_email_trace_erasure_discards_whole_dependent_summary(kind: str) -> None:
    value = {
        "beliefs": [
            {
                "belief_id": "summary",
                "record_kind": kind,
                "statement": "A permitted clause",
                "support_ids": ["rendered", "omitted"],
            },
            {"belief_id": "independent", "record_kind": "belief", "statement": "Independent fact"},
        ],
        "returned": ["summary", "independent"],
        "cited": ["summary"],
        "rendered": "A permitted clause",
    }
    erased = erase_trace(value, {"omitted"})
    assert erased is not None
    assert erased["beliefs"] == [
        {"belief_id": "independent", "record_kind": "belief", "statement": "Independent fact"}
    ]
    assert erased["returned"] == ["independent"] and erased["cited"] == []
    assert erased["rendered"] == ""


def test_every_trace_identifier_list_is_scrubbed_by_erasure() -> None:
    """A new list of belief identifiers must not escape the three erasure paths."""

    from uuid import UUID

    from agent_core.domain import memory as domain

    identifier_lists = {
        name
        for name, field in domain.RecallTrace.model_fields.items()
        if field.annotation == list[UUID]
    }
    assert "ranked_candidates" in identifier_lists
    assert identifier_lists == set(getattr(domain, "TRACE_BELIEF_ID_FIELDS", ()))


def test_email_trace_erasure_scrubs_the_candidate_ranking() -> None:
    value = {
        "beliefs": [{"belief_id": "doomed", "record_kind": "belief", "statement": "Gone"}],
        "returned": ["doomed"],
        "ranked_candidates": ["kept", "doomed", "also-kept"],
        "rendered": "Gone",
    }
    erased = erase_trace(value, {"doomed"})
    assert erased is not None
    assert erased["ranked_candidates"] == ["kept", "also-kept"]
