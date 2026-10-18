"""Preserve a stopped segment, completed rows and known unsent reader admission."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import CheckpointStore, Slot, _digest, _json, _read
from agent_core.evals.general_recall_collector_recovery import (
    admit_collector_recovery,
    collector_inventory,
)
from agent_core.evals.general_recall_formation_repair import admit_formation_repair
from tests.unit.test_general_recall_checkpoint_recovery import PREPARATION
from tests.unit.test_general_recall_formation_repair import repair_fixture

pytestmark = pytest.mark.static
UNSENT: Slot = ("case-2", "deliberate", "formation", "sparse")
FINISHED: Slot = ("case-1", "automatic", "formation", "sparse")


def fixture(tmp_path: Path) -> tuple[CheckpointStore, dict[str, Any]]:
    store, repair = repair_fixture(tmp_path)
    admit_formation_repair(store, **repair)
    first = store._count + 1
    store.begin(PREPARATION)
    prep = store._states[PREPARATION].index
    store.begin(FINISHED)
    row = _json(
        {
            "identity_sha256": store.plan.identity_sha256,
            **dict(zip(("scenario_id", "path", "pipeline", "channel"), FINISHED, strict=True)),
        }
    )
    store.complete(FINISHED, row, validate=lambda _: None)
    store.begin(UNSENT)
    reader = store._states[UNSENT].index
    records = []
    for index, key in enumerate((PREPARATION, FINISHED), 1):
        record = {
            "index": index,
            "provider": "openai",
            "model": "frozen",
            "slot": key,
            "request_sha256": "9" * 64,
            "reservation_usd": "1",
        }
        records += [
            {**record, "outcome": "reserved"},
            {**record, "outcome": "completed", "actual_cost_usd": "0.1"},
        ]
    (store.directory / "budget/000003.jsonl").write_bytes(b"".join(_json(r) for r in records))
    (store.directory / "budget/000003.admission.json").write_bytes(_json({"maximum_usd": "148"}))
    bank_file = "banks/" + _digest(PREPARATION[0].encode()) + ".deadline-repair.json.pending.jsonl"
    (store.directory / bank_file).write_bytes(b'{"state":"reserved"}\n')
    owner = repair["owner"]
    assessment = _json(
        {
            "original_process_gone": True,
            "paid_work_active": False,
            "completed_observations": 3,
            "provider_calls_in_new_segment": 2,
            "all_new_calls_have_completed_cost_records": True,
            "original_terminal_response_retained": False,
            "exact_formation_completion_rejection_reason": "Unknown",
            "new_allocation_usd": "0",
            "other_incomplete_reader": {
                "model_call_count": 0,
                "actual_runtime_failure_class": "ContinuationHaltedError",
            },
        }
    )
    manifest = _json(
        {
            "version": "recall-collector-recovery@1",
            "plan_sha256": _digest(store.plan.encoded()),
            "predecessor_protocol_sha256": repair["protocol_sha256"],
            "history_events": store._count,
            "history_sha256": store._previous,
            "artifacts": collector_inventory(store),
            "owner_sha256": _digest(owner),
            "assessment_sha256": _digest(assessment),
            "completed_rows": len(store.completed()),
            "segment_first_event": first,
            "formation_key": PREPARATION,
            "formation_started": prep,
            "reader_key": UNSENT,
            "reader_started": reader,
            "ledger_file": "budget/000003.jsonl",
            "bank_file": bank_file,
        }
    )
    return store, {
        "manifest": manifest,
        "owner": owner,
        "assessment": assessment,
        "protocol_sha256": "7" * 64,
    }


def test_recovery_keeps_completed_bytes_and_only_admits_the_two_proved_slots(
    tmp_path: Path,
) -> None:
    store, args = fixture(tmp_path)
    originals = {n: _read(store.directory / n) for n in json.loads(args["manifest"])["artifacts"]}
    with store:
        admit_collector_recovery(store, **args)
        assert PREPARATION in store.pending() and UNSENT in store.pending()
        assert not store.begin(FINISHED)
        assert len(store.completed()) == 3
        for name, raw in originals.items():
            assert _read(store.directory / name) == raw
        report = json.loads(_read(store.directory / "collector-recovery/reconciled-segment.json"))
        completed = [r for r in report["attempts"] if r["state"] == "completed"]
        assert len(completed) == 1 and tuple(completed[0]["key"]) == FINISHED
        assert completed[0]["wall_seconds"] is None
        assert report["derived_from_durable_events"] is True
    with CheckpointStore(store.directory, store.plan) as resumed:
        admit_collector_recovery(resumed, **args)
        resumed.begin(PREPARATION)
        with pytest.raises(ValueError):
            admit_collector_recovery(resumed, **args)


@pytest.mark.parametrize(
    "damage",
    [
        "owner",
        "result",
        "omitted",
        "unknown_reader",
        "uncertain",
        "deadline",
        "live",
        "history",
        "bank",
        "completed_key",
    ],
)
def test_collector_recovery_refuses_unproved_retries_before_append(
    tmp_path: Path, damage: str
) -> None:
    store, args = fixture(tmp_path)
    manifest = json.loads(args["manifest"])
    count = store._count
    if damage == "owner":
        args["owner"] = b"{}"
    elif damage == "omitted":
        manifest["artifacts"].pop(
            next(n for n in manifest["artifacts"] if n.startswith("results/"))
        )
    elif damage == "history":
        manifest["history_sha256"] = "0" * 64
    elif damage == "completed_key":
        manifest["reader_key"] = FINISHED
    elif damage == "live":
        facts = json.loads(args["assessment"])
        facts["original_process_gone"] = False
        args["assessment"] = _json(facts)
        manifest["assessment_sha256"] = _digest(args["assessment"])
    elif damage in {"unknown_reader", "uncertain"}:
        path = store.directory / manifest["ledger_file"]
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        if damage == "unknown_reader":
            rows[-2]["slot"] = rows[-1]["slot"] = UNSENT
        else:
            rows[-1].pop("actual_cost_usd")
            rows[-1]["outcome"] = "uncertain"
        path.write_bytes(b"".join(_json(r) for r in rows))
        manifest["artifacts"][manifest["ledger_file"]] = _digest(path.read_bytes())
    else:
        name = {
            "result": next(n for n in manifest["artifacts"] if n.startswith("results/")),
            "deadline": "deadline.json",
            "bank": manifest["bank_file"],
        }[damage]
        (store.directory / name).write_bytes(b"{}\n")
    args["manifest"] = _json(manifest)
    with store, pytest.raises(ValueError):
        admit_collector_recovery(store, **args)
    assert store._count == count


def test_four_protocol_epochs_and_new_bank_are_explicit(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent
    from agent_core.evals.general_recall_checkpoint_recovery import protocol_for_segment

    store, args = fixture(tmp_path)
    with store:
        admit_collector_recovery(store, **args)
        assert [protocol_for_segment(store, args["protocol_sha256"], i) for i in (1, 2, 3, 4)] == [
            "d" * 64,
            "e" * 64,
            "f" * 64,
            "7" * 64,
        ]
        parent: Any = SimpleNamespace(
            by_id={PREPARATION[0]: None}, banks={}, pending_banks={PREPARATION[0]: None}
        )
        bank = CheckpointParent.bank_path(parent, store, PREPARATION[0])
        assert bank.name.endswith(".terminal-repair.json") and not bank.exists()


def test_stopped_journal_is_sealed_from_settled_original_records(tmp_path: Path) -> None:
    store, args = fixture(tmp_path)
    ledger = store.directory / "budget/000003.jsonl"
    original = ledger.read_bytes()
    with store:
        admit_collector_recovery(store, **args)
        seal = json.loads((ledger.with_suffix(".sealed.json")).read_bytes())
        assert seal == {"journal_sha256": _digest(original), "reported_usd": "0.2", "held_usd": "0"}
        assert ledger.read_bytes() == original
