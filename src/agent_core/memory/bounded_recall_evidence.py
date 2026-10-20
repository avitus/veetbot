"""Explicit ADR-0220 assessment; original quality gates remain unchanged."""

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, StrictBool

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.domain.general_recall_release import Digest, Identifier, ReleaseValue
from agent_core.memory.general_recall_evidence import _require, _score_measurements, _unique_object


class _OperationalCheck(ReleaseValue):
    id: Identifier
    passed: StrictBool


class _OperationalReport(ReleaseValue):
    version: Literal["recall-gate-report@1"]
    identity_sha256: Digest
    gate: Literal[10]
    checks: tuple[_OperationalCheck, ...] = Field(min_length=1, max_length=20000)


def _cost_only_operational_failure(bundle: CheckpointReleaseBundle, raw: str | None) -> bool:
    """Bind the original report and allow only its measured per-answer cost failure."""
    receipts = [g for g in bundle.gates if g.gate == 10]
    _require(len(receipts) == 1, "operational gate receipt missing or duplicated")
    receipt = receipts[0]
    if receipt.failed == 0:
        return False
    _require(raw is not None and len(raw) <= 2 * 1024 * 1024, "operational gate report required")
    assert raw is not None
    _require(
        hashlib.sha256(raw.encode()).hexdigest() == receipt.report_sha256,
        "operational gate report hash mismatch",
    )
    try:
        report = _OperationalReport.model_validate(
            json.loads(raw, object_pairs_hook=_unique_object)
        )
    except ValueError as exc:
        raise ValueError("Invalid operational gate report") from exc
    checks = {check.id: check.passed for check in report.checks}
    _require(
        report.identity_sha256 == receipt.identity_sha256
        and len(checks) == len(report.checks)
        and sum(checks.values()) == receipt.passed
        and len(checks) - sum(checks.values()) == receipt.failed
        and {name for name, passed in checks.items() if not passed} == {"per-answer-cost"}
        and all(
            checks.get(name) is True
            for name in ("per-answer-context", "bounded-search-and-rounds", "latency-ceiling")
        )
        and any(
            row.cost_usd > bundle.preregistration.maximum_case_usd for row in bundle.observations
        ),
        "operational gate failure is not exclusively measured per-answer cost",
    )
    return True


def assess_bounded_comparison(
    bundle: CheckpointReleaseBundle, *, operational_gate_report: str | None = None
) -> dict[str, Any]:
    """Recompute comparative benefit and retain every original quality exception."""
    bundle = CheckpointReleaseBundle.model_validate(bundle.model_dump())
    identity = hashlib.sha256(bundle.identity.model_dump_json().encode()).hexdigest()
    _require(
        bundle.lineage.parent_identity_sha256 == identity
        and bundle.lineage.evaluated_inputs_sha256 == bundle.identity.evaluated_inputs_sha256,
        "checkpoint measurement lineage mismatch",
    )
    exceptions: list[dict[str, Any]] = []
    cost_only_failure = _cost_only_operational_failure(bundle, operational_gate_report)
    report = _score_measurements(
        bundle, quality_exceptions=exceptions, operational_gate_cost_exception=cost_only_failure
    )
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
