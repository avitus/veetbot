"""Ended email work consumes its admitted budget once, without permanent holds."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.email import save_value
from agent_core.bootstrap import Composition
from agent_core.domain.email import EmailBudgetLimits, EmailTask
from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_experience_m26 import _terminal_email_attempts, email_client

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


async def seed_uncertain(
    app: Composition,
    *,
    age: int = 1,
    status: RunStatus | None = RunStatus.FAILED,
    cost: Decimal = Decimal("0"),
) -> EmailTask:
    service = app.services.email
    service.clock = FixedClock(NOW)
    run_id = await _terminal_email_attempts(
        app,
        [("model.response.failed", {"error_class": "ModelTransientError"}, cost)],
        status=status or RunStatus.FAILED,
    )
    task = await service.get_task(app.principal, run_id)
    assert task is not None
    task = task.model_copy(update={"created_at": NOW - timedelta(days=age)})
    async with app.uow_factory() as uow:
        if status is None:
            await uow.email.delete(app.principal, "task", str(run_id), expected_revision=1)
            task = task.model_copy(update={"run_id": uuid4()})
        await save_value(uow.email, app.principal, "task", str(task.run_id), task, NOW)
    return task


@pytest.mark.parametrize(
    "status", [RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.COMPLETED, None]
)
async def test_admission_recovers_ended_unknown_holds_in_original_budget_period(
    status: RunStatus | None,
) -> None:
    async with email_client() as (app, _):
        task = await seed_uncertain(app, status=status)
        service = app.services.email
        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("1"), monthly_cost=Decimal("2")
        )
        operation = await service.submit_task(app.principal, kind="refresh")
        assert operation.run_id != task.run_id
        recovered = await service.get_task(app.principal, task.run_id)
        assert recovered is not None
        assert recovered.settled_cost is None
        assert recovered.reservation == 1
        assert recovered.budget_charge == 1
        assert recovered.budget_charge_reason == (
            "missing_run" if status is None else "incomplete_usage"
        )
        # Reconciliation is idempotent, including for a deleted run.
        await service.settle(app.principal, task.run_id)
        assert await service.get_task(app.principal, task.run_id) == recovered
        async with app.uow_factory() as uow:
            with pytest.raises(BudgetExceededError) as caught:
                await service._check_budget(uow.email, app.principal, Decimal("1"))
        assert Decimal(str(caught.value.details["daily_spent"])) == 0
        assert Decimal(str(caught.value.details["daily_reserved"])) == 1
        assert Decimal(str(caught.value.details["monthly_spent"])) == 1


@pytest.mark.parametrize("age", [0, 1, 30, 31])
@pytest.mark.parametrize("cost", [Decimal("0"), Decimal("2.5")])
async def test_conservative_charge_honors_daily_monthly_and_expired_windows(
    age: int, cost: Decimal
) -> None:
    async with email_client() as (app, _):
        task = await seed_uncertain(app, age=age, cost=cost)
        service = app.services.email
        await service.settle(app.principal, task.run_id)
        recovered = await service.get_task(app.principal, task.run_id)
        assert recovered is not None
        charge = max(Decimal("1"), cost)
        assert recovered.budget_charge == charge
        assert recovered.settled_cost is None
        service.budget_limits = EmailBudgetLimits(daily_cost=charge, monthly_cost=charge)
        async with app.uow_factory() as uow:
            if age > 30:
                await service._check_budget(uow.email, app.principal, Decimal("1"))
            else:
                with pytest.raises(BudgetExceededError) as caught:
                    await service._check_budget(uow.email, app.principal, Decimal("1"))
                assert Decimal(str(caught.value.details["daily_spent"])) == (
                    charge if age == 0 else 0
                )
                assert Decimal(str(caught.value.details["monthly_spent"])) == charge
                assert Decimal(str(caught.value.details["monthly_estimated"])) == charge
                assert Decimal(str(caught.value.details["daily_estimated"])) == (
                    charge if age == 0 else 0
                )
                assert Decimal(str(caught.value.details["daily_reserved"])) == 0
                assert datetime.fromisoformat(
                    str(caught.value.details["retry_at"])
                ) == task.created_at + timedelta(days=30, microseconds=1)


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_FOR_USER,
        RunStatus.WAITING_FOR_APPROVAL,
    ],
)
async def test_live_unknown_runs_keep_full_hold_even_outside_rolling_window(
    status: RunStatus,
) -> None:
    async with email_client() as (app, _):
        task = await seed_uncertain(app, age=40, status=status)
        service = app.services.email
        await service.settle(app.principal, task.run_id)
        recovered = await service.get_task(app.principal, task.run_id)
        assert recovered is not None
        assert recovered.budget_charge is None
        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("1"), monthly_cost=Decimal("1")
        )
        async with app.uow_factory() as uow:
            with pytest.raises(BudgetExceededError) as caught:
                await service._check_budget(uow.email, app.principal, Decimal("1"))
        assert Decimal(str(caught.value.details["daily_reserved"])) == 1
        assert Decimal(str(caught.value.details["monthly_spent"])) == 0


async def test_later_complete_usage_replaces_conservative_charge_once() -> None:
    async with email_client() as (app, _):
        task = await seed_uncertain(app, age=0, cost=Decimal("0.25"))
        service = app.services.email
        await service.settle(app.principal, task.run_id)
        recovered = await service.get_task(app.principal, task.run_id)
        assert recovered is not None and recovered.budget_charge == 1
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(
                task.session_id, 0, app.principal, run_id=task.run_id
            )
            started = next(event for event in events if event.event_type == "model.request.started")
            await uow.events.append(
                NewEvent(
                    session_id=task.session_id,
                    run_id=task.run_id,
                    event_type="model.response.completed",
                    actor_type="runtime",
                    payload={"attempt_id": started.payload["attempt_id"]},
                )
            )
        await service.settle(app.principal, task.run_id)
        settled = await service.get_task(app.principal, task.run_id)
        assert settled is not None
        assert settled.settled_cost == Decimal("0.25")
        # Preserve the conservative accounting provenance, but count actual usage instead.
        assert settled.budget_charge == 1
        await service.settle(app.principal, task.run_id)
        assert await service.get_task(app.principal, task.run_id) == settled
        service.budget_limits = EmailBudgetLimits(
            daily_cost=Decimal("1.25"), monthly_cost=Decimal("1.25")
        )
        async with app.uow_factory() as uow:
            await service._check_budget(uow.email, app.principal, Decimal("1"))


async def test_missing_run_preserves_a_higher_previously_recorded_charge() -> None:
    async with email_client() as (app, _):
        task = await seed_uncertain(app, cost=Decimal("2.5"))
        service = app.services.email
        await service.settle(app.principal, task.run_id)
        charged = await service.get_task(app.principal, task.run_id)
        assert charged is not None
        missing = charged.model_copy(update={"run_id": uuid4()})
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "task", str(task.run_id))
            assert row is not None
            await uow.email.delete(app.principal, "task", row.key, expected_revision=row.revision)
            await save_value(uow.email, app.principal, "task", str(missing.run_id), missing, NOW)
        await service.settle(app.principal, missing.run_id)
        recovered = await service.get_task(app.principal, missing.run_id)
        assert recovered is not None
        assert recovered.budget_charge == Decimal("2.5")
        assert recovered.budget_charge_reason == "missing_run"
        assert recovered.settled_cost is None
