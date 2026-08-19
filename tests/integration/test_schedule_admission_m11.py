"""PostgreSQL schedule-admission limits and reservations."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.adapters.schedule_admission import PostgresScheduleAdmissionController
from agent_core.bootstrap import build
from agent_core.domain.schedules import (
    ScheduleAdmissionLimits,
    ScheduleAdmissionOutcome,
)
from tests.contract.test_schedule_repository_contract import revision
from tests.integration.m2_support import database_settings
from tests.integration.test_schedule_materializer_m11 import (
    _create_due_schedule,
    _materializer,
)

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)


async def test_postgres_schedule_admission_allows_an_empty_tenant() -> None:
    limits = ScheduleAdmissionLimits(
        max_active_runs_per_tenant=1,
        max_materializations_per_minute=1,
        daily_cost=Decimal("2"),
        monthly_cost=Decimal("4"),
    )
    async with (
        build(settings=database_settings(), storage="postgres", fixed_clock_at=NOW) as composition,
        composition.uow_factory() as uow,
    ):
        session = cast(PostgresUnitOfWork, uow)._session
        assert session is not None
        controller = PostgresScheduleAdmissionController(session, limits)
        decision = await controller.check(
            composition.principal.tenant_id,
            revision().model_copy(
                update={
                    "created_by_principal_id": composition.principal.principal_id,
                }
            ),
            NOW,
        )
        assert decision.outcome is ScheduleAdmissionOutcome.ALLOW


async def test_postgres_schedule_admission_enforces_concurrency_rate_and_reservations() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        schedule_id = uuid4()
        await _create_due_schedule(composition, schedule_id)
        occurrence = await _materializer(composition).materialize(schedule_id)
        assert occurrence is not None

        candidate = revision().model_copy(
            update={"created_by_principal_id": composition.principal.principal_id}
        )
        cases = (
            (
                ScheduleAdmissionLimits(
                    max_active_runs_per_tenant=1,
                    max_materializations_per_minute=10,
                    daily_cost=Decimal("10"),
                    monthly_cost=Decimal("10"),
                ),
                ScheduleAdmissionOutcome.RETRY,
                "schedule.concurrency_limit",
            ),
            (
                ScheduleAdmissionLimits(
                    max_active_runs_per_tenant=10,
                    max_materializations_per_minute=1,
                    daily_cost=Decimal("10"),
                    monthly_cost=Decimal("10"),
                ),
                ScheduleAdmissionOutcome.REJECT,
                "schedule.rate_limit",
            ),
            (
                ScheduleAdmissionLimits(
                    max_active_runs_per_tenant=10,
                    max_materializations_per_minute=10,
                    daily_cost=Decimal("1.5"),
                    monthly_cost=Decimal("10"),
                ),
                ScheduleAdmissionOutcome.REJECT,
                "schedule.daily_cost_limit",
            ),
            (
                ScheduleAdmissionLimits(
                    max_active_runs_per_tenant=10,
                    max_materializations_per_minute=10,
                    daily_cost=Decimal("10"),
                    monthly_cost=Decimal("1.5"),
                ),
                ScheduleAdmissionOutcome.REJECT,
                "schedule.monthly_cost_limit",
            ),
        )
        for limits, outcome, reason in cases:
            async with composition.uow_factory() as uow:
                session = cast(PostgresUnitOfWork, uow)._session
                assert session is not None
                decision = await PostgresScheduleAdmissionController(session, limits).check(
                    composition.principal.tenant_id, candidate, NOW
                )
                assert decision.outcome is outcome
                assert decision.reason_code == reason


async def test_concurrent_tenant_reservations_are_serialized(tmp_path: Path) -> None:
    runtime_overlay = tmp_path / "runtime" / "limits.yaml"
    runtime_overlay.parent.mkdir(parents=True)
    runtime_overlay.write_text(
        "scheduling:\n  max_active_runs_per_tenant: 1\n",
        encoding="utf-8",
    )
    settings = replace(database_settings(), config_dir=tmp_path)
    async with build(settings=settings, storage="postgres", fixed_clock_at=NOW) as composition:
        first_id, second_id = uuid4(), uuid4()
        await _create_due_schedule(composition, first_id)
        await _create_due_schedule(composition, second_id)

        first, second = await asyncio.gather(
            _materializer(composition).materialize(first_id),
            _materializer(composition).materialize(second_id),
        )

        assert sum(value is not None for value in (first, second)) == 1
        async with composition.uow_factory() as uow:
            occurrences = await uow.schedule_occurrences.list(
                first_id, composition.principal, limit=10
            ) + await uow.schedule_occurrences.list(second_id, composition.principal, limit=10)
            assert len(occurrences) == 1
            due = await uow.schedules.due(NOW, limit=10)
            assert (first_id in due) != (second_id in due)
