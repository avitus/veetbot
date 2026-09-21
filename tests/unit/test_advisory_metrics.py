"""The advisory observer answers "is the advisor running" from the service log alone."""

from __future__ import annotations

import logging

import pytest

from agent_core.observability.policy import AdvisoryMetrics

LOGGER = "agent_core.observability.policy"


def _fields(record: logging.LogRecord, *names: str) -> dict[str, object]:
    return {name: getattr(record, name) for name in names}


@pytest.mark.parametrize(
    ("verdict", "signals"),
    [("abstain", ()), ("require_approval", ("personal_data",))],
    ids=["abstain", "escalation"],
)
def test_every_consultation_writes_one_content_free_line(
    caplog: pytest.LogCaptureFixture, verdict: str, signals: tuple[str, ...]
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        AdvisoryMetrics().consulted(
            tool="web.search", verdict=verdict, enforced=False, signals=signals, seconds=0.1234567
        )

    (record,) = caplog.records
    assert record.getMessage() == "policy_advisory_consulted"
    assert _fields(record, "tool", "verdict", "enforced", "signals", "seconds") == {
        "tool": "web.search",
        "verdict": verdict,
        "enforced": False,
        "signals": signals,
        "seconds": 0.123,
    }


def test_an_abstention_writes_its_tool_and_cause(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        AdvisoryMetrics().abstained(tool="web.fetch", cause="judgment.payment_required")

    (record,) = caplog.records
    assert record.getMessage() == "policy_advisory_abstained"
    assert _fields(record, "tool", "cause") == {
        "tool": "web.fetch",
        "cause": "judgment.payment_required",
    }
