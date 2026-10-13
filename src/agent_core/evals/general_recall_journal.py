"""Reconcile original provider reservations with every observed fresh completion."""

import json
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import Field

from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.evals.general_recall_observation import ProviderAttempt
from agent_core.memory.general_recall_evidence import _unique_object


class JournalEntry(ReleaseValue):
    index: int = Field(gt=0)
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    request_sha256: Digest
    reservation_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    outcome: Literal["reserved", "completed"]
    actual_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


def verify_provider_journal(
    raw: bytes, attempts: Sequence[ProviderAttempt], *, maximum: Decimal
) -> Decimal:
    """No missing, duplicate, uncertain or unobserved call can disappear from cost.

    Formation replay is deliberately absent from this ledger: its per-case cost
    remains attributed by the observation compiler, while original charges occur
    once. Exact repeated requests retain their multiplicity. Concurrent calls may
    settle out of order but cannot renew or exceed the shared reservation budget.
    """
    if not maximum.is_finite() or maximum <= 0 or not raw or len(raw) > 16 * 1024 * 1024:
        raise ValueError("Invalid recall provider journal or allowance")
    pending: dict[int, JournalEntry] = {}
    charged: Counter[tuple[str, str, str, Decimal]] = Counter()
    held = spent = Decimal(0)
    calls = 0
    for line in raw.splitlines():
        if not line.strip():
            raise ValueError("Blank recall provider journal entry")
        entry = JournalEntry.model_validate(json.loads(line, object_pairs_hook=_unique_object))
        if entry.outcome == "reserved":
            if (
                entry.index != calls + 1
                or entry.actual_cost_usd is not None
                or spent + held + entry.reservation_usd > maximum
            ):
                raise ValueError(
                    "Recall provider reservation is duplicated, missing or over budget"
                )
            calls += 1
            held += entry.reservation_usd
            pending[entry.index] = entry
        else:
            reserved = pending.pop(entry.index, None)
            if (
                reserved is None
                or entry.actual_cost_usd is None
                or entry.actual_cost_usd > entry.reservation_usd
                or entry.model_copy(update={"outcome": "reserved", "actual_cost_usd": None})
                != reserved
            ):
                raise ValueError("Recall provider completion does not settle its reservation")
            held -= entry.reservation_usd
            spent += entry.actual_cost_usd
            charged[(entry.provider, entry.model, entry.request_sha256, entry.actual_cost_usd)] += 1
    observed = Counter((a.provider, a.model, a.request_sha256, a.usage.cost) for a in attempts)
    if pending or held or not calls or charged != observed:
        raise ValueError("Recall provider journal is unsettled or differs from observed calls")
    return spent
