"""The synthetic complete matrix exercises provenance, never certifies recall."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.general_recall_release import RecallObservation
from agent_core.evals import general_recall_checkpoint_collection as module
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    _digest,
    _json,
    required_slots,
)
from agent_core.evals.general_recall_checkpoint_collection import compile_checkpoint_collection
from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent, formation_identity
from agent_core.evals.general_recall_checkpoint_runtime import ContinuationDeadline
from agent_core.evals.general_recall_continuation_budget import ContinuationBudget, ParentLedger
from tests.unit.test_general_recall_checkpoint_parent import parent_fixture
from tests.unit.test_general_recall_checkpoint_scoring import fixture as scoring_fixture
from tests.unit.test_general_recall_confirmatory import scratch_settings
from tests.unit.test_general_recall_continuation_budget import completed, reserved
from tests.unit.test_general_recall_continuation_budget import parents as old_ledgers

pytestmark = pytest.mark.static


def setup_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CheckpointStore, CheckpointParent, tuple[ParentLedger, ...]]:
    archive, plan, _ = parent_fixture(tmp_path, monkeypatch)
    parent = CheckpointParent(archive, scratch_settings())
    parents = old_ledgers()
    plan = replace(plan, prior_exposure_usd=Decimal("0.8"))
    store = CheckpointStore(tmp_path / "state", plan, create=True)
    parent.import_into(store)
    now = datetime.now(UTC)
    ContinuationDeadline(
        store, protocol_sha256="a" * 64, create=True, now=lambda: now, monotonic=lambda: 0
    )
    budget = ContinuationBudget(store, parents)
    workers = store.directory / "operator-segments"
    workers.mkdir()
    (workers / "000001.json").write_bytes(
        _json(
            {
                "version": "recall-checkpoint-worker-admission@1",
                "plan_sha256": _digest(plan.encoded()),
                "protocol_sha256": "a" * 64,
                "budget_admission_sha256": _digest(
                    budget.journal.with_suffix(".admission.json").read_bytes()
                ),
                "databases": ["veetbot_m33_eval_new_1", "veetbot_m33_eval_new_2"],
                "forbidden_databases": ["veetbot_m33_eval_parent_1", "veetbot_m33_eval_parent_2"],
                "options_sha256": _digest(parent.options.model_dump_json().encode()),
                "effective_inputs_sha256": _digest(_json(parent.inputs)),
            }
        )
    )
    paid_slot = next(key for key in required_slots(plan.scenarios) if key not in parent.rows)
    budget.append({**reserved(), "slot": paid_slot})
    budget.append({**completed(), "slot": paid_slot})
    budget.seal()
    scored: dict[tuple[str, str, str, str], RecallObservation] = {
        (r.scenario_id, r.path, r.pipeline, r.channel): r for r in scoring_fixture().observations
    }

    def compiler(case: Any, row: dict[str, Any], **_kwargs: Any) -> Any:
        return scored[parent._key(row)].model_copy(
            update={
                "identity_sha256": plan.identity_sha256,
                "observed_at": datetime.fromisoformat(row["observed_at"]),
            }
        )

    monkeypatch.setattr(module, "compile_runtime_observation", compiler, raising=False)
    # This fixture deliberately supplies a distinct parent journal. Production uses
    # the archive's exact provider-costs.jsonl, which is not replaced or edited.
    monkeypatch.setattr(module, "_parent_journal", lambda p: parents[-1].raw, raising=False)
    for key in required_slots(plan.scenarios):
        if key in parent.rows:
            continue
        path = parent.bank_path(store, key[0])
        if not path.exists():
            path.write_bytes(
                _json(
                    {
                        "identity": formation_identity(
                            parent.by_id[key[0]],
                            settings=scratch_settings(),
                            options=parent.options,
                            implementation_sha256=parent.identity.implementation_sha256,
                        ),
                        "state_sha256": "a" * 64,
                        "calls": [],
                    }
                )
            )
        variant = next(v for v in parent.manifest.variants if v.key == (key[2], key[1], key[3]))
        raw = _json(
            {
                "scenario_id": key[0],
                "path": key[1],
                "pipeline": key[2],
                "channel": key[3],
                **parent.confirmation.observation_fields(),
                "configuration_sha256": variant.digest,
                "formation_bank_sha256": _digest(path.read_bytes()),
                "observed_at": now.isoformat(),
                "provider_attempts": [
                    {
                        "provider": "test",
                        "model": "test",
                        "request_sha256": "a" * 64,
                        "completed": True,
                        "usage": {"cost": "0.1"},
                    }
                ]
                if key == paid_slot
                else [],
            }
        )
        store.begin(key)
        store.complete(key, raw, validate=lambda _r: None)
    report = {
        "version": "recall-continuation-segment@1",
        "plan_sha256": _digest(plan.encoded()),
        "measurement_identity_sha256": plan.identity_sha256,
        "ledger_sha256": _digest(budget.journal.read_bytes()),
        "state": "collected",
        "completed_slots": 1600,
        "required_slots": 1600,
        "interrupted": False,
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "wall_seconds": 0,
        "attempts": [
            {
                "key": key,
                "state": "completed",
                "result_sha256": store._states[key].result_sha256,
                "worker": 1,
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "wall_seconds": 0,
            }
            for key in required_slots(plan.scenarios)
            if key not in parent.rows
        ],
    }
    (store.directory / "segments").mkdir()
    raw = _json(report)
    (store.directory / "segments" / (_digest(raw) + ".json")).write_bytes(raw)
    return store, parent, parents


def test_complete_raw_lineage_preserves_parent_rows_and_unselected_charges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, parent, parents = setup_collection(tmp_path, monkeypatch)
    with store:
        result = compile_checkpoint_collection(store, parent, parents)
        assert len(result.observations) == 1600 and len(result.scenarios) == 400
        assert result.reported_usd == Decimal("0.9") and result.held_usd == 0
        manifest = json.loads(result.manifest)
        assert manifest["version"] == "recall-checkpoint-collection@1"
        assert manifest["parent_selected_rows"] == 4 and manifest["parent_original_rows"] == 12
        assert len(manifest["parent_ledgers"]) == 8
        assert manifest["activation_eligible"] is False
        assert result.review_items  # All unobserved original charges remain explicit.
        assert compile_checkpoint_collection(store, parent, parents).manifest == result.manifest


def test_raw_lineage_refuses_changed_original_results_banks_ledger_reports_and_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, parent, parents = setup_collection(tmp_path, monkeypatch)
    with store:
        event = next((store.directory / "events").iterdir())
        original_event = event.read_bytes()
        event.write_bytes(original_event + b" ")
        with pytest.raises(ValueError, match=r"event|chain|history"):
            compile_checkpoint_collection(store, parent, parents)
        event.write_bytes(original_event)
        files = [
            store.directory / "operator-segments/000001.json",
            next((store.directory / "results").iterdir()),
            next((store.directory / "banks").iterdir()),
            store.directory / "budget/000001.sealed.json",
            store.directory / "deadline.json",
            next((store.directory / "segments").iterdir()),
            store.directory / "parent-import.json",
        ]
        for path in files:
            original = path.read_bytes()
            path.write_bytes(b"{}\n")
            with pytest.raises((ValueError, KeyError)):
                compile_checkpoint_collection(store, parent, parents)
            path.write_bytes(original)
        with pytest.raises(ValueError):
            compile_checkpoint_collection(store, parent, parents[:-1])
        with pytest.raises(ValueError):
            compile_checkpoint_collection(
                store,
                parent,
                tuple(replace(p, sha256="0" * 64) if i == 0 else p for i, p in enumerate(parents)),
            )


def test_incomplete_lineage_never_compiles_or_creates_a_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, plan, _ = parent_fixture(tmp_path, monkeypatch)
    parent = CheckpointParent(archive, scratch_settings())
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        parent.import_into(store)
        with pytest.raises(ValueError, match=r"complete|matrix"):
            compile_checkpoint_collection(store, parent, old_ledgers())


def test_reconciled_completed_rows_keep_original_hashes_and_missing_wall_timings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from agent_core.evals import general_recall_collector_recovery as recovery

    store, parent, parents = setup_collection(tmp_path, monkeypatch)
    with store:
        path = next((store.directory / "segments").iterdir())
        report = json.loads(path.read_bytes())
        completed = report["attempts"].pop()
        path.unlink()
        raw = _json(report)
        (path.parent / (_digest(raw) + ".json")).write_bytes(raw)
        directory = store.directory / "collector-recovery"
        directory.mkdir()
        reconciled = {
            "version": "recall-continuation-reconciled@1",
            "derived_from_durable_events": True,
            "plan_sha256": _digest(store.plan.encoded()),
            "measurement_identity_sha256": parent.identity_sha256,
            "ledger_sha256": report["ledger_sha256"],
            "attempts": [
                {
                    k: v
                    for k, v in completed.items()
                    if k not in {"worker", "started_at", "finished_at"}
                }
            ],
        }
        reconciled["attempts"][0]["wall_seconds"] = None
        target = directory / "reconciled-segment.json"
        target.write_bytes(_json(reconciled))
        # The proof validator has its own original-byte/unknown-send regressions.
        # This seam isolates merging a validated reconciliation into the full matrix.
        monkeypatch.setattr(
            recovery,
            "retained_collector_recovery",
            lambda _: (
                SimpleNamespace(predecessor_protocol_sha256="a" * 64, completed_rows=5),
                {
                    "successor_protocol_sha256": "b" * 64,
                    "first_segment": 2,
                    "manifest_sha256": "f" * 64,
                },
            ),
        )
        result = compile_checkpoint_collection(store, parent, parents)
        assert len(result.observations) == 1600
        items = [i for i in result.review_items if i["kind"] == "reconciled_collector_segment"]
        assert items[0]["report"]["attempts"][0]["wall_seconds"] is None
        assert items[0]["report"]["attempts"][0]["result_sha256"] == completed["result_sha256"]
        reconciled["attempts"][0]["result_sha256"] = "0" * 64
        target.write_bytes(_json(reconciled))
        with pytest.raises(ValueError):
            compile_checkpoint_collection(store, parent, parents)
