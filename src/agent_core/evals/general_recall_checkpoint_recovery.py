"""ADR-0213: two explicitly approved, evidence-bound operational replacements.

The retained owner decision and offline assessment establish eligibility. This
module verifies their exact evidence and enforces single use; it does not infer a
human decision, reconstruct an answer, forgive usage or change evaluated code.
"""

import json
import re
from pathlib import Path, PurePosixPath
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
from agent_core.evals.general_recall_continuation_budget import _Entry, _journal, account_ledger
from agent_core.memory.general_recall_evidence import _unique_object


class RecoveryClaim(ReleaseValue):
    key: Slot
    stage: Literal["formation", "answer"]
    failed_event: int = Field(gt=0, strict=True)
    failed_event_sha256: Digest
    ledger_file: str
    report_file: str
    bank_file: str | None


class RecoveryManifest(ReleaseValue):
    version: Literal["recall-bounded-recovery@1"]
    plan_sha256: Digest
    parent_archive_sha256: Digest
    predecessor_protocol_sha256: Digest
    history_sha256: Digest
    history_events: int = Field(gt=0, strict=True)
    owner_authorization_sha256: Digest
    assessment_sha256: Digest
    artifacts: dict[str, Digest]
    claims: tuple[RecoveryClaim, RecoveryClaim]


def _load(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("Bounded recovery: " + reason)


def recovery_inventory(directory: Path) -> dict[str, str]:
    """Inventory immutable state, including failed banks and every old charge."""
    paths = [directory / n for n in ("plan.json", "deadline.json", "parent-import.json")]
    for name in (
        "events",
        "results",
        "banks",
        "failures",
        "segments",
        "budget",
        "operator-segments",
    ):
        folder = directory / name
        if folder.exists():
            _require(folder.is_dir() and not folder.is_symlink(), "unsafe evidence directory")
            paths.extend(p for p in folder.iterdir() if p.name != ".lock")
    return {
        p.relative_to(directory).as_posix(): _digest(
            _journal(p) if p.suffix == ".jsonl" else _read(p)
        )
        for p in paths
    }


def _evidence(store: CheckpointStore, proof: RecoveryManifest, name: str) -> bytes:
    path = PurePosixPath(name)
    _require(
        not path.is_absolute()
        and path.as_posix() == name
        and not set(path.parts) & {".", "..", ".lock"},
        "unsafe evidence path",
    )
    target = store.directory / name
    _require(
        name in proof.artifacts and not any(p.is_symlink() for p in (target, *target.parents)),
        "unbound evidence",
    )
    raw = _journal(target) if target.suffix == ".jsonl" else _read(target)
    _require(_digest(raw) == proof.artifacts[name], "original evidence changed")
    return raw


def _validate_proof(
    store: CheckpointStore, raw: bytes, owner: bytes, assessment: bytes
) -> RecoveryManifest:
    proof = RecoveryManifest.model_validate(_load(raw))
    decision, facts = _load(owner), _load(assessment)
    _require(
        proof.plan_sha256 == _digest(store.plan.encoded())
        and proof.parent_archive_sha256 == store.plan.parent_archive_sha256
        and _digest(owner) == proof.owner_authorization_sha256
        and _digest(assessment) == proof.assessment_sha256
        and decision.get("assessment_sha256") == proof.assessment_sha256
        and decision.get("answer") == "Approve the two bounded retries (Recommended)"
        and decision.get("required_rows") == 1600
        and decision.get("aggregate_cap_usd") == "300"
        and decision.get("new_allocation_usd") == "0"
        and decision.get("stop_if_either_recovery_fails") is True,
        "owner decision or unchanged limits are not bound",
    )
    for name in proof.artifacts:
        _evidence(store, proof, name)
    _require(
        _load(_evidence(store, proof, "deadline.json"))["protocol_sha256"]
        == proof.predecessor_protocol_sha256
        and _evidence(store, proof, "plan.json") == store.plan.encoded()
        and {n for n in proof.artifacts if n.startswith("events/")}
        == {f"events/{i:06d}.json" for i in range(1, proof.history_events + 1)}
        and proof.artifacts[f"events/{proof.history_events:06d}.json"] == proof.history_sha256
        and facts.get("event_chain_tip") == proof.history_sha256
        and facts.get("events_verified") == proof.history_events
        and facts.get("provider_calls_this_assessment") == 0
        and facts.get("database_writes") is False
        and decision.get("completed_rows_preserved")
        == facts.get("completed_observations_preserved")
        == sum(n.startswith("results/") for n in proof.artifacts),
        "checkpoint, deadline or completed-row inventory changed",
    )
    _require(
        tuple(c.stage for c in proof.claims) == ("formation", "answer")
        and proof.claims[0].key[0] != proof.claims[1].key[0],
        "exactly two distinct exceptions are required",
    )
    for claim in proof.claims:
        original = _load(_evidence(store, proof, f"events/{claim.failed_event:06d}.json"))
        ledger = _evidence(store, proof, claim.ledger_file)
        _require(
            original["action"] == "failed"
            and original["stage"] == claim.stage
            and tuple(original["key"]) == claim.key
            and proof.artifacts[f"events/{claim.failed_event:06d}.json"]
            == claim.failed_event_sha256
            and original["ledger_sha256"] == _digest(ledger),
            "failure is not the approved original",
        )
        account = account_ledger(ledger, maximum=store.plan.allowance_usd)
        sealed = _load(
            _evidence(
                store, proof, str(PurePosixPath(claim.ledger_file).with_suffix(".sealed.json"))
            )
        )
        _require(
            not account.overrun
            and sealed
            == {
                "journal_sha256": _digest(ledger),
                "reported_usd": str(account.reported),
                "held_usd": str(account.held),
            },
            "predecessor ledger is not sealed",
        )
        report = _load(_evidence(store, proof, claim.report_file))
        attempts = [a for a in report["attempts"] if tuple(a["key"]) == claim.key]
        _require(
            report["ledger_sha256"] == _digest(ledger)
            and len(attempts) == 1
            and attempts[0]["state"] == "failed"
            and attempts[0]["stage"] == claim.stage
            and attempts[0]["error_class"] == original["error_class"],
            "failure differs from its segment report",
        )
        entries = [_Entry.model_validate_json(line) for line in ledger.splitlines()]
        slot_entries = [e for e in entries if e.slot == claim.key]
        reserved = [e for e in slot_entries if e.outcome == "reserved"]
        settled = [e for e in slot_entries if e.outcome == "completed"]
        if claim.stage == "formation":
            formation = facts["formation"]
            _require(
                claim.key == (formation["case"], "automatic", "formation", "sparse")
                and original["error_class"] in {"CancelledError", "TimeoutError"}
                and claim.bank_file is not None
                and claim.bank_file
                == "banks/" + _digest(claim.key[0].encode()) + ".replacement.json.pending.jsonl"
                and _digest(_evidence(store, proof, claim.bank_file))
                == formation["pending_bank_sha256"]
                and formation["sealed_bank_exists"] is False
                and formation["last_request_outcome"] == "uncertain_cancelled"
                and len(reserved) == len(settled) + 1
                and sum(
                    e.outcome == "uncertain" and e.error_class in {"CancelledError", "TimeoutError"}
                    for e in slot_entries
                )
                == 1,
                "final preparation is not the assessed cancellation",
            )
        else:
            reader = facts["reader"]
            _require(
                tuple(reader["slot"]) == claim.key
                and claim.bank_file is None
                and original["error_class"] == "ValueError"
                and reader["completed_final_answers"] == 0
                and reader["followup_provider_reservation_present"] is False
                and reader["checkpoint_is_terminal_failed"] is True
                and len(reserved)
                == len(settled)
                == reader["provider_journal_requests"]
                == reader["completed_provider_turns"]
                == 1
                and len(slot_entries) == 2,
                "reader follow-up is not proved unsent",
            )
    return proof


def retained_recovery(store: CheckpointStore) -> tuple[RecoveryManifest, dict[str, Any]] | None:
    """Validate retained authority and predecessor bytes without changing state."""
    directory = store.directory / "recovery"
    if not directory.exists() and not directory.is_symlink():
        return None
    _require(directory.is_dir() and not directory.is_symlink(), "unsafe recovery directory")
    proof = _validate_proof(
        store,
        _read(directory / "manifest.json"),
        _read(directory / "owner.json"),
        _read(directory / "assessment.json"),
    )
    transition = _load(_read(directory / "transition.json"))
    count = sum(n.startswith("budget/") and n.endswith(".jsonl") for n in proof.artifacts)
    _require(
        transition.get("version") == "recall-checkpoint-protocol-transition@1"
        and transition.get("manifest_sha256") == _digest(_read(directory / "manifest.json"))
        and transition.get("predecessor_protocol_sha256") == proof.predecessor_protocol_sha256
        and transition.get("deadline_sha256") == proof.artifacts["deadline.json"]
        and transition.get("first_segment") == count + 1
        and re.fullmatch(r"[0-9a-f]{64}", transition.get("successor_protocol_sha256", ""))
        is not None
        and transition["successor_protocol_sha256"] != proof.predecessor_protocol_sha256,
        "protocol ancestry changed",
    )
    return proof, transition


def validate_recovery_event(store: CheckpointStore, key: Slot, source_sha256: str) -> None:
    retained = retained_recovery(store)
    _require(retained is not None, "missing owner authorization")
    assert retained is not None
    proof, _ = retained
    _require(
        _digest(_read(store.directory / "recovery/manifest.json")) == source_sha256,
        "recovery declaration changed",
    )
    claim = next((c for c in proof.claims if c.key == key), None)
    previous = store._states.get(key)
    _require(claim is not None and previous is not None, "unapproved replacement slot")
    assert claim is not None and previous is not None
    _require(
        previous.action == "failed"
        and previous.index == claim.failed_event
        and key not in store._bounded_replaced
        and (store._replaying or not store._retained(key))
        and (
            claim.stage != "formation"
            or (
                key[0] in store._replaced
                and not any(k[0] == key[0] and k != key for k in store._states)
            )
        ),
        "exception was consumed or failure is ineligible",
    )


def admit_bounded_recovery(
    store: CheckpointStore,
    *,
    manifest: bytes,
    owner: bytes,
    assessment: bytes,
    protocol_sha256: str,
) -> None:
    """Publish both exceptions crash-safely, without changing sealed originals."""
    store._owned()
    proof = _validate_proof(store, manifest, owner, assessment)
    _require(
        re.fullmatch(r"[0-9a-f]{64}", protocol_sha256) is not None
        and protocol_sha256 != proof.predecessor_protocol_sha256,
        "a separately pinned successor is required",
    )
    directory = store.directory / "recovery"
    if store._count == proof.history_events:
        _require(
            store._previous == proof.history_sha256
            and recovery_inventory(store.directory) == proof.artifacts,
            "original inventory is incomplete or changed",
        )
    elif not directory.exists():
        raise ValueError("Bounded recovery: original history changed before admission")
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _sync(store.directory)
    _require(directory.is_dir() and not directory.is_symlink(), "unsafe recovery directory")
    transition = _json(
        {
            "version": "recall-checkpoint-protocol-transition@1",
            "manifest_sha256": _digest(manifest),
            "predecessor_protocol_sha256": proof.predecessor_protocol_sha256,
            "successor_protocol_sha256": protocol_sha256,
            "deadline_sha256": proof.artifacts["deadline.json"],
            "first_segment": 1
            + sum(n.startswith("budget/") and n.endswith(".jsonl") for n in proof.artifacts),
        }
    )
    for name, raw in (
        ("manifest.json", manifest),
        ("owner.json", owner),
        ("assessment.json", assessment),
        ("transition.json", transition),
    ):
        path = directory / name
        if path.exists():
            _require(_read(path) == raw, "a retained recovery input changed")
        else:
            _write(path, raw)
    for claim in proof.claims:
        state = store._states.get(claim.key)
        _require(state is not None, "missing failed slot")
        assert state is not None
        if claim.key not in store._bounded_replaced:
            store._record("bounded_replacement", claim.key, source_sha256=_digest(manifest))
        else:
            _require(
                state.action in {"bounded_replacement", "completed"},
                "approved recovery already failed or has an unknown outcome",
            )


def protocol_ancestry(store: CheckpointStore, protocol_sha256: str) -> tuple[str, int]:
    """Return the immutable deadline protocol; every later epoch stays explicit."""
    from agent_core.evals.general_recall_formation_repair import retained_formation_repair

    repaired = retained_formation_repair(store)
    if repaired is not None:
        repair_proof, transition = repaired
        _require(
            protocol_sha256 == transition["successor_protocol_sha256"],
            "unadmitted repaired protocol",
        )
        protocol_sha256 = repair_proof.predecessor_protocol_sha256
    retained = retained_recovery(store)
    if retained is None:
        return protocol_sha256, 0
    proof, transition = retained
    _require(
        protocol_sha256 == transition["successor_protocol_sha256"], "unadmitted successor protocol"
    )
    return proof.predecessor_protocol_sha256, int(transition["first_segment"])


def protocol_for_segment(store: CheckpointStore, protocol_sha256: str, index: int) -> str:
    from agent_core.evals.general_recall_formation_repair import retained_formation_repair

    root, first = protocol_ancestry(store, protocol_sha256)
    repaired = retained_formation_repair(store)
    if repaired is not None and index < repaired[1]["first_segment"]:
        protocol_sha256 = repaired[0].predecessor_protocol_sha256
    return root if index < first else protocol_sha256
