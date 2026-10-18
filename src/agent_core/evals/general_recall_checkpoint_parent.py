"""Outcome-blind import of every eligible row from one immutable parent."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from agent_core.config import Settings, load_config_document
from agent_core.domain.general_recall_release import (
    Digest,
    Identifier,
    RecallPreregistration,
    RecallReleaseIdentity,
    ReleaseValue,
)
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    Slot,
    _digest,
    _json,
    _read,
    _sync,
    _write,
    required_slots,
)
from agent_core.evals.general_recall_checkpoint_runtime import VerifiedParentArchive
from agent_core.evals.general_recall_collection import scenario_manifest
from agent_core.evals.general_recall_composition import system_clock
from agent_core.evals.general_recall_confirmation import ConfirmedExecution, RecallExecutionManifest
from agent_core.evals.general_recall_confirmatory import ConfirmationOptions
from agent_core.evals.general_recall_corpus import RecallFixture, corpus_digest, validate_corpus
from agent_core.evals.general_recall_formation_bank import _Bank
from agent_core.evals.general_recall_observation import compile_runtime_observation
from agent_core.memory.general_recall_evidence import _unique_object


class ParentPreparationApproval(ReleaseValue):
    """Operator assessment, not a claim that differently encoded request keys match."""

    version: Literal["recall-parent-preparation-approval@1"]
    scenario_id: Identifier
    parent_archive_sha256: Digest
    pending_bank_sha256: Digest
    ledger_file: str = Field(min_length=1, max_length=256)
    ledger_sha256: Digest
    call_index: int = Field(gt=0, strict=True)
    error_class: Literal["CancelledError", "TimeoutError"]
    finished_at: AwareDatetime
    diagnostic_sha256: Digest
    owner_authorization_sha256: Digest
    assessment: Literal["cancelled-pre-answer-preparation"]
    linkage: Literal["operator-assessed-terminal-context"]


def formation_identity(
    case: RecallFixture,
    *,
    settings: Settings,
    options: ConfirmationOptions,
    implementation_sha256: str,
) -> str:
    """The original evaluate_fixture bank key, without running the provider."""
    return _digest(
        json.dumps(
            {
                "case": case.model_dump(mode="json"),
                "formation_policy": options.formation_policy,
                "storage": options.storage,
                "formation": load_config_document(settings, "memory/profiles.yaml")["formation"],
                "implementation": implementation_sha256,
            },
            sort_keys=True,
        ).encode()
    )


class CheckpointParent:
    """Validate the whole original inventory before selecting the fixed four arms."""

    def __init__(self, archive: VerifiedParentArchive, settings: Settings) -> None:
        self.archive = archive
        self.identity = RecallReleaseIdentity.model_validate_json(archive.read("identity.json"))
        self.identity_sha256 = _digest(self.identity.model_dump_json().encode())
        self.manifest = RecallExecutionManifest.model_validate_json(
            archive.read("confirmation.json")
        )
        self.confirmation = ConfirmedExecution(self.identity, self.manifest)
        self.confirmation.validate(self.manifest.variants[0].configuration)
        self.preregistration = RecallPreregistration.model_validate_json(
            archive.read("preregistration.json")
        )
        if (
            _digest(self.preregistration.model_dump_json().encode())
            != self.identity.preregistration_sha256
            or self.manifest.captured_at > self.preregistration.registered_at
        ):
            raise ValueError("Parent preregistration or capture identity changed")
        self.inputs = json.loads(archive.read("inputs.json"), object_pairs_hook=_unique_object)
        run = json.loads(archive.read("run.json"), object_pairs_hook=_unique_object)
        self.options = ConfirmationOptions.model_validate(run["options"])
        if run.get("state") not in {"failed", "completed"}:
            raise ValueError("A running or unknown parent cannot be imported")
        self.cases = tuple(
            RecallFixture.model_validate_json(line)
            for line in archive.read("corpus.jsonl").splitlines()
        )
        validate_corpus(self.cases, release=True)
        scenarios = scenario_manifest(self.cases)
        if (
            corpus_digest(self.cases) != self.identity.corpus_sha256
            or _digest(b"".join(s.model_dump_json().encode() + b"\n" for s in scenarios))
            != self.identity.scenario_manifest_sha256
        ):
            raise ValueError("Parent corpus or reviewed scenario identity changed")
        self.by_id = {case.id: case for case in self.cases}
        self.banks: dict[str, Path] = {}
        self.bank_identities: dict[str, str] = {}
        self.pending_banks: dict[str, Path] = {}
        bank_names: set[str] = set()
        for case in self.cases:
            prefix = "banks/" + _digest(case.id.encode()) + ".json"
            expected = formation_identity(
                case,
                settings=settings,
                options=self.options,
                implementation_sha256=self.identity.implementation_sha256,
            )
            self.bank_identities[case.id] = expected
            if prefix in archive.members:
                bank = _Bank.model_validate_json(archive.read(prefix))
                if bank.identity != expected:
                    raise ValueError("Parent formation bank identity changed")
                self.banks[case.id] = archive.directory / prefix
                bank_names.add(prefix)
            pending = prefix + ".pending.jsonl"
            if pending in archive.members:
                header = json.loads(
                    archive.read(pending).splitlines()[0], object_pairs_hook=_unique_object
                )
                if case.id in self.banks or header != {"identity": expected, "state": "recording"}:
                    raise ValueError("Conflicting or changed pending formation bank")
                self.pending_banks[case.id] = archive.directory / pending
                bank_names.add(pending)
        if bank_names != {name for name in archive.members if name.startswith("banks/")}:
            raise ValueError("Unbound or unknown parent formation bank")
        selected_slots = set(required_slots(tuple(self.by_id)))
        self.rows: dict[Slot, bytes] = {}
        self.references: list[dict[str, Any]] = []
        seen: set[Slot] = set()
        for name in sorted(archive.members):
            if not name.startswith("observations/"):
                continue
            if not name.endswith(".jsonl"):
                raise ValueError("Unknown parent observation artifact")
            for index, raw in enumerate(archive.read(name).splitlines(keepends=True)):
                value = json.loads(raw, object_pairs_hook=_unique_object)
                self.confirmation.validate_observation(value)
                key = self._key(value)
                if key in seen or key[0] not in self.by_id:
                    raise ValueError("Duplicate or foreign original observation")
                seen.add(key)
                if key not in selected_slots:
                    continue
                self.validate_row(value)
                self.rows[key] = raw
                self.references.append(
                    {
                        "slot": key,
                        "raw_sha256": _digest(raw),
                        "parent_file": name,
                        "parent_file_sha256": archive.members[name],
                        "line_index": index,
                    }
                )
        if not seen:
            raise ValueError("Parent has no retained observations")
        self.total_rows = len(seen)
        self.original_slots = frozenset(seen)
        archive.verify()

    @staticmethod
    def _key(value: dict[str, Any]) -> Slot:
        return (
            str(value.get("scenario_id")),
            str(value.get("path")),
            str(value.get("pipeline")),
            str(value.get("channel")),
        )

    def validate_row(self, value: dict[str, Any]) -> None:
        self.confirmation.validate_observation(value)
        key = self._key(value)
        if key not in required_slots(tuple(self.by_id)):
            raise ValueError("Parent row is outside the amended matrix")
        bank = self.banks.get(key[0])
        if bank is None:
            raise ValueError("Completed formation observation has no complete original bank")
        raw_bank = self.archive.read(bank.relative_to(self.archive.directory).as_posix())
        if _digest(raw_bank) != value.get("formation_bank_sha256"):
            raise ValueError("Observation formation bank changed")
        observed_at = datetime.fromisoformat(str(value.get("observed_at")))
        if (
            observed_at.tzinfo is None
            or not self.preregistration.registered_at <= observed_at <= system_clock().now()
        ):
            raise ValueError("Parent observation timestamp is outside its original registration")
        compile_runtime_observation(
            self.by_id[key[0]],
            value,
            identity_sha256=self.identity_sha256,
            formation_bank=bank,
            principal=self.manifest.principal.principal(),
        )

    def import_into(self, store: CheckpointStore) -> dict[str, Any]:
        """Resume a byte-identical interrupted import; never reselect by outcome."""
        store._owned()
        if (
            store.plan.identity_sha256 != self.identity_sha256
            or store.plan.parent_archive_sha256 != self.archive.sha256
            or store.plan.evaluated_inputs_sha256 != self.identity.evaluated_inputs_sha256
            or store.plan.scenarios != tuple(self.by_id)
        ):
            raise ValueError("Continuation does not name this complete parent")
        self.archive.verify()
        report = {
            "version": "recall-parent-import@1",
            "plan_sha256": _digest(store.plan.encoded()),
            "parent_archive_sha256": self.archive.sha256,
            "measurement_identity_sha256": self.identity_sha256,
            "parent_files": len(self.archive.members),
            "original_rows": self.total_rows,
            "selected_rows": len(self.rows),
            "rows": self.references,
        }
        encoded = _json(report)
        receipt = store.directory / "parent-import.json"
        if receipt.exists() and _read(receipt) != encoded:
            raise ValueError("Parent import declaration changed")
        for key, raw in self.rows.items():
            state = store._states.get(key)
            if state and state.action == "imported":
                if (
                    state.result_sha256 != _digest(raw)
                    or _read(store._result_path(key, state.result_sha256)) != raw
                ):
                    raise ValueError("Imported original result changed")
            else:
                store.import_result(raw, expected_sha256=_digest(raw), validate=self.validate_row)
        if {key for key, value in store._states.items() if value.action == "imported"} != set(
            self.rows
        ):
            raise ValueError("An imported observation does not belong to this parent")
        if not receipt.exists():
            _write(receipt, encoded)
        return report

    def bank_path(self, store: CheckpointStore, case_id: str) -> Path:
        if case_id not in self.by_id:
            raise ValueError("Unknown continuation scenario")
        if case_id in self.banks:
            if case_id in store._replaced:
                raise ValueError("A complete original bank cannot be replaced")
            path = self.banks[case_id]
            self.archive.read(path.relative_to(self.archive.directory).as_posix())
            return path
        if case_id in self.pending_banks and case_id not in store._replaced:
            raise ValueError("Original unfinished formation requires an admitted replacement")
        directory = store.directory / "banks"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            _sync(store.directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unsafe continuation bank directory")
        suffix = ".replacement.json" if case_id in store._replaced else ".json"
        return directory / (_digest(case_id.encode()) + suffix)

    def admit_preparation_replacement(
        self, store: CheckpointStore, approval: bytes, *, expected_sha256: str, diagnostic: bytes
    ) -> None:
        """Import an explicitly assessed original interruption, never a new send."""
        # Imported lazily so reading a parent alone never loads provider machinery.
        from agent_core.evals.general_recall_continuation_budget import _Entry, account_ledger

        store._owned()
        proof = ParentPreparationApproval.model_validate_json(approval)
        if (
            _digest(approval) != expected_sha256
            or proof.parent_archive_sha256 != self.archive.sha256
            or store.plan.parent_archive_sha256 != self.archive.sha256
            or store.plan.identity_sha256 != self.identity_sha256
            or _digest(diagnostic) != proof.diagnostic_sha256
            or proof.scenario_id not in self.pending_banks
            or len(self.pending_banks) != 1
            or any(
                key[0] == proof.scenario_id and key[2] == "formation" for key in self.original_slots
            )
        ):
            raise ValueError("Original preparation assessment or parent identity changed")
        key: Slot = (proof.scenario_id, "automatic", "formation", "sparse")
        previous = store._states.get(key)
        retained_proof = store.directory / "failures" / f"{expected_sha256}.json"
        if previous is not None:
            if not retained_proof.exists() or _read(retained_proof) != approval:
                raise ValueError("Original preparation was already admitted differently")
            if previous.action not in {"parent_failed", "replacement"}:
                raise ValueError("Preparation replacement already executed")
        elif any(slot[0] == key[0] for slot in store._states):
            raise ValueError("Original preparation already has continuation activity")
        run = json.loads(self.archive.read("run.json"), object_pairs_hook=_unique_object)
        pending = self.archive.read(
            self.pending_banks[key[0]].relative_to(self.archive.directory).as_posix()
        )
        ledger = self.archive.read(proof.ledger_file)
        account = account_ledger(ledger, maximum=self.options.maximum_cost_usd)
        entries = [_Entry.model_validate_json(line) for line in ledger.splitlines()]
        uncertain = [entry for entry in entries if entry.outcome == "uncertain"]
        context = json.loads(diagnostic, object_pairs_hook=_unique_object)
        bank_lines = [
            json.loads(line, object_pairs_hook=_unique_object) for line in pending.splitlines()
        ]
        if (
            run.get("state") != "failed"
            or datetime.fromisoformat(str(run.get("finished_at"))) != proof.finished_at
            or not self.preregistration.registered_at <= proof.finished_at <= system_clock().now()
            or _digest(pending) != proof.pending_bank_sha256
            or _digest(ledger) != proof.ledger_sha256
            or account.overrun
            or account.unresolved != (proof.call_index,)
            or len(uncertain) != 1
            or uncertain[0].index != proof.call_index
            or uncertain[0].error_class != proof.error_class
            or context.get("primary_archive_sha256") != self.archive.sha256
            or context.get("provider_call")
            != uncertain[0].model_dump(mode="json", exclude_none=True)
            or len(bank_lines) < 2
            or bank_lines[-1].get("state") != "reserved"
        ):
            raise ValueError("Original cancellation, pending bank or terminal context is unproven")
        # Preserve original timestamps and diagnostic bytes. This is not a synthetic
        # start in the continuation and never resolves the uncertain provider charge.
        if previous is None:
            store.import_preparation_failure(
                key,
                source=approval,
                ledger_sha256=proof.ledger_sha256,
                error_class=proof.error_class,
            )
        diagnostic_path = store.directory / "failures" / f"{proof.diagnostic_sha256}.json"
        if diagnostic_path.exists():
            if _read(diagnostic_path) != diagnostic:
                raise ValueError("Original diagnostic changed")
        else:
            _write(diagnostic_path, diagnostic)
        if store._states[key].action == "parent_failed":
            store.replace_formation(key)
