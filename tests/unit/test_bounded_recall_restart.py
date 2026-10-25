"""Synthetic separate restart decisions; never production authorization or feedback."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.memory.production_recall_trial import ProductionRecallTrial
from agent_core.memory.semantic_deployment import bind_index_manifest
from tests.unit.test_bounded_recall_admission import NOW, digest, write
from tests.unit.test_bounded_recall_production_trial import check_in, trial_files

pytestmark = pytest.mark.static


def restart_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, hour: int = 72
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    kwargs, manifest, evidence = trial_files(tmp_path, monkeypatch)
    original = kwargs["evidence"].read_bytes()
    trial = ProductionRecallTrial.model_validate(evidence["production_trial"])
    evidence["version"] = "recall-bounded-evidence@4"
    evidence["production_restart"] = {
        "version": "recall-owner-production-restart@1",
        "trial_id": trial.trial_id,
        "tenant_id": trial.tenant_id,
        "principal_id": trial.principal_id,
        "owner_kind": "human",
        "decision": "restart",
        "authorized_at": (NOW + timedelta(hours=hour)).isoformat(),
        "restarted_at": (NOW + timedelta(hours=hour)).isoformat(),
        "instruction": "Synthetic owner direction: restart within the original seven-day limit.",
        "owner_response_ref": "synthetic-separate-restart-decision",
        "owner_statement_sha256": "8" * 64,
        "prior_trial_sha256": digest(trial.model_dump_json().encode()),
        "prior_evidence_sha256": digest(original),
        "previous_expiry_at": manifest["expires_at"],
        "assessment_sha256": "6" * 64,
        "withdrawal_receipt_sha256": "7" * 64,
        "target_inputs_sha256": evidence["compatibility"]["target_inputs_sha256"],
        "rollback_build_ref": manifest["rollback_build_ref"],
        "check_ins": [],
    }
    manifest["version"] = "recall-bounded-admission@4"
    manifest["expires_at"] = (NOW + timedelta(hours=min(hour + 24, 168))).isoformat()
    kwargs["clock"].value = NOW + timedelta(hours=hour)
    write(kwargs, manifest, evidence)
    return kwargs, manifest, evidence


@pytest.mark.parametrize("hour", [24, 72, 160])
def test_separate_restart_keeps_original_start_and_absolute_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hour: int
) -> None:
    kwargs, manifest, evidence = restart_files(tmp_path, monkeypatch, hour=hour)
    admitted = bind_index_manifest(**kwargs)
    assert admitted()
    assert manifest["activated_at"] == evidence["production_trial"]["started_at"] == NOW.isoformat()
    assert evidence["production_trial"]["check_ins"] == []
    kwargs["clock"].value = NOW + timedelta(hours=min(hour + 24, 168))
    assert not admitted()
    kwargs["clock"].value = NOW + timedelta(hours=hour)
    assert not admitted(), "Restart does not revive an expired composed handle"


def test_restart_review_uses_only_the_new_active_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, manifest, evidence = restart_files(tmp_path, monkeypatch)
    review = check_in(manifest, 92)
    review["window_start"] = (NOW + timedelta(hours=72)).isoformat()
    evidence["production_restart"]["check_ins"] = [review]
    manifest["expires_at"] = (NOW + timedelta(hours=116)).isoformat()
    kwargs["clock"].value = NOW + timedelta(hours=92)
    write(kwargs, manifest, evidence)
    assert bind_index_manifest(**kwargs)()
    assert evidence["production_trial"]["check_ins"] == [], "Missing old feedback stays missing"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "legacy-evidence",
        "legacy-manifest",
        "owner",
        "tenant",
        "trial",
        "agent",
        "blank-instruction",
        "blank-reference",
        "missing-assessment",
        "missing-withdrawal",
        "old-target",
        "other-rollback",
        "old-trial-mutated",
        "early-restart",
        "pre-expiry-decision",
        "future-decision",
        "wrong-prior-expiry",
        "late-review",
        "old-window-review",
        "past-absolute-end",
        "reset-end",
        "short-expiry",
        "source",
        "stop",
    ],
)
def test_restart_refuses_incomplete_decisions_and_preserves_admission_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    kwargs, manifest, evidence = restart_files(tmp_path, monkeypatch)
    restart = evidence["production_restart"]
    if change == "missing":
        evidence.pop("production_restart")
    elif change.startswith("legacy-"):
        (evidence if change == "legacy-evidence" else manifest)["version"] = (
            "recall-bounded-evidence@3"
            if change == "legacy-evidence"
            else "recall-bounded-admission@3"
        )
    elif change in {"owner", "tenant", "trial"}:
        restart[{"owner": "principal_id", "tenant": "tenant_id", "trial": "trial_id"}[change]] = (
            "other"
        )
    elif change == "agent":
        restart["owner_kind"] = "agent"
    elif change == "blank-instruction":
        restart["instruction"] = " " * 30
    elif change == "blank-reference":
        restart["owner_response_ref"] = " "
    elif change in {"missing-assessment", "missing-withdrawal"}:
        restart.pop(
            "assessment_sha256" if change == "missing-assessment" else "withdrawal_receipt_sha256"
        )
    elif change == "old-target":
        restart["target_inputs_sha256"] = "0" * 64
    elif change == "other-rollback":
        restart["rollback_build_ref"] = "c" * 40
    elif change == "old-trial-mutated":
        evidence["production_trial"]["instruction"] += " Changed old instruction."
    elif change == "early-restart":
        restart["restarted_at"] = (NOW + timedelta(hours=23)).isoformat()
    elif change == "pre-expiry-decision":
        restart["authorized_at"] = NOW.isoformat()
    elif change == "future-decision":
        restart["authorized_at"] = (NOW + timedelta(hours=73)).isoformat()
    elif change == "wrong-prior-expiry":
        restart["previous_expiry_at"] = (NOW + timedelta(hours=48)).isoformat()
    elif change in {"late-review", "old-window-review", "stop"}:
        review = check_in(manifest, 97 if change == "late-review" else 92)
        review["window_start"] = (NOW + timedelta(hours=72)).isoformat()
        if change == "old-window-review":
            review["window_start"] = NOW.isoformat()
        if change == "stop":
            review["decision"] = "stop"
        restart["check_ins"] = [review]
        kwargs["clock"].value = NOW + timedelta(hours=97)
        manifest["expires_at"] = (kwargs["clock"].value + timedelta(hours=24)).isoformat()
    elif change in {"past-absolute-end", "reset-end"}:
        kwargs["clock"].value = NOW + timedelta(days=7)
        manifest["expires_at"] = (NOW + timedelta(days=8)).isoformat()
        if change == "reset-end":
            manifest["activated_at"] = restart["restarted_at"]
    elif change == "short-expiry":
        manifest["expires_at"] = (NOW + timedelta(hours=95)).isoformat()
    elif change == "source":
        (kwargs["root"] / "runtime.py").write_text("Unreviewed change")
    write(kwargs, manifest, evidence)
    with pytest.raises(ValueError):
        bind_index_manifest(**kwargs)


@pytest.mark.parametrize("change", ["clock", "manifest", "evidence", "source"])
def test_restart_handle_latches_on_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    kwargs, _, _ = restart_files(tmp_path, monkeypatch)
    admitted = bind_index_manifest(**kwargs)
    assert admitted()
    if change == "clock":
        kwargs["clock"].value = NOW + timedelta(hours=71)
        assert not admitted()
        kwargs["clock"].value = NOW + timedelta(hours=72)
    else:
        path = kwargs[change] if change != "source" else kwargs["root"] / "runtime.py"
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        assert not admitted()
        path.write_bytes(original)
    assert not admitted()


@pytest.mark.parametrize(
    "change",
    [
        None,
        "old-stop",
        "old-incident",
        "old-late",
        "reused-decision",
        "reused-run",
        "new-incident",
        "missing-sample",
        "future-review",
        "reused-review",
    ],
)
def test_restart_preserves_prior_reviews_and_requires_complete_fresh_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str | None
) -> None:
    from uuid import UUID

    kwargs, manifest, evidence = restart_files(tmp_path, monkeypatch)
    restart = evidence["production_restart"]
    old = check_in(manifest, 20)
    if change == "old-stop":
        old["decision"] = "stop"
    elif change == "old-incident":
        old["serious_incident"] = True
    elif change == "old-late":
        old["reviewed_at"] = (NOW + timedelta(hours=25)).isoformat()
    evidence["production_trial"]["check_ins"] = [old]
    restart["prior_trial_sha256"] = digest(
        ProductionRecallTrial.model_validate(evidence["production_trial"])
        .model_dump_json()
        .encode()
    )
    restart["previous_expiry_at"] = (NOW + timedelta(hours=44)).isoformat()
    review = check_in(manifest, 92)
    review["window_start"] = restart["restarted_at"]
    review["reviewed_runs"] = [str(UUID(int=i)) for i in range(11, 16)]
    if change == "reused-decision":
        restart["owner_response_ref"] = old["owner_response_ref"]
    elif change == "reused-run":
        review["reviewed_runs"] = old["reviewed_runs"]
    elif change == "new-incident":
        review["serious_incident"] = True
    elif change == "missing-sample":
        review["reviewed_runs"].pop()
    elif change == "future-review":
        review["reviewed_at"] = (NOW + timedelta(hours=93)).isoformat()
    elif change == "reused-review":
        review["owner_response_ref"] = restart["owner_response_ref"]
    restart["check_ins"] = [review]
    manifest["expires_at"] = (NOW + timedelta(hours=116)).isoformat()
    kwargs["clock"].value = NOW + timedelta(hours=92)
    write(kwargs, manifest, evidence)
    if change is None:
        assert bind_index_manifest(**kwargs)()
    else:
        with pytest.raises(ValueError):
            bind_index_manifest(**kwargs)
