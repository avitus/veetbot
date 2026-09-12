"""Malformed external sender data does not invalidate owner feedback."""

from uuid import UUID

import pytest

from agent_core.domain.email import EmailFeedback, EmailThread, apply_feedback
from tests.contract.support import NOW


@pytest.mark.parametrize(
    "senders,expected", [(["invalid", "Alex <alex@example.test>"], 1.0), (["invalid"], 0.0)]
)
def test_person_feedback_ignores_malformed_senders(senders: list[str], expected: float) -> None:
    thread = EmailThread(
        id=UUID(int=1),
        account_id="work",
        provider_thread_id="t1",
        subject="Board",
        senders=senders,
        updated_at=NOW,
        last_accessed_at=NOW,
    )
    feedback = EmailFeedback(
        id=UUID(int=2),
        thread_id=thread.id,
        target="person",
        judgment="important",
        target_values=["alex@example.test"],
        created_at=NOW,
    )
    assert apply_feedback(thread, [feedback]).priority == expected
