"""Shared recall-question store behavior, reused by the PostgreSQL adapter."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from agent_core.adapters.memory.transactions import MemoryTransaction
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


def make_result(
    *, delivered: int = 1, content: RecallQuestionContent | None = None
) -> QuestionResult:
    return QuestionResult(
        recorded_at=NOW,
        report_digest="a" * 64,
        content_sha256=(content or make_content()).criteria_sha256,
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
    assert not await store.record_result(stranger, question.key, make_result(), expected_revision=1)
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

    assert await store.record_result(owner, "home-city", make_result(), expected_revision=1)
    stored = await store.get(owner, question.id)
    assert stored is not None and stored.latest_result == make_result()
    # A recorded run is not an edit: revision and history stand.
    assert stored.revision == 1 and len(await store.history(owner, question.id)) == 1
    assert await store.record_result(
        owner, "home-city", make_result(delivered=0), expected_revision=1
    )
    replaced = await store.get(owner, question.id)
    assert replaced is not None and replaced.latest_result is not None
    assert replaced.latest_result.passed is False
    assert not await store.record_result(owner, "missing", make_result(), expected_revision=1)
    # A result for a revision the question has left changes nothing.
    assert not await store.record_result(owner, "home-city", make_result(), expected_revision=2)
    assert await store.get(owner, question.id) == replaced


async def questions_save_carries_the_result_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    question = make_question(owner)
    await store.create(question)
    await store.record_result(owner, "home-city", make_result(), expected_revision=1)
    recorded = await store.get(owner, question.id)
    assert recorded is not None and recorded.latest_result is not None

    kept = recorded.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 2})
    assert await store.save(kept, expected_revision=1) is True
    after_status = await store.get(owner, question.id)
    assert after_status is not None and after_status.latest_result == make_result()

    cleared = after_status.model_copy(
        update={"content": make_content(fact="New Town"), "revision": 3}
    )
    assert await store.save(cleared, expected_revision=2, clear_result=True) is True
    after_edit = await store.get(owner, question.id)
    assert after_edit is not None and after_edit.latest_result is None


async def questions_save_keeps_a_result_recorded_meanwhile_contract(
    store: RecallQuestionStore,
) -> None:
    """A run recorded between an editor's read and its save is not written over.

    Recording moves no revision, so the save's revision check cannot see it.
    A save that does not change what the question measures leaves the stored
    result alone, whatever the copy it was handed holds.
    """

    owner = principal()
    question = make_question(owner)
    await store.create(question)
    read_before = await store.get(owner, question.id)
    assert read_before is not None and read_before.latest_result is None

    assert await store.record_result(owner, "home-city", make_result(), expected_revision=1)
    noted = read_before.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 2})
    assert await store.save(noted, expected_revision=1) is True
    stored = await store.get(owner, question.id)
    assert stored is not None and stored.latest_result == make_result()

    # The same with an older result in the editor's copy.
    read_again = stored
    newer = make_result(delivered=0)
    assert await store.record_result(owner, "home-city", newer, expected_revision=2)
    restored = read_again.model_copy(update={"status": QuestionStatus.ACTIVE, "revision": 3})
    assert await store.save(restored, expected_revision=2) is True
    final = await store.get(owner, question.id)
    assert final is not None and final.latest_result == newer


async def questions_same_id_two_owners_contract(store: RecallQuestionStore) -> None:
    owner = principal()
    mine = make_question(owner, "mine")
    theirs = make_question(other(), "theirs").model_copy(update={"id": mine.id})
    await store.create(mine)
    await store.create(theirs)
    edited = mine.model_copy(update={"content": make_content(fact="New Town"), "revision": 2})
    assert await store.save(edited, expected_revision=1) is True

    # One identifier under two owners is two questions with two histories.
    assert [item.revision for item in await store.history(owner, mine.id)] == [1, 2]
    assert [item.revision for item in await store.history(other(), mine.id)] == [1]

    assert await store.erase_principal(owner) == 1
    assert [item.revision for item in await store.history(other(), mine.id)] == [1]
    kept = theirs.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 2})
    assert await store.save(kept, expected_revision=1) is True
    assert len(await store.history(other(), mine.id)) == 2


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
    questions_save_carries_the_result_contract,
    questions_save_keeps_a_result_recorded_meanwhile_contract,
    questions_same_id_two_owners_contract,
    questions_erase_principal_contract,
]


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda fn: fn.__name__)
async def test_memory_recall_question_store_contract(contract) -> None:  # type: ignore[no-untyped-def]
    await contract(InMemoryRecallQuestionStore())


async def test_a_failed_transaction_never_undoes_another_transactions_question() -> None:
    """In memory, the owner's lock is held until the unit of work ends.

    A journaled write that another transaction could read and build on would
    take that transaction's committed work with it when it is undone.
    """

    transaction = MemoryTransaction()
    store = InMemoryRecallQuestionStore(transaction)
    owner = principal()
    question = make_question(owner)
    created = asyncio.Event()
    release = asyncio.Event()

    async def failing() -> None:
        async with transaction.transaction():
            await store.create(question)
            created.set()
            await release.wait()
            raise RuntimeError("the unit of work failed")

    async def other() -> RecallQuestion | None:
        await created.wait()
        async with transaction.transaction():
            seen = await store.get(owner, question.id)
            if seen is not None:
                retired = seen.model_copy(update={"status": QuestionStatus.RETIRED, "revision": 2})
                await store.save(retired, expected_revision=1)
            return seen

    first = asyncio.create_task(failing())
    second = asyncio.create_task(other())
    await created.wait()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done(), "a read must wait for the transaction that holds the owner"
    release.set()
    with pytest.raises(RuntimeError, match="unit of work failed"):
        await first
    assert await second is None
    assert await store.get(owner, question.id) is None
    assert await store.history(owner, question.id) == []


async def test_a_failed_transaction_restores_an_erased_owners_questions() -> None:
    transaction = MemoryTransaction()
    store = InMemoryRecallQuestionStore(transaction)
    owner = principal()
    question = make_question(owner)
    await store.create(question)
    edited = question.model_copy(update={"content": make_content(fact="New Town"), "revision": 2})
    assert await store.save(edited, expected_revision=1) is True
    before = (await store.list_questions(owner), await store.history(owner, question.id))

    with pytest.raises(RuntimeError, match="unit of work failed"):
        async with transaction.transaction():
            assert await store.erase_principal(owner) == 1
            assert await store.list_questions(owner) == []
            raise RuntimeError("the unit of work failed")
    assert (await store.list_questions(owner), await store.history(owner, question.id)) == before

    async with transaction.transaction():
        assert await store.erase_principal(owner) == 1
    assert await store.list_questions(owner) == []
    assert await store.history(owner, question.id) == []
