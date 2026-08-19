"""Shared contract for firing-time schedule admission."""

from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.domain.schedules import ScheduleAdmissionOutcome
from agent_core.ports.schedules import ScheduleAdmissionController
from tests.contract.support import NOW, TENANT
from tests.contract.test_schedule_repository_contract import revision


async def assert_schedule_admission_controller_returns_a_stable_decision(
    controller: ScheduleAdmissionController,
) -> None:
    decision = await controller.check(TENANT, revision(), NOW)
    assert decision.outcome is ScheduleAdmissionOutcome.ALLOW
    assert decision.reason_code is None


async def test_allow_schedule_admission_controller_satisfies_contract() -> None:
    await assert_schedule_admission_controller_returns_a_stable_decision(
        AllowScheduleAdmissionController()
    )
