"""Code draft storage contract shared by memory and PostgreSQL (ADR-0281)."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid5

import pytest

from agent_core.domain.agents import Principal
from agent_core.domain.code import CodeDraft, CodeDraftState, CodeProposal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.code import CodeDraftRepository
from tests.contract.support import NOW, memory_uow_factory, principal

SCENARIOS: list[Callable[[CodeDraftRepository, Principal], Awaitable[None]]] = []


def scenario(
    function: Callable[[CodeDraftRepository, Principal], Awaitable[None]],
) -> Callable[[CodeDraftRepository, Principal], Awaitable[None]]:
    SCENARIOS.append(function)
    return function


def draft(owner: Principal, slug: str = "fix-pause", **changes: Any) -> CodeDraft:
    fields: dict[str, Any] = {
        "id": uuid5(UUID(int=0), f"{owner.principal_id}/{slug}"),
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "slug": slug,
        "base_release": "release-1",
        "base_revision": "a" * 40,
        "files": {"src/agent_core/a.py": "print('é')\n", "src/agent_core/gone.py": None},
        "created_at": NOW,
        "updated_at": NOW,
    }
    return CodeDraft(**{**fields, **changes})


def proposal(*, due_in: timedelta = timedelta(0), **changes: Any) -> CodeProposal:
    return CodeProposal(
        title="Stop counting sleep",
        body="The watchdog counted sleep.",
        branch="veetbot/fix-pause",
        next_attempt_at=NOW + due_in,
        **changes,
    )


def proposing(owner: Principal, slug: str, *, due_in: timedelta = timedelta(0)) -> CodeDraft:
    return draft(owner, slug, state=CodeDraftState.PROPOSING, proposal=proposal(due_in=due_in))


def other_owner(owner: Principal, name: str = "another-owner") -> Principal:
    return owner.model_copy(update={"principal_id": f"{owner.principal_id}-{name}"})


@scenario
async def insert_refuses_a_second_draft_with_the_same_slug(
    store: CodeDraftRepository, owner: Principal
) -> None:
    saved = draft(owner)
    assert await store.put(saved, expected_version=None) == saved
    with pytest.raises(ConflictError):
        await store.put(draft(owner, base_release="release-2"), expected_version=None)
    assert await store.get(owner, saved.slug) == saved
    foreign = draft(other_owner(owner))
    await store.put(foreign, expected_version=None)
    assert await store.get(other_owner(owner), saved.slug) == foreign


@scenario
async def replacing_requires_the_current_version(
    store: CodeDraftRepository, owner: Principal
) -> None:
    saved = draft(owner)
    changed = saved.model_copy(update={"version": 2, "files": {"README.md": "new\n"}})
    with pytest.raises(ConflictError):
        await store.put(changed, expected_version=1)
    await store.put(saved, expected_version=None)
    with pytest.raises(ConflictError):
        await store.put(changed.model_copy(update={"version": 10}), expected_version=9)
    with pytest.raises(ConflictError):
        await store.put(changed.model_copy(update={"version": 3}), expected_version=1)
    assert await store.put(changed, expected_version=1) == changed
    assert await store.get(owner, saved.slug) == changed
    with pytest.raises(ConflictError):
        await store.put(changed, expected_version=1)
    assert await store.get(owner, saved.slug) == changed


@scenario
async def get_hides_missing_and_foreign_drafts(
    store: CodeDraftRepository, owner: Principal
) -> None:
    saved = draft(owner)
    await store.put(saved, expected_version=None)
    with pytest.raises(NotFoundError):
        await store.get(owner, "missing-draft")
    with pytest.raises(NotFoundError):
        await store.get(other_owner(owner), saved.slug)
    with pytest.raises(NotFoundError):
        await store.get(owner.model_copy(update={"tenant_id": "another-tenant"}), saved.slug)


@scenario
async def list_open_is_the_owners_open_drafts_newest_first(
    store: CodeDraftRepository, owner: Principal
) -> None:
    old = draft(owner, "old-draft")
    new = draft(owner, "new-draft").model_copy(update={"updated_at": NOW + timedelta(hours=2)})
    middle = draft(owner, "mid-draft").model_copy(update={"updated_at": NOW + timedelta(hours=1)})
    closed = [
        draft(owner, "sent-draft", state=CodeDraftState.PROPOSING, proposal=proposal()),
        draft(owner, "done-draft", state=CodeDraftState.PROPOSED, proposal=proposal()),
        draft(owner, "dead-draft", state=CodeDraftState.DISCARDED),
    ]
    for item in (old, new, middle, *closed, draft(other_owner(owner), "foreign-draft")):
        await store.put(item, expected_version=None)
    assert await store.list_open(owner, limit=10) == [new, middle, old]
    assert await store.list_open(owner, limit=2) == [new, middle]
    assert await store.list_open(owner.model_copy(update={"tenant_id": "another"}), limit=9) == []


@scenario
async def due_proposals_are_oldest_first_across_owners(
    store: CodeDraftRepository, owner: Principal
) -> None:
    second_owner = other_owner(owner)
    oldest = proposing(second_owner, "oldest-draft", due_in=timedelta(hours=-2))
    middle = proposing(owner, "middle-draft", due_in=timedelta(hours=-1))
    exactly_now = proposing(second_owner, "exact-draft")
    not_yet = [
        proposing(owner, "later-draft", due_in=timedelta(seconds=1)),
        draft(owner, "open-draft"),
        draft(owner, "open-with-proposal", proposal=proposal(due_in=timedelta(hours=-3))),
        draft(
            owner,
            "done-draft",
            state=CodeDraftState.PROPOSED,
            proposal=proposal(due_in=timedelta(hours=-3)),
        ),
        draft(
            owner,
            "dead-draft",
            state=CodeDraftState.DISCARDED,
            proposal=proposal(due_in=timedelta(hours=-3)),
        ),
    ]
    for item in (exactly_now, *not_yet, middle, oldest):
        await store.put(item, expected_version=None)
    assert await store.due_proposals(NOW, limit=10) == [oldest, middle, exactly_now]
    assert await store.due_proposals(NOW, limit=2) == [oldest, middle]
    assert await store.due_proposals(NOW - timedelta(hours=3), limit=10) == []


@scenario
async def a_retried_proposal_follows_its_new_due_time(
    store: CodeDraftRepository, owner: Principal
) -> None:
    saved = proposing(owner, "retry-draft")
    await store.put(saved, expected_version=None)
    assert saved.proposal is not None
    retry = saved.proposal.model_copy(
        update={
            "attempts": 1,
            "next_attempt_at": NOW + timedelta(minutes=5),
            "last_error": "GitHub answered 502.",
        }
    )
    waiting = saved.model_copy(update={"version": 2, "proposal": retry})
    await store.put(waiting, expected_version=1)
    assert await store.due_proposals(NOW, limit=10) == []
    assert await store.due_proposals(NOW + timedelta(minutes=5), limit=10) == [waiting]
    finished = waiting.model_copy(update={"version": 3, "state": CodeDraftState.PROPOSED})
    await store.put(finished, expected_version=2)
    assert await store.due_proposals(NOW + timedelta(days=1), limit=10) == []


@scenario
async def a_draft_round_trips_exactly(store: CodeDraftRepository, owner: Principal) -> None:
    bare = draft(owner, "bare-draft", files={})
    deleted = draft(owner, "delete-draft", files={"src/a.py": None, "src/b.py": ""})
    whole = draft(
        owner,
        "whole-draft",
        state=CodeDraftState.PROPOSED,
        version=1,
        proposal=proposal(
            attempts=3,
            last_error="A prior attempt failed.",
            commit_sha="b" * 40,
            pull_number=42,
            pull_url="https://github.com/avitus/veetbot/pull/42",
        ),
    ).model_copy(update={"updated_at": NOW + timedelta(microseconds=7)})
    for item in (bare, deleted, whole):
        await store.put(item, expected_version=None)
        read = await store.get(owner, item.slug)
        assert read == item
        assert read.model_dump(mode="json") == item.model_dump(mode="json")
    assert (await store.get(owner, deleted.slug)).files == {"src/a.py": None, "src/b.py": ""}
    assert (await store.get(owner, bare.slug)).proposal is None
    cleared = whole.model_copy(update={"version": 2, "proposal": None})
    await store.put(cleared, expected_version=1)
    assert (await store.get(owner, whole.slug)).proposal is None


@scenario
async def callers_cannot_mutate_a_stored_draft_through_their_copy(
    store: CodeDraftRepository, owner: Principal
) -> None:
    saved = draft(owner)
    original = dict(saved.files)
    returned = await store.put(saved, expected_version=None)
    saved.files["added-by-caller"] = "x"
    returned.files.clear()
    read = await store.get(owner, saved.slug)
    assert read.files == original
    read.files.clear()
    assert (await store.get(owner, saved.slug)).files == original
    (await store.list_open(owner, limit=1))[0].files.clear()
    assert (await store.get(owner, saved.slug)).files == original


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda function: function.__name__)
async def test_memory_code_draft_store(
    check: Callable[[CodeDraftRepository, Principal], Awaitable[None]],
) -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await check(uow.code_drafts, principal())


async def test_memory_unit_of_work_keeps_drafts_between_transactions() -> None:
    _, factory = await memory_uow_factory()
    owner = principal()
    async with factory() as uow:
        await uow.code_drafts.put(draft(owner), expected_version=None)
    async with factory() as uow:
        assert await uow.code_drafts.get(owner, "fix-pause") == draft(owner)
