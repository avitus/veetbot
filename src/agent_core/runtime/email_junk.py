"""Metered, replay-safe first-pass judgments; failures retain ordinary email assessment."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord, EmailThread
from agent_core.domain.email_junk import (
    EXPEDITE_POLICY,
    EXPEDITE_QUESTION,
    JUNK_MODEL,
    JUNK_POLICY,
    JUNK_RESERVATION,
    JUNK_THRESHOLD,
    EmailJunkMode,
    JunkAuthorization,
    JunkPilot,
    JunkStandingAuthorization,
    expedite_result,
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
        pilot: JunkAuthorization | None = None,
    ) -> None:
        """Downgrade unadmitted filtering to observation and bind the cache revision."""
        self.provider = provider
        self.mode = mode if mode != EmailJunkMode.FILTER or admitted else EmailJunkMode.SHADOW
        scoped = mode in {EmailJunkMode.PILOT, EmailJunkMode.OWNER_FILTER}
        expected = JunkPilot if mode == EmailJunkMode.PILOT else JunkStandingAuthorization
        self.pilot = pilot if scoped and isinstance(pilot, expected) else None
        if scoped and self.pilot is None:
            self.mode = EmailJunkMode.OFF
        policy = self.pilot.policy if self.pilot is not None else JUNK_POLICY
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
        """Apply the calibrated threshold or the active pilot's scoped raw-choice rule."""
        if not (
            receipt
            and receipt.get("status") == "completed"
            and receipt.get("model") == JUNK_MODEL
            and receipt.get("policy") == JUNK_POLICY
        ):
            return False
        if self.mode in {EmailJunkMode.PILOT, EmailJunkMode.OWNER_FILTER}:
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
        self,
        c: RunContext,
        thread: EmailThread,
        learning: dict[str, Any],
        previous: dict[str, Any],
        *,
        positive: bool = False,
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
        if positive:
            if self.mode != EmailJunkMode.OWNER_FILTER or thread.account_id != "work":
                return None
            request = request.model_copy(update={"questions": {"expedite": EXPEDITE_QUESTION}})
        prefix = "email.triage.positive" if positive else "email.junk"
        fingerprint = junk_fingerprint(request, thread, learning)
        if positive:
            fingerprint = hashlib.sha256(
                f"{EXPEDITE_POLICY}:{self.revision}:{fingerprint}".encode()
            ).hexdigest()
        cached = previous.get("positive_screen" if positive else "junk_screen")
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            return cached
        fallback: dict[str, Any] = {
            "fingerprint": fingerprint,
            "policy": EXPEDITE_POLICY if positive else JUNK_POLICY,
            "model": JUNK_MODEL,
            "category": "keep",
            "probability": 0.0,
            "status": "uncertain",
        }
        if positive and thread.arrival:
            fallback.update(arrival_id=str(thread.arrival.id), account_id=thread.account_id)
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
                if e.event_type.startswith(prefix + ".")
                and e.payload.get("fingerprint") == fingerprint
            ]
            for event in reversed(matching):
                if event.event_type == prefix + ".completed":
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
            if positive:
                current = await uow.email.get(c.principal, "thread", str(thread.id))
                state = await uow.email.get(c.principal, "learning", "shared")
                revision_now = state.payload.get("profile_revision", 1) if state else 1
                if (
                    current is None
                    or not current.payload.get("in_inbox")
                    or current.payload.get("in_spam")
                    or current.payload.get("spam_cleared")
                    or revision_now != learning.get("profile_revision")
                    or current.payload.get("source_fingerprint") != thread.source_fingerprint
                ):
                    return None
                verified = junk_request(
                    EmailThread.model_validate(current.payload), learning, c.clock.now()
                )
                if verified is None or verified.state != request.state:
                    return None
                cached_row = await uow.email.get(c.principal, "positive_screen", str(thread.id))
                if cached_row and cached_row.payload.get("fingerprint") == fingerprint:
                    return dict(cached_row.payload)
                now = c.clock.now()
                revision = 0 if cached_row is None else cached_row.revision
                await uow.email.put(
                    EmailRecord(
                        tenant_id=c.principal.tenant_id,
                        principal_id=c.principal.principal_id,
                        kind="positive_screen",
                        key=str(thread.id),
                        revision=revision + 1,
                        payload=fallback,
                        created_at=cached_row.created_at if cached_row else now,
                        updated_at=now,
                    ),
                    expected_revision=revision,
                )
            # Reserve before egress in the same fenced transaction as the audit event.
            updated = c.run.model_copy(deep=True)
            updated.usage.cost += JUNK_RESERVATION
            await uow.runs.update_counters(updated, lease=c.lease)
            await uow.events.append(
                NewEvent(
                    session_id=c.run.session_id,
                    run_id=c.run.id,
                    event_type=prefix + ".started",
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
            receipt = {
                **fallback,
                **(expedite_result(request, result) if positive else junk_result(request, result)),
            }
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
                    event_type=prefix + ".completed",
                    actor_type="runtime",
                    payload=receipt,
                ),
                lease=c.lease,
            )
            if positive:
                # An erasure or a newer source can retire this claim while the provider replies.
                row = await uow.email.get(c.principal, "positive_screen", str(thread.id))
                if row and row.payload.get("fingerprint") == fingerprint:
                    await uow.email.put(
                        row.model_copy(
                            update={
                                "revision": row.revision + 1,
                                "payload": receipt,
                                "updated_at": c.clock.now(),
                            }
                        ),
                        expected_revision=row.revision,
                    )
        c.run.usage = updated.usage
        return receipt
