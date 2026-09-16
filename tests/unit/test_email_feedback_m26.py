"""Owner feedback uses an explicit supported target and actionable validation."""

from uuid import UUID

import pytest

from agent_core.application.email import save_value
from agent_core.domain.email import EmailFeedback, EmailThread, apply_feedback
from tests.contract.support import NOW
from tests.gates.test_email_experience_m26 import email_client, seed_mail


@pytest.mark.parametrize(
    "topics,target_value",
    [([], None), (["Board", "Hiring"], None), (["Board"], "private-unmatched-topic")],
)
async def test_topic_feedback_validation_explains_selection_without_echoing_content(
    topics: list[str], target_value: str | None
) -> None:
    """Return safe selection guidance when topic feedback lacks a supported target."""
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        thread = thread.model_copy(update={"topics": topics})
        async with app.uow_factory() as uow:
            await save_value(uow.email, app.principal, "thread", str(thread.id), thread, NOW)
        response = await client.post(
            "/v1/email/feedback",
            json={
                "thread_id": str(thread.id),
                "target": "topic",
                "judgment": "important",
                "target_value": target_value,
                "expected_revision": thread.revision,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_request"
        assert response.json()["error"]["message"] == (
            "Choose an available person or content topic, or apply feedback to This thread."
        )
        assert response.json()["error"]["details"] == {}
        assert response.headers["cache-control"] == "private, no-store"
        assert "private-unmatched-topic" not in response.text
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "feedback") == []


@pytest.mark.parametrize("judgment,priority", [("important", 1.0), ("less_important", 0.0)])
async def test_selected_topic_feedback_applies_replays_and_undoes(
    judgment: str, priority: float
) -> None:
    """Apply exact-topic feedback once and restore the prior projection on undo."""
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        thread = thread.model_copy(update={"topics": ["Board", "Hiring"]})
        async with app.uow_factory() as uow:
            await save_value(uow.email, app.principal, "thread", str(thread.id), thread, NOW)
        detail = await client.get(f"/v1/email/threads/{thread.id}")
        assert detail.json()["topics"] == ["Board", "Hiring"]
        body = {
            "thread_id": str(thread.id),
            "target": "topic",
            "judgment": judgment,
            "target_value": "Board",
            "expected_revision": thread.revision,
        }
        response = await client.post(
            "/v1/email/feedback", json=body, headers={"Idempotency-Key": "topic-feedback"}
        )
        assert response.status_code == 200
        assert response.json()["thread"]["priority"] == priority
        replay = await client.post(
            "/v1/email/feedback", json=body, headers={"Idempotency-Key": "topic-feedback"}
        )
        assert replay.json() == response.json()
        async with app.uow_factory() as uow:
            rows = await uow.email.list(app.principal, "feedback")
        assert len(rows) == 1
        assert rows[0].payload["target_values"] == ["Board"]
        undone = await client.delete(f"/v1/email/feedback/{response.json()['feedback_id']}")
        assert undone.status_code == 200
        assert undone.json()["priority"] == thread.priority


@pytest.mark.parametrize(
    "senders,expected", [(["invalid", "Alex <alex@example.test>"], 1.0), (["invalid"], 0.0)]
)
def test_person_feedback_ignores_malformed_senders(senders: list[str], expected: float) -> None:
    """Keep malformed sender strings outside the selectable person-feedback targets."""
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
    assert apply_feedback(thread, [feedback], now=NOW).priority == expected


def test_latest_matching_feedback_wins_whatever_the_input_order() -> None:
    """Judgments apply oldest first; unrelated or undone feedback never takes a turn."""
    from datetime import timedelta

    thread = EmailThread(
        id=UUID(int=1),
        account_id="work",
        provider_thread_id="t1",
        subject="Board",
        senders=["Alex <alex@example.test>"],
        topics=["board"],
        updated_at=NOW,
        last_accessed_at=NOW,
    )

    def item(
        number: int, judgment: str, target: str, value: str, *, minutes: int, undone: bool = False
    ) -> EmailFeedback:
        return EmailFeedback.model_validate(
            {
                "id": UUID(int=number),
                "thread_id": UUID(int=99) if target != "thread" else thread.id,
                "target": target,
                "judgment": judgment,
                "target_values": [value],
                "explanation": f"feedback {number}",
                "created_at": NOW + timedelta(minutes=minutes),
                "undone_at": NOW if undone else None,
            }
        )

    feedback = [
        item(5, "important", "topic", "hiring", minutes=9),
        item(4, "important", "topic", "board", minutes=8, undone=True),
        item(3, "less_important", "person", "alex@example.test", minutes=2),
        item(2, "important", "thread", str(thread.id), minutes=1),
        item(6, "important", "topic", "board", minutes=2),
    ]
    for order in (feedback, list(reversed(feedback))):
        selected = apply_feedback(thread, order, now=NOW)
        assert (selected.priority, selected.reason) == (1.0, "feedback 6")
