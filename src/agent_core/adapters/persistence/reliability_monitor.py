"""Atomic operational alarm episodes over the existing process log and outbox."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_core.adapters.persistence.reliability_report import read_reliability_report
from agent_core.domain.agents import Principal
from agent_core.domain.events import ProcessEvent
from agent_core.domain.notifications import (
    NOTIFICATION_TITLES,
    NewNotification,
    NotificationKind,
    NotificationPayload,
    NotificationSeverity,
)
from agent_core.observability.reliability import (
    ReliabilityReport,
    ReliabilityThresholds,
    degraded_signals,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.events import ProcessEventRepository
from agent_core.ports.notifications import NotificationOutbox

SIGNALS = ("run_failure_rate", "internal_errors", "queue_lag", "stale_leases")


async def check_reliability(
    sessions: async_sessionmaker[AsyncSession],
    hours: int,
    thresholds: ReliabilityThresholds,
    *,
    outbox_factory: Callable[[AsyncSession], NotificationOutbox],
    events_factory: Callable[[AsyncSession], ProcessEventRepository],
    principal: Principal,
    clock: Clock,
    ids: IdFactory,
    release_id: str | None,
) -> ReliabilityReport:
    """Serialize each owner's checks and commit episode state with its notification."""

    if not 1 <= hours <= 2160:
        raise ValueError("Reliability windows must be between one hour and ninety days.")
    scope = hashlib.sha256(
        json.dumps([principal.tenant_id, principal.principal_id]).encode()
    ).hexdigest()
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL statement_timeout = '10s'"))
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": "reliability:" + scope},
        )
        # READ COMMITTED intentionally reads episode state after acquiring the lock.
        now = clock.now()
        report = await read_reliability_report(
            session, now - timedelta(hours=hours), now, principal=principal
        )
        degraded = degraded_signals(report, thresholds)
        outbox = outbox_factory(session)
        events = events_factory(session)
        for signal in SIGNALS:
            previous = (
                await session.execute(
                    text("""
                select payload from process_events
                where event_type='ops.reliability.transition'
                  and payload->>'tenant_id'=:tenant and payload->>'principal_id'=:principal
                  and payload->>'signal'=:signal
                order by (payload->>'sequence')::bigint desc limit 1
            """),
                    {
                        "tenant": principal.tenant_id,
                        "principal": principal.principal_id,
                        "signal": signal,
                    },
                )
            ).scalar_one_or_none()
            active = signal in degraded
            was_active = previous is not None and previous["active"] is True
            if active == was_active:
                continue
            if not active and signal in {"run_failure_rate", "internal_errors"}:
                # A quiet hour is not evidence of recovered task completion.
                if report.completed == 0:
                    continue
                if (
                    signal == "run_failure_rate"
                    and report.completed + report.failed < thresholds.minimum_outcomes
                ):
                    continue
            sequence = 1 if previous is None else int(previous["sequence"]) + 1
            key = f"ops.reliability:{scope}:{signal}:{sequence}"
            kind = NotificationKind.OPS_ALERT if active else NotificationKind.OPS_RECOVERED
            notification_id = ids.new_id()
            await outbox.enqueue(
                NewNotification(
                    id=notification_id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kind=kind,
                    dedupe_key=key,
                    payload=NotificationPayload(
                        kind=kind,
                        title=NOTIFICATION_TITLES[kind],
                        notification_id=notification_id,
                        signal=signal,
                        severity=NotificationSeverity.WARN
                        if active
                        else NotificationSeverity.RECOVERED,
                        reason_code=f"ops.{signal}.{'degraded' if active else 'recovered'}",
                        release_id=release_id,
                    ),
                    priority=10,
                    expires_at=now + timedelta(hours=24),
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            await events.append(
                ProcessEvent(
                    id=ids.new_id(),
                    event_type="ops.reliability.transition",
                    actor_type="operator",
                    actor_id=principal.principal_id,
                    derivation_key=key,
                    created_at=now,
                    payload={
                        "tenant_id": principal.tenant_id,
                        "principal_id": principal.principal_id,
                        "signal": signal,
                        "sequence": sequence,
                        "active": active,
                        "release_id": release_id,
                    },
                )
            )
        return report
