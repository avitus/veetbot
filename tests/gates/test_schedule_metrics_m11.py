"""Milestone 11 schedule metric vocabulary and content-safety gate."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.domain.errors import ConflictError
from agent_core.domain.schedules import OccurrenceDisposition
from agent_core.observability.schedules import ScheduleMetrics
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.contract.support import NOW
from tests.contract.test_schedule_repository_contract import revision, schedule


def test_schedule_metrics_cover_operations_without_instruction_attributes() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ScheduleMetrics(provider.get_meter("schedule-test"))
    now = datetime(2026, 8, 20, 16, tzinfo=UTC)
    metrics.record_scan(due_count=3, duration_seconds=0.25)
    metrics.record_occurrence(
        disposition=OccurrenceDisposition.MATERIALIZED,
        nominal_fire_at=now - timedelta(seconds=2),
        observed_at=now,
    )
    metrics.record_misfires(count=10, outage_seconds=600)
    metrics.record_admission(
        tenant_id="tenant-private",
        active_runs=2,
        day_cost=Decimal("3"),
        month_cost=Decimal("7"),
        day_ceiling=Decimal("25"),
        month_ceiling=Decimal("250"),
    )
    metrics.record_terminal(
        tenant_id="tenant-private",
        status="FAILED",
        duration_seconds=5,
        cost=Decimal("0.2"),
        cancellation_seconds=None,
        lease_reclaims=1,
    )
    metrics.record_auto_pause()
    metrics.record_claim(worker_class="interactive", duration_seconds=0.01)

    data = reader.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert {
        "agent.schedule.scan.duration",
        "agent.schedule.scan.due",
        "agent.schedule.occurrence.lag",
        "agent.schedule.occurrence.count",
        "agent.schedule.misfires.coalesced",
        "agent.schedule.admission.active_runs",
        "agent.schedule.cost.day_ratio",
        "agent.schedule.cost.month_ratio",
        "agent.schedule.run.duration",
        "agent.schedule.run.outcome",
        "agent.schedule.run.cost",
        "agent.schedule.run.lease_reclaims",
        "agent.schedule.auto_paused",
        "agent.queue.claim.duration",
    } <= names
    attributes = [
        dict(point.attributes or {})
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
    ]
    assert all("instruction" not in item for item in attributes)
    assert all("tenant-private" not in str(value) for item in attributes for value in item.values())


async def test_auto_pause_metric_requires_successful_state_persistence() -> None:
    class MetricProbe:
        def __init__(self) -> None:
            self.auto_pauses = 0

        def record_auto_pause(self) -> None:
            self.auto_pauses += 1

        def record_occurrence(self, **_kwargs: object) -> None:
            pass

    metrics = MetricProbe()
    uow = SimpleNamespace(
        schedule_occurrences=SimpleNamespace(insert=AsyncMock()),
        process_events=SimpleNamespace(append=AsyncMock()),
        schedules=SimpleNamespace(advance=AsyncMock(side_effect=ConflictError("schedule changed"))),
    )
    materializer = ScheduleMaterializer(
        uow_factory=cast(Any, object()),
        principals=cast(Any, object()),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([UUID(int=1), UUID(int=2), UUID(int=3)]),
        seed_checkpoint=cast(Any, AsyncMock()),
        metrics=cast(Any, metrics),
    )
    current = schedule().model_copy(update={"consecutive_failures": 2})

    with pytest.raises(ConflictError):
        await materializer._record_failure(
            cast(Any, uow),
            current,
            revision(),
            NOW,
            NOW + timedelta(days=1),
            NOW,
            OccurrenceDisposition.MISSED,
            "schedule.misfire_grace_expired",
        )

    assert metrics.auto_pauses == 0
