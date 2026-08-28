"""PostgreSQL contracts for Milestone 13 delegation persistence."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.delegations import Delegation, DelegationChild
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import RiskLevel, SideEffectClass
from agent_core.domain.runs import RunKind, RunStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from tests.contract.support import NOW, agent, run, session
from tests.contract.test_delegation_repository_contract import (
    INVOCATION_ID,
    SIBLING_INVOCATION_ID,
    assert_create_is_unique_by_invocation_and_reads_are_owner_scoped,
    assert_live_children_are_counted_until_terminal,
    delegation,
)
from tests.integration.m2_support import database_settings


class _RollbackContractError(Exception):
    pass


class _DelegationGraph:
    """One committed parent-child delegation graph with fresh identifiers."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.parent_session_id = uuid4()
        self.parent_run_id = uuid4()
        self.invocation_id = uuid4()
        self.delegation_id = uuid4()
        self.child_session_id = uuid4()
        self.child_run_id = uuid4()

    def ledger(self) -> Delegation:
        return delegation(
            id=self.delegation_id,
            tenant_id=self.principal.tenant_id,
            principal_id=self.principal.principal_id,
            parent_run_id=self.parent_run_id,
            parent_session_id=self.parent_session_id,
            invocation_id=self.invocation_id,
            children=[
                DelegationChild(
                    index=0,
                    child_run_id=self.child_run_id,
                    child_session_id=self.child_session_id,
                )
            ],
        )

    def _session(self, session_id: UUID, metadata: dict[str, Any]) -> Any:
        return session().model_copy(
            update={
                "id": session_id,
                "tenant_id": self.principal.tenant_id,
                "principal_id": self.principal.principal_id,
                "metadata": metadata,
            }
        )

    def _run(self, run_id: UUID, session_id: UUID, **updates: Any) -> Any:
        return run(status=RunStatus.COMPLETED).model_copy(
            update={
                "id": run_id,
                "session_id": session_id,
                "tenant_id": self.principal.tenant_id,
                **updates,
            }
        )

    async def create(self, uow: Any) -> None:
        await uow.agents.put(agent())
        await uow.sessions.create(self._session(self.parent_session_id, {}))
        await uow.runs.create(self._run(self.parent_run_id, self.parent_session_id))
        await uow.invocations.create(
            _invocation(self.invocation_id, self.parent_run_id, self.parent_session_id)
        )
        await uow.sessions.create(
            self._session(
                self.child_session_id,
                {
                    "run_kind": "delegated",
                    "parent_run_id": str(self.parent_run_id),
                    "parent_session_id": str(self.parent_session_id),
                    "delegation_id": str(self.delegation_id),
                },
            )
        )
        await uow.runs.create(
            self._run(
                self.child_run_id,
                self.child_session_id,
                parent_run_id=self.parent_run_id,
                kind=RunKind.DELEGATED,
            )
        )
        await uow.delegations.create(self.ledger())


def _invocation(invocation_id: UUID, run_id: UUID, session_id: UUID) -> ToolInvocation:
    return ToolInvocation(
        id=invocation_id,
        run_id=run_id,
        session_id=session_id,
        step_number=1,
        call_id=f"delegate-call-{invocation_id}",
        tool_name="delegate.run",
        tool_version="1.0.0",
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=ToolInvocationStatus.RUNNING,
        raw_arguments="{}",
        idempotency_key=f"delegate-key-{invocation_id}",
        created_at=NOW,
        updated_at=NOW,
    )


async def test_postgres_delegation_repository_satisfies_shared_contracts() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        for contract in (
            assert_create_is_unique_by_invocation_and_reads_are_owner_scoped,
            assert_live_children_are_counted_until_terminal,
        ):
            with pytest.raises(_RollbackContractError):
                async with composition.uow_factory() as uow:
                    assert isinstance(uow, PostgresUnitOfWork)
                    await uow.session.execute(
                        text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
                    )
                    await uow.agents.put(agent())
                    await uow.sessions.create(session())
                    parent_run = run(status=RunStatus.RUNNING)
                    await uow.runs.create(parent_run)
                    await uow.invocations.create(
                        _invocation(INVOCATION_ID, parent_run.id, parent_run.session_id)
                    )
                    await uow.invocations.create(
                        _invocation(SIBLING_INVOCATION_ID, parent_run.id, parent_run.session_id)
                    )
                    await contract(uow.delegations)
                    raise _RollbackContractError


async def test_postgres_child_session_erasure_stamps_the_ledger() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        graph = _DelegationGraph(composition.principal)
        erased_at = NOW + timedelta(days=1)

        async with composition.uow_factory() as uow:
            await graph.create(uow)

        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(
                graph.child_session_id, graph.principal, erased_at
            )

        async with composition.uow_factory() as uow:
            stamped = await uow.delegations.get(graph.delegation_id, graph.principal)
            assert stamped.links_erased_at == erased_at
            assert stamped.children[0].child_run_id is None
            assert stamped.children[0].child_session_id is None
            with pytest.raises(NotFoundError):
                await uow.sessions.get(graph.child_session_id, graph.principal)
            assert await uow.sessions.get(graph.parent_session_id, graph.principal) is not None


async def test_postgres_parent_session_erasure_deletes_children_and_the_ledger() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        graph = _DelegationGraph(composition.principal)
        erased_at = NOW + timedelta(days=1)

        async with composition.uow_factory() as uow:
            await graph.create(uow)

        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(
                graph.parent_session_id, graph.principal, erased_at
            )

        async with composition.uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.sessions.get(graph.child_session_id, graph.principal)
            with pytest.raises(NotFoundError):
                await uow.sessions.get(graph.parent_session_id, graph.principal)
            assert await uow.delegations.get_by_invocation(graph.invocation_id) is None


async def test_postgres_parent_erasure_survives_a_dangling_child_link() -> None:
    """A ledger link whose child session and tombstone are gone never blocks the parent."""

    from sqlalchemy import delete as sql_delete

    from agent_core.adapters.persistence.sqlalchemy_models import SessionRow
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork

    async with build(settings=database_settings(), storage="postgres") as composition:
        graph = _DelegationGraph(composition.principal)
        erased_at = NOW + timedelta(days=1)

        async with composition.uow_factory() as uow:
            await graph.create(uow)

        # Remove the child session out of band, bypassing the deletion
        # contract, so the ledger still names it but no tombstone exists.
        async with composition.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            await uow.session.execute(
                sql_delete(SessionRow).where(SessionRow.id == graph.child_session_id)
            )

        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(
                graph.parent_session_id, graph.principal, erased_at
            )

        async with composition.uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.sessions.get(graph.parent_session_id, graph.principal)
            assert await uow.delegations.get_by_invocation(graph.invocation_id) is None
