"""ADR-0212 scheduler regressions with no provider or database calls."""

import asyncio
import hashlib
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    ContinuationPlan,
    Slot,
    required_slots,
)
from agent_core.evals.general_recall_continuation_collector import (
    CollectionFailureError,
    collect_segment,
)

pytestmark = pytest.mark.static


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CheckpointStore]:
    plan = ContinuationPlan(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        tuple(f"case-{n}" for n in range(400)),
        Decimal("135.90882425"),
        Decimal("150"),
    )
    with CheckpointStore(tmp_path / "collection", plan, create=True) as value:
        yield value


def raw(key: Slot) -> bytes:
    return json.dumps(
        {
            "identity_sha256": "a" * 64,
            **dict(zip(("scenario_id", "path", "pipeline", "channel"), key, strict=True)),
            "answer": "incorrect answer retained without selective retries",
        }
    ).encode()


def validate(value: dict[str, Any]) -> None:
    assert "answer" in value


@pytest.mark.asyncio
async def test_completed_bad_answer_is_skipped_and_each_case_stays_on_one_worker(
    store: CheckpointStore,
) -> None:
    original = required_slots(store.plan.scenarios)[0]
    data = raw(original)
    store.import_result(data, expected_sha256=hashlib.sha256(data).hexdigest(), validate=validate)
    calls: list[tuple[Slot, int]] = []

    async def evaluate(key: Slot, worker: int) -> bytes:
        calls.append((key, worker))
        await asyncio.sleep(0)
        return raw(key)

    report = await collect_segment(
        store,
        evaluate=evaluate,
        validate=validate,
        admit=lambda key: None,
        seal_ledger=lambda: "d" * 64,
    )
    assert original not in [key for key, _ in calls]
    assert len(calls) == len({key for key, _ in calls}) == 1599
    assert all(len({w for key, w in calls if key[0] == case}) == 1 for case in ("case-0", "case-1"))
    assert report["completed_slots"] == 1600
    assert report["state"] == "collected"
    assert report["ledger_sha256"] == "d" * 64


@pytest.mark.asyncio
async def test_first_failure_stops_admissions_but_allows_other_admitted_answer_to_settle(
    store: CheckpointStore,
) -> None:
    peer_started = asyncio.Event()
    calls: list[Slot] = []

    async def evaluate(key: Slot, worker: int) -> bytes:
        calls.append(key)
        if key[0] == "case-0":
            await peer_started.wait()
            raise CollectionFailureError("formation", TimeoutError("Private provider detail"))
        peer_started.set()
        await asyncio.sleep(0.01)
        return raw(key)

    sealed: list[bool] = []

    def seal() -> str:
        sealed.append(True)
        return "e" * 64

    report = await collect_segment(
        store,
        evaluate=evaluate,
        validate=validate,
        admit=lambda key: None,
        seal_ledger=seal,
    )
    assert len(calls) == 2
    assert sealed == [True]
    assert report["completed_slots"] == 1
    assert store.begin(calls[1]) is False
    with pytest.raises(ValueError, match=r"unknown|unresolved"):
        store.begin(calls[0])
    store.replace_formation(calls[0])
    assert "Private provider detail" not in json.dumps(report)
    assert report["attempts"][0]["error_class"] == "TimeoutError"
    assert report["attempts"][0]["wall_seconds"] >= 0


@pytest.mark.asyncio
async def test_unknown_started_case_is_not_resent_or_reported_complete(
    store: CheckpointStore,
) -> None:
    unknown = required_slots(store.plan.scenarios)[0]
    store.begin(unknown)
    calls: list[Slot] = []

    async def evaluate(key: Slot, worker: int) -> bytes:
        calls.append(key)
        raise ConnectionError("Interrupted reader")

    report = await collect_segment(
        store,
        evaluate=evaluate,
        validate=validate,
        admit=lambda key: None,
        seal_ledger=lambda: "d" * 64,
    )
    assert calls and all(key[0] != "case-0" for key in calls)
    assert report["state"] == "incomplete" and report["completed_slots"] == 0
    with pytest.raises(ValueError, match=r"replacement|retry"):
        store.replace_formation(calls[0])


@pytest.mark.asyncio
async def test_invalid_mechanical_result_is_retained_and_never_retried(
    store: CheckpointStore,
) -> None:
    calls: list[Slot] = []

    async def evaluate(key: Slot, worker: int) -> bytes:
        calls.append(key)
        return raw(key)

    def reject(value: dict[str, Any]) -> None:
        raise ValueError("Mechanical compiler rejected evidence")

    report = await collect_segment(
        store,
        evaluate=evaluate,
        validate=reject,
        admit=lambda key: None,
        seal_ledger=lambda: "d" * 64,
    )
    assert len(calls) == 1
    assert report["attempts"][0]["state"] == "retained_unvalidated"
    assert [p.read_bytes() for p in (store.directory / "results").iterdir()] == [raw(calls[0])]
    with pytest.raises(ValueError, match=r"unknown|unresolved"):
        store.begin(calls[0])


@pytest.mark.asyncio
async def test_admission_refuses_before_durable_start_or_provider_work(
    store: CheckpointStore,
) -> None:
    async def evaluate(key: Slot, worker: int) -> bytes:
        pytest.fail("A refused slot must not call the evaluator")

    def reject(key: Slot) -> None:
        raise ValueError("Runtime or deadline differs")

    report = await collect_segment(
        store,
        evaluate=evaluate,
        validate=validate,
        admit=reject,
        seal_ledger=lambda: "d" * 64,
    )
    assert report["attempts"][0]["state"] == "refused"
    assert len(store.pending()) == 1600


@pytest.mark.asyncio
async def test_empty_return_is_unknown_instead_of_claiming_retained_bytes(
    store: CheckpointStore,
) -> None:
    async def evaluate(key: Slot, worker: int) -> bytes:
        return b""

    report = await collect_segment(
        store,
        evaluate=evaluate,
        validate=validate,
        admit=lambda key: None,
        seal_ledger=lambda: "d" * 64,
    )
    assert report["attempts"][0]["state"] == "unknown_result"
    assert not tuple((store.directory / "results").iterdir())
    with pytest.raises(ValueError, match=r"unknown|unresolved"):
        store.begin(required_slots(store.plan.scenarios)[0])


@pytest.mark.asyncio
async def test_supervisor_cancellation_preserves_admitted_answers_before_propagating(
    store: CheckpointStore,
) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    calls: list[Slot] = []

    async def evaluate(key: Slot, worker: int) -> bytes:
        calls.append(key)
        if len(calls) == 2:
            ready.set()
        await release.wait()
        return raw(key)

    task = asyncio.create_task(
        collect_segment(
            store,
            evaluate=evaluate,
            validate=validate,
            admit=lambda key: None,
            seal_ledger=lambda: "d" * 64,
        )
    )
    waiting = asyncio.create_task(ready.wait())
    done, _ = await asyncio.wait((waiting, task), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        waiting.cancel()
        await task
    await waiting
    with pytest.raises(ValueError, match="active"):
        await collect_segment(
            store,
            evaluate=evaluate,
            validate=validate,
            admit=lambda key: None,
            seal_ledger=lambda: "d" * 64,
        )
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2
    assert all(store.begin(key) is False for key in calls)
    reports = tuple((store.directory / "segments").glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_bytes())["interrupted"] is True
