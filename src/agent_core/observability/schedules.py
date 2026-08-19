"""Content-free OpenTelemetry metrics for scheduled task management."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from agent_core.domain.schedules import OccurrenceDisposition


class ScheduleMetrics:
    def __init__(self, meter: Meter | None = None) -> None:
        active_meter = meter or metrics.get_meter("agent_core.scheduling")
        self._scan_duration = active_meter.create_histogram(
            "agent.schedule.scan.duration", unit="s"
        )
        self._scan_due = active_meter.create_histogram("agent.schedule.scan.due", unit="{schedule}")
        self._occurrence_lag = active_meter.create_histogram(
            "agent.schedule.occurrence.lag", unit="s"
        )
        self._occurrences = active_meter.create_counter(
            "agent.schedule.occurrence.count", unit="{occurrence}"
        )
        self._misfires = active_meter.create_counter(
            "agent.schedule.misfires.coalesced", unit="{occurrence}"
        )
        self._outage = active_meter.create_histogram("agent.schedule.misfires.outage", unit="s")
        self._active = active_meter.create_histogram(
            "agent.schedule.admission.active_runs", unit="{run}"
        )
        self._day_ratio = active_meter.create_histogram("agent.schedule.cost.day_ratio", unit="1")
        self._month_ratio = active_meter.create_histogram(
            "agent.schedule.cost.month_ratio", unit="1"
        )
        self._auto_paused = active_meter.create_counter(
            "agent.schedule.auto_paused", unit="{schedule}"
        )
        self._run_duration = active_meter.create_histogram("agent.schedule.run.duration", unit="s")
        self._run_outcome = active_meter.create_counter("agent.schedule.run.outcome", unit="{run}")
        self._run_cost = active_meter.create_histogram("agent.schedule.run.cost", unit="USD")
        self._lease_reclaims = active_meter.create_counter(
            "agent.schedule.run.lease_reclaims", unit="{reclaim}"
        )
        self._cancellation = active_meter.create_histogram(
            "agent.schedule.run.cancellation_latency", unit="s"
        )
        self._claim = active_meter.create_histogram("agent.queue.claim.duration", unit="s")

    def record_scan(self, *, due_count: int, duration_seconds: float) -> None:
        self._scan_due.record(due_count)
        self._scan_duration.record(max(0.0, duration_seconds))

    def record_occurrence(
        self,
        *,
        disposition: OccurrenceDisposition,
        nominal_fire_at: datetime,
        observed_at: datetime,
    ) -> None:
        self._occurrences.add(1, {"disposition": disposition.value})
        if disposition is OccurrenceDisposition.MATERIALIZED:
            self._occurrence_lag.record(max(0.0, (observed_at - nominal_fire_at).total_seconds()))

    def record_misfires(self, *, count: int, outage_seconds: float) -> None:
        self._misfires.add(max(0, count))
        self._outage.record(max(0.0, outage_seconds))

    def record_admission(
        self,
        *,
        tenant_id: str,
        active_runs: int,
        day_cost: Decimal,
        month_cost: Decimal,
        day_ceiling: Decimal,
        month_ceiling: Decimal,
    ) -> None:
        attributes = {"tenant_hash": _tenant_hash(tenant_id)}
        self._active.record(active_runs, attributes)
        self._day_ratio.record(float(day_cost / day_ceiling), attributes)
        self._month_ratio.record(float(month_cost / month_ceiling), attributes)

    def record_terminal(
        self,
        *,
        tenant_id: str,
        status: str,
        duration_seconds: float,
        cost: Decimal,
        cancellation_seconds: float | None,
        lease_reclaims: int,
    ) -> None:
        attributes = {"tenant_hash": _tenant_hash(tenant_id)}
        self._run_duration.record(max(0.0, duration_seconds), attributes)
        self._run_outcome.add(1, {**attributes, "status": status})
        self._run_cost.record(float(cost), attributes)
        if lease_reclaims > 0:
            self._lease_reclaims.add(lease_reclaims, attributes)
        if cancellation_seconds is not None:
            self._cancellation.record(max(0.0, cancellation_seconds), attributes)

    def record_auto_pause(self) -> None:
        self._auto_paused.add(1)

    def record_claim(self, *, worker_class: str, duration_seconds: float) -> None:
        if worker_class not in {"interactive", "async"}:
            worker_class = "other"
        self._claim.record(max(0.0, duration_seconds), {"worker_class": worker_class})


def _tenant_hash(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode()).hexdigest()[:16]
