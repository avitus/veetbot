"""Credential-exclusion corpus and schedule-state structural gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.application.schedule_service import ScheduleService
from agent_core.bootstrap import build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import ScheduleValidationError
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    DailyCadence,
    Schedule,
    ScheduleDefinition,
    ScheduleDefinitionLimits,
    ScheduleIdempotencyRecord,
    ScheduleOccurrence,
    ScheduleRevision,
)
from tests.integration.m2_support import memory_settings

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests/corpora/schedule_credentials"
NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000000811")


def _principal() -> Principal:
    return Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"schedule.read", "schedule.write", "schedule.cancel"},
    )


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Credential rejection agent",
        instructions="Perform the scheduled task.",
        model_policy="fake",
        enabled_tools=[],
        policy_profile="default",
        limits=RunLimits(),
    )


def _definition(instruction: str, *, title: str = "Credential rejection") -> ScheduleDefinition:
    return ScheduleDefinition(
        title=title,
        instruction=instruction,
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=DailyCadence(local_time=time(9), timezone="UTC"),
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
    )


def _limits() -> ScheduleDefinitionLimits:
    return ScheduleDefinitionLimits(
        max_run_timeout_seconds=60,
        max_misfire_grace_seconds=60,
        max_steps_per_run=4,
        max_model_calls_per_run=4,
        max_tool_calls_per_run=4,
        max_cost_per_run=Decimal("1"),
    )


async def test_credential_shaped_instructions_are_rejected_without_storage_or_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    members = sorted(CORPUS.glob("*.json"))
    assert len(members) >= 12
    principal = _principal()
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        service = ScheduleService(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
            limits=_limits(),
        )
        rejected: list[str] = []
        for index, member in enumerate(members):
            raw = json.loads(member.read_text(encoding="utf-8"))
            instruction = "".join(raw["parts"])
            with pytest.raises(ScheduleValidationError) as exc:
                await service.create(principal, _definition(instruction), f"credential-{index}")
            assert exc.value.reason == "schedule.instruction_contains_credential"
            rejected.append(instruction)

        async with composition.uow_factory() as uow:
            assert await uow.schedules.list(principal, limit=100) == []
            assert not [
                event
                for event in await uow.process_events.list()
                if event.event_type.startswith("schedule.")
            ]
        captured = capsys.readouterr()
        emitted = captured.out + captured.err
        assert all(value not in emitted for value in rejected)


async def test_credential_shaped_title_is_rejected_before_persistence() -> None:
    principal = _principal()
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        service = ScheduleService(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
            limits=_limits(),
        )

        with pytest.raises(ScheduleValidationError) as exc:
            await service.create(
                principal,
                _definition(
                    "Summarize project changes.",
                    title="Authorization: " + "Bearer secret-value",
                ),
                "credential-title",
            )

        assert exc.value.reason == "schedule.title_contains_credential"
        async with composition.uow_factory() as uow:
            assert await uow.schedules.list(principal, limit=100) == []


def test_schedule_domain_and_persistence_models_have_no_credential_fields() -> None:
    forbidden = {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
    models = (
        Schedule,
        ScheduleDefinition,
        ScheduleIdempotencyRecord,
        ScheduleOccurrence,
        ScheduleRevision,
    )
    assert all(forbidden.isdisjoint(model.model_fields) for model in models)

    from agent_core.adapters.persistence.sqlalchemy_models import (
        ScheduleIdempotencyKeyRow,
        ScheduleOccurrenceRow,
        ScheduleRevisionRow,
        ScheduleRow,
    )

    rows = (ScheduleRow, ScheduleRevisionRow, ScheduleOccurrenceRow, ScheduleIdempotencyKeyRow)
    assert all(forbidden.isdisjoint(row.__table__.columns.keys()) for row in rows)
