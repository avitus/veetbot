"""Metered, replay-safe first-pass judgments; failures retain ordinary email assessment."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from agent_core.domain.email import EmailThread
from agent_core.domain.email_junk import (
    JUNK_MODEL,
    JUNK_POLICY,
    JUNK_RESERVATION,
    JUNK_THRESHOLD,
    EmailJunkMode,
    junk_fingerprint,
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
    ) -> None:
        self.provider = provider
        self.mode = mode if mode != EmailJunkMode.FILTER or admitted else EmailJunkMode.SHADOW
        self.revision = f"{JUNK_POLICY}:{self.mode.value}:{evidence_digest}"

    @property
    def enabled(self) -> bool:
        return self.mode != EmailJunkMode.OFF and self.provider is not None

    def bypass(self, receipt: dict[str, Any] | None) -> bool:
        return bool(
            self.mode == EmailJunkMode.FILTER
            and receipt
            and receipt.get("status") == "completed"
            and receipt.get("model") == JUNK_MODEL
            and receipt.get("policy") == JUNK_POLICY
            and receipt.get("category") in {"spam", "bulk"}
            and float(receipt.get("probability", 0)) >= JUNK_THRESHOLD
        )

    async def screen(
        self, c: RunContext, thread: EmailThread, learning: dict[str, Any], previous: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.enabled or self.provider is None:
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
        timeout = (
            min(8.0, max(0.0, (c.run.deadline_at - c.clock.now()).total_seconds()))
            if c.run.deadline_at
            else 8.0
        )
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
