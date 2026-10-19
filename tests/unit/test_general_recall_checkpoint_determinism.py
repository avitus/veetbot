"""Checkpoint evidence uses the same injected determinism ports as the runtime."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.evals.general_recall_checkpoint import CheckpointStore, ContinuationPlan, Slot
from agent_core.evals.general_recall_continuation_collector import collect_segment

pytestmark = pytest.mark.static


def plan() -> ContinuationPlan:
    return ContinuationPlan(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        tuple(f"case-{n}" for n in range(400)),
        Decimal("135.90882425"),
        Decimal("150"),
    )


def test_event_timestamp_comes_from_injected_clock(tmp_path: Path) -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    with CheckpointStore(tmp_path / "state", plan(), create=True, clock=FixedClock(now)) as store:
        store.begin(store.pending()[0])
        event = json.loads((store.directory / "events/000001.json").read_bytes())
        assert datetime.fromisoformat(event["recorded_at"]) == now


async def test_segment_uses_injected_wall_and_elapsed_time(tmp_path: Path) -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)

    async def evaluate(_key: Slot, _worker: int) -> bytes:
        raise TimeoutError("No provider is used by this test")

    def validate(_value: dict[str, Any]) -> None:
        raise AssertionError("No answer was returned")

    with CheckpointStore(tmp_path / "state", plan(), create=True, clock=FixedClock(now)) as store:
        report = await collect_segment(
            store,
            evaluate=evaluate,
            validate=validate,
            admit=lambda key: None,
            seal_ledger=lambda: "d" * 64,
            monotonic=lambda: 17.0,
        )
        assert report["started_at"] == report["finished_at"] == now.isoformat()
        assert report["wall_seconds"] == 0.0
        assert report["attempts"]
        for attempt in report["attempts"]:
            assert attempt["started_at"] == attempt["finished_at"] == now.isoformat()
            assert attempt["wall_seconds"] == 0.0
