"""PostgreSQL contracts for Milestone 11 scheduling persistence."""

from __future__ import annotations

from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    Schedule,
    ScheduleIdempotencyRecord,
    ScheduleOccurrence,
    ScheduleRevision,
)
from tests.contract.support import NOW, agent, run, session
from tests.contract.test_schedule_idempotency_repository_contract import (
    assert_schedule_idempotency_replays_exact_requests_and_rejects_reuse,
)
from tests.contract.test_schedule_occurrence_repository_contract import (
    assert_occurrence_insert_is_idempotent_by_schedule_and_nominal_instant,
)
from tests.contract.test_schedule_repository_contract import (
    assert_schedule_repository_is_principal_isolated_and_revisioned,
    assert_schedule_repository_lists_and_finds_due_definitions_deterministically,
    assert_schedule_repository_mutates_state_and_revisions_with_cas,
    revision,
    schedule,
)
from tests.integration.m2_support import database_settings


class _RollbackContractError(Exception):
    pass


def _owned_schedule(principal: Principal, schedule_id: UUID) -> Schedule:
    return schedule(schedule_id=schedule_id).model_copy(
        update={
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
        }
    )


def _owned_revision(principal: Principal, schedule_id: UUID) -> ScheduleRevision:
    return revision(schedule_id).model_copy(
        update={"created_by_principal_id": principal.principal_id}
    )


async def test_postgres_schedule_adapters_satisfy_shared_contracts() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        for contract in (
            assert_schedule_repository_is_principal_isolated_and_revisioned,
            assert_schedule_repository_lists_and_finds_due_definitions_deterministically,
            assert_schedule_repository_mutates_state_and_revisions_with_cas,
        ):
            with pytest.raises(_RollbackContractError):
                async with composition.uow_factory() as uow:
                    database_session = cast(PostgresUnitOfWork, uow)._session
                    assert database_session is not None
                    await database_session.execute(
                        text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
                    )
                    await contract(uow.schedules)
                    raise _RollbackContractError

        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                database_session = cast(PostgresUnitOfWork, uow)._session
                assert database_session is not None
                await database_session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
                )
                await uow.agents.put(agent())
                await uow.sessions.create(session())
                await uow.runs.create(run())
                await uow.schedules.create(schedule(), revision())
                await assert_occurrence_insert_is_idempotent_by_schedule_and_nominal_instant(
                    uow.schedule_occurrences
                )
                await assert_schedule_idempotency_replays_exact_requests_and_rejects_reuse(
                    uow.schedule_idempotency
                )
                raise _RollbackContractError


async def test_schedule_repositories_commit_and_roll_back_as_one_unit() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        principal = composition.principal
        schedule_id = uuid4()
        occurrence = ScheduleOccurrence(
            id=uuid4(),
            schedule_id=schedule_id,
            schedule_revision=1,
            nominal_fire_at=NOW,
            disposition=OccurrenceDisposition.MISSED,
            reason_code="schedule.grace_expired",
            created_at=NOW,
        )
        request = ScheduleIdempotencyRecord(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            key=f"schedule-{schedule_id}",
            request_hash="a" * 64,
            schedule_id=schedule_id,
            created_at=NOW,
        )

        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                await uow.schedules.create(
                    _owned_schedule(principal, schedule_id),
                    _owned_revision(principal, schedule_id),
                )
                await uow.schedule_occurrences.insert(occurrence)
                await uow.schedule_idempotency.create(request)
                raise _RollbackContractError

        async with composition.uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.schedules.get(schedule_id, principal)
            await uow.schedules.create(
                _owned_schedule(principal, schedule_id),
                _owned_revision(principal, schedule_id),
            )
            await uow.schedule_occurrences.insert(occurrence)
            await uow.schedule_idempotency.create(request)

        async with composition.uow_factory() as uow:
            assert await uow.schedules.get(schedule_id, principal) == _owned_schedule(
                principal, schedule_id
            )
            assert await uow.schedule_occurrences.list(schedule_id, principal, limit=10) == [
                occurrence
            ]
            assert (
                await uow.schedule_idempotency.get(
                    principal.tenant_id, principal.principal_id, request.key
                )
                == request
            )


async def test_session_erasure_marks_and_unlinks_schedule_occurrence() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        principal = composition.principal
        schedule_id, session_id, run_id, occurrence_id = (uuid4() for _ in range(4))
        erased_at = NOW + timedelta(days=1)

        async with composition.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(
                session().model_copy(
                    update={
                        "id": session_id,
                        "tenant_id": principal.tenant_id,
                        "principal_id": principal.principal_id,
                    }
                )
            )
            await uow.runs.create(
                run(status=RunStatus.CANCELLED).model_copy(
                    update={
                        "id": run_id,
                        "session_id": session_id,
                        "tenant_id": principal.tenant_id,
                    }
                )
            )
            await uow.schedules.create(
                _owned_schedule(principal, schedule_id),
                _owned_revision(principal, schedule_id),
            )
            await uow.schedule_occurrences.insert(
                ScheduleOccurrence(
                    id=occurrence_id,
                    schedule_id=schedule_id,
                    schedule_revision=1,
                    nominal_fire_at=NOW,
                    disposition=OccurrenceDisposition.MATERIALIZED,
                    session_id=session_id,
                    run_id=run_id,
                    authority_version="authority-1",
                    materialized_at=NOW,
                    created_at=NOW,
                )
            )

        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(session_id, principal, erased_at)

        async with composition.uow_factory() as uow:
            [occurrence] = await uow.schedule_occurrences.list(schedule_id, principal, limit=10)
            assert occurrence.session_id is None
            assert occurrence.run_id is None
            assert occurrence.links_erased_at == erased_at
            assert occurrence.disposition is OccurrenceDisposition.MATERIALIZED


async def test_schedule_rows_are_principal_isolated() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        principal = composition.principal
        schedule_id = uuid4()
        async with composition.uow_factory() as uow:
            await uow.schedules.create(
                _owned_schedule(principal, schedule_id),
                _owned_revision(principal, schedule_id),
            )
            stranger = Principal(
                tenant_id=principal.tenant_id,
                principal_id="another-principal",
                roles={"user"},
                scopes=set(),
            )
            foreign = stranger.model_copy(update={"tenant_id": "another-tenant"})
            with pytest.raises(NotFoundError):
                await uow.schedules.get(schedule_id, stranger)
            with pytest.raises(NotFoundError):
                await uow.schedules.get(schedule_id, foreign)

            database_session = cast(PostgresUnitOfWork, uow)._session
            assert database_session is not None
            rows = (
                await database_session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname = ANY(:tables)"
                    ),
                    {
                        "tables": [
                            "schedules",
                            "schedule_revisions",
                            "schedule_occurrences",
                            "schedule_idempotency_keys",
                        ]
                    },
                )
            ).all()
            assert {row.relname for row in rows} == {
                "schedules",
                "schedule_revisions",
                "schedule_occurrences",
                "schedule_idempotency_keys",
            }
            assert all(row.relrowsecurity and row.relforcerowsecurity for row in rows)
