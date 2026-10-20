"""Synthetic scoped findings only; never an actual owner review."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.memory import bounded_recall_admission as admission_module
from agent_core.memory.bounded_recall_admission import bind_bounded_recall
from tests.unit.test_bounded_recall_admission import digest, encoded, files, write

pytestmark = pytest.mark.static


def scoped_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    kwargs, manifest, evidence = files(tmp_path)
    old = evidence["human_review"]
    selected = old["reviewed_scenarios"][:37]
    omitted = old["reviewed_scenarios"][37:]
    groups = {
        name: [digest(encoded({"kind": name}))]
        for name in ("Recovery", "Interruptions", "Uncertainty", "Unobserved charges")
    }
    evidence["review_items"] = [{"kind": name} for name in groups]
    items_hash = digest(encoded(evidence["review_items"]) + b"\n")
    scope = {
        "proposed_individual_scenarios": selected,
        "omitted_required_scenarios": omitted,
        "interruption_summary_groups": groups,
    }
    raw = json.dumps(scope)
    for name, value in (
        ("APPROVED_SCOPE_SHA256", digest(raw.encode())),
        ("APPROVED_OBSERVATIONS_SHA256", old["observations_sha256"]),
        ("APPROVED_ITEMS_SHA256", items_hash),
    ):
        monkeypatch.setattr(admission_module, name, value, raising=False)
    review = {k: v for k, v in old.items() if k not in ("version", "reviewed_items")}
    review.update(
        version="recall-owner-review-submission@2",
        scope_proposal_sha256=digest(raw.encode()),
        review_items_sha256=items_hash,
        reviewed_scenarios=selected,
        omitted_scenarios=omitted,
        scenario_findings={
            s: {"verdict": "acceptable", "notes": "Synthetic case finding."} for s in selected
        },
        summary_findings={
            k: {"verdict": "acceptable", "notes": "Synthetic summary finding."}
            for k in ("cost", *groups)
        },
        reviewed_item_groups=groups,
        residual_risk_findings="Synthetic acceptance of the explicitly unreviewed cases.",
    )
    evidence.update(version="recall-bounded-evidence@2", human_review=review, scope_proposal=raw)
    manifest["version"] = "recall-bounded-admission@2"
    write(kwargs, manifest, evidence)
    return kwargs, manifest, evidence


def test_approved_subset_with_individual_and_summary_findings_binds_and_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs, _, _ = scoped_files(tmp_path, monkeypatch)
    from agent_core.memory.semantic_deployment import bind_index_manifest

    admitted = bind_index_manifest(**kwargs)
    assert admitted()
    kwargs["clock"].value += timedelta(days=7)
    assert not admitted()
    kwargs["clock"].value -= timedelta(days=7)
    assert not admitted()


@pytest.mark.parametrize(
    "change",
    [
        "scope",
        "observations",
        "items",
        "case",
        "omitted",
        "duplicate",
        "case-findings",
        "blank-case",
        "summary",
        "blank-summary",
        "group",
        "residual-risk",
        "scope-approval-only",
        "legacy-review",
        "legacy-evidence",
        "legacy-manifest",
        "source",
        "compatibility",
        "owner",
        "expiry",
        "gate",
        "boundary",
        "future-review",
        "reject",
        "unacceptable",
    ],
)
def test_scoped_admission_refuses_incomplete_or_relabelled_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    kwargs, manifest, evidence = scoped_files(tmp_path, monkeypatch)
    review = evidence["human_review"]
    if change == "scope":
        evidence["scope_proposal"] += " "
    elif change == "observations":
        monkeypatch.setattr(admission_module, "APPROVED_OBSERVATIONS_SHA256", "9" * 64)
    elif change == "items":
        monkeypatch.setattr(admission_module, "APPROVED_ITEMS_SHA256", "9" * 64)
    elif change == "case":
        review["reviewed_scenarios"].pop()
    elif change == "omitted":
        review["omitted_scenarios"].pop()
    elif change == "duplicate":
        review["reviewed_scenarios"].append(review["reviewed_scenarios"][0])
    elif change == "case-findings":
        review["scenario_findings"].pop(next(iter(review["scenario_findings"])))
    elif change == "blank-case":
        review["scenario_findings"][review["reviewed_scenarios"][0]] = {
            "verdict": "concern",
            "notes": " " * 30,
        }
    elif change == "summary":
        review["summary_findings"].pop("cost")
    elif change == "blank-summary":
        review["summary_findings"]["cost"] = {"verdict": "concern", "notes": " " * 30}
    elif change == "group":
        review["reviewed_item_groups"]["Recovery"] = []
    elif change == "residual-risk":
        review["residual_risk_findings"] = " " * 30
    elif change == "scope-approval-only":
        review["scenario_findings"] = {}
        review["summary_findings"] = {}
    elif change.startswith("legacy-"):
        target = {
            "legacy-review": review,
            "legacy-evidence": evidence,
            "legacy-manifest": manifest,
        }[change]
        target["version"] = target["version"].replace("@2", "@1")
    elif change == "source":
        (kwargs["root"] / "runtime.py").write_text("different source")
    elif change == "compatibility":
        evidence["compatibility"]["changes"] = {}
    elif change == "owner":
        manifest["principal_id"] = "different-owner"
    elif change == "expiry":
        manifest["expires_at"] = (kwargs["clock"].now() + timedelta(days=8)).isoformat()
    elif change == "gate":
        evidence["bundle"]["gates"][0]["failed"] = 1
    elif change == "boundary":
        evidence["bundle"]["observations"][0]["boundary_failures"] = ["access"]
    elif change == "future-review":
        review["reviewed_at"] = (kwargs["clock"].now() + timedelta(days=1)).isoformat()
    elif change == "reject":
        review["decision"] = "reject"
    elif change == "unacceptable":
        review["scenario_findings"][review["reviewed_scenarios"][0]] = {
            "verdict": "unacceptable",
            "notes": "Synthetic unacceptable finding.",
        }
    write(kwargs, manifest, evidence)
    with pytest.raises(ValueError):
        bind_bounded_recall(**kwargs)
