"""All old charges and uncertain sends survive checkpointed collection."""

import asyncio
import hashlib
import json
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
    StopReason,
    TextDeltaEvent,
)
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    ContinuationPlan,
    required_slots,
)
from agent_core.evals.general_recall_continuation_budget import (
    ContinuationBudget,
    ContinuationHaltedError,
    ContinuationProvider,
    ParentLedger,
    account_ledger,
)
from tests.unit.test_people_execution import CompletingProvider, request_model_attempt

pytestmark = pytest.mark.static


def reserved(index: int = 1) -> dict[str, object]:
    return {
        "index": index,
        "provider": "test",
        "model": "test",
        "request_sha256": "a" * 64,
        "reservation_usd": "0.25",
        "outcome": "reserved",
    }


def completed(index: int = 1) -> dict[str, object]:
    return {**reserved(index), "outcome": "completed", "actual_cost_usd": "0.1"}


def encode(*rows: dict[str, object]) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def parents() -> tuple[ParentLedger, ...]:
    raw = encode(reserved(), completed())
    return tuple(
        ParentLedger(f"parent-{index}", hashlib.sha256(raw).hexdigest(), raw, Decimal(1))
        for index in range(8)
    )


def plan() -> ContinuationPlan:
    return ContinuationPlan(
        identity_sha256="a" * 64,
        evaluated_inputs_sha256="b" * 64,
        parent_archive_sha256="c" * 64,
        scenarios=tuple(f"case-{index}" for index in range(400)),
        prior_exposure_usd=Decimal("0.8"),
        allowance_usd=Decimal(1),
    )


def test_original_unknown_reservations_and_out_of_order_settlements_count() -> None:
    result = account_ledger(
        encode(
            reserved(),
            reserved(2),
            completed(2),
            {**reserved(), "outcome": "uncertain", "error_class": "CancelledError"},
        ),
        maximum=Decimal(1),
    )
    assert (result.reported, result.held, result.calls, result.unresolved) == (
        Decimal("0.1"),
        Decimal("0.25"),
        2,
        (1,),
    )
    assert account_ledger(encode(reserved()), maximum=Decimal(1)).held == Decimal("0.25")


@pytest.mark.parametrize(
    "damage", ["duplicate", "identity", "index", "negative", "nan", "cap", "blank"]
)
def test_invalid_ledger_cannot_create_new_allowance(damage: str) -> None:
    rows = [reserved(), completed()]
    maximum = Decimal(1)
    if damage == "duplicate":
        rows.append(completed())
    elif damage == "identity":
        rows[1]["model"] = "changed"
    elif damage == "index":
        rows[0]["index"] = 2
    elif damage == "negative":
        rows[1]["actual_cost_usd"] = "-1"
    elif damage == "nan":
        rows[0]["reservation_usd"] = "NaN"
    elif damage == "cap":
        maximum = Decimal("0.1")
    raw = encode(*rows) + (b"\n" if damage == "blank" else b"")
    with pytest.raises(ValueError):
        account_ledger(raw, maximum=maximum)


def test_new_segment_only_gets_the_remainder_and_preserves_unknowns(tmp_path: Path) -> None:
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        first = ContinuationBudget(store, parents())
        first.append(reserved())
        first.append({**reserved(), "outcome": "uncertain", "error_class": "CancelledError"})
        original = first.journal.read_bytes()
        first.seal()
        second = ContinuationBudget(store, parents())
        assert second.maximum == Decimal("0.75")
        assert second.journal != first.journal
        assert first.journal.read_bytes() == original
        second.append(reserved())
        second.append(completed())
        second.seal()
    with CheckpointStore(tmp_path / "collection", plan()) as resumed:
        third = ContinuationBudget(resumed, parents())
        assert third.maximum == Decimal("0.65")
        third.seal()


@pytest.mark.parametrize("damage", ["omitted", "changed", "duplicate", "old_segment"])
def test_parent_and_segment_lineage_cannot_be_rewritten(tmp_path: Path, damage: str) -> None:
    originals = parents()
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        first = ContinuationBudget(store, originals)
        first.append(reserved())
        first.append(completed())
        first.seal()
        ContinuationBudget(store, originals).seal()
        if damage == "omitted":
            originals = originals[:-1]
        elif damage == "changed":
            originals = (replace(originals[0], raw=b""), *originals[1:])
        elif damage == "duplicate":
            originals = (originals[0], *originals[:-1])
        else:
            first.journal.write_bytes(b"")
        with pytest.raises(ValueError):
            ContinuationBudget(store, originals)


async def test_shared_stop_has_its_own_error_and_makes_no_provider_call(tmp_path: Path) -> None:
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        provider = CompletingProvider(Decimal("0.01"))
        key = required_slots(store.plan.scenarios)[0]
        store.begin(key)
        guarded = ContinuationProvider(provider, ledger, slot=key)
        ledger.failed = True
        req, model, attempt = request_model_attempt()
        with pytest.raises(ContinuationHaltedError):
            async for _ in guarded.stream(req, model, attempt):
                pass
        assert provider.calls == 0
        ledger.seal()


def test_only_one_live_segment_and_sealed_segments_cannot_send(tmp_path: Path) -> None:
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        with pytest.raises(ValueError, match=r"active|locked"):
            ContinuationBudget(store, parents())
        ledger.seal()
        with pytest.raises(ValueError, match="sealed"):
            ledger.append(reserved())


def test_process_death_keeps_an_unsettled_reservation(tmp_path: Path) -> None:
    directory = tmp_path / "collection"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys\n"
            "from pathlib import Path\n"
            "from tests.unit.test_general_recall_continuation_budget import *\n"
            "store=CheckpointStore(Path(sys.argv[1]),plan(),create=True)\n"
            "ledger=ContinuationBudget(store,parents())\n"
            "ledger.append(reserved())\n"
            "os._exit(0)\n",
            str(directory),
        ],
        check=True,
    )
    original = (directory / "budget/000001.jsonl").read_bytes()
    with CheckpointStore(directory, plan()) as store:
        resumed = ContinuationBudget(store, parents())
        assert resumed.maximum == Decimal("0.75")
        assert (directory / "budget/000001.jsonl").read_bytes() == original
        assert (
            json.loads((directory / "budget/000001.sealed.json").read_bytes())["held_usd"] == "0.25"
        )
        resumed.seal()


@pytest.mark.parametrize("cost", [Decimal("0.03"), None, Decimal("0.12")])
async def test_real_reservation_wrapper_preserves_primary_failure_and_cost(
    tmp_path: Path, cost: Decimal | None
) -> None:
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        inner = CompletingProvider(cost)
        key = required_slots(store.plan.scenarios)[0]
        store.begin(key)
        provider = ContinuationProvider(inner, ledger, slot=key)
        if cost is None:
            with pytest.raises(RuntimeError, match="interrupted transport"):
                _ = [event async for event in provider.stream(*request_model_attempt())]
            with pytest.raises(ContinuationHaltedError, match="uncertain_usage"):
                _ = [event async for event in provider.stream(*request_model_attempt())]
        elif cost > Decimal("0.11"):
            with pytest.raises(ContinuationHaltedError, match="reservation_overrun"):
                _ = [event async for event in provider.stream(*request_model_attempt())]
        else:
            _ = [event async for event in provider.stream(*request_model_attempt())]
        assert inner.calls == 1
        result = ledger.seal()
        assert result.reported == (cost or Decimal(0))
        assert result.held == (Decimal("0.11") if cost is None else Decimal(0))
        if result.overrun:
            with pytest.raises(ValueError, match="overrun"):
                ContinuationBudget(store, parents())


async def test_actual_allowance_exhaustion_is_not_a_shared_stop(tmp_path: Path) -> None:
    with CheckpointStore(
        tmp_path / "collection", replace(plan(), allowance_usd=Decimal("0.12")), create=True
    ) as store:
        ledger = ContinuationBudget(store, parents())
        inner = CompletingProvider(Decimal("0.03"))
        key = required_slots(store.plan.scenarios)[0]
        store.begin(key)
        provider = ContinuationProvider(inner, ledger, slot=key)
        _ = [event async for event in provider.stream(*request_model_attempt())]
        with pytest.raises(BudgetExceededError):
            _ = [event async for event in provider.stream(*request_model_attempt())]
        assert inner.calls == 1 and not ledger.failed
        ledger.seal()


async def test_provider_refuses_work_without_a_durable_slot_start(tmp_path: Path) -> None:
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        inner = CompletingProvider(Decimal("0.03"))
        provider = ContinuationProvider(inner, ledger)
        with pytest.raises(ValueError, match=r"slot|admitted"):
            _ = [event async for event in provider.stream(*request_model_attempt())]
        assert inner.calls == 0 and ledger.calls == 0
        ledger.seal()


async def test_parallel_provider_reservations_and_settlements_keep_their_slot(
    tmp_path: Path,
) -> None:
    class InterleavingProvider(CompletingProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            await asyncio.sleep(0)
            async for event in super().stream(request, resolved, attempt):
                yield event

    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        inner = InterleavingProvider(Decimal("0.03"))
        keys = tuple(required_slots(store.plan.scenarios)[index] for index in (0, 4))
        for key in keys:
            store.begin(key)

        async def consume(provider: ContinuationProvider) -> None:
            _ = [event async for event in provider.stream(*request_model_attempt())]

        await asyncio.gather(
            *(consume(ContinuationProvider(inner, ledger, slot=key)) for key in keys)
        )
        entries = [json.loads(line) for line in ledger.journal.read_bytes().splitlines()]
        assert [entry["outcome"] for entry in entries[:2]] == ["reserved", "reserved"]
        for index, key in enumerate(keys, 1):
            pair = [entry for entry in entries if entry["index"] == index]
            assert {tuple(entry["slot"]) for entry in pair} == {key}
            assert {entry["outcome"] for entry in pair} == {"reserved", "completed"}
        assert ledger.seal().reported == Decimal("0.06")


def test_settlement_cannot_change_its_slot() -> None:
    first = ("case-0", "automatic", "formation", "sparse")
    second = ("case-1", "automatic", "formation", "sparse")
    with pytest.raises(ValueError, match="reservation"):
        account_ledger(
            encode({**reserved(), "slot": first}, {**completed(), "slot": second}),
            maximum=Decimal(1),
        )


@pytest.mark.parametrize("terminal", [True, False])
async def test_consumer_close_in_another_context_settles_its_original_slot(
    tmp_path: Path, terminal: bool
) -> None:
    class StreamingProvider(CompletingProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            if not terminal:
                yield TextDeltaEvent(
                    attempt_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    step_number=attempt.step_number,
                    sequence=1,
                    item_index=0,
                    text="Unfinished ordinary output",
                )
            async for event in super().stream(request, resolved, attempt):
                yield event

    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        key = required_slots(store.plan.scenarios)[0]
        store.begin(key)
        provider = ContinuationProvider(StreamingProvider(Decimal("0.03")), ledger, slot=key)
        stream = cast(AsyncGenerator[ModelEvent, None], provider.stream(*request_model_attempt()))

        async def receive() -> ModelEvent:
            return await anext(stream)

        await asyncio.create_task(receive())
        await stream.aclose()
        assert ledger.active_streams == 0
        account = ledger.seal()
        assert account.reported == (Decimal("0.03") if terminal else Decimal(0))
        assert account.held == (Decimal(0) if terminal else Decimal("0.11"))
        assert ledger.failed is not terminal
        entries = [json.loads(line) for line in ledger.journal.read_bytes().splitlines()]
        assert {tuple(entry["slot"]) for entry in entries} == {key}


async def test_terminal_settles_before_a_bank_can_reject_it(tmp_path: Path) -> None:
    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        key = required_slots(store.plan.scenarios)[0]
        store.begin(key)
        provider = ContinuationProvider(CompletingProvider(Decimal("0.03")), ledger, slot=key)
        stream = cast(AsyncGenerator[ModelEvent, None], provider.stream(*request_model_attempt()))
        try:
            await anext(stream)
            assert ledger.active_streams == 0
            assert ledger.seal().reported == Decimal("0.03")
            assert not ledger.failed
        finally:
            await stream.aclose()


async def test_rejected_terminal_facts_survive_without_assistant_or_opaque_content(
    tmp_path: Path,
) -> None:
    class LimitedProvider(CompletingProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            async for event in super().stream(request, resolved, attempt):
                assert isinstance(event, ModelCompletedEvent)
                yield event.model_copy(
                    update={
                        "stop_reason": StopReason.MAX_TOKENS,
                        "turn": event.turn.model_copy(
                            update={"stop_reason": StopReason.MAX_TOKENS}
                        ),
                    }
                )

    with CheckpointStore(tmp_path / "collection", plan(), create=True) as store:
        ledger = ContinuationBudget(store, parents())
        key = required_slots(store.plan.scenarios)[0]
        store.begin(key)
        stream = cast(
            AsyncGenerator[ModelEvent, None],
            ContinuationProvider(LimitedProvider(Decimal("0.03")), ledger, slot=key).stream(
                *request_model_attempt()
            ),
        )
        try:
            await anext(stream)
            files = tuple((store.directory / "provider-terminals").glob("*.json"))
            assert len(files) == 1
            facts = json.loads(files[0].read_bytes())
            assert facts["slot"] == list(key)
            assert facts["ledger_file"] == "budget/000001.jsonl"
            assert facts["index"] == 1
            assert facts["stop_reason"] == "max_tokens"
            assert facts["internal_retry_count"] == 0
            assert facts["usage"]["cost"] == "0.03"
            assert not set(facts) & {"turn", "messages", "content", "provider_state"}
            ledger.seal()
        finally:
            await stream.aclose()
