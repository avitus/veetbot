"""Synthetic trial decisions only; no owner authorization or production evidence."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.memory import bounded_recall_admission as admission_module
from agent_core.memory.bounded_recall_admission import bind_bounded_recall
from tests.unit.test_bounded_recall_admission import NOW, files, write

pytestmark = pytest.mark.static


def trial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    kwargs, manifest, evidence = files(tmp_path)
    old = evidence.pop("human_review")
    for key, value in (
        ("APPROVED_OBSERVATIONS_SHA256", old["observations_sha256"]),
        ("APPROVED_ITEMS_SHA256", old["review_items_sha256"]),
    ):
        monkeypatch.setattr(admission_module, key, value)
    evidence.update(
        version="recall-bounded-evidence@3",
        production_trial={
            "version": "recall-owner-production-trial@1",
            "trial_id": "synthetic-trial",
            "tenant_id": manifest["tenant_id"],
            "principal_id": manifest["principal_id"],
            "owner_kind": "human",
            "authorized_at": NOW.isoformat(),
            "instruction": "Synthetic owner direction: start a trial with daily real-use review.",
            "authorization_sha256": evidence["owner_authorization_sha256"],
            "observations_sha256": old["observations_sha256"],
            "review_items_sha256": old["review_items_sha256"],
            "started_at": NOW.isoformat(),
            "check_ins": [],
        },
    )
    manifest.update(
        version="recall-bounded-admission@3",
        expires_at=(NOW + timedelta(hours=24)).isoformat(),
    )
    write(kwargs, manifest, evidence)
    return kwargs, manifest, evidence


def check_in(manifest: dict[str, Any], at_hours: int = 20) -> dict[str, Any]:
    return {
        "trial_id": "synthetic-trial",
        "tenant_id": manifest["tenant_id"],
        "principal_id": manifest["principal_id"],
        "reviewer_kind": "human",
        "reviewed_at": (NOW + timedelta(hours=at_hours)).isoformat(),
        "window_start": NOW.isoformat(),
        "window_end": (NOW + timedelta(hours=at_hours)).isoformat(),
        "decision": "continue",
        "owner_response_ref": f"synthetic-owner-response-{at_hours}",
        "owner_statement_sha256": "7" * 64,
        "eligible_run_count": 7,
        "reviewed_runs": [str(UUID(int=i)) for i in range(1, 6)],
        "selection_receipt_sha256": "5" * 64,
        "provenance_verification_sha256": "6" * 64,
        "serious_incident": False,
    }


def test_trial_starts_without_fabricated_case_review_and_stops_after_one_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core.memory.semantic_deployment import bind_index_manifest

    kwargs, _, _ = trial_files(tmp_path, monkeypatch)
    admitted = bind_index_manifest(**kwargs)
    assert admitted()
    kwargs["clock"].value += timedelta(hours=24)
    assert not admitted()
    kwargs["clock"].value = NOW
    assert not admitted(), "A withdrawn handle must remain withdrawn"


@pytest.mark.parametrize("eligible", [0, 1, 5, 8])
def test_genuine_timely_check_in_binds_a_bounded_next_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eligible: int
) -> None:
    kwargs, manifest, evidence = trial_files(tmp_path, monkeypatch)
    initial = bind_bounded_recall(**kwargs)
    review = check_in(manifest)
    review["eligible_run_count"] = eligible
    review["reviewed_runs"] = review["reviewed_runs"][: min(eligible, 5)]
    evidence["production_trial"]["check_ins"] = [review]
    kwargs["clock"].value = NOW + timedelta(hours=20)
    manifest["expires_at"] = (NOW + timedelta(hours=44)).isoformat()
    write(kwargs, manifest, evidence)
    assert not initial(), "Renewal evidence must withdraw the previous handle"
    renewed = bind_bounded_recall(**kwargs)
    assert renewed()
    kwargs["clock"].value = NOW + timedelta(hours=44)
    assert not renewed()


def test_daily_renewals_never_move_the_original_seven_day_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, manifest, evidence = trial_files(tmp_path, monkeypatch)
    reviews = []
    previous = NOW
    for hours in range(20, 161, 20):
        review = check_in(manifest, hours)
        review["window_start"] = previous.isoformat()
        review["reviewed_runs"] = [str(UUID(int=hours + i)) for i in range(5)]
        reviews.append(review)
        previous = NOW + timedelta(hours=hours)
    evidence["production_trial"]["check_ins"] = reviews
    kwargs["clock"].value = previous
    manifest["expires_at"] = (NOW + timedelta(days=7)).isoformat()
    write(kwargs, manifest, evidence)
    admitted = bind_bounded_recall(**kwargs)
    assert admitted()
    kwargs["clock"].value = NOW + timedelta(days=7)
    assert not admitted()


def test_inventory_cutoff_is_distinct_from_owner_response_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, manifest, evidence = trial_files(tmp_path, monkeypatch)
    first = check_in(manifest)
    first["window_end"] = (NOW + timedelta(hours=19)).isoformat()
    second = check_in(manifest, 40)
    second["window_start"] = first["window_end"]
    second["window_end"] = (NOW + timedelta(hours=39)).isoformat()
    second["reviewed_runs"] = [str(UUID(int=40 + i)) for i in range(5)]
    evidence["production_trial"]["check_ins"] = [first, second]
    kwargs["clock"].value = NOW + timedelta(hours=40)
    manifest["expires_at"] = (NOW + timedelta(hours=64)).isoformat()
    write(kwargs, manifest, evidence)
    assert bind_bounded_recall(**kwargs)()


@pytest.mark.parametrize(
    "change",
    [
        "initial-long",
        "lifetime",
        "late",
        "future",
        "gap",
        "duplicate",
        "owner",
        "tenant",
        "trial",
        "start",
        "authorization",
        "observations",
        "items",
        "blank-instruction",
        "blank-findings",
        "agent",
        "stop",
        "incident",
        "missing-sample",
        "duplicate-run",
        "missing-provenance",
        "missing-selection",
        "legacy-review",
        "legacy-evidence",
        "legacy-manifest",
        "scope",
        "source",
        "compatibility",
        "boundary",
        "gate",
        "short-expiry",
        "trial-owner",
        "trial-tenant",
        "future-authorization",
        "unknown-count",
        "reused-run",
        "legacy-missing-review",
        "missing-statement-hash",
        "private-prose",
        "stale-inventory",
        "future-inventory",
        "reused-response",
    ],
)
def test_trial_refuses_missing_review_and_preserves_release_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    kwargs, manifest, evidence = trial_files(tmp_path, monkeypatch)
    trial = evidence["production_trial"]
    review = check_in(manifest)
    trial["check_ins"] = [review]
    kwargs["clock"].value = NOW + timedelta(hours=20)
    manifest["expires_at"] = (NOW + timedelta(hours=44)).isoformat()
    if change == "initial-long":
        trial["check_ins"] = []
    elif change == "lifetime":
        manifest["expires_at"] = (NOW + timedelta(days=8)).isoformat()
    elif change == "late":
        review["reviewed_at"] = (NOW + timedelta(hours=25)).isoformat()
        kwargs["clock"].value = NOW + timedelta(hours=25)
    elif change == "future":
        review["reviewed_at"] = (NOW + timedelta(hours=21)).isoformat()
    elif change == "gap":
        review["window_start"] = (NOW + timedelta(hours=1)).isoformat()
    elif change == "duplicate":
        trial["check_ins"].append(review.copy())
    elif change in ("owner", "tenant", "trial"):
        review[{"owner": "principal_id", "tenant": "tenant_id", "trial": "trial_id"}[change]] = (
            "other"
        )
    elif change == "start":
        trial["started_at"] = (NOW - timedelta(days=1)).isoformat()
    elif change == "authorization":
        trial["authorization_sha256"] = "9" * 64
    elif change in ("observations", "items"):
        trial[{"observations": "observations_sha256", "items": "review_items_sha256"}[change]] = (
            "9" * 64
        )
    elif change == "blank-instruction":
        trial["instruction"] = " " * 30
    elif change == "blank-findings":
        review["owner_response_ref"] = " "
    elif change == "agent":
        review["reviewer_kind"] = "agent"
    elif change == "stop":
        review["decision"] = "stop"
    elif change == "incident":
        review["serious_incident"] = True
    elif change == "missing-sample":
        review["reviewed_runs"].pop()
    elif change == "duplicate-run":
        review["reviewed_runs"][-1] = review["reviewed_runs"][0]
    elif change == "missing-provenance":
        review.pop("provenance_verification_sha256")
    elif change == "missing-selection":
        review.pop("selection_receipt_sha256")
    elif change == "legacy-review":
        (tmp_path / "legacy").mkdir()
        evidence["human_review"] = files(tmp_path / "legacy")[2]["human_review"]
    elif change == "legacy-evidence":
        evidence["version"] = "recall-bounded-evidence@2"
    elif change == "legacy-manifest":
        manifest["version"] = "recall-bounded-admission@2"
    elif change == "scope":
        evidence["scope_proposal"] = "{}"
    elif change == "source":
        (kwargs["root"] / "runtime.py").write_text("changed source")
    elif change == "compatibility":
        evidence["compatibility"]["changes"] = {}
    elif change == "boundary":
        evidence["bundle"]["observations"][0]["boundary_failures"] = ["access"]
    elif change == "gate":
        evidence["bundle"]["gates"][0]["failed"] = 1
    elif change == "short-expiry":
        manifest["expires_at"] = (NOW + timedelta(hours=42)).isoformat()
    elif change in ("trial-owner", "trial-tenant"):
        trial[{"trial-owner": "principal_id", "trial-tenant": "tenant_id"}[change]] = "other"
    elif change == "future-authorization":
        trial["authorized_at"] = (NOW + timedelta(hours=21)).isoformat()
    elif change == "unknown-count":
        review["eligible_run_count"] = None
    elif change in ("reused-run", "reused-response"):
        later = review.copy()
        later["window_start"] = review["reviewed_at"]
        later["reviewed_at"] = (NOW + timedelta(hours=40)).isoformat()
        later["window_end"] = later["reviewed_at"]
        if change == "reused-run":
            later["owner_response_ref"] = "synthetic-second-response"
        else:
            later["reviewed_runs"] = [str(UUID(int=40 + i)) for i in range(5)]
        trial["check_ins"].append(later)
        kwargs["clock"].value = NOW + timedelta(hours=40)
        manifest["expires_at"] = (NOW + timedelta(hours=64)).isoformat()
    elif change == "legacy-missing-review":
        evidence["version"] = "recall-bounded-evidence@1"
        manifest["version"] = "recall-bounded-admission@1"
        evidence.pop("production_trial")
    elif change == "missing-statement-hash":
        review.pop("owner_statement_sha256")
    elif change == "private-prose":
        review["owner_statement"] = "Private conversation text is not admission metadata."
    elif change == "stale-inventory":
        review["window_end"] = (NOW - timedelta(hours=1)).isoformat()
    elif change == "future-inventory":
        review["window_end"] = (NOW + timedelta(hours=21)).isoformat()
    write(kwargs, manifest, evidence)
    with pytest.raises(ValueError):
        bind_bounded_recall(**kwargs)
