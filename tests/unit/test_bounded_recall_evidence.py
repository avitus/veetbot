"""Synthetic bounded-rollout arithmetic; never an actual human acceptance."""

import hashlib
import json

import pytest

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.evals.general_recall_checkpoint_scoring import score_checkpoint_release
from agent_core.memory.bounded_recall_evidence import assess_bounded_comparison
from tests.unit.test_general_recall_checkpoint_scoring import fixture

pytestmark = pytest.mark.static


def with_quality_failures() -> CheckpointReleaseBundle:
    value = fixture().model_dump(mode="json")
    incidents = {s["id"] for s in value["scenarios"] if s["known_incident"]}
    hybrid = [
        r
        for r in value["observations"]
        if r["channel"] == "hybrid" and r["scenario_id"] not in incidents
    ]
    for row in hybrid[:30]:
        row["rendered"] = []
        row["recovered_sources"] = []
    hybrid[-1]["invalid_current_ids"] = ["old"]
    hybrid[-2]["cost_usd"] = value["preregistration"]["maximum_case_usd"] + 1
    return CheckpointReleaseBundle.model_validate(value)


def test_bounded_assessment_preserves_failed_originals_and_does_not_grant_activation() -> None:
    bundle = with_quality_failures()
    original = bundle.model_dump_json()
    with pytest.raises(ValueError):
        score_checkpoint_release(bundle)
    report = assess_bounded_comparison(bundle)
    assert report["activation_eligible"] is False
    assert report["observations"] == 1600
    assert report["comparative_benefit_satisfied"] is True
    assert report["original_quality_gates_satisfied"] is False
    assert report["exceptions"], "Known failures must survive the revised assessment"
    assert any(item["kind"] == "invalid_current_fact" for item in report["exceptions"])
    assert any(item["kind"] == "case_cost" for item in report["exceptions"])
    assert len(report["cells"]) == 72
    assert bundle.model_dump_json() == original


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "foreign",
        "timestamp",
        "boundary",
        "context",
        "search",
        "round",
        "rank",
        "noise",
        "gate",
        "lineage",
        "preregistration",
        "source",
    ],
)
def test_bounded_assessment_retains_integrity_security_and_work_limits(change: str) -> None:
    value = fixture().model_dump(mode="json")
    row = next(r for r in value["observations"] if r["channel"] == "hybrid")
    if change == "missing":
        value["observations"].pop()
    elif change == "duplicate":
        value["observations"][-1] = value["observations"][0]
    elif change == "gate":
        value["gates"][0]["failed"] = 1
    elif change == "lineage":
        value["lineage"]["parent_identity_sha256"] = "9" * 64
    elif change == "preregistration":
        value["preregistration"]["context_tokens"] += 1
    elif change == "source":
        row["recovered_sources"][0]["text_sha256"] = "9" * 64
    else:
        field, replacement = {
            "foreign": ("identity_sha256", "9" * 64),
            "timestamp": ("observed_at", value["preregistration"]["registered_at"]),
            "boundary": ("boundary_failures", ["privacy"]),
            "context": ("context_tokens", value["preregistration"]["context_tokens"] + 1),
            "search": ("searches", 5),
            "round": ("rounds", 3),
            "rank": ("global_rank_certifiable", False),
            "noise": ("rendered_noise_items", None),
        }[change]
        row[field] = replacement
    with pytest.raises(ValueError):
        assess_bounded_comparison(CheckpointReleaseBundle.model_validate(value))


def test_no_comparative_benefit_is_reported_without_inventing_a_pass() -> None:
    value = fixture().model_dump(mode="json")
    rows = value["observations"]
    for sparse in (r for r in rows if r["channel"] == "sparse"):
        hybrid = next(
            r
            for r in rows
            if r["scenario_id"] == sparse["scenario_id"]
            and r["path"] == sparse["path"]
            and r["channel"] == "hybrid"
        )
        for field in (
            "formed",
            "candidates",
            "ranked",
            "rendered",
            "recovered_sources",
            "answer_atoms",
            "action",
        ):
            sparse[field] = hybrid[field]
    report = assess_bounded_comparison(CheckpointReleaseBundle.model_validate(value))
    assert report["comparative_benefit_satisfied"] is False
    assert report["activation_eligible"] is False


@pytest.mark.parametrize("rendered", [False, True])
def test_candidate_omitted_from_context_receives_no_rank_render_or_answer_credit(
    rendered: bool,
) -> None:
    value = fixture().model_dump(mode="json")
    scenario = next(
        s for s in value["scenarios"] if s["split"] == "holdout" and not s["known_incident"]
    )
    row = next(
        r
        for r in value["observations"]
        if r["scenario_id"] == scenario["id"] and r["channel"] == "hybrid"
    )
    row["recovered_sources"][0].update(anchor_rank=None, rendered=rendered)
    bundle = CheckpointReleaseBundle.model_validate(value)
    original = bundle.model_dump_json()
    with pytest.raises(ValueError, match="global ranking"):
        score_checkpoint_release(bundle)
    if rendered:
        with pytest.raises(ValueError, match="global ranking"):
            assess_bounded_comparison(bundle)
        return

    report = assess_bounded_comparison(bundle)
    cell = next(
        c
        for c in report["cells"]
        if c["split"] == "holdout"
        and c["family"] == "all"
        and c["path"] == row["path"]
        and c["channel"] == "hybrid"
    )
    assert cell["candidate_recall"] == 1
    assert cell["ranked_recall_at_20"] == cell["rendered_recall"] < 1
    assert cell["answer_success"] < 1
    row["recovered_sources"] = []
    without_candidate = assess_bounded_comparison(CheckpointReleaseBundle.model_validate(value))
    other = next(
        c
        for c in without_candidate["cells"]
        if all(c[k] == cell[k] for k in ("split", "family", "path", "channel"))
    )
    assert other["candidate_recall"] < cell["candidate_recall"]
    for key in ("ranked_recall_at_20", "rendered_recall", "answer_success"):
        assert other[key] == cell[key]
    assert bundle.model_dump_json() == original


def test_failed_operational_gate_requires_cost_only_original_report() -> None:
    value = fixture().model_dump(mode="json")
    value["gates"][9]["failed"] = 1
    with pytest.raises(ValueError, match="operational gate"):
        assess_bounded_comparison(CheckpointReleaseBundle.model_validate(value))


def cost_only_gate_fixture() -> tuple[CheckpointReleaseBundle, str]:
    value = with_quality_failures().model_dump(mode="json")
    gate = next(g for g in value["gates"] if g["gate"] == 10)
    report = {
        "version": "recall-gate-report@1",
        "identity_sha256": gate["identity_sha256"],
        "gate": 10,
        "checks": [
            {"id": "per-answer-cost", "passed": False},
            {"id": "per-answer-context", "passed": True},
            {"id": "bounded-search-and-rounds", "passed": True},
            {"id": "latency-ceiling", "passed": True},
            {"id": "finite-retries-deadlines-cancellation", "passed": True},
        ],
    }
    raw = json.dumps(report) + "\n"
    gate.update(report_sha256=hashlib.sha256(raw.encode()).hexdigest(), passed=4, failed=1)
    return CheckpointReleaseBundle.model_validate(value), raw


def test_cost_only_gate_report_preserves_failure_without_relaxing_other_operations() -> None:
    bundle, raw = cost_only_gate_fixture()
    original = bundle.model_dump_json()
    with pytest.raises(ValueError):
        score_checkpoint_release(bundle)
    report = assess_bounded_comparison(bundle, operational_gate_report=raw)
    assert report["original_quality_gates_satisfied"] is False
    assert any(e.get("gate") == 10 for e in report["exceptions"])
    assert bundle.model_dump_json() == original


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "identity",
        "gate",
        "counts",
        "duplicate",
        "missing",
        "retry",
        "deadline",
        "partial-failure",
        "cancellation",
        "latency",
        "no-cost",
    ],
)
def test_operational_gate_rejects_unbound_or_non_cost_failures(change: str) -> None:
    bundle, raw = cost_only_gate_fixture()
    value = bundle.model_dump(mode="json")
    gate = next(g for g in value["gates"] if g["gate"] == 10)
    report = json.loads(raw)
    if change == "hash":
        raw += " "
    else:
        if change == "identity":
            report["identity_sha256"] = "9" * 64
        elif change == "gate":
            report["gate"] = 9
        elif change == "counts":
            gate["passed"] += 1
        elif change == "duplicate":
            report["checks"][-1] = report["checks"][1]
        elif change == "missing":
            report["checks"] = report["checks"][:1]
        elif change == "no-cost":
            for row in value["observations"]:
                row["cost_usd"] = 0
        else:
            report["checks"][-1] = {"id": change, "passed": False}
            gate.update(passed=3, failed=2)
        raw = json.dumps(report) + "\n"
        gate["report_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(ValueError, match="operational gate"):
        assess_bounded_comparison(
            CheckpointReleaseBundle.model_validate(value), operational_gate_report=raw
        )


@pytest.mark.parametrize("duplicate", [False, True])
def test_operational_gate_receipt_must_be_present_exactly_once(duplicate: bool) -> None:
    value = fixture().model_dump(mode="json")
    if duplicate:
        value["gates"][0] = value["gates"][9]
    else:
        value["gates"][9] = value["gates"][0]
    with pytest.raises(ValueError, match="operational gate"):
        assess_bounded_comparison(CheckpointReleaseBundle.model_validate(value))
