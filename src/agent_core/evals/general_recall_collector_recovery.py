"""Evidence-bound recovery after the collector lost terminal bookkeeping.

This retains the original stopped segment, derives only facts present in durable
records, and permits one changed-collector successor under the retained owner
repair instruction. It never reconstructs a missing response or elapsed time.
"""

import json
import re
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field

from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    Slot,
    _digest,
    _json,
    _read,
    _sync,
    _write,
)
from agent_core.evals.general_recall_continuation_budget import _journal, account_ledger
from agent_core.evals.general_recall_formation_repair import (
    repair_inventory,
    retained_formation_repair,
)
from agent_core.memory.general_recall_evidence import _unique_object


class CollectorRecovery(ReleaseValue):
    version: Literal["recall-collector-recovery@1"]
    plan_sha256: Digest
    predecessor_protocol_sha256: Digest
    history_events: int = Field(gt=0, strict=True)
    history_sha256: Digest
    artifacts: dict[str, Digest]
    owner_sha256: Digest
    assessment_sha256: Digest
    completed_rows: int = Field(gt=0, lt=1600, strict=True)
    segment_first_event: int = Field(gt=0, strict=True)
    formation_key: Slot
    formation_started: int = Field(gt=0, strict=True)
    reader_key: Slot
    reader_started: int = Field(gt=0, strict=True)
    ledger_file: str
    bank_file: str


def _load(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("Collector recovery: " + reason)


def collector_inventory(store: CheckpointStore) -> dict[str, str]:
    inventory = repair_inventory(store)
    for name in ("formation-repair", "provider-terminals", "unsealed-segments", "cleanup-failures"):
        folder = store.directory / name
        if folder.exists() or folder.is_symlink():
            _require(folder.is_dir() and not folder.is_symlink(), "unsafe evidence directory")
            inventory.update({name + "/" + p.name: _digest(_read(p)) for p in folder.iterdir()})
    return inventory


def _evidence(store: CheckpointStore, proof: CollectorRecovery, name: str) -> bytes:
    p = PurePosixPath(name)
    _require(
        not p.is_absolute() and str(p) == name and not set(p.parts) & {".", "..", ".lock"},
        "unsafe original path",
    )
    path = store.directory / name
    _require(
        name in proof.artifacts and not any(p.is_symlink() for p in (path, *path.parents)),
        "unbound or unsafe original",
    )
    raw = _journal(path) if path.suffix == ".jsonl" else _read(path)
    _require(_digest(raw) == proof.artifacts[name], "original artifact changed")
    return raw


def _report(store: CheckpointStore, proof: CollectorRecovery) -> dict[str, Any]:
    attempts: dict[Slot, dict[str, Any]] = {}
    for index in range(proof.segment_first_event, proof.history_events + 1):
        event = _load(_evidence(store, proof, f"events/{index:06d}.json"))
        key = tuple(event["key"])
        _require(event["index"] == index, "event index changed")
        if event["action"] == "started":
            _require(key not in attempts, "duplicate segment start")
            attempts[key] = {
                "key": key,
                "state": "unresolved",
                "started_event": index,
                "wall_seconds": None,
            }
        else:
            _require(
                event["action"] == "completed"
                and key in attempts
                and attempts[key]["state"] == "unresolved",
                "unrecognized stopped-segment outcome",
            )
            name = "results/" + _digest(_json(key)) + "-" + event["result_sha256"] + ".json"
            _require(
                _digest(_evidence(store, proof, name)) == event["result_sha256"],
                "completed row differs from its event",
            )
            attempts[key].update(
                state="completed", completed_event=index, result_sha256=event["result_sha256"]
            )
    unresolved = {key for key, value in attempts.items() if value["state"] == "unresolved"}
    _require(
        unresolved == {proof.formation_key, proof.reader_key},
        "unfinished slots differ from the assessment",
    )
    for key, index in (
        (proof.formation_key, proof.formation_started),
        (proof.reader_key, proof.reader_started),
    ):
        _require(attempts[key]["started_event"] == index, "original start differs")
    return {
        "version": "recall-continuation-reconciled@1",
        "derived_from_durable_events": True,
        "plan_sha256": proof.plan_sha256,
        "measurement_identity_sha256": store.plan.identity_sha256,
        "ledger_sha256": proof.artifacts[proof.ledger_file],
        "attempts": list(attempts.values()),
    }


def _proof(
    store: CheckpointStore, manifest: bytes, owner: bytes, assessment: bytes
) -> CollectorRecovery:
    proof = CollectorRecovery.model_validate(_load(manifest))
    decision, facts = _load(owner), _load(assessment)
    _require(
        proof.plan_sha256 == _digest(store.plan.encoded())
        and proof.owner_sha256 == _digest(owner)
        and proof.assessment_sha256 == _digest(assessment),
        "unbound plan, decision or assessment",
    )
    _require(
        decision.get("exact_owner_instruction")
        == (
            "I approve the formation repair, including runtime changes. Fix the root cause, "
            "preserve the 413 observations and raise the cap to $500 if necessary"
        )
        and decision.get("new_allocation_usd") == facts.get("new_allocation_usd") == "0"
        and facts.get("paid_work_active") is False
        and facts.get("original_process_gone") is True
        and facts.get("completed_observations") == proof.completed_rows
        and facts.get("all_new_calls_have_completed_cost_records") is True
        and facts.get("original_terminal_response_retained") is False
        and facts.get("other_incomplete_reader", {}).get("model_call_count") == 0
        and facts.get("other_incomplete_reader", {}).get("actual_runtime_failure_class")
        == "ContinuationHaltedError",
        "specific repair authority or stopped-process assessment is missing",
    )
    repair = retained_formation_repair(store)
    _require(
        repair is not None
        and repair[1]["successor_protocol_sha256"] == proof.predecessor_protocol_sha256,
        "unknown repaired predecessor",
    )
    for name in proof.artifacts:
        _evidence(store, proof, name)
    _require(
        {n for n in proof.artifacts if n.startswith("events/")}
        == {f"events/{i:06d}.json" for i in range(1, proof.history_events + 1)}
        and proof.artifacts[f"events/{proof.history_events:06d}.json"] == proof.history_sha256
        and sum(n.startswith("results/") for n in proof.artifacts) == proof.completed_rows
        and proof.formation_key != proof.reader_key
        and proof.formation_key[0] != proof.reader_key[0]
        and proof.formation_key[1:] == ("automatic", "formation", "sparse")
        and proof.segment_first_event <= min(proof.formation_started, proof.reader_started)
        and re.fullmatch(r"budget/[0-9]{6}\.jsonl", proof.ledger_file) is not None,
        "incomplete inventory or foreign slot",
    )
    assert repair is not None
    _require(
        proof.completed_rows >= repair[0].completed_rows and proof.formation_key == repair[0].key,
        "completed rows lost or unrelated preparation",
    )
    _require(
        proof.bank_file
        == "banks/"
        + _digest(proof.formation_key[0].encode())
        + ".deadline-repair.json.pending.jsonl"
        and proof.bank_file.removesuffix(".pending.jsonl") not in proof.artifacts,
        "failed preparation bank is not the original pending bank",
    )
    _evidence(store, proof, proof.bank_file)
    admission = _load(
        _evidence(
            store, proof, str(PurePosixPath(proof.ledger_file).with_suffix(".admission.json"))
        )
    )
    ledger = _evidence(store, proof, proof.ledger_file)
    account = account_ledger(ledger, maximum=Decimal(admission["maximum_usd"]))
    _require(
        not account.overrun
        and not account.unresolved
        and account.calls == facts.get("provider_calls_in_new_segment"),
        "new segment has unknown calls or differs from assessment",
    )
    records = [_load(line) for line in ledger.splitlines()]
    _require(
        any(tuple(r.get("slot", ())) == proof.formation_key for r in records)
        and not any(tuple(r.get("slot", ())) == proof.reader_key for r in records),
        "reader may have sent work or formation has no journal",
    )
    for name in proof.artifacts:
        if name.startswith("results/"):
            row = _load(_evidence(store, proof, name))
            _require(
                row.get("scenario_id") != proof.formation_key[0],
                "completed formation cannot be repeated",
            )
    report = _report(store, proof)
    keys = {tuple(a["key"]) for a in report["attempts"]}
    _require(
        all(tuple(r.get("slot", ())) in keys for r in records),
        "provider journal has an unrecorded slot",
    )
    return proof


def retained_collector_recovery(
    store: CheckpointStore,
) -> tuple[CollectorRecovery, dict[str, Any]] | None:
    directory = store.directory / "collector-recovery"
    if not directory.exists() and not directory.is_symlink():
        return None
    _require(
        directory.is_dir()
        and not directory.is_symlink()
        and {p.name for p in directory.iterdir()}
        == {
            "manifest.json",
            "owner.json",
            "assessment.json",
            "transition.json",
            "reconciled-segment.json",
        },
        "incomplete retained recovery",
    )
    proof = _proof(
        store,
        _read(directory / "manifest.json"),
        _read(directory / "owner.json"),
        _read(directory / "assessment.json"),
    )
    transition = _load(_read(directory / "transition.json"))
    _require(
        transition["manifest_sha256"] == _digest(_read(directory / "manifest.json"))
        and transition["predecessor_protocol_sha256"] == proof.predecessor_protocol_sha256
        and transition["first_segment"] == int(PurePosixPath(proof.ledger_file).stem) + 1
        and transition["deadline_sha256"] == proof.artifacts["deadline.json"],
        "recovery transition changed",
    )
    _require(
        _read(directory / "reconciled-segment.json") == _json(_report(store, proof)),
        "derived event provenance changed",
    )
    return proof, transition


def validate_collector_recovery_event(store: CheckpointStore, key: Slot, digest: str) -> None:
    retained = retained_collector_recovery(store)
    _require(retained is not None, "missing repair proof")
    assert retained is not None
    proof, transition = retained
    starts = {proof.formation_key: proof.formation_started, proof.reader_key: proof.reader_started}
    previous = store._states.get(key)
    _require(
        key in starts
        and previous is not None
        and previous.index == starts[key]
        and previous.action == "started"
        and key not in store._collector_repaired
        and digest == transition["manifest_sha256"]
        and (store._replaying or not store._retained(key)),
        "repair is consumed, completed or not the proved start",
    )


def admit_collector_recovery(
    store: CheckpointStore,
    *,
    manifest: bytes,
    owner: bytes,
    assessment: bytes,
    protocol_sha256: str,
) -> None:
    store._owned()
    proof = _proof(store, manifest, owner, assessment)
    _require(
        re.fullmatch(r"[0-9a-f]{64}", protocol_sha256) is not None
        and protocol_sha256 != proof.predecessor_protocol_sha256,
        "invalid successor protocol",
    )
    directory = store.directory / "collector-recovery"
    if store._count == proof.history_events:
        _require(
            store._previous == proof.history_sha256
            and collector_inventory(store) == proof.artifacts,
            "original inventory changed before admission",
        )
    elif not directory.exists():
        raise ValueError("Collector recovery: history changed before admission")
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _sync(store.directory)
    _require(directory.is_dir() and not directory.is_symlink(), "unsafe recovery directory")
    transition = _json(
        {
            "version": "recall-collector-transition@1",
            "manifest_sha256": _digest(manifest),
            "predecessor_protocol_sha256": proof.predecessor_protocol_sha256,
            "successor_protocol_sha256": protocol_sha256,
            "deadline_sha256": proof.artifacts["deadline.json"],
            "first_segment": int(PurePosixPath(proof.ledger_file).stem) + 1,
        }
    )
    for name, raw in (
        ("manifest.json", manifest),
        ("owner.json", owner),
        ("assessment.json", assessment),
        ("transition.json", transition),
        ("reconciled-segment.json", _json(_report(store, proof))),
    ):
        path = directory / name
        if path.exists():
            _require(_read(path) == raw, "retained recovery changed")
        else:
            _write(path, raw)
    for key in (proof.formation_key, proof.reader_key):
        if key not in store._collector_repaired:
            store._record("collector_repair", key, source_sha256=_digest(manifest))
        else:
            _require(
                store._states[key].action in {"collector_repair", "completed"},
                "repaired slot failed or has an unknown outcome",
            )
    # A new derived seal supplements the immutable stopped journal. It never
    # replaces a journal or claims that the original process sealed successfully.
    ledger = _evidence(store, proof, proof.ledger_file)
    admission = _load(
        _evidence(
            store, proof, str(PurePosixPath(proof.ledger_file).with_suffix(".admission.json"))
        )
    )
    account = account_ledger(ledger, maximum=Decimal(admission["maximum_usd"]))
    seal = _json(
        {
            "journal_sha256": _digest(ledger),
            "reported_usd": str(account.reported),
            "held_usd": str(account.held),
        }
    )
    path = (store.directory / proof.ledger_file).with_suffix(".sealed.json")
    if path.exists():
        _require(_read(path) == seal, "reconciled seal changed")
    else:
        _write(path, seal)
