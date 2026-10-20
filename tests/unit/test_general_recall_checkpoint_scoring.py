"""Synthetic comparisons prove unchanged gates; these are never release evidence."""

import hashlib
from functools import lru_cache
from typing import Any

import pytest

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.domain.general_recall_release import RecallReleaseBundle, RecallReleaseIdentity
from agent_core.evals.general_recall_checkpoint_scoring import score_checkpoint_release
from agent_core.memory.general_recall_evidence import score_release
from tests.unit.test_general_recall_source_scoring import recovered_bundle

pytestmark = pytest.mark.static


@lru_cache(maxsize=1)
def fixture() -> CheckpointReleaseBundle:
    value = recovered_bundle().model_dump(mode="json")
    value["version"] = "general-recall-checkpoint-release@1"
    value["observations"] = [
        row
        for row in value["observations"]
        if row["pipeline"] == "formation" and row["channel"] in {"sparse", "hybrid"}
    ]
    value["identity"]["evaluated_inputs_sha256"] = "b" * 64
    identity = hashlib.sha256(
        RecallReleaseIdentity.model_validate(value["identity"]).model_dump_json().encode()
    ).hexdigest()
    for row in (*value["observations"], *value["gates"]):
        row["identity_sha256"] = identity
    value["lineage"] = {
        "approval_sha256": "a" * 64,
        "plan_sha256": "c" * 64,
        "parent_archive_sha256": "d" * 64,
        "parent_identity_sha256": identity,
        "evaluated_inputs_sha256": "b" * 64,
        "collector_commit": "e" * 40,
        "collector_manifest_sha256": "f" * 64,
        "scorer_sha256": "0" * 64,
        "collection_manifest_sha256": "1" * 64,
    }
    return CheckpointReleaseBundle.model_validate(value)


def test_reduced_matrix_matches_original_cells_without_claiming_activation() -> None:
    original = score_release(recovered_bundle())
    before = fixture().model_dump_json()
    report = score_checkpoint_release(fixture())
    assert fixture().model_dump_json() == before
    assert report["observations"] == 1600 and report["scenarios"] == 400
    assert report["activation_eligible"] is False
    assert report["quality_gates_satisfied"] is True
    assert report["cells"] == [
        cell
        for cell in original["cells"]
        if cell["pipeline"] == "formation" and cell["channel"] in {"sparse", "hybrid"}
    ]


@pytest.mark.parametrize("field", ["parent_identity_sha256", "evaluated_inputs_sha256"])
def test_original_measurement_and_evaluated_input_identity_cannot_be_retagged(field: str) -> None:
    original = fixture()
    changed = original.model_copy(
        update={"lineage": original.lineage.model_copy(update={field: "9" * 64})}
    )
    with pytest.raises(ValueError, match=r"identity|input"):
        score_checkpoint_release(changed)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "dense",
        "seeded",
        "foreign",
        "boundary",
        "cost",
        "context",
        "latency",
        "rank",
        "noise",
        "timestamp",
        "failed_gate",
        "missing_lift",
        "recall_floor",
    ],
)
def test_reduced_protocol_preserves_coverage_and_every_quality_limit(change: str) -> None:
    value: dict[str, Any] = fixture().model_dump(mode="json")
    rows = value["observations"]
    hybrid = next(row for row in rows if row["channel"] == "hybrid")
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows[-1] = rows[0]
    elif change in {"dense", "seeded", "foreign"}:
        field, result = {
            "dense": ("channel", "dense"),
            "seeded": ("pipeline", "seeded"),
            "foreign": ("identity_sha256", "9" * 64),
        }[change]
        rows[0][field] = result
    elif change == "failed_gate":
        value["gates"][0]["failed"] = 1
    elif change == "missing_lift":
        for row in rows:
            if row["channel"] == "sparse":
                peer = next(
                    r
                    for r in rows
                    if r["scenario_id"] == row["scenario_id"]
                    and r["path"] == row["path"]
                    and r["channel"] == "hybrid"
                )
                row.update(
                    {
                        key: peer[key]
                        for key in (
                            "formed",
                            "candidates",
                            "ranked",
                            "rendered",
                            "recovered_sources",
                            "answer_atoms",
                            "action",
                        )
                    }
                )
    elif change == "recall_floor":
        incidents = {s["id"] for s in value["scenarios"] if s["known_incident"]}
        for row in rows:
            if row["channel"] == "hybrid" and row["scenario_id"] not in incidents:
                row["rendered"] = []
                row["recovered_sources"] = []
    elif change == "latency":
        for row in rows:
            if row["channel"] == "hybrid":
                row["elapsed_ms"] = value["preregistration"]["p95_latency_ms"] + 1
    else:
        field, result = {
            "boundary": ("boundary_failures", ["privacy"]),
            "cost": ("cost_usd", value["preregistration"]["maximum_case_usd"] + 1),
            "context": ("context_tokens", value["preregistration"]["context_tokens"] + 1),
            "rank": ("global_rank_certifiable", False),
            "noise": ("rendered_noise_items", None),
            "timestamp": ("observed_at", value["preregistration"]["registered_at"]),
        }[change]
        hybrid[field] = result
    with pytest.raises(ValueError):
        score_checkpoint_release(CheckpointReleaseBundle.model_validate(value))


def test_legacy_formats_still_refuse_reduced_matrix_and_new_protocol() -> None:
    value = fixture().model_dump(mode="json")
    with pytest.raises(ValueError):
        RecallReleaseBundle.model_validate(value)
    value.pop("lineage")
    value["version"] = "general-recall-release@2"
    with pytest.raises(ValueError):
        RecallReleaseBundle.model_validate(value)
