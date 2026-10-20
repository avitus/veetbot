"""Owner-approved ADR-0213 exceptions preserve evidence and remain single-use."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    ContinuationPlan,
    Slot,
    _digest,
    _json,
    _read,
    required_slots,
)
from agent_core.evals.general_recall_checkpoint_recovery import (
    admit_bounded_recovery,
    recovery_inventory,
)

pytestmark = pytest.mark.static
PREPARATION: Slot = ("dev-decisions-03", "automatic", "formation", "sparse")
READER: Slot = ("dev-decisions-04", "deliberate", "formation", "hybrid")


def fixture(tmp_path: Path) -> tuple[CheckpointStore, dict[str, Any]]:
    plan = ContinuationPlan(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (PREPARATION[0], READER[0], *(f"case-{i}" for i in range(398))),
        Decimal("135.90882425"),
        Decimal("150"),
    )
    store = CheckpointStore(tmp_path / "state", plan, create=True)
    (store.directory / "deadline.json").write_bytes(
        _json(
            {
                "version": "recall-continuation-deadline@1",
                "plan_sha256": _digest(plan.encoded()),
                "protocol_sha256": "d" * 64,
                "started_at": "2026-10-18T00:27:32.501898Z",
                "expires_at": "2026-10-25T00:27:32.501898Z",
                "duration_seconds": 604800,
            }
        )
    )
    (store.directory / "parent-import.json").write_bytes(b"{}\n")
    store.begin(PREPARATION)
    store.fail(PREPARATION, stage="formation", error_class="CancelledError", ledger_sha256="1" * 64)
    store.replace_formation(PREPARATION)
    store.begin(PREPARATION)
    store.begin(READER)
    raw = _json(
        {
            "identity_sha256": plan.identity_sha256,
            "scenario_id": "case-0",
            "path": "automatic",
            "pipeline": "formation",
            "channel": "sparse",
        }
    )
    store.import_result(raw, expected_sha256=_digest(raw), validate=lambda _: None)
    budget = store.directory / "budget"
    budget.mkdir()
    # Complete request/settlement pairs, including retained uncertain preparation.
    entries = []
    for index, slot in enumerate((PREPARATION, PREPARATION, PREPARATION, READER), 1):
        base = {
            "index": index,
            "provider": "openai",
            "model": "frozen",
            "slot": slot,
            "request_sha256": str(index) * 64,
            "reservation_usd": "1",
        }
        entries.extend(
            [
                {**base, "outcome": "reserved"},
                {**base, "outcome": "uncertain", "error_class": "CancelledError"}
                if index == 3
                else {**base, "outcome": "completed", "actual_cost_usd": "0.01"},
            ]
        )
    journal = b"".join(_json(v) for v in entries)
    (budget / "000001.jsonl").write_bytes(journal)
    (budget / "000001.sealed.json").write_bytes(
        _json({"journal_sha256": _digest(journal), "reported_usd": "0.03", "held_usd": "1"})
    )
    store.fail(
        PREPARATION, stage="formation", error_class="CancelledError", ledger_sha256=_digest(journal)
    )
    store.fail(READER, stage="answer", error_class="ValueError", ledger_sha256=_digest(journal))
    banks = store.directory / "banks"
    banks.mkdir()
    bank_name = f"banks/{_digest(PREPARATION[0].encode())}.replacement.json.pending.jsonl"
    (store.directory / bank_name).write_bytes(b'{"state":"reserved"}\n')
    report = {
        "ledger_sha256": _digest(journal),
        "attempts": [
            {
                "key": PREPARATION,
                "state": "failed",
                "stage": "formation",
                "error_class": "CancelledError",
            },
            {"key": READER, "state": "failed", "stage": "answer", "error_class": "ValueError"},
        ],
    }
    report_raw = _json(report)
    reports = store.directory / "segments"
    reports.mkdir()
    (reports / f"{_digest(report_raw)}.json").write_bytes(report_raw)
    assessment = _json(
        {
            "event_chain_tip": store._previous,
            "events_verified": store._count,
            "completed_observations_preserved": 1,
            "provider_calls_this_assessment": 0,
            "database_writes": False,
            "reader": {
                "slot": READER,
                "completed_final_answers": 0,
                "completed_provider_turns": 1,
                "provider_journal_requests": 1,
                "followup_provider_reservation_present": False,
                "checkpoint_is_terminal_failed": True,
            },
            "formation": {
                "case": PREPARATION[0],
                "pending_bank_sha256": _digest(_read(store.directory / bank_name)),
                "sealed_bank_exists": False,
                "last_request_outcome": "uncertain_cancelled",
            },
        }
    )
    owner = _json(
        {
            "answer": "Approve the two bounded retries (Recommended)",
            "assessment_sha256": _digest(assessment),
            "completed_rows_preserved": 1,
            "required_rows": 1600,
            "aggregate_cap_usd": "300",
            "new_allocation_usd": "0",
            "stop_if_either_recovery_fails": True,
        }
    )
    claims = []
    for slot in (PREPARATION, READER):
        event = store._states[slot]
        claims.append(
            {
                "key": slot,
                "stage": event.stage,
                "failed_event": event.index,
                "failed_event_sha256": _digest(
                    _read(store.directory / "events" / f"{event.index:06d}.json")
                ),
                "ledger_file": "budget/000001.jsonl",
                "report_file": f"segments/{_digest(report_raw)}.json",
                "bank_file": bank_name if slot == PREPARATION else None,
            }
        )
    manifest = _json(
        {
            "version": "recall-bounded-recovery@1",
            "plan_sha256": _digest(plan.encoded()),
            "parent_archive_sha256": plan.parent_archive_sha256,
            "predecessor_protocol_sha256": "d" * 64,
            "history_sha256": store._previous,
            "history_events": store._count,
            "owner_authorization_sha256": _digest(owner),
            "assessment_sha256": _digest(assessment),
            "artifacts": recovery_inventory(store.directory),
            "claims": claims,
        }
    )
    return store, {
        "manifest": manifest,
        "owner": owner,
        "assessment": assessment,
        "protocol_sha256": "e" * 64,
    }


def test_append_only_recovery_preserves_rows_and_reopens(tmp_path: Path) -> None:
    store, args = fixture(tmp_path)
    original = recovery_inventory(store.directory)
    with store:
        admit_bounded_recovery(store, **args)
        assert PREPARATION in store.pending() and READER in store.pending()
        assert len(store.completed()) == 1
        for name, digest in original.items():
            assert _digest(_read(store.directory / name)) == digest
    with CheckpointStore(store.directory, store.plan) as reopened:
        admit_bounded_recovery(reopened, **args)
        assert reopened.begin(READER)
    with CheckpointStore(store.directory, store.plan) as reopened, pytest.raises(ValueError):
        reopened.begin(READER)


@pytest.mark.parametrize(
    "damage",
    [
        "answer",
        "assessment",
        "event",
        "result",
        "deadline",
        "journal",
        "omission",
        "slot",
        "unknown_send",
        "completed_answer",
        "second_preparation",
        "symlink",
    ],
)
def test_recovery_refuses_changed_or_unapproved_evidence(tmp_path: Path, damage: str) -> None:
    store, args = fixture(tmp_path)
    manifest = json.loads(args["manifest"])
    with store:
        if damage in {"answer", "assessment", "unknown_send", "completed_answer"}:
            if damage == "answer":
                owner = json.loads(args["owner"])
                owner["answer"] = "No"
                args["owner"] = _json(owner)
                manifest["owner_authorization_sha256"] = _digest(args["owner"])
            else:
                assessment = json.loads(args["assessment"])
                if damage == "unknown_send":
                    assessment["reader"]["followup_provider_reservation_present"] = True
                if damage == "completed_answer":
                    assessment["reader"]["completed_final_answers"] = 1
                if damage == "assessment":
                    assessment["formation"]["case"] = "foreign"
                args["assessment"] = _json(assessment)
                manifest["assessment_sha256"] = _digest(args["assessment"])
        elif damage == "omission":
            manifest["artifacts"].pop("deadline.json")
        elif damage == "slot":
            manifest["claims"][1]["key"][1] = "automatic"
        elif damage == "second_preparation":
            manifest["claims"][1] = manifest["claims"][0]
        elif damage == "symlink":
            (store.directory / "recovery").symlink_to(tmp_path, target_is_directory=True)
        else:
            name = {
                "event": "events/000001.json",
                "result": next(n for n in manifest["artifacts"] if n.startswith("results/")),
                "deadline": "deadline.json",
                "journal": "budget/000001.jsonl",
            }[damage]
            (store.directory / name).write_bytes(b"{}\n")
        args["manifest"] = _json(manifest)
        with pytest.raises(ValueError):
            admit_bounded_recovery(store, **args)
        assert PREPARATION not in store.pending() and READER not in store.pending()


def test_approved_retry_failure_stops_every_further_admission(tmp_path: Path) -> None:
    store, args = fixture(tmp_path)
    with store:
        admit_bounded_recovery(store, **args)
        store.begin(PREPARATION)
        store.fail(
            PREPARATION, stage="formation", error_class="CancelledError", ledger_sha256="f" * 64
        )
        assert not store.pending()
        for key in (PREPARATION, READER, required_slots(store.plan.scenarios)[9]):
            with pytest.raises(ValueError):
                store.begin(key)
        with pytest.raises(ValueError):
            admit_bounded_recovery(store, **args)


def test_crash_between_authorized_events_is_recoverable_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, args = fixture(tmp_path)
    with store:
        original = store._record

        def interrupt(action: str, key: Slot, **details: object) -> None:
            if action == "bounded_replacement" and key == READER:
                raise OSError("Interrupted publication")
            original(action, key, **details)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_record", interrupt)
            with pytest.raises(OSError):
                admit_bounded_recovery(store, **args)
    with CheckpointStore(store.directory, store.plan) as reopened:
        admit_bounded_recovery(reopened, **args)
        assert PREPARATION in reopened.pending() and READER in reopened.pending()
        assert (
            len(list((store.directory / "events").iterdir()))
            == json.loads(args["manifest"])["history_events"] + 2
        )


def test_successor_protocol_preserves_absolute_deadline(tmp_path: Path) -> None:
    from agent_core.evals.general_recall_checkpoint_runtime import ContinuationDeadline

    store, args = fixture(tmp_path)
    with store:
        original = _read(store.directory / "deadline.json")
        admit_bounded_recovery(store, **args)
        instant = datetime(2026, 10, 19, tzinfo=UTC)
        deadline = ContinuationDeadline(
            store,
            protocol_sha256=args["protocol_sha256"],
            now=lambda: instant,
            monotonic=lambda: 10.0,
        )
        assert deadline.value.protocol_sha256 == "d" * 64
        assert 5 * 86400 < deadline.remaining() < 7 * 86400
        assert _read(store.directory / "deadline.json") == original
        with pytest.raises(ValueError):
            ContinuationDeadline(
                store, protocol_sha256="f" * 64, now=lambda: instant, monotonic=lambda: 10.0
            )
        with pytest.raises(TimeoutError):
            ContinuationDeadline(
                store,
                protocol_sha256=args["protocol_sha256"],
                now=lambda: instant + timedelta(days=7),
                monotonic=lambda: 10.0,
            )


def test_final_preparation_uses_a_distinct_bank_and_preserves_failed_bank(tmp_path: Path) -> None:
    from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent

    store, args = fixture(tmp_path)
    parent = CheckpointParent.__new__(CheckpointParent)
    parent.by_id = {PREPARATION[0]: None}  # type: ignore[dict-item]
    parent.banks = {}
    parent.pending_banks = {PREPARATION[0]: tmp_path / "original-pending"}
    with store:
        previous = parent.bank_path(store, PREPARATION[0])
        pending = Path(str(previous) + ".pending.jsonl")
        old = pending.read_bytes()
        admit_bounded_recovery(store, **args)
        final = parent.bank_path(store, PREPARATION[0])
        assert final != previous and final.name.endswith(".final-replacement.json")
        assert not final.exists() and pending.read_bytes() == old


def test_precreated_recovery_directory_cannot_hide_omitted_original(tmp_path: Path) -> None:
    store, args = fixture(tmp_path)
    with store:
        (store.directory / "budget/anchor.json").write_bytes(b"Immutable budget anchor")
        (store.directory / "recovery").mkdir()
        with pytest.raises(ValueError, match="inventory"):
            admit_bounded_recovery(store, **args)
