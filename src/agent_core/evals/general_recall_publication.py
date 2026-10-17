"""Offline publication of reviewed M33 evidence; never supplies human judgments."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from agent_core.domain.general_recall_release import (
    Digest,
    Identifier,
    RecallReleaseBundle,
    RecallReleaseIdentity,
    ReleaseValue,
)
from agent_core.evals.general_recall_collection import compile_collection
from agent_core.evals.general_recall_composition import system_clock
from agent_core.evals.memory_distillation import require_committed_tree
from agent_core.memory.general_recall_evidence import (
    _answer,
    _read_bounded,
    _stage,
    _unique_object,
    score_release,
    scorer_digest,
)
from agent_core.memory.recall_release_binding import capture_inputs
from agent_core.memory.reconsolidation_evidence import implementation_digest


class IndependenceReview(ReleaseValue):
    version: Literal["recall-independence-review@1"]
    reviewer: str = Field(min_length=2, max_length=256)
    reviewed_at: AwareDatetime
    decision: Literal["accepted"]
    corpus_sha256: Digest
    scenario_manifest_sha256: Digest
    reviewed_scenarios: tuple[Identifier, ...] = Field(min_length=400, max_length=10000)


class FailureReview(ReleaseValue):
    version: Literal["recall-failure-review@1"]
    reviewer: str = Field(min_length=2, max_length=256)
    reviewed_at: AwareDatetime
    decision: Literal["accepted"]
    identity_sha256: Digest
    observations_sha256: Digest
    reviewed_scenarios: tuple[Identifier, ...] = Field(max_length=10000)


class GateCheck(ReleaseValue):
    id: Identifier
    passed: bool


class GateReport(ReleaseValue):
    version: Literal["recall-gate-report@1"]
    identity_sha256: Digest
    gate: int = Field(ge=1, le=11)
    checks: tuple[GateCheck, ...] = Field(min_length=1, max_length=20000)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(f"Recall publication: {reason}")


def publish_release(
    root: Path,
    bundle_path: Path,
    expected: RecallReleaseIdentity,
    *,
    independence_review: Path,
    failure_review: Path,
    gate_reports: tuple[Path, ...],
    collection_manifest: Path,
    output: Path,
) -> Path:
    """Recompute gates, bind independently supplied reviews, then write new files.

    The caller supplies the independently resolved deployment identity. Reviewer
    attribution and test-report provenance remain operator responsibilities; a
    digest is not a signature or evidence that a human performed the review.
    This compiler never turns a development run or an aggregate score into a
    receipt and never changes a deployment's configuration.
    """
    _require(not output.exists(), "output already exists; retain prior publication attempts")
    expected = RecallReleaseIdentity.model_validate(expected.model_dump())
    require_committed_tree(root, expected.build_ref)
    _require(
        expected.implementation_sha256 == implementation_digest(root)
        and expected.scorer_sha256 == scorer_digest(root),
        "implementation or scorer changed",
    )
    if expected.evaluated_inputs_sha256 is not None:
        _require(
            capture_inputs(root, expected.model_dump()).digest == expected.evaluated_inputs_sha256,
            "evaluated inputs changed",
        )
    _require(len(gate_reports) == 11, "eleven gate report files are required")
    paths = (bundle_path, independence_review, failure_review, collection_manifest, *gate_reports)
    raw = {path: _read_bounded(path) for path in paths}
    bundle = RecallReleaseBundle.model_validate(
        json.loads(raw[bundle_path], object_pairs_hook=_unique_object)
    )
    _require(
        bundle.version == "general-recall-release@2", "current publication requires version two"
    )
    _require(bundle.identity == expected, "independent deployment identity does not match")
    collected = compile_collection(collection_manifest, expected, bundle.preregistration)
    _require(
        collected.scenarios == bundle.scenarios
        and collected.observations == bundle.observations
        and collected.manifest_sha256 == _digest(raw[collection_manifest]),
        "bundle differs from original runtime observations or reviewed corpus",
    )
    _require(collected.confirmed, "original pre-execution confirmation is required")
    comparison = score_release(bundle)
    independence = IndependenceReview.model_validate(
        json.loads(raw[independence_review], object_pairs_hook=_unique_object)
    )
    failures = FailureReview.model_validate(
        json.loads(raw[failure_review], object_pairs_hook=_unique_object)
    )
    scenarios = {scenario.id: scenario for scenario in bundle.scenarios}
    _require(
        _digest(raw[independence_review]) == expected.independence_review_sha256
        and independence.corpus_sha256 == expected.corpus_sha256
        and independence.scenario_manifest_sha256 == expected.scenario_manifest_sha256
        and len(set(independence.reviewed_scenarios)) == len(independence.reviewed_scenarios)
        and set(independence.reviewed_scenarios) == set(scenarios)
        and independence.reviewed_at <= bundle.preregistration.registered_at,
        "independence review is missing, late or bound to different scenarios",
    )
    identity = _digest(expected.model_dump_json().encode())
    observed = _digest(
        b"".join(row.model_dump_json().encode() + b"\n" for row in bundle.observations)
    )
    difficult = {
        row.scenario_id
        for row in bundle.observations
        if not _answer(row, scenarios[row.scenario_id])
        or any(
            not set(scenarios[row.scenario_id].required_evidence)
            <= _stage(row, scenarios[row.scenario_id], stage)
            for stage in ("formed", "candidates", "ranked", "rendered")
        )
    }
    _require(
        failures.identity_sha256 == identity
        and failures.observations_sha256 == observed
        and len(set(failures.reviewed_scenarios)) == len(failures.reviewed_scenarios)
        and difficult <= set(failures.reviewed_scenarios) <= set(scenarios)
        and max(row.observed_at for row in bundle.observations)
        <= failures.reviewed_at
        <= system_clock().now(),
        "difficult-failure review is incomplete or belongs to other observations",
    )
    reports = [
        GateReport.model_validate(json.loads(raw[path], object_pairs_hook=_unique_object))
        for path in gate_reports
    ]
    _require(
        {report.gate for report in reports} == set(range(1, 12)), "gate reports are duplicated"
    )
    receipts = {receipt.gate: receipt for receipt in bundle.gates}
    for path, report in zip(gate_reports, reports, strict=True):
        receipt = receipts[report.gate]
        _require(
            report.identity_sha256 == identity
            and _digest(raw[path]) == receipt.report_sha256
            and len({check.id for check in report.checks}) == len(report.checks)
            and sum(check.passed for check in report.checks) == receipt.passed
            and sum(not check.passed for check in report.checks) == receipt.failed,
            "gate report does not match its receipt or observed checks",
        )
    # Validate once more before creating any output. Review or source changes
    # during a long score cannot silently publish the earlier accepted bytes.
    _require(
        all(_read_bounded(path) == data for path, data in raw.items()),
        "input changed during publication",
    )
    _require(
        all(_digest(_read_bounded(path)) == digest for path, digest in collected.artifacts),
        "original runtime input changed during publication",
    )
    require_committed_tree(root, expected.build_ref)
    _require(
        implementation_digest(root) == expected.implementation_sha256
        and scorer_digest(root) == expected.scorer_sha256,
        "implementation changed during publication",
    )
    if expected.evaluated_inputs_sha256 is not None:
        _require(
            capture_inputs(root, expected.model_dump()).digest == expected.evaluated_inputs_sha256,
            "evaluated inputs changed during publication",
        )
    artifacts = {
        "manifest.json": expected.model_dump_json(indent=2).encode() + b"\n",
        "report.json": json.dumps(comparison, indent=2, allow_nan=False).encode() + b"\n",
        "provenance.json": json.dumps(
            {
                "version": "recall-publication@1",
                "identity_sha256": identity,
                "observations_sha256": observed,
                "collection_manifest_sha256": collected.manifest_sha256,
                "actual_provider_cost_usd": str(collected.actual_cost_usd),
                "independence_review_sha256": _digest(raw[independence_review]),
                "failure_review_sha256": _digest(raw[failure_review]),
                "gate_report_sha256": {
                    str(r.gate): _digest(raw[p]) for p, r in zip(gate_reports, reports, strict=True)
                },
            },
            indent=2,
        ).encode()
        + b"\n",
        # The bundle is the last file: a failed earlier write never leaves an
        # apparently complete admission bundle. Partial directories are retained.
        "release.json": raw[bundle_path],
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, content in artifacts.items():
        with (output / name).open("xb") as stream:
            stream.write(content)
    return output / "release.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--independence-review", type=Path, required=True)
    parser.add_argument("--failure-review", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, action="append", required=True)
    parser.add_argument("--collection-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = RecallReleaseIdentity.model_validate(
        json.loads(_read_bounded(args.identity), object_pairs_hook=_unique_object)
    )
    path = publish_release(
        args.root,
        args.bundle,
        expected,
        independence_review=args.independence_review,
        failure_review=args.failure_review,
        gate_reports=tuple(args.gate_report),
        collection_manifest=args.collection_manifest,
        output=args.output,
    )
    print(json.dumps({"published": str(path)}))


if __name__ == "__main__":
    main()
