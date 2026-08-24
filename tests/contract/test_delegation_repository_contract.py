"""Shared delegation-ledger repository contract."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from agent_core.adapters.persistence.delegations import InMemoryDelegationRepository
from agent_core.domain.agents import Principal
from agent_core.domain.delegations import (
    ChildOutcome,
    Delegation,
    DelegationBrief,
    DelegationChild,
    DelegationRequest,
    DelegationResult,
    DelegationStatus,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.runs import RunLimits, RunStatus, RunUsage
from agent_core.ports.delegations import DelegationRepository
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal

DELEGATION_ID = UUID("00000000-0000-0000-0000-000000000150")
SIBLING_DELEGATION_ID = UUID("00000000-0000-0000-0000-000000000151")
INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000152")
SIBLING_INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000153")
CHILD_RUN_ID = UUID("00000000-0000-0000-0000-000000000154")
CHILD_SESSION_ID = UUID("00000000-0000-0000-0000-000000000155")


def delegation(**updates: object) -> Delegation:
    values: dict[str, object] = {
        "id": DELEGATION_ID,
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "parent_run_id": RUN_ID,
        "parent_session_id": SESSION_ID,
        "invocation_id": INVOCATION_ID,
        "depth": 0,
        "request": DelegationRequest(
            briefs=[
                DelegationBrief(
                    objective="Find the three most-cited retrieval papers.",
                    success_condition="Three citations with relevance notes.",
                    allowed_tools=["web.search"],
                )
            ]
        ),
        "derived_limits": [
            RunLimits(max_cost=Decimal("1.50"), deadline_at=NOW),
        ],
        "granted_scopes": [frozenset({"web.read"})],
        "status": DelegationStatus.PENDING,
        "children": [
            DelegationChild(
                index=0,
                child_run_id=CHILD_RUN_ID,
                child_session_id=CHILD_SESSION_ID,
            )
        ],
        "created_at": NOW,
    }
    values.update(updates)
    return Delegation.model_validate(values)


async def assert_create_is_unique_by_invocation_and_reads_are_owner_scoped(
    repository: DelegationRepository,
) -> None:
    created = await repository.create(delegation())

    assert created == delegation()
    assert await repository.get(DELEGATION_ID, principal()) == created
    assert await repository.get_by_invocation(INVOCATION_ID) == created
    assert await repository.get_by_invocation(SIBLING_INVOCATION_ID) is None
    assert await repository.get_for_parent_run(RUN_ID) == [created]

    with pytest.raises(ConflictError):
        await repository.create(delegation(id=SIBLING_DELEGATION_ID))
    with pytest.raises(ConflictError):
        await repository.create(delegation(invocation_id=SIBLING_INVOCATION_ID))

    stranger = Principal(
        tenant_id="tenant-b",
        principal_id=principal().principal_id,
        roles=set(),
        scopes=set(),
    )
    with pytest.raises(NotFoundError):
        await repository.get(DELEGATION_ID, stranger)
    neighbour = Principal(
        tenant_id=principal().tenant_id,
        principal_id="principal-b",
        roles=set(),
        scopes=set(),
    )
    with pytest.raises(NotFoundError):
        await repository.get(DELEGATION_ID, neighbour)
    with pytest.raises(NotFoundError):
        await repository.get(SIBLING_DELEGATION_ID, principal())


async def assert_live_children_are_counted_until_terminal(
    repository: DelegationRepository,
) -> None:
    created = await repository.create(delegation())

    assert await repository.live_children_for_parent(RUN_ID) == 1
    assert await repository.live_children_for_tenant(principal().tenant_id) == 1
    assert await repository.live_children_for_parent(CHILD_RUN_ID) == 0
    assert await repository.live_children_for_tenant("tenant-b") == 0

    running = await repository.transition(
        DELEGATION_ID,
        DelegationStatus.PENDING,
        created.model_copy(update={"status": DelegationStatus.RUNNING}),
    )
    assert running.status is DelegationStatus.RUNNING
    assert await repository.live_children_for_parent(RUN_ID) == 1

    terminal_child = running.children[0].model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "summary": "Three citations found.",
            "usage": RunUsage(cost=Decimal("0.75")),
        }
    )
    joined = await repository.transition(
        DELEGATION_ID,
        DelegationStatus.RUNNING,
        running.model_copy(
            update={
                "status": DelegationStatus.JOINED,
                "children": [terminal_child],
                "result": DelegationResult(
                    delegation_id=DELEGATION_ID,
                    children=[
                        ChildOutcome(
                            child_run_id=CHILD_RUN_ID,
                            child_session_id=CHILD_SESSION_ID,
                            status=RunStatus.COMPLETED,
                            summary="Three citations found.",
                            usage=RunUsage(cost=Decimal("0.75")),
                        )
                    ],
                ),
                "joined_at": NOW,
            }
        ),
    )
    assert joined.status is DelegationStatus.JOINED
    assert await repository.live_children_for_parent(RUN_ID) == 0
    assert await repository.live_children_for_tenant(principal().tenant_id) == 0

    with pytest.raises(ConflictError):
        await repository.transition(
            DELEGATION_ID,
            DelegationStatus.RUNNING,
            joined,
        )
    with pytest.raises(NotFoundError):
        await repository.transition(
            SIBLING_DELEGATION_ID,
            DelegationStatus.PENDING,
            joined.model_copy(update={"id": SIBLING_DELEGATION_ID}),
        )


async def test_in_memory_delegation_repository_satisfies_contract() -> None:
    await assert_create_is_unique_by_invocation_and_reads_are_owner_scoped(
        InMemoryDelegationRepository()
    )
    await assert_live_children_are_counted_until_terminal(InMemoryDelegationRepository())
