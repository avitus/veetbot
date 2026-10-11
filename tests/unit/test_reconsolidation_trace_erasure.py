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
