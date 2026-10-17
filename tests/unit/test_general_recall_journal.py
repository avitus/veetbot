import json
from decimal import Decimal

import pytest

from agent_core.evals.general_recall_journal import verify_provider_journal
from agent_core.evals.general_recall_observation import ProviderAttempt


def attempt() -> ProviderAttempt:
    return ProviderAttempt.model_validate(
        {
            "provider": "fake",
            "model": "scripted",
            "request_sha256": "a" * 64,
            "completed": True,
            "usage": {"input_tokens": 12, "output_tokens": 3, "cost": "0.02"},
        }
    )


def entries() -> list[dict[str, object]]:
    reserved = {
        "index": 1,
        "provider": "fake",
        "model": "scripted",
        "request_sha256": "a" * 64,
        "reservation_usd": "0.05",
        "outcome": "reserved",
    }
    return [reserved, {**reserved, "outcome": "completed", "actual_cost_usd": "0.02"}]


def encode(rows: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def test_journal_reconciles_every_original_charge_and_retains_duplicate_requests() -> None:
    first, second = entries(), [{**row, "index": 2} for row in entries()]
    raw = encode([first[0], second[0], second[1], first[1]])
    assert verify_provider_journal(raw, [attempt(), attempt()], maximum=Decimal("0.10")) == (
        Decimal("0.04")
    )


@pytest.mark.parametrize(
    "change",
    ["missing", "duplicate", "uncertain", "renumbered", "changed", "overrun", "extra", "budget"],
)
def test_unsettled_or_misattributed_provider_charges_cannot_supply_evidence(change: str) -> None:
    rows = entries()
    maximum = Decimal("1")
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(rows[-1])
    elif change == "uncertain":
        rows[-1]["outcome"] = "uncertain"
    elif change == "renumbered":
        rows[0]["index"] = 2
    elif change == "changed":
        rows[-1]["request_sha256"] = "b" * 64
    elif change == "overrun":
        rows[-1]["actual_cost_usd"] = "0.1"
    elif change == "extra":
        rows.extend({**row, "index": 2} for row in entries())
    else:
        maximum = Decimal("0.01")
    with pytest.raises(ValueError):
        verify_provider_journal(encode(rows), [attempt()], maximum=maximum)
