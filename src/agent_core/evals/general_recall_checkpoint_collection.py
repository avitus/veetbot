"""Reconstruct the complete amended matrix and every immutable charge offline."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from agent_core.domain.general_recall_release import RecallObservation, RecallScenario
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    Slot,
    _digest,
    _Event,
    _json,
    _read,
    required_slots,
)
from agent_core.evals.general_recall_checkpoint_parent import (
    CheckpointParent,
    ParentPreparationApproval,
)
from agent_core.evals.general_recall_checkpoint_runtime import (
    WorkerAdmission,
    _Deadline,
    verify_worker_admissions,
)
from agent_core.evals.general_recall_collection import scenario_manifest
from agent_core.evals.general_recall_composition import system_clock
from agent_core.evals.general_recall_continuation_budget import (
    ParentLedger,
    _Entry,
    _journal,
    account_ledger,
)
from agent_core.evals.general_recall_formation_bank import _Bank
from agent_core.evals.general_recall_observation import ProviderAttempt, compile_runtime_observation
from agent_core.memory.general_recall_evidence import _unique_object


def _load(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("Checkpoint collection: " + reason)


def _parent_journal(parent: CheckpointParent) -> bytes:
    return parent.archive.read("provider-costs.jsonl")


def _history(store: CheckpointStore) -> list[dict[str, Any]]:
    """Recheck bytes against the chain held by the exclusive coordinator."""
    previous = _digest(store.plan.encoded())
    events = []
    for index, path in enumerate(sorted((store.directory / "events").iterdir()), 1):
        raw = _read(path)
        event = _Event.model_validate(_load(raw))
        _require(
            path.name == f"{index:06d}.json"
            and event.index == index
            and event.previous_sha256 == previous,
            "event history chain changed",
        )
        previous = _digest(raw)
        events.append(event.model_dump(mode="json"))
    _require(
        len(events) == store._count and previous == store._previous,
        "event history differs from the exclusive coordinator",
    )
    return events


def _inventory(store: CheckpointStore) -> dict[str, str]:
    paths = [
        store.directory / name for name in ("plan.json", "parent-import.json", "deadline.json")
    ]
    for name in (
        "events",
        "results",
        "banks",
        "failures",
        "segments",
        "budget",
        "operator-segments",
        "recovery",
        "formation-repair",
        "collector-recovery",
        "provider-terminals",
        "unsealed-segments",
        "cleanup-failures",
    ):
        directory = store.directory / name
        if not directory.exists():
            continue
        _require(directory.is_dir() and not directory.is_symlink(), "unsafe artifact directory")
        for path in directory.iterdir():
            if path.name == ".lock" and name == "budget":
                continue
            _require(not path.is_symlink() and path.is_file(), "unsafe or nested artifact")
            paths.append(path)
    return {
        p.relative_to(store.directory).as_posix(): _digest(
            _journal(p) if p.parent.name == "budget" and p.suffix == ".jsonl" else _read(p)
        )
        for p in sorted(paths)
    }


def _charge(entry: dict[str, Any]) -> tuple[str, str, str, Decimal]:
    return (
        entry["provider"],
        entry["model"],
        entry["request_sha256"],
        Decimal(entry["actual_cost_usd"]),
    )


def _observed(raw: dict[str, Any]) -> Counter[tuple[str, str, str, Decimal]]:
    attempts = (ProviderAttempt.model_validate(value) for value in raw.get("provider_attempts", []))
    return Counter((a.provider, a.model, a.request_sha256, a.usage.cost) for a in attempts)


@dataclass(frozen=True)
class CheckpointCollection:
    scenarios: tuple[RecallScenario, ...]
    observations: tuple[RecallObservation, ...]
    reported_usd: Decimal
    held_usd: Decimal
    manifest: bytes
    review_items: tuple[dict[str, Any], ...]


def compile_checkpoint_collection(
    store: CheckpointStore, parent: CheckpointParent, parents: tuple[ParentLedger, ...]
) -> CheckpointCollection:
    """All-or-nothing raw reconstruction; never score, select answers or activate.

    The operator supplies the pinned eight-ledger inventory and a parent verified
    against the frozen runtime. Extra charges and unknowns remain explicit review
    items; they are never assigned invented answer latencies or refunded. Hashes
    establish consistency, not human authorship or instrumentation authenticity.
    """
    store._owned()
    slots = required_slots(store.plan.scenarios)
    _require(store.completed() == slots, "the complete 1600-slot matrix is required")
    history = _history(store)
    before = _inventory(store)
    parent.archive.verify()
    _require(
        store.plan.identity_sha256 == parent.identity_sha256
        and store.plan.parent_archive_sha256 == parent.archive.sha256
        and store.plan.scenarios == tuple(parent.by_id)
        and store.plan.evaluated_inputs_sha256 == parent.identity.evaluated_inputs_sha256,
        "parent or plan identity changed",
    )
    imported = _load(_read(store.directory / "parent-import.json"))
    _require(
        imported
        == {
            "version": "recall-parent-import@1",
            "plan_sha256": _digest(store.plan.encoded()),
            "parent_archive_sha256": parent.archive.sha256,
            "measurement_identity_sha256": parent.identity_sha256,
            "parent_files": len(parent.archive.members),
            "original_rows": parent.total_rows,
            "selected_rows": len(parent.rows),
            "rows": _load(_json(parent.references)),
        },
        "original import declaration changed",
    )
    _require(
        {key for key, event in store._states.items() if event.action == "imported"}
        == set(parent.rows),
        "parent selection changed",
    )
    deadline = _Deadline.model_validate_json(_read(store.directory / "deadline.json"))
    _require(
        deadline.plan_sha256 == _digest(store.plan.encoded())
        and deadline.expires_at
        == deadline.started_at + timedelta(seconds=deadline.duration_seconds),
        "absolute deadline changed",
    )
    first_worker = WorkerAdmission.model_validate_json(
        _read(store.directory / "operator-segments/000001.json")
    )
    _require(
        len(first_worker.forbidden_databases) >= 2
        and len(set(first_worker.forbidden_databases)) == len(first_worker.forbidden_databases),
        "original scratch databases are not excluded",
    )
    from agent_core.evals.general_recall_checkpoint_recovery import retained_recovery

    recovery = retained_recovery(store)
    protocol_sha256 = (
        recovery[1]["successor_protocol_sha256"] if recovery else deadline.protocol_sha256
    )
    from agent_core.evals.general_recall_formation_repair import (
        execution_claim,
        retained_formation_repair,
        validate_execution_claim,
    )

    repair = retained_formation_repair(store)
    claim = execution_claim(store)
    if repair is not None:
        protocol_sha256 = repair[1]["successor_protocol_sha256"]
    from agent_core.evals.general_recall_collector_recovery import retained_collector_recovery

    collector_recovery = retained_collector_recovery(store)
    if collector_recovery is not None:
        protocol_sha256 = collector_recovery[1]["successor_protocol_sha256"]
    verify_worker_admissions(
        store,
        protocol_sha256=protocol_sha256,
        forbidden_databases=first_worker.forbidden_databases,
    )
    for path in (store.directory / "operator-segments").iterdir():
        worker_admission = WorkerAdmission.model_validate_json(_read(path))
        _require(
            worker_admission.effective_inputs_sha256 == _digest(_json(parent.inputs)),
            "worker effective inputs changed",
        )
    _require(
        len(parents) == 8 and len({p.name for p in parents}) == 8,
        "all eight distinct parent ledgers are required",
    )
    references = []
    reported = held = Decimal(0)
    review: list[dict[str, Any]] = []
    if repair is not None:
        review.append(
            {
                "kind": "owner_approved_formation_deadline_repair",
                "manifest": repair[0].model_dump(mode="json"),
                "transition": repair[1],
                "execution_repair": claim,
            }
        )
    if recovery is not None:
        review.append(
            {
                "kind": "owner_approved_bounded_recovery",
                "manifest": recovery[0].model_dump(mode="json"),
                "transition": recovery[1],
            }
        )
    for item in parents:
        _require(_digest(item.raw) == item.sha256, "parent ledger changed")
        account = account_ledger(item.raw, maximum=item.maximum)
        _require(not account.overrun, "parent overrun is not admitted")
        reported += account.reported
        held += account.held
        references.append({"name": item.name, "sha256": item.sha256, "maximum": str(item.maximum)})
        if account.unresolved:
            review.append(
                {
                    "kind": "parent_uncertainty",
                    "ledger_sha256": item.sha256,
                    "indices": account.unresolved,
                    "held_usd": str(account.held),
                }
            )
    _require(reported + held == store.plan.prior_exposure_usd, "prior exposure changed")
    original_journal = _parent_journal(parent)
    _require(
        any(item.raw == original_journal for item in parents),
        "measurement parent ledger is omitted",
    )
    original_account = account_ledger(original_journal, maximum=parent.options.maximum_cost_usd)
    original_observed: Counter[tuple[str, str, str, Decimal]] = Counter()
    for name in sorted(parent.archive.members):
        if name.startswith("observations/"):
            for line in parent.archive.read(name).splitlines():
                original_observed.update(_observed(_load(line)))
    charged = Counter(_charge(entry) for entry in original_account.completions)
    _require(
        not original_observed - charged,
        "original observations contain uncharged or duplicated calls",
    )
    remaining_parent = charged - original_observed
    for entry in original_account.completions:
        charge = _charge(entry)
        if remaining_parent[charge]:
            review.append(
                {
                    "kind": "unobserved_parent_completion",
                    "ledger_sha256": _digest(original_journal),
                    "entry": entry,
                }
            )
            remaining_parent[charge] -= 1

    directory = store.directory / "budget"
    anchor = _json(
        {
            "plan_sha256": _digest(store.plan.encoded()),
            "parents": references,
            "prior_exposure_usd": str(reported + held),
            "allocation_usd": str(store.plan.allowance_usd),
        }
    )
    _require(_read(directory / "anchor.json") == anchor, "budget lineage or allocation changed")
    remaining = store.plan.allowance_usd
    previous: dict[str, str] = {}
    ledger_calls: dict[str, dict[Slot, Counter[tuple[str, str, str, Decimal]]]] = {}
    segment_accounts = []
    expected = {"anchor.json", ".lock"}
    for index, path in enumerate(sorted(directory.glob("*.jsonl")), 1):
        _require(path.name == f"{index:06d}.jsonl", "segment is missing or renumbered")
        admission = _json(
            {"anchor_sha256": _digest(anchor), "maximum_usd": str(remaining), "previous": previous}
        )
        _require(
            _read(path.with_suffix(".admission.json")) == admission,
            "segment allowance or ancestry changed",
        )
        raw = _journal(path)
        account = account_ledger(raw, maximum=remaining)
        digest = _digest(raw)
        _require(not account.overrun, "segment overrun is not admitted")
        _require(
            _read(path.with_suffix(".sealed.json"))
            == _json(
                {
                    "journal_sha256": digest,
                    "reported_usd": str(account.reported),
                    "held_usd": str(account.held),
                }
            ),
            "segment is live, unsealed or changed",
        )
        by_slot: dict[Slot, Counter[tuple[str, str, str, Decimal]]] = {}
        for line in raw.splitlines():
            journal_entry = _Entry.model_validate_json(line)
            _require(
                journal_entry.slot is not None and journal_entry.slot in slots,
                "segment call has a foreign or missing slot",
            )
        for entry in account.completions:
            key = tuple(entry["slot"])
            by_slot.setdefault(key, Counter())[_charge(entry)] += 1
        _require(digest not in ledger_calls or not raw, "duplicate nonempty segment journal")
        ledger_calls[digest] = by_slot
        if account.unresolved:
            review.append(
                {
                    "kind": "segment_uncertainty",
                    "ledger_sha256": digest,
                    "indices": account.unresolved,
                    "held_usd": str(account.held),
                }
            )
        remaining -= account.exposure
        reported += account.reported
        held += account.held
        previous[path.name] = digest
        segment_accounts.append(
            {
                "file": path.name,
                "sha256": digest,
                "reported_usd": str(account.reported),
                "held_usd": str(account.held),
            }
        )
        expected.update(
            {
                path.name,
                path.with_suffix(".admission.json").name,
                path.with_suffix(".sealed.json").name,
            }
        )
    _require(
        bool(previous) and {p.name for p in directory.iterdir()} == expected,
        "incomplete segment inventory",
    )
    _require(remaining >= 0 and reported + held <= 300, "aggregate or continuation cap exceeded")

    # Reports identify the exact segment responsible for each fresh result.
    attempts: dict[Slot, dict[str, Any]] = {}
    failures: set[tuple[str, Slot]] = set()
    for path in sorted((store.directory / "segments").iterdir()):
        raw = _read(path)
        report = _load(raw)
        _require(
            path.name == _digest(raw) + ".json"
            and report["version"] == "recall-continuation-segment@1"
            and report["plan_sha256"] == _digest(store.plan.encoded())
            and report["measurement_identity_sha256"] == parent.identity_sha256
            and report["ledger_sha256"] in ledger_calls,
            "segment report identity changed",
        )
        for attempt in report["attempts"]:
            key = tuple(attempt["key"])
            _require(key in slots and attempt["worker"] in {1, 2}, "foreign segment attempt")
            if attempt["state"] == "completed":
                _require(
                    key not in attempts and key not in parent.rows,
                    "duplicate or imported segment result",
                )
                attempts[key] = {**attempt, "ledger_sha256": report["ledger_sha256"]}
            else:
                failures.add((report["ledger_sha256"], key))
                review.append(
                    {
                        "kind": "interrupted_attempt",
                        "segment_sha256": _digest(raw),
                        "ledger_sha256": report["ledger_sha256"],
                        "attempt": attempt,
                    }
                )
    if collector_recovery is not None:
        reconciled = _load(_read(store.directory / "collector-recovery/reconciled-segment.json"))
        _require(
            reconciled["version"] == "recall-continuation-reconciled@1"
            and reconciled["derived_from_durable_events"] is True
            and reconciled["plan_sha256"] == _digest(store.plan.encoded())
            and reconciled["measurement_identity_sha256"] == parent.identity_sha256
            and reconciled["ledger_sha256"] in ledger_calls,
            "reconciled segment identity changed",
        )
        for attempt in reconciled["attempts"]:
            key = tuple(attempt["key"])
            _require(key in slots and attempt["wall_seconds"] is None, "foreign reconciled attempt")
            if attempt["state"] == "completed":
                _require(
                    key not in attempts and key not in parent.rows,
                    "duplicate or imported reconciled result",
                )
                attempts[key] = {**attempt, "ledger_sha256": reconciled["ledger_sha256"]}
            else:
                _require(attempt["state"] == "unresolved", "invented reconciled outcome")
                failures.add((reconciled["ledger_sha256"], key))
        review.append(
            {
                "kind": "reconciled_collector_segment",
                "manifest_sha256": collector_recovery[1]["manifest_sha256"],
                "transition": collector_recovery[1],
                "report": reconciled,
            }
        )
    for name in ("unsealed-segments", "cleanup-failures"):
        folder = store.directory / name
        if folder.exists():
            for path in sorted(folder.iterdir()):
                review.append(
                    {
                        "kind": "collector_failure",
                        "artifact": name + "/" + path.name,
                        "sha256": _digest(_read(path)),
                        "facts": _load(_read(path)),
                    }
                )
    _require(
        set(attempts) == set(slots) - set(parent.rows),
        "completed results lack exact segment reports",
    )

    # All imported preparation assessments are retained independently of answers.
    for history_event in history:
        if history_event["action"] == "parent_failed":
            proof_raw = _read(
                store.directory / "failures" / (history_event["source_sha256"] + ".json")
            )
            proof = ParentPreparationApproval.model_validate_json(proof_raw)
            _require(
                _digest(proof_raw) == history_event["source_sha256"]
                and proof.parent_archive_sha256 == parent.archive.sha256
                and _digest(
                    _read(store.directory / "failures" / (proof.diagnostic_sha256 + ".json"))
                )
                == proof.diagnostic_sha256,
                "parent preparation assessment changed",
            )
            review.append(
                {
                    "kind": "parent_preparation_replacement",
                    "approval_sha256": history_event["source_sha256"],
                    "approval": proof.model_dump(mode="json"),
                }
            )

    observations = []
    for key in slots:
        event = store._states[key]
        raw = _read(store._result_path(key, str(event.result_sha256)))
        _require(_digest(raw) == event.result_sha256, "retained result changed")
        row = _load(raw)
        _require(parent._key(row) == key, "result moved to another slot")
        repaired_row = repair is not None and event.index > repair[0].history_events
        validate_execution_claim(claim, row, repaired=repaired_row)
        parent.confirmation.validate_observation(row)
        if key in parent.rows:
            _require(raw == parent.rows[key], "imported original bytes changed")
        else:
            attempt = attempts[key]
            _require(
                attempt["result_sha256"] == event.result_sha256,
                "result differs from segment report",
            )
            calls = ledger_calls[attempt["ledger_sha256"]].get(key, Counter())
            observed = _observed(row)
            _require(
                not observed - calls, "fresh observation differs from its slot's provider charges"
            )
            calls.subtract(observed)
        bank = parent.bank_path(store, key[0])
        bank_raw = _read(bank)
        _require(_digest(bank_raw) == row["formation_bank_sha256"], "formation bank changed")
        original_bank = parent.banks.get(key[0])
        if original_bank is None:
            # The original bank identity for each scenario is already independently
            # derived by CheckpointParent, including scenarios with no parent bank.
            _require(
                _Bank.model_validate_json(bank_raw).identity == parent.bank_identities[key[0]],
                "new formation bank has foreign runtime inputs",
            )
        observed_row = compile_runtime_observation(
            parent.by_id[key[0]],
            row,
            identity_sha256=parent.identity_sha256,
            formation_bank=bank,
            principal=parent.manifest.principal.principal(),
        )
        _require(
            parent.preregistration.registered_at
            <= observed_row.observed_at
            <= system_clock().now(),
            "observation has a foreign timestamp",
        )
        if key not in parent.rows:
            _require(
                deadline.started_at <= observed_row.observed_at <= deadline.expires_at,
                "answer is outside the absolute deadline",
            )
        observations.append(observed_row)
    for digest, by_slot in ledger_calls.items():
        for key, calls in by_slot.items():
            leftovers = +calls
            if leftovers:
                _require(
                    (digest, key) in failures,
                    "unobserved segment calls have no interrupted attempt",
                )
                review.append(
                    {
                        "kind": "unobserved_segment_completions",
                        "ledger_sha256": digest,
                        "slot": key,
                        "calls": [
                            {
                                "provider": c[0],
                                "model": c[1],
                                "request_sha256": c[2],
                                "cost_usd": str(c[3]),
                                "count": n,
                            }
                            for c, n in sorted(leftovers.items())
                        ],
                    }
                )
    _require(_inventory(store) == before, "checkpoint changed during compilation")
    parent.archive.verify()
    manifest = _json(
        {
            "version": "recall-checkpoint-collection@2"
            if repair
            else "recall-checkpoint-collection@1",
            "formation_repair_sha256": repair[1]["manifest_sha256"] if repair else None,
            "collector_recovery_sha256": collector_recovery[1]["manifest_sha256"]
            if collector_recovery
            else None,
            "execution_repair": claim,
            "preserved_original_rows": collector_recovery[0].completed_rows
            if collector_recovery
            else repair[0].completed_rows
            if repair
            else len(parent.rows),
            "activation_eligible": False,
            "plan_sha256": _digest(store.plan.encoded()),
            "measurement_identity_sha256": parent.identity_sha256,
            "parent_archive_sha256": parent.archive.sha256,
            "parent_selected_rows": len(parent.rows),
            "parent_original_rows": parent.total_rows,
            "parent_ledgers": references,
            "segments": segment_accounts,
            "protocol_sha256": protocol_sha256,
            "original_deadline_protocol_sha256": deadline.protocol_sha256,
            "artifacts": before,
            "reported_usd": str(reported),
            "held_usd": str(held),
            "review_items": review,
            "observations_sha256": _digest(
                b"".join(row.model_dump_json().encode() + b"\n" for row in observations)
            ),
        }
    )
    return CheckpointCollection(
        scenario_manifest(parent.cases),
        tuple(observations),
        reported,
        held,
        manifest,
        tuple(review),
    )
