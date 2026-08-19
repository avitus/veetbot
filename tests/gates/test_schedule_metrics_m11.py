"""Milestone 11 schedule metric vocabulary and content-safety gate."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agent_core.domain.schedules import OccurrenceDisposition
from agent_core.observability.schedules import ScheduleMetrics


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
    assert all("tenant-private" not in item.values() for item in attributes)
