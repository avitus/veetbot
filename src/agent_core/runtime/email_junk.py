"""Metered, replay-safe first-pass judgments; failures retain ordinary email assessment."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailThread
from agent_core.domain.email_junk import (
    JUNK_MODEL,
    JUNK_PILOT_POLICY,
    JUNK_POLICY,
    JUNK_RESERVATION,
    JUNK_THRESHOLD,
    EmailJunkMode,
    JunkPilot,
    junk_fingerprint,
    junk_pilot_capacity,
    junk_pilot_keys,
    junk_request,
    junk_result,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.judgment import (
    JudgmentFailure,
    JudgmentProviderError,
)
from agent_core.domain.runs import BudgetScope
from agent_core.ports.judgment import JudgmentProvider
from agent_core.runtime.loop import RunContext


class EmailJunkFilter:
    def __init__(
        self,
        provider: JudgmentProvider | None,
        mode: EmailJunkMode,
        *,
        admitted: bool = False,
        evidence_digest: str = "none",
        pilot: JunkPilot | None = None,
    ) -> None:
        """Downgrade unadmitted filtering to observation and bind the cache revision."""
        self.provider = provider
        self.mode = mode if mode != EmailJunkMode.FILTER or admitted else EmailJunkMode.SHADOW
        self.pilot = pilot if mode == EmailJunkMode.PILOT else None
        if mode == EmailJunkMode.PILOT and pilot is None:
            self.mode = EmailJunkMode.OFF
        policy = JUNK_PILOT_POLICY if self.pilot is not None else JUNK_POLICY
        self.revision = f"{policy}:{self.mode.value}:{evidence_digest}"

    @property
    def enabled(self) -> bool:
        """Permit screening only when a provider exists and the mode allows calls."""
        return self.mode != EmailJunkMode.OFF and self.provider is not None

    def bypass(
        self,
        receipt: dict[str, Any] | None,
        *,
        principal: Principal | None = None,
        account_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Require admitted filter mode and a completed, matching, high-scoring junk receipt."""
        if not (
            receipt
            and receipt.get("status") == "completed"
            and receipt.get("model") == JUNK_MODEL
            and receipt.get("policy") == JUNK_POLICY
        ):
            return False
        if self.mode == EmailJunkMode.PILOT:
            return bool(
                self.pilot is not None
                and principal is not None
                and account_id is not None
                and now is not None
                and self.pilot.admitted(principal, account_id, now)
                and receipt.get("raw_category") in {"spam", "bulk"}
            )
        return (
            self.mode == EmailJunkMode.FILTER
            and receipt.get("category") in {"spam", "bulk"}
            and float(receipt.get("probability", 0)) >= JUNK_THRESHOLD
        )

    async def screen(
        self, c: RunContext, thread: EmailThread, learning: dict[str, Any], previous: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Reconcile prior attempts, reserve before egress and settle verified usage atomically."""
        if not self.enabled or self.provider is None:
            return None
        if self.pilot is not None and not self.pilot.admitted(
            c.principal, thread.account_id, c.clock.now()
        ):
            return None
        request = junk_request(thread, learning, c.clock.now())
        if request is None:
            return None
        fingerprint = junk_fingerprint(request, thread, learning)
        cached = previous.get("junk_screen")
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            return cached
        fallback: dict[str, Any] = {
            "fingerprint": fingerprint,
            "policy": JUNK_POLICY,
            "model": JUNK_MODEL,
            "category": "keep",
            "probability": 0.0,
            "status": "uncertain",
        }
        # Reconcile a possibly dispatched judgment before considering another request.
        async with c.uow_factory() as uow, uow.email.lock(c.principal):
            if self.pilot is not None:
                if not self.pilot.admitted(c.principal, thread.account_id, c.clock.now()):
                    return None
                key, day = junk_pilot_keys(thread, c.clock.now())
                admission = await uow.email.get(c.principal, "junk_pilot_admission", key)
                counter = await uow.email.get(c.principal, "junk_pilot_day", day)
                if not junk_pilot_capacity(
                    None if admission is None else admission.payload,
                    None if counter is None else counter.payload,
                ):
                    return None
            events = await uow.events.list_after(c.run.session_id, 0, c.principal, run_id=c.run.id)
            matching = [
                e
                for e in events
                if e.event_type.startswith("email.junk.")
                and e.payload.get("fingerprint") == fingerprint
            ]
            for event in reversed(matching):
                if event.event_type == "email.junk.completed":
                    return dict(event.payload)
            if matching:
                return fallback
            c.token.raise_if_cancelled()
            c.budgets.check(c.run, BudgetScope.STEP)
            if (
                c.run.limits.max_cost is not None
                and c.run.usage.cost + JUNK_RESERVATION > c.run.limits.max_cost
            ):
                return None
            timeout = (
                min(8.0, max(0.0, (c.run.deadline_at - c.clock.now()).total_seconds()))
                if c.run.deadline_at
                else 8.0
            )
            if timeout <= 0:
                return None
            if self.pilot is not None and not self.pilot.admitted(
                c.principal, thread.account_id, c.clock.now()
            ):
                return None
            # Reserve before egress in the same fenced transaction as the audit event.
            updated = c.run.model_copy(deep=True)
            updated.usage.cost += JUNK_RESERVATION
            await uow.runs.update_counters(updated, lease=c.lease)
            await uow.events.append(
                NewEvent(
                    session_id=c.run.session_id,
                    run_id=c.run.id,
                    event_type="email.junk.started",
                    actor_type="runtime",
                    payload={**fallback, "reserved_cost": str(JUNK_RESERVATION)},
                ),
                lease=c.lease,
            )
        c.run.usage = updated.usage
        actual: Decimal | None = None
        receipt = fallback
        try:
            async with asyncio.timeout(timeout):
                result = await self.provider.judge(request)
            # Accounting precedes trusting the decision, including wrong-model responses.
            actual = result.usage.cost
            if not actual.is_finite() or actual < 0:
                actual = None
            receipt = {**fallback, **junk_result(request, result)}
        except JudgmentProviderError as exc:
            if exc.reason in {
                JudgmentFailure.AUTH_FAILED,
                JudgmentFailure.REQUEST_REJECTED,
                JudgmentFailure.PAYMENT_REQUIRED,
                JudgmentFailure.INPUT_TOO_LARGE,
            }:
                actual = Decimal(0)
            receipt = {**fallback, "reason": exc.reason.value}
        except TimeoutError:
            receipt = {**fallback, "reason": "judgment.provider_unavailable"}
        # Cancellation/crashes leave the start reservation untouched for recovery.
        receipt = {
            **receipt,
            "usage_known": actual is not None,
            "cost": str(JUNK_RESERVATION if actual is None else actual),
        }
        async with c.uow_factory() as uow, uow.email.lock(c.principal):
            updated = c.run.model_copy(deep=True)
            if actual is not None:
                updated.usage.cost += actual - JUNK_RESERVATION
            await uow.runs.update_counters(updated, lease=c.lease)
            await uow.events.append(
                NewEvent(
                    session_id=c.run.session_id,
                    run_id=c.run.id,
                    event_type="email.junk.completed",
                    actor_type="runtime",
                    payload=receipt,
                ),
                lease=c.lease,
            )
        c.run.usage = updated.usage
        return receipt
