"""Verify the explicitly authorized formation deadline repair and its provenance."""

import hashlib
import json
import re
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
from agent_core.evals.general_recall_checkpoint_recovery import (
    recovery_inventory,
    retained_recovery,
)
from agent_core.evals.general_recall_continuation_budget import _journal, account_ledger
from agent_core.memory.general_recall_evidence import _unique_object

# Exact runtime sources whose request equivalence is covered by ADR-0214 and
# the progressing-stream regression. Any further edit requires new evidence.
ORIGINAL_SOURCE_SHA256 = "e7ce0e0f0e93f2279126ba8a7d6f9e554222c22f2c9fd074836ed48c2bad6823"
REPAIRED_SOURCE_SHA256 = "110141418041c582f635dcb25ae22bb3088d748e00227424060c4da1b3e6a1a7"


def verify_deadline_source(original: bytes, repaired: bytes) -> None:
    """Refuse any runtime change beyond the declared deadline and safe audit repair."""
    if (
        hashlib.sha256(original).hexdigest() != ORIGINAL_SOURCE_SHA256
        or hashlib.sha256(repaired).hexdigest() != REPAIRED_SOURCE_SHA256
    ):
        raise ValueError("Unverified formation deadline repair source")


class FormationRepair(ReleaseValue):
    version: Literal["recall-formation-deadline-repair@1"]
    plan_sha256: Digest
    predecessor_protocol_sha256: Digest
    history_events: int = Field(gt=0, strict=True)
    history_sha256: Digest
    artifacts: dict[str, Digest]
    owner_sha256: Digest
    assessment_sha256: Digest
    completed_rows: int = Field(gt=0, lt=1600, strict=True)
    key: Slot
    failed_event: int = Field(gt=0, strict=True)
    ledger_file: str
    report_file: str
    bank_file: str
    original_source_sha256: Digest
    repaired_source_sha256: Digest


def _load(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("Formation deadline repair: " + reason)


def repair_inventory(store: CheckpointStore) -> dict[str, str]:
    inventory = recovery_inventory(store.directory)
    recovery = store.directory / "recovery"
    _require(recovery.is_dir() and not recovery.is_symlink(), "missing predecessor recovery")
    inventory.update({"recovery/" + p.name: _digest(_read(p)) for p in recovery.iterdir()})
    return inventory


def _evidence(store: CheckpointStore, proof: FormationRepair, name: str) -> bytes:
    path = PurePosixPath(name)
    _require(
        not path.is_absolute()
        and path.as_posix() == name
        and not set(path.parts) & {".", "..", ".lock"},
        "unsafe artifact name",
    )
    p = store.directory / name
    _require(
        name in proof.artifacts and not any(q.is_symlink() for q in (p, *p.parents)),
        "unbound or unsafe original",
    )
    raw = _journal(p) if p.suffix == ".jsonl" else _read(p)
    _require(_digest(raw) == proof.artifacts[name], "original artifact changed")
    return raw


def _proof(
    store: CheckpointStore,
    manifest: bytes,
    owner: bytes,
    assessment: bytes,
    original_source: bytes,
    repaired_source: bytes,
) -> FormationRepair:
    proof = FormationRepair.model_validate(_load(manifest))
    verify_deadline_source(original_source, repaired_source)
    decision, facts = _load(owner), _load(assessment)
    _require(
        proof.plan_sha256 == _digest(store.plan.encoded())
        and proof.original_source_sha256 == _digest(original_source)
        and proof.repaired_source_sha256 == _digest(repaired_source)
        and proof.owner_sha256 == _digest(owner)
        and proof.assessment_sha256 == _digest(assessment),
        "source, plan or owner evidence changed",
    )
    _require(
        decision.get("exact_owner_instruction")
        == (
            "I approve the formation repair, including runtime changes. "
            "Fix the root cause, preserve the 413 observations "
            "and raise the cap to $500 if necessary"
        )
        and decision.get("completed_observations")
        == facts.get("completed_rows_preserved")
        == proof.completed_rows
        and decision.get("new_allocation_usd") == "0"
        and facts.get("provider_calls_this_action") == 0,
        "specific repair authority is missing",
    )
    predecessor = retained_recovery(store)
    _require(
        predecessor is not None
        and predecessor[1]["successor_protocol_sha256"] == proof.predecessor_protocol_sha256,
        "unknown predecessor protocol",
    )
    for name in proof.artifacts:
        _evidence(store, proof, name)
    _require(
        _evidence(store, proof, "plan.json") == store.plan.encoded()
        and {n for n in proof.artifacts if n.startswith("events/")}
        == {f"events/{i:06d}.json" for i in range(1, proof.history_events + 1)}
        and proof.artifacts[f"events/{proof.history_events:06d}.json"] == proof.history_sha256
        and sum(n.startswith("results/") for n in proof.artifacts) == proof.completed_rows,
        "incomplete original inventory",
    )
    original = _load(_evidence(store, proof, f"events/{proof.failed_event:06d}.json"))
    ledger = _evidence(store, proof, proof.ledger_file)
    account = account_ledger(ledger, maximum=store.plan.allowance_usd)
    sealed = _load(
        _evidence(store, proof, str(PurePosixPath(proof.ledger_file).with_suffix(".sealed.json")))
    )
    _require(
        not account.overrun
        and sealed
        == {
            "journal_sha256": _digest(ledger),
            "reported_usd": str(account.reported),
            "held_usd": str(account.held),
        },
        "unsealed or overrun predecessor",
    )
    _require(
        tuple(original["key"]) == proof.key
        and original["action"] == "failed"
        and original["stage"] == "formation"
        and original["error_class"] in {"CancelledError", "TimeoutError"}
        and original["ledger_sha256"] == _digest(ledger)
        and proof.key[1:] == ("automatic", "formation", "sparse"),
        "not an eligible formation interruption",
    )
    report = _load(_evidence(store, proof, proof.report_file))
    attempts = [a for a in report["attempts"] if tuple(a["key"]) == proof.key]
    _require(
        report["ledger_sha256"] == _digest(ledger)
        and len(attempts) == 1
        and attempts[0]["state"] == "failed"
        and attempts[0]["stage"] == "formation"
        and attempts[0]["error_class"] == original["error_class"],
        "terminal report mismatch",
    )
    _require(
        proof.bank_file
        == "banks/" + _digest(proof.key[0].encode()) + ".final-replacement.json.pending.jsonl",
        "not the failed preparation bank",
    )
    _evidence(store, proof, proof.bank_file)
    _require(
        proof.bank_file.removesuffix(".pending.jsonl") not in proof.artifacts,
        "a completed bank cannot be replaced",
    )
    return proof


def retained_formation_repair(
    store: CheckpointStore,
) -> tuple[FormationRepair, dict[str, Any]] | None:
    directory = store.directory / "formation-repair"
    if not directory.exists() and not directory.is_symlink():
        return None
    _require(directory.is_dir() and not directory.is_symlink(), "unsafe repair directory")
    _require(
        {p.name for p in directory.iterdir()}
        == {
            "manifest.json",
            "owner.json",
            "assessment.json",
            "original-source.py",
            "repaired-source.py",
            "transition.json",
        },
        "incomplete repair evidence",
    )
    proof = _proof(
        store,
        _read(directory / "manifest.json"),
        _read(directory / "owner.json"),
        _read(directory / "assessment.json"),
        _read(directory / "original-source.py"),
        _read(directory / "repaired-source.py"),
    )
    transition = _load(_read(directory / "transition.json"))
    _require(
        transition.get("version") == "recall-formation-protocol-transition@1"
        and transition.get("manifest_sha256") == _digest(_read(directory / "manifest.json"))
        and transition.get("predecessor_protocol_sha256") == proof.predecessor_protocol_sha256
        and transition.get("deadline_sha256") == proof.artifacts["deadline.json"]
        and transition.get("first_segment")
        == 1 + sum(n.startswith("budget/") and n.endswith(".jsonl") for n in proof.artifacts)
        and re.fullmatch(r"[0-9a-f]{64}", transition.get("successor_protocol_sha256", ""))
        is not None
        and transition["successor_protocol_sha256"] != proof.predecessor_protocol_sha256,
        "changed protocol transition",
    )
    return proof, transition


def validate_formation_repair_event(store: CheckpointStore, key: Slot, source_sha256: str) -> None:
    retained = retained_formation_repair(store)
    _require(retained is not None, "missing retained repair")
    assert retained is not None
    proof, transition = retained
    previous = store._states.get(key)
    _require(
        key == proof.key
        and source_sha256 == transition["manifest_sha256"]
        and previous is not None
        and previous.index == proof.failed_event
        and previous.action == "failed"
        and key in store._bounded_replaced
        and key not in store._formation_repaired
        and (store._replaying or not store._retained(key))
        and not any(k != key and k[0] == key[0] for k in store._states),
        "repair consumed or completed/unknown answer exists",
    )


def admit_formation_repair(
    store: CheckpointStore,
    *,
    manifest: bytes,
    owner: bytes,
    assessment: bytes,
    original_source: bytes,
    repaired_source: bytes,
    protocol_sha256: str,
) -> None:
    store._owned()
    proof = _proof(store, manifest, owner, assessment, original_source, repaired_source)
    _require(
        re.fullmatch(r"[0-9a-f]{64}", protocol_sha256) is not None
        and protocol_sha256 != proof.predecessor_protocol_sha256,
        "invalid successor protocol",
    )
    directory = store.directory / "formation-repair"
    if store._count == proof.history_events:
        _require(
            store._previous == proof.history_sha256 and repair_inventory(store) == proof.artifacts,
            "missing or changed original inventory",
        )
    elif not directory.exists():
        raise ValueError("Formation deadline repair: history changed before admission")
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _sync(store.directory)
    _require(directory.is_dir() and not directory.is_symlink(), "unsafe repair directory")
    transition = _json(
        {
            "version": "recall-formation-protocol-transition@1",
            "manifest_sha256": _digest(manifest),
            "predecessor_protocol_sha256": proof.predecessor_protocol_sha256,
            "successor_protocol_sha256": protocol_sha256,
            "deadline_sha256": proof.artifacts["deadline.json"],
            "first_segment": 1
            + sum(n.startswith("budget/") and n.endswith(".jsonl") for n in proof.artifacts),
        }
    )
    for name, raw in [
        ("manifest.json", manifest),
        ("owner.json", owner),
        ("assessment.json", assessment),
        ("original-source.py", original_source),
        ("repaired-source.py", repaired_source),
        ("transition.json", transition),
    ]:
        p = directory / name
        if p.exists():
            _require(_read(p) == raw, "retained repair changed")
        else:
            _write(p, raw)
    if proof.key not in store._formation_repaired:
        store._record("formation_repair", proof.key, source_sha256=_digest(manifest))
    else:
        _require(
            store._states[proof.key].action in {"formation_repair", "completed"},
            "repaired preparation failed or outcome is unknown",
        )


def execution_claim(store: CheckpointStore) -> dict[str, str] | None:
    retained = retained_formation_repair(store)
    if retained is None:
        return None
    proof, transition = retained
    value = {
        "version": "recall-formation-execution-repair@1",
        "base_identity_sha256": store.plan.identity_sha256,
        "repair_manifest_sha256": transition["manifest_sha256"],
        "original_source_sha256": proof.original_source_sha256,
        "repaired_source_sha256": proof.repaired_source_sha256,
    }
    return {**value, "execution_identity_sha256": _digest(_json(value))}


def validate_execution_claim(
    claim: dict[str, str] | None, row: dict[str, Any], *, repaired: bool
) -> None:
    expected = claim if repaired else None
    _require(
        (not repaired or expected is not None) and row.get("execution_repair") == expected,
        "row execution provenance is missing, changed or retroactively applied",
    )


def validate_repair_lineage(bundle: Any, manifest: dict[str, Any]) -> None:
    repair = manifest.get("formation_repair_sha256")
    _require(
        bundle.lineage.formation_repair_sha256 == repair
        and (
            repair is None
            or (
                manifest.get("version") == "recall-checkpoint-collection@2"
                and bundle.version == "general-recall-checkpoint-release@2"
            )
        ),
        "publication must retain the separate repaired runtime cohort",
    )
