"""ADR-0212 durability and outcome-blind continuation regressions."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    ContinuationPlan,
    Slot,
    required_slots,
)

pytestmark = pytest.mark.static


@pytest.fixture
def plan() -> ContinuationPlan:
    return ContinuationPlan(
        identity_sha256="a" * 64,
        evaluated_inputs_sha256="b" * 64,
        parent_archive_sha256="c" * 64,
        scenarios=tuple(f"case-{index}" for index in range(400)),
        prior_exposure_usd=Decimal("135.90882425"),
        allowance_usd=Decimal("150"),
    )


def row(plan: ContinuationPlan, *, answer: str = "an unsuccessful answer") -> bytes:
    return (
        json.dumps(
            {
                "identity_sha256": plan.identity_sha256,
                "scenario_id": plan.scenarios[0],
                "path": "automatic",
                "pipeline": "formation",
                "channel": "sparse",
                "answer": answer,
            }
        )
        + "\n"
    ).encode()


def checked(value: dict[str, object]) -> None:
    # These tests exercise durable scheduling, not runtime observation scoring.
    assert "answer" in value


def test_matrix_keeps_every_scenario_and_both_paths(plan: ContinuationPlan) -> None:
    slots = required_slots(plan.scenarios)
    assert len(slots) == len(set(slots)) == 1600
    assert {key[0] for key in slots} == set(plan.scenarios)
    assert {(key[1], key[2], key[3]) for key in slots} == {
        (path, "formation", channel)
        for path in ("automatic", "deliberate")
        for channel in ("sparse", "hybrid")
    }


def test_reopen_skips_imported_unsuccessful_answer_without_retagging(
    tmp_path: Path, plan: ContinuationPlan
) -> None:
    raw = row(plan)
    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        key = store.import_result(
            raw, expected_sha256=hashlib.sha256(raw).hexdigest(), validate=checked
        )
    with CheckpointStore(tmp_path / "collection", plan) as resumed:
        assert resumed.begin(key) is False
        assert key not in resumed.pending()
    assert raw in [path.read_bytes() for path in (tmp_path / "collection/results").iterdir()]


def test_restart_never_resends_an_unknown_started_slot(
    tmp_path: Path, plan: ContinuationPlan
) -> None:
    key = required_slots(plan.scenarios)[0]
    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        assert store.begin(key)
    with (
        CheckpointStore(tmp_path / "collection", plan) as resumed,
        pytest.raises(ValueError, match=r"unknown|unresolved"),
    ):
        resumed.begin(key)


def test_changed_input_or_insufficient_aggregate_refuses_reopen(
    tmp_path: Path, plan: ContinuationPlan
) -> None:
    with CheckpointStore(tmp_path / "collection", plan, create=True):
        pass
    with pytest.raises(ValueError, match=r"plan|input"):
        CheckpointStore(tmp_path / "collection", replace(plan, evaluated_inputs_sha256="d" * 64))
    with pytest.raises(ValueError, match=r"allowance|cap"):
        CheckpointStore(
            tmp_path / "over-budget", replace(plan, allowance_usd=Decimal("210")), create=True
        )


def test_only_one_pre_answer_formation_replacement_is_allowed(
    tmp_path: Path, plan: ContinuationPlan
) -> None:
    key = required_slots(plan.scenarios)[0]
    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        store.begin(key)
        store.fail(key, stage="formation", error_class="CancelledError", ledger_sha256="d" * 64)
        store.replace_formation(key)
        assert store.begin(key)
        store.fail(key, stage="formation", error_class="CancelledError", ledger_sha256="e" * 64)
        with pytest.raises(ValueError, match=r"replacement|retry"):
            store.replace_formation(key)


def test_second_coordinator_cannot_admit_work(tmp_path: Path, plan: ContinuationPlan) -> None:
    directory = tmp_path / "collection"
    with (
        CheckpointStore(directory, plan, create=True),
        pytest.raises(ValueError, match=r"coordinator|locked"),
    ):
        CheckpointStore(directory, plan)


def test_crash_after_result_write_reconciles_without_resending(
    tmp_path: Path, plan: ContinuationPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "collection"
    key = required_slots(plan.scenarios)[0]
    with CheckpointStore(directory, plan, create=True) as store:
        store.begin(key)
        # Simulate power loss after the result is durable but before its completion record.
        with monkeypatch.context() as patch:
            patch.setattr(
                store, "_record", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
            )
            with pytest.raises(OSError):
                store.complete(key, row(plan), validate=checked)
    with CheckpointStore(directory, plan) as resumed:
        assert resumed.reconcile_result(key, validate=checked).read_bytes() == row(plan)
        assert not resumed.begin(key)


def test_import_crash_can_finish_import_without_admitting_a_send(
    tmp_path: Path, plan: ContinuationPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "collection"
    raw = row(plan)
    digest = hashlib.sha256(raw).hexdigest()
    with CheckpointStore(directory, plan, create=True) as store:
        original = store._record

        def interrupted(action: str, key: Slot, **kwargs: object) -> None:
            if action == "imported":
                raise OSError("Completion record interrupted")
            original(action, key, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_record", interrupted)
            with pytest.raises(OSError):
                store.import_result(raw, expected_sha256=digest, validate=checked)
    with CheckpointStore(directory, plan) as resumed:
        key = resumed.import_result(raw, expected_sha256=digest, validate=checked)
        assert not resumed.begin(key)


@pytest.mark.parametrize("changed", ["identity", "slot", "duplicate", "hash", "validator"])
def test_import_refuses_unverified_or_conflicting_rows(
    tmp_path: Path, plan: ContinuationPlan, changed: str
) -> None:
    raw = row(plan)
    if changed == "identity":
        raw = raw.replace(b"a" * 64, b"f" * 64)
    if changed == "slot":
        raw = raw.replace(b"formation", b"seeded")

    def reject(value: dict[str, object]) -> None:
        raise ValueError("Compiler refused the evidence")

    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        if changed == "duplicate":
            store.import_result(
                raw, expected_sha256=hashlib.sha256(raw).hexdigest(), validate=checked
            )
            raw = row(plan, answer="changed answer")
        with pytest.raises(ValueError):
            store.import_result(
                raw,
                expected_sha256="0" * 64 if changed == "hash" else hashlib.sha256(raw).hexdigest(),
                validate=reject if changed == "validator" else checked,
            )


@pytest.mark.parametrize("damage", ["changed", "missing", "symlink", "event"])
def test_restart_refuses_damaged_evidence(
    tmp_path: Path, plan: ContinuationPlan, damage: str
) -> None:
    directory = tmp_path / "collection"
    raw = row(plan)
    with CheckpointStore(directory, plan, create=True) as store:
        store.import_result(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), validate=checked)
    result = next((directory / "results").iterdir())
    if damage == "changed":
        result.write_bytes(raw + b" ")
    elif damage == "missing":
        result.unlink()
    elif damage == "symlink":
        result.unlink()
        outside = tmp_path / "outside.json"
        outside.write_bytes(raw)
        result.symlink_to(outside)
    else:
        next((directory / "events").iterdir()).write_bytes(b"{}\n")
    with pytest.raises((ValueError, OSError)):
        CheckpointStore(directory, plan)


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("answer", "CancelledError"),
        ("formation", "ModelProtocolError"),
        ("formation", "BudgetExceededError"),
    ],
)
def test_answer_security_and_budget_failures_cannot_get_replacement(
    tmp_path: Path, plan: ContinuationPlan, stage: str, error: str
) -> None:
    key = required_slots(plan.scenarios)[0]
    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        store.begin(key)
        store.fail(key, stage=stage, error_class=error, ledger_sha256="d" * 64)
        with pytest.raises(ValueError, match=r"replacement|retry"):
            store.replace_formation(key)


def test_completed_scenario_answer_prevents_formation_replacement(
    tmp_path: Path, plan: ContinuationPlan
) -> None:
    key = required_slots(plan.scenarios)[1]
    raw = row(plan)
    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        store.import_result(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), validate=checked)
        store.begin(key)
        store.fail(key, stage="formation", error_class="CancelledError", ledger_sha256="d" * 64)
        with pytest.raises(ValueError, match=r"replacement|retry"):
            store.replace_formation(key)


def test_rejected_completion_keeps_its_original_bytes_without_allowing_resend(
    tmp_path: Path, plan: ContinuationPlan
) -> None:
    directory = tmp_path / "collection"
    key = required_slots(plan.scenarios)[0]
    raw = row(plan)

    def reject(value: dict[str, object]) -> None:
        raise ValueError("Invalid mechanical witness")

    with CheckpointStore(directory, plan, create=True) as store:
        store.begin(key)
        with pytest.raises(ValueError, match="mechanical"):
            store.complete(key, raw, validate=reject)
        assert [path.read_bytes() for path in (directory / "results").iterdir()] == [raw]
    with (
        CheckpointStore(directory, plan) as resumed,
        pytest.raises(ValueError, match=r"unknown|unresolved"),
    ):
        resumed.begin(key)


@pytest.mark.parametrize("raw", [b"not json", b'{"identity_sha256":"foreign"}', b"{}"])
def test_structurally_invalid_completion_still_retains_original_bytes(
    tmp_path: Path, plan: ContinuationPlan, raw: bytes
) -> None:
    key = required_slots(plan.scenarios)[0]
    with CheckpointStore(tmp_path / "collection", plan, create=True) as store:
        store.begin(key)
        with pytest.raises(ValueError):
            store.complete(key, raw, validate=checked)
        assert [p.read_bytes() for p in (store.directory / "results").iterdir()] == [raw]
