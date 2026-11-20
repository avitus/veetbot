"""Shared recall-question store behavior, reused by the PostgreSQL adapter."""

from datetime import timedelta
from uuid import uuid4

import pytest

from agent_core.adapters.persistence.recall_questions import InMemoryRecallQuestionStore
from agent_core.domain.agents import Principal
from agent_core.domain.recall_questions import (
    QuestionPathResult,
    QuestionResult,
    QuestionSplit,
    QuestionStatus,
    RecallQuestion,
    RecallQuestionContent,
)
from agent_core.ports.recall_questions import QuestionKeyExists, RecallQuestionStore
from tests.contract.support import NOW, principal


def other() -> Principal:
    return principal().model_copy(update={"principal_id": "other"})


def make_content(
    question: str = "Where do I live?", fact: str = "Example Town"
) -> RecallQuestionContent:
    return RecallQuestionContent.model_validate(
        {
            "question": question,
            "family": "attributes",
            "expected": ({"statement_contains": fact},),
            "delivered_must_contain": ("Erin",),
        }
    )


def make_question(
    owner: Principal,
    key: str = "home-city",
    *,
    split: QuestionSplit = QuestionSplit.DEVELOPMENT,
    content: RecallQuestionContent | None = None,
) -> RecallQuestion:
    return RecallQuestion(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        key=key,
        split=split,
        content=content or make_content(),
        created_at=NOW,
        updated_at=NOW,
    )


def make_result(*, delivered: int = 1) -> QuestionResult:
    return QuestionResult(
        recorded_at=NOW,
        report_digest="a" * 64,
        paths=(
            QuestionPathResult(
                path="automatic",
                stages=("delivered" if delivered else "ranked",),
                required_total=1,
                required_delivered=delivered,
                delivered_found=(True,),
            ),
        ),
    )


async def questions_create_and_read_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    question = make_question(owner)
    await store.create(question)

    assert await store.get(owner, question.id) == question
    assert await store.get_by_key(owner, "home-city") == question
    assert await store.list_questions(owner) == [question]
    assert await store.count(owner) == 1
    (first,) = await store.history(owner, question.id)
    assert (first.revision, first.status, first.content) == (
        1,
        QuestionStatus.ACTIVE,
        question.content,
    )
    assert await store.get(owner, uuid4()) is None
    assert await store.get_by_key(owner, "missing") is None


async def questions_key_collision_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    await store.create(make_question(owner))
    with pytest.raises(QuestionKeyExists):
        await store.create(make_question(owner))
    # A key is the owner's own namespace: another owner may use the same one.
    await store.create(make_question(other()))
    assert await store.count(owner) == 1 and await store.count(other()) == 1


async def questions_scope_isolation_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    question = make_question(owner)
    await store.create(question)
    stranger = other()

    assert await store.get(stranger, question.id) is None
    assert await store.get_by_key(stranger, question.key) is None
    assert await store.list_questions(stranger) == []
    assert await store.history(stranger, question.id) == []
    assert await store.record_result(stranger, question.key, make_result()) is False
    forged = question.model_copy(
        update={
            "principal_id": stranger.principal_id,
            "revision": 2,
            "status": QuestionStatus.RETIRED,
        }
    )
    assert await store.save(forged, expected_revision=1) is False
    assert await store.get(owner, question.id) == question


async def questions_save_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    question = make_question(owner)
    await store.create(question)
    later = NOW + timedelta(minutes=5)
    edited = question.model_copy(
        update={
            "content": make_content(fact="New Town"),
            "revision": 2,
            "updated_at": later,
            # Neither of these is the store's to change.
            "key": "renamed",
            "split": QuestionSplit.HOLDOUT,
        }
    )

    assert await store.save(edited, expected_revision=1) is True
    stored = await store.get(owner, question.id)
    assert stored is not None
    assert (stored.key, stored.split) == ("home-city", QuestionSplit.DEVELOPMENT)
    assert (stored.revision, stored.updated_at, stored.created_at) == (2, later, NOW)
    assert stored.content.expected[0].statement_contains == "New Town"
    assert [item.revision for item in await store.history(owner, question.id)] == [1, 2]

    # A writer holding the old revision changes nothing.
    stale = stored.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 2})
    assert await store.save(stale, expected_revision=1) is False
    assert await store.get(owner, question.id) == stored
    assert len(await store.history(owner, question.id)) == 2

    retired = stored.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 3})
    assert await store.save(retired, expected_revision=2) is True
    assert [item.status for item in await store.history(owner, question.id)][
        -1
    ] is QuestionStatus.RETIRED
    assert await store.save(make_question(owner, "never-created"), expected_revision=1) is False


async def questions_list_filters_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    zebra = make_question(owner, "zebra", split=QuestionSplit.HOLDOUT)
    apple = make_question(owner, "apple")
    mango = make_question(owner, "mango")
    for question in (zebra, apple, mango):
        await store.create(question)
    assert await store.save(
        mango.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 2}),
        expected_revision=1,
    )

    assert [item.key for item in await store.list_questions(owner)] == ["apple", "mango", "zebra"]
    assert [
        item.key for item in await store.list_questions(owner, split=QuestionSplit.HOLDOUT)
    ] == ["zebra"]
    assert [
        item.key for item in await store.list_questions(owner, status=QuestionStatus.ACTIVE)
    ] == ["apple", "zebra"]
    assert [
        item.key
        for item in await store.list_questions(
            owner, split=QuestionSplit.DEVELOPMENT, status=QuestionStatus.RETIRED
        )
    ] == ["mango"]


async def questions_record_result_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    question = make_question(owner)
    await store.create(question)

    assert await store.record_result(owner, "home-city", make_result()) is True
    stored = await store.get(owner, question.id)
    assert stored is not None and stored.latest_result == make_result()
    # A recorded run is not an edit: revision and history stand.
    assert stored.revision == 1 and len(await store.history(owner, question.id)) == 1
    assert await store.record_result(owner, "home-city", make_result(delivered=0)) is True
    replaced = await store.get(owner, question.id)
    assert replaced is not None and replaced.latest_result is not None
    assert replaced.latest_result.passed is False
    assert await store.record_result(owner, "missing", make_result()) is False


async def questions_erase_principal_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    first = make_question(owner, "first")
    await store.create(first)
    await store.create(make_question(owner, "second"))
    kept = make_question(other(), "first")
    await store.create(kept)

    assert await store.erase_principal(owner) == 2
    assert await store.list_questions(owner) == [] and await store.count(owner) == 0
    assert await store.history(owner, first.id) == []
    assert await store.list_questions(other()) == [kept]
    assert await store.erase_principal(owner) == 0


CONTRACTS = [
    questions_create_and_read_contract,
    questions_key_collision_contract,
    questions_scope_isolation_contract,
    questions_save_contract,
    questions_list_filters_contract,
    questions_record_result_contract,
    questions_erase_principal_contract,
]


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda fn: fn.__name__)
async def test_memory_recall_question_store_contract(contract) -> None:  # type: ignore[no-untyped-def]
    await contract(InMemoryRecallQuestionStore())
