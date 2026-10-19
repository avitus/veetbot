"""Explicit ADR-0220 assessment; original quality gates remain unchanged."""

import hashlib
from typing import Any

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.memory.general_recall_evidence import _require, _score_measurements


def assess_bounded_comparison(bundle: CheckpointReleaseBundle) -> dict[str, Any]:
    """Recompute comparative benefit and retain every original quality exception."""
    bundle = CheckpointReleaseBundle.model_validate(bundle.model_dump())
    identity = hashlib.sha256(bundle.identity.model_dump_json().encode()).hexdigest()
    _require(
        bundle.lineage.parent_identity_sha256 == identity
        and bundle.lineage.evaluated_inputs_sha256 == bundle.identity.evaluated_inputs_sha256,
        "checkpoint measurement lineage mismatch",
    )
    exceptions: list[dict[str, Any]] = []
    report = _score_measurements(bundle, quality_exceptions=exceptions)
    cells = {
        (cell["path"], cell["channel"]): cell
        for cell in report["cells"]
        if cell["split"] == "holdout" and cell["family"] == "all"
    }
    benefit = all(
        all(
            cells[(path, "hybrid")][metric] > cells[(path, "sparse")][metric]
            for metric in ("ranked_recall_at_20", "rendered_recall")
        )
        and cells[(path, "hybrid")]["answer_success"] >= cells[(path, "sparse")]["answer_success"]
        for path in ("automatic", "deliberate")
    )
    report.update(
        version="recall-bounded-comparison@1",
        activation_eligible=False,
        comparative_benefit_satisfied=benefit,
        original_quality_gates_satisfied=not exceptions,
        exceptions=exceptions,
        measurement_identity_sha256=identity,
    )
    return report
