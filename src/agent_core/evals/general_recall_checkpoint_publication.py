"""Publish independently reviewed checkpoint evidence without activation authority."""

import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.domain.general_recall_release import Digest, Identifier, ReleaseValue
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    _digest,
    _json,
    _read,
    _write,
)
from agent_core.evals.general_recall_checkpoint_collection import compile_checkpoint_collection
from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent
from agent_core.evals.general_recall_checkpoint_scoring import (
    checkpoint_scorer_digest,
    score_checkpoint_release,
)
from agent_core.evals.general_recall_composition import system_clock
from agent_core.evals.general_recall_continuation_budget import ParentLedger
from agent_core.evals.general_recall_publication import GateReport, IndependenceReview
from agent_core.evals.memory_distillation import require_committed_tree
from agent_core.memory.general_recall_evidence import _answer, _stage, _unique_object
from agent_core.memory.recall_release_binding import capture_inputs


class CheckpointFailureReview(ReleaseValue):
    """Supplied by an independent human; the compiler cannot authenticate authorship."""

    version: Literal["recall-checkpoint-failure-review@1"]
    reviewer: str = Field(min_length=2, max_length=256)
    reviewer_kind: Literal["human"]
    independent_of_implementation: Literal[True]
    reviewed_at: AwareDatetime
    decision: Literal["accepted"]
    identity_sha256: Digest
    observations_sha256: Digest
    review_items_sha256: Digest
    reviewed_items: tuple[Digest, ...] = Field(max_length=20000)
    reviewed_scenarios: tuple[Identifier, ...] = Field(max_length=400)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("Checkpoint publication: " + reason)


def _plan_digest(store: CheckpointStore) -> str:
    return _digest(store.plan.encoded())


def publish_checkpoint_release(
    root: Path,
    frozen_root: Path,
    bundle_path: Path,
    *,
    store: CheckpointStore,
    parent: CheckpointParent,
    parents: tuple[ParentLedger, ...],
    approval: Path,
    operator_manifest: Path,
    independence_review: Path,
    failure_review: Path,
    gate_reports: tuple[Path, ...],
    output: Path,
) -> Path:
    """Recompile originals and bind supplied human review before any publication.

    Real reviewer attribution and provenance remain operator responsibilities.
    Never generate review judgments to fill these files. This creates evaluated
    evidence only: deployment integration and activation require a separate binding.
    """
    _require(not output.exists(), "output already exists; retain previous publications")
    paths = (
        bundle_path,
        approval,
        operator_manifest,
        independence_review,
        failure_review,
        *gate_reports,
    )
    raw = {p: _read(p) for p in paths}
    bundle = CheckpointReleaseBundle.model_validate(
        json.loads(raw[bundle_path], object_pairs_hook=_unique_object)
    )
    lineage = bundle.lineage
    _require(
        bundle.identity == parent.identity
        and bundle.preregistration == parent.preregistration
        and lineage.parent_identity_sha256 == parent.identity_sha256
        and lineage.parent_archive_sha256 == parent.archive.sha256
        and lineage.plan_sha256 == _plan_digest(store),
        "measurement or continuation identity changed",
    )
    _require(
        lineage.approval_sha256 == _digest(raw[approval])
        and lineage.collector_manifest_sha256 == _digest(raw[operator_manifest]),
        "owner approval or frozen collector identity changed",
    )

    def verify_source() -> None:
        require_committed_tree(root, lineage.collector_commit)
        require_committed_tree(frozen_root, parent.identity.build_ref)
        _require(
            checkpoint_scorer_digest(root) == lineage.scorer_sha256
            and capture_inputs(frozen_root, parent.identity.model_dump(mode="json")).digest
            == lineage.evaluated_inputs_sha256,
            "scorer or original evaluated source changed",
        )

    verify_source()
    collected = compile_checkpoint_collection(store, parent, parents)
    _require(
        collected.scenarios == bundle.scenarios
        and collected.observations == bundle.observations
        and _digest(collected.manifest) == lineage.collection_manifest_sha256
        and json.loads(collected.manifest, object_pairs_hook=_unique_object)["protocol_sha256"]
        == lineage.collector_manifest_sha256,
        "bundle differs from original rows or frozen collection protocol",
    )
    comparison = score_checkpoint_release(bundle)
    independence = IndependenceReview.model_validate(
        json.loads(raw[independence_review], object_pairs_hook=_unique_object)
    )
    scenarios = {s.id: s for s in bundle.scenarios}
    _require(
        _digest(raw[independence_review]) == bundle.identity.independence_review_sha256
        and independence.corpus_sha256 == bundle.identity.corpus_sha256
        and independence.scenario_manifest_sha256 == bundle.identity.scenario_manifest_sha256
        and set(independence.reviewed_scenarios) == set(scenarios)
        and len(independence.reviewed_scenarios) == len(scenarios)
        and independence.reviewed_at <= bundle.preregistration.registered_at,
        "independent corpus review is missing, late or changed",
    )
    failures = CheckpointFailureReview.model_validate(
        json.loads(raw[failure_review], object_pairs_hook=_unique_object)
    )
    observed_sha = _digest(
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
    for item in collected.review_items:
        if item["kind"] == "interrupted_attempt":
            difficult.add(item["attempt"]["key"][0])
        elif item["kind"] == "parent_preparation_replacement":
            difficult.add(item["approval"]["scenario_id"])
    review_items = {_digest(_json(item)) for item in collected.review_items}
    _require(
        failures.identity_sha256 == parent.identity_sha256
        and failures.observations_sha256 == observed_sha
        and failures.review_items_sha256 == _digest(_json(collected.review_items))
        and set(failures.reviewed_items) == review_items
        and len(failures.reviewed_items) == len(review_items)
        and len(set(failures.reviewed_scenarios)) == len(failures.reviewed_scenarios)
        and difficult <= set(failures.reviewed_scenarios) <= set(scenarios)
        and max(row.observed_at for row in bundle.observations)
        <= failures.reviewed_at
        <= system_clock().now(),
        "independent human failure review omits observations, interruptions or uncertain charges",
    )
    _require(len(gate_reports) == 11, "eleven original gate reports are required")
    reports = [
        GateReport.model_validate(json.loads(raw[p], object_pairs_hook=_unique_object))
        for p in gate_reports
    ]
    _require(
        {report.gate for report in reports} == set(range(1, 12)), "gate reports are duplicated"
    )
    receipts = {r.gate: r for r in bundle.gates}
    for path, report in zip(gate_reports, reports, strict=True):
        receipt = receipts[report.gate]
        _require(
            report.identity_sha256 == parent.identity_sha256
            and _digest(raw[path]) == receipt.report_sha256
            and len({c.id for c in report.checks}) == len(report.checks)
            and sum(c.passed for c in report.checks) == receipt.passed
            and sum(not c.passed for c in report.checks) == receipt.failed,
            "gate report differs from its actual checks",
        )
    verify_source()
    _require(all(_read(p) == data for p, data in raw.items()), "publication input changed")
    _require(
        compile_checkpoint_collection(store, parent, parents).manifest == collected.manifest,
        "retained collection changed during review verification",
    )
    provenance = {
        "version": "recall-checkpoint-publication@1",
        "release_sha256": _digest(raw[bundle_path]),
        "activation_eligible": False,
        "measurement_identity_sha256": parent.identity_sha256,
        "collector_commit": lineage.collector_commit,
        "collector_manifest_sha256": lineage.collector_manifest_sha256,
        "collection_manifest_sha256": lineage.collection_manifest_sha256,
        "observations_sha256": observed_sha,
        "reported_usd": str(collected.reported_usd),
        "held_usd": str(collected.held_usd),
        "review_items_sha256": failures.review_items_sha256,
        "independence_review_sha256": _digest(raw[independence_review]),
        "failure_review_sha256": _digest(raw[failure_review]),
        "gate_report_sha256": {
            str(r.gate): _digest(raw[p]) for p, r in zip(gate_reports, reports, strict=True)
        },
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, data in (
        ("identity.json", bundle.identity.model_dump_json().encode()),
        ("collection.json", collected.manifest),
        ("review-items.json", _json(collected.review_items)),
        ("report.json", _json(comparison)),
        ("provenance.json", _json(provenance)),
        ("release.json", raw[bundle_path]),
    ):
        _write(output / name, data)
    return output / "release.json"
