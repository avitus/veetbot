"""The proposal pass: eligibility, the bounded set, durable decline, withdrawal."""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.config import ConfigurationError, load_config_document
from agent_core.domain.events import NewEvent, ProcessEvent
from agent_core.domain.folders import (
    FolderProposal,
    FolderProposalKind,
    FolderProposalState,
    FolderWithdrawalReason,
    ThreadFolder,
)
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.folders.clustering import LexicalGrouper
from agent_core.folders.profiles import FolderProfiles, FolderProposalProfile
from agent_core.folders.proposals import FolderProposalPass
from tests.contract.support import (
    AGENT_ID,
    NOW,
    PRINCIPAL_ID,
    TENANT,
    memory_uow_factory,
    principal,
)
from tests.integration.m2_support import memory_settings

LISBON_TITLES = [
    "Lisbon trip flights",
    "Lisbon trip hotel booking",
    "Lisbon trip itinerary museums",
    "Lisbon trip restaurant list",
    "Lisbon trip packing list",
    "Lisbon trip day trips",
]
KITCHEN_TITLES = [
    "Kitchen renovation budget",
    "Kitchen renovation contractor quotes",
    "Kitchen renovation tile choices",
    "Kitchen renovation lighting plan",
]


async def _stack() -> tuple[FixedClock, MemoryUnitOfWorkFactory]:
    return await memory_uow_factory()


async def _session(
    factory: MemoryUnitOfWorkFactory,
    number: int,
    title: str | None,
    *,
    metadata: dict[str, object] | None = None,
    message: str | None = None,
) -> UUID:
    session_id = UUID(int=1000 + number)
    async with factory() as uow:
        await uow.sessions.create(
            Session(
                id=session_id,
                tenant_id=TENANT,
                principal_id=PRINCIPAL_ID,
                agent_id=AGENT_ID,
                agent_version="1.0.0",
                status=SessionStatus.ACTIVE,
                title=title,
                metadata=metadata or {},
                created_at=NOW + timedelta(seconds=number),
                updated_at=NOW + timedelta(seconds=number),
            )
        )
        text = message if message is not None else (title or "")
        if text:
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    payload_schema_version=1,
                    actor_type="principal",
                    actor_id=PRINCIPAL_ID,
                    payload={"content": f"{text} — details about it"},
                )
            )
    return session_id


def _pass(
    clock: FixedClock,
    factory: MemoryUnitOfWorkFactory,
    **overrides: object,
) -> FolderProposalPass:
    return FolderProposalPass(
        uow_factory=factory,
        clock=clock,
        ids=SequenceIdFactory(),
        principal=principal(),
        profile=FolderProposalProfile(**overrides),  # type: ignore[arg-type]
        grouper=LexicalGrouper(),
    )


async def _open(factory: MemoryUnitOfWorkFactory) -> list[FolderProposal]:
    async with factory() as uow:
        return await uow.folders.list_proposals(principal(), state=FolderProposalState.PROPOSED)


async def _events(factory: MemoryUnitOfWorkFactory) -> list[ProcessEvent]:
    async with factory() as uow:
        return [
            event
            for event in await uow.process_events.list()
            if event.event_type.startswith("folder.")
        ]


def test_shipped_document_matches_the_defaults() -> None:
    document = load_config_document(memory_settings(), "folders/profiles.yaml")
    assert FolderProfiles.from_document(document) == FolderProfiles()
    assert FolderProfiles().proposals.threshold == 4
    assert FolderProfiles().proposals.max_open == 3


def test_invalid_document_names_the_file() -> None:
    with pytest.raises(ConfigurationError, match=re.escape("folders/profiles.yaml")):
        FolderProfiles.from_document({"schema_version": 1, "proposals": {"threshold": 1}})
    with pytest.raises(ConfigurationError, match="max_members"):
        FolderProfiles.from_document(
            {"schema_version": 1, "proposals": {"threshold": 6, "max_members": 5}}
        )


async def test_a_new_folder_is_proposed_only_at_the_threshold() -> None:
    clock, factory = await _stack()
    for number, title in enumerate(LISBON_TITLES[:3]):
        await _session(factory, number, title)
    pass_ = _pass(clock, factory)
    assert await pass_.run_once() == 0
    assert await _open(factory) == []

    fourth = await _session(factory, 3, LISBON_TITLES[3])
    assert await pass_.run_once() == 1
    (proposal,) = await _open(factory)
    assert proposal.kind is FolderProposalKind.NEW_FOLDER
    assert proposal.proposed_name == "Lisbon Trip"
    assert fourth in proposal.member_session_ids
    assert len(proposal.member_session_ids) == 4
    assert proposal.derivation.value == "lexical"
    events = await _events(factory)
    assert {event.event_type for event in events} == {
        "folder.proposal.created",
        "folder.proposal.pass",
    }
    for event in events:
        assert event.actor_type == "system"
        assert "Lisbon" not in repr(event.payload)
    pass_event = [event for event in events if event.event_type == "folder.proposal.pass"][-1]
    assert pass_event.payload["created"] == 1
    assert pass_event.payload["fallback_used"] is False


async def test_a_second_run_replays_without_a_second_proposal() -> None:
    clock, factory = await _stack()
    for number, title in enumerate(LISBON_TITLES[:4]):
        await _session(factory, number, title)
    pass_ = _pass(clock, factory)
    assert await pass_.run_once() == 1
    assert await pass_.run_once() == 0
    assert len(await _open(factory)) == 1


async def test_the_open_set_is_bounded_and_frees_on_resolution() -> None:
    clock, factory = await _stack()
    for number, title in enumerate(LISBON_TITLES[:4]):
        await _session(factory, number, title)
    for number, title in enumerate(KITCHEN_TITLES, start=10):
        await _session(factory, number, title)
    pass_ = _pass(clock, factory, max_open=1)
    assert await pass_.run_once() == 1
    assert await pass_.run_once() == 0
    (first,) = await _open(factory)
    async with factory() as uow:
        await uow.folders.resolve_proposal(
            first.id,
            principal(),
            state=FolderProposalState.DECLINED,
            resolved_at=clock.now(),
        )
    assert await pass_.run_once() == 1
    (second,) = await _open(factory)
    assert second.content_key != first.content_key
    assert set(second.member_session_ids).isdisjoint(first.member_session_ids)


async def test_declined_groupings_and_near_duplicates_never_return() -> None:
    clock, factory = await _stack()
    for number, title in enumerate(LISBON_TITLES[:4]):
        await _session(factory, number, title)
    pass_ = _pass(clock, factory)
    assert await pass_.run_once() == 1
    (proposal,) = await _open(factory)
    async with factory() as uow:
        await uow.folders.resolve_proposal(
            proposal.id,
            principal(),
            state=FolderProposalState.DECLINED,
            resolved_at=clock.now(),
        )
    assert await pass_.run_once() == 0

    await _session(factory, 4, LISBON_TITLES[4])
    assert await pass_.run_once() == 0  # four of five members: a near duplicate

    await _session(factory, 5, LISBON_TITLES[5])
    assert await pass_.run_once() == 1  # four of six: the grouping changed materially
    (again,) = await _open(factory)
    assert len(again.member_session_ids) == 6


async def test_filed_members_gone_targets_and_taken_names_withdraw() -> None:
    clock, factory = await _stack()
    ids = [await _session(factory, number, title) for number, title in enumerate(LISBON_TITLES[:4])]
    pass_ = _pass(clock, factory)
    assert await pass_.run_once() == 1
    (proposal,) = await _open(factory)
    travel = ThreadFolder(
        id=UUID(int=7001),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        name="Travel",
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory() as uow:
        await uow.folders.create_folder(travel)
        await uow.folders.set_membership(ids[0], principal(), folder_id=travel.id, added_at=NOW)
    # Filing a member withdraws the new-folder proposal, and the filed
    # conversation now attracts the rest as an addition to Travel.
    assert await pass_.run_once() == 1
    async with factory() as uow:
        withdrawn = await uow.folders.get_proposal(proposal.id, principal())
    assert withdrawn.state is FolderProposalState.WITHDRAWN
    assert withdrawn.withdrawal_reason is FolderWithdrawalReason.MEMBER_GONE
    (addition,) = await _open(factory)
    assert addition.kind is FolderProposalKind.ADD_TO_FOLDER
    assert addition.target_folder_id == travel.id
    assert set(addition.member_session_ids) == {ids[1], ids[2], ids[3]}
    async with factory() as uow:
        await uow.folders.delete_folder(travel.id, principal())
    assert await pass_.run_once() == 1  # target gone withdraws; the four regroup
    async with factory() as uow:
        gone = await uow.folders.get_proposal(addition.id, principal())
        (renewed,) = await uow.folders.list_proposals(
            principal(), state=FolderProposalState.PROPOSED
        )
    assert gone.withdrawal_reason is FolderWithdrawalReason.TARGET_GONE
    assert renewed.kind is FolderProposalKind.NEW_FOLDER

    async with factory() as uow:
        await uow.folders.create_folder(travel.model_copy(update={"name": "lisbon trip"}))
    assert await pass_.run_once() == 1  # the name is taken; the group joins that folder
    async with factory() as uow:
        taken = await uow.folders.get_proposal(renewed.id, principal())
        (joined,) = await uow.folders.list_proposals(
            principal(), state=FolderProposalState.PROPOSED
        )
    assert taken.withdrawal_reason is FolderWithdrawalReason.NAME_TAKEN
    assert joined.kind is FolderProposalKind.ADD_TO_FOLDER


async def test_email_scheduled_delegated_and_untitled_sessions_are_never_candidates() -> None:
    clock, factory = await _stack()
    await _session(factory, 0, LISBON_TITLES[0], metadata={"schedule_id": "s1"})
    await _session(factory, 1, LISBON_TITLES[1], metadata={"email_thread_id": "t1"})
    await _session(factory, 2, LISBON_TITLES[2], metadata={"run_kind": "delegated"})
    await _session(factory, 3, None, message=LISBON_TITLES[3])
    await _session(factory, 4, LISBON_TITLES[4])
    await _session(factory, 5, LISBON_TITLES[5])
    assert await _pass(clock, factory).run_once() == 0
    assert await _open(factory) == []


async def test_a_disabled_profile_runs_nothing() -> None:
    clock, factory = await _stack()
    for number, title in enumerate(LISBON_TITLES[:4]):
        await _session(factory, number, title)
    assert await _pass(clock, factory, enabled=False).run_once() == 0
    assert await _events(factory) == []
