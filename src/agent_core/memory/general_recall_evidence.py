"""Recompute M33 admission from a complete, exact-version reviewed bundle.

The operator supplies trusted review and runner artifacts. Hashes bind their
identity; they do not prove honest authorship, instrumentation or human review.
No diagnostic runner or aggregate pass flag can stand in for these observations.
"""

import hashlib
import json
import math
import os
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.domain.general_recall_release import (
    RecallExpectedSource,
    RecallObservation,
    RecallReleaseBundle,
    RecallReleaseIdentity,
    RecallScenario,
    RecallSourceWitness,
)
from agent_core.memory.reconsolidation_evidence import implementation_digest

_FAMILIES = {
    "attributes",
    "preferences",
    "relationships",
    "goals",
    "decisions",
    "routines",
    "temporal",
    "complex",
}
_MAX_BYTES = 16 * 1024 * 1024


def scorer_digest(root: Path) -> str:
    return hashlib.sha256(
        b"".join(
            (root / name).read_bytes()
            for name in (
                "src/agent_core/domain/general_recall_release.py",
                "src/agent_core/memory/general_recall_evidence.py",
            )
        )
    ).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(f"general recall evidence: {reason}")


def _unique(values: tuple[str, ...]) -> bool:
    return len(values) == len(set(values))


def source_evidence(row: RecallObservation, scenario: RecallScenario, stage: str) -> set[str]:
    return source_labels(scenario.evidence_sources, row.recovered_sources, stage)


def source_labels(
    expected_sources: Mapping[str, tuple[RecallExpectedSource, ...]],
    recovered_sources: Sequence[RecallSourceWitness],
    stage: str,
) -> set[str]:
    """Count an authored claim only when every exact supporting source was observed."""
    if stage == "formed":
        return set()
    observed = {
        (source.source_id, source.text_sha256)
        for source in recovered_sources
        if (stage != "ranked" or (source.anchor_rank is not None and source.anchor_rank <= 20))
        and (stage != "rendered" or source.rendered)
    }
    return {
        label
        for label, expected in expected_sources.items()
        if expected and {(source.source_id, source.text_sha256) for source in expected} <= observed
    }


def _stage(row: RecallObservation, scenario: RecallScenario, stage: str) -> set[str]:
    return set(getattr(row, stage)) | source_evidence(row, scenario, stage)


def _answer(row: RecallObservation, scenario: RecallScenario) -> bool:
    return (
        row.action == scenario.expected_action
        and set(row.answer_atoms) == set(scenario.required_answer_atoms)
        and set(scenario.required_evidence) <= _stage(row, scenario, "rendered")
        and not row.unsupported_atoms
        and not row.invalid_current_ids
        and not row.boundary_failures
    )


def _validate_corpus(
    bundle: RecallReleaseBundle | CheckpointReleaseBundle,
) -> dict[str, RecallScenario]:
    scenarios = {s.id: s for s in bundle.scenarios}
    _require(len(scenarios) == len(bundle.scenarios), "duplicate scenario")
    _require(
        len({s.scenario_lineage for s in bundle.scenarios}) == len(scenarios),
        "scenario lineage variants cannot inflate the census",
    )
    counts = Counter(s.family for s in bundle.scenarios)
    _require(set(counts) == _FAMILIES and min(counts.values()) >= 50, "family census")
    for field in ("scenario_lineage", "query_lineage", "history_sha256"):
        development = {getattr(s, field) for s in bundle.scenarios if s.split == "development"}
        holdout = {getattr(s, field) for s in bundle.scenarios if s.split == "holdout"}
        _require(not development & holdout, "development/holdout lineage overlap")
    for family in _FAMILIES:
        _require(
            {s.split for s in bundle.scenarios if s.family == family} == {"development", "holdout"},
            "family split coverage",
        )
    for scenario in bundle.scenarios:
        for expected in scenario.evidence_sources.values():
            _require(
                1 <= len(expected) <= 20
                and len({source.source_id for source in expected}) == len(expected),
                "invalid expected source provenance",
            )
        source_digests: dict[str, str] = {}
        for sources in scenario.evidence_sources.values():
            for source in sources:
                previous = source_digests.setdefault(source.source_id, source.text_sha256)
                _require(previous == source.text_sha256, "conflicting source identity")
        _require(
            _unique(scenario.required_evidence) and _unique(scenario.required_answer_atoms),
            "duplicate expected evidence or answer atom",
        )
        _require(
            scenario.expected_action != "answer" or bool(scenario.required_answer_atoms),
            "answer case has no mechanically scored answer atoms",
        )
    _require(any(s.known_incident for s in bundle.scenarios), "missing incident regressions")
    return scenarios


def score_release(bundle: RecallReleaseBundle) -> dict[str, Any]:
    """Validate complete observations and recompute admission, never trust a pass flag.

    Formation/candidate/rank/render recall counts required evidence IDs. Answer
    success also requires the expected action/atoms and rendered supporting IDs.
    Paired confidence intervals use the normal approximation to per-case binary
    outcome differences; they are reported, not an additional acceptance gate.
    """
    # model_copy/model_construct are not validation, including for nested values.
    bundle = RecallReleaseBundle.model_validate(bundle.model_dump())
    return _score_measurements(bundle)


def _score_measurements(
    bundle: RecallReleaseBundle | CheckpointReleaseBundle,
) -> dict[str, Any]:
    """Shared numeric gates; public callers validate their own versioned schema."""
    if bundle.version == "general-recall-release@1":
        _require(
            not any(s.evidence_sources for s in bundle.scenarios)
            and not any(
                r.recovered_sources or r.rendered_noise_items is not None
                for r in bundle.observations
            ),
            "version one cannot carry source recovery accounting",
        )
    else:
        _require(
            all(r.rendered_noise_items is not None for r in bundle.observations),
            "source-accounted evidence requires measured noise counts",
        )
    scenarios = _validate_corpus(bundle)
    identity_hash = hashlib.sha256(bundle.identity.model_dump_json().encode()).hexdigest()
    manifest_hash = hashlib.sha256(
        b"".join(s.model_dump_json().encode() + b"\n" for s in bundle.scenarios)
    ).hexdigest()
    _require(
        manifest_hash == bundle.identity.scenario_manifest_sha256
        and all(r.identity_sha256 == identity_hash for r in bundle.observations)
        and all(g.identity_sha256 == identity_hash for g in bundle.gates),
        "observation, gate or scenario manifest tuple mismatch",
    )
    _require(
        hashlib.sha256(bundle.preregistration.model_dump_json().encode()).hexdigest()
        == bundle.identity.preregistration_sha256,
        "preregistration tuple mismatch",
    )
    _require(
        {g.gate for g in bundle.gates} == set(range(1, 12))
        and all(g.failed == 0 for g in bundle.gates),
        "incomplete or failing gate receipts",
    )
    reduced = isinstance(bundle, CheckpointReleaseBundle)
    expected = {
        (s.id, path, pipeline, channel)
        for s in bundle.scenarios
        for path in ("automatic", "deliberate")
        for pipeline in (("formation",) if reduced else ("seeded", "formation"))
        for channel in (("sparse", "hybrid") if reduced else ("sparse", "dense", "hybrid"))
    }
    rows: dict[tuple[str, str, str, str], RecallObservation] = {
        (r.scenario_id, r.path, r.pipeline, r.channel): r for r in bundle.observations
    }
    _require(
        set(rows) == expected and len(rows) == len(bundle.observations),
        "observation coverage differs from the registered experiment",
    )
    grouped: dict[tuple[str, str, str, str, str], list[RecallObservation]] = defaultdict(list)
    for row in bundle.observations:
        scenario = scenarios[row.scenario_id]
        expected_sources = {
            (source.source_id, source.text_sha256)
            for expected_sources in scenario.evidence_sources.values()
            for source in expected_sources
        }
        _require(
            all(
                (source.source_id, source.text_sha256) in expected_sources
                for source in row.recovered_sources
            )
            and len({(s.session_id, s.event_sequence) for s in row.recovered_sources})
            == len(row.recovered_sources)
            and len({s.source_id for s in row.recovered_sources}) == len(row.recovered_sources)
            and len(row.recovered_sources) <= row.searches * 3,
            "invalid recovered source provenance or work bound",
        )
        if bundle.version != "general-recall-release@1":
            _require(
                row.global_rank_certifiable is True
                and all(source.anchor_rank is not None for source in row.recovered_sources),
                "global ranking was not observed",
            )
        else:
            _require(row.global_rank_certifiable is None, "version one has no global ranking field")
        _require(row.observed_at > bundle.preregistration.registered_at, "late preregistration")
        _require(row.context_tokens <= bundle.preregistration.context_tokens, "context budget")
        _require(row.cost_usd <= bundle.preregistration.maximum_case_usd, "cost ceiling")
        _require(
            not row.boundary_failures and not row.invalid_current_ids,
            "boundary or invalid-current-fact failure",
        )
        for field in ("formed", "candidates", "ranked", "rendered", "answer_atoms"):
            _require(_unique(getattr(row, field)), "duplicate stage evidence")
        _require(
            set(row.rendered) <= set(row.ranked) <= set(row.candidates) <= set(row.formed),
            "inconsistent stage provenance",
        )
        if row.channel == "hybrid" and scenario.known_incident:
            _require(_answer(row, scenario), "known incident regression")
        for family in ("all", scenario.family):
            grouped[(scenario.split, row.path, row.pipeline, row.channel, family)].append(row)
    cells: list[dict[str, Any]] = []
    for (split, path, pipeline, channel, family), group in sorted(grouped.items()):
        required = sum(len(scenarios[r.scenario_id].required_evidence) for r in group)
        _require(required > 0, "family cell has no evidence recall observations")
        recall = {
            stage: sum(
                len(
                    _stage(r, scenarios[r.scenario_id], stage)
                    & set(scenarios[r.scenario_id].required_evidence)
                )
                for r in group
            )
            / required
            for stage in ("formed", "candidates", "ranked", "rendered")
        }
        correct = [_answer(r, scenarios[r.scenario_id]) for r in group]
        differences = [
            int(success)
            - int(
                _answer(rows[(r.scenario_id, path, pipeline, "sparse")], scenarios[r.scenario_id])
            )
            for r, success in zip(group, correct, strict=True)
        ]
        lift = sum(differences) / len(group)
        # At least two paired scenarios per cell are needed to report uncertainty.
        _require(len(group) >= 2, "insufficient paired observations")
        radius = 1.96 * math.sqrt(
            sum((d - lift) ** 2 for d in differences) / (len(group) * (len(group) - 1))
        )
        implicit_pairs = [
            (difference, success)
            for row, difference, success in zip(group, differences, correct, strict=True)
            if scenarios[row.scenario_id].implicit_task
        ]
        _require(len(implicit_pairs) >= 2, "insufficient implicit paired observations")
        implicit_lift = sum(d for d, _ in implicit_pairs) / len(implicit_pairs)
        implicit_radius = 1.96 * math.sqrt(
            sum((d - implicit_lift) ** 2 for d, _ in implicit_pairs)
            / (len(implicit_pairs) * (len(implicit_pairs) - 1))
        )
        times = sorted(r.elapsed_ms for r in group)
        p95 = times[math.ceil(len(times) * 0.95) - 1]
        if channel == "hybrid":
            minimum = 0.95 if family == "all" else 0.90
            _require(
                recall["ranked"] + 1e-12 >= minimum and recall["rendered"] + 1e-12 >= minimum,
                "required-evidence recall floor",
            )
            _require(p95 <= bundle.preregistration.p95_latency_ms, "latency ceiling")
            if split == "holdout" and pipeline == "formation":
                _require(family == "all" or lift + 1e-12 >= -0.02, "held-out family regression")
                _require(
                    implicit_lift + 1e-12 >= (0.10 if family == "all" else -0.02),
                    "held-out implicit answer lift or family regression",
                )
        cells.append(
            {
                "split": split,
                "path": path,
                "pipeline": pipeline,
                "channel": channel,
                "family": family,
                "cases": len(group),
                "required_evidence": required,
                "formed_recall": recall["formed"],
                "candidate_recall": recall["candidates"],
                "ranked_recall_at_20": recall["ranked"],
                "rendered_recall": recall["rendered"],
                "atomic_ranked_recall_at_20": sum(
                    len(set(r.ranked) & set(scenarios[r.scenario_id].required_evidence))
                    for r in group
                )
                / required,
                "source_rendered_recall": sum(
                    len(
                        source_evidence(r, scenarios[r.scenario_id], "rendered")
                        & set(scenarios[r.scenario_id].required_evidence)
                    )
                    for r in group
                )
                / required,
                "answer_success": sum(correct) / len(group),
                "answer_lift": lift,
                "answer_lift_ci95": [max(-1.0, lift - radius), min(1.0, lift + radius)],
                "implicit_cases": len(implicit_pairs),
                "implicit_answer_success": sum(success for _, success in implicit_pairs)
                / len(implicit_pairs),
                "implicit_answer_lift": implicit_lift,
                "implicit_answer_lift_ci95": [
                    max(-1.0, implicit_lift - implicit_radius),
                    min(1.0, implicit_lift + implicit_radius),
                ],
                "retrieved_but_unused": sum(
                    bool(scenarios[r.scenario_id].required_evidence)
                    and set(scenarios[r.scenario_id].required_evidence)
                    <= _stage(r, scenarios[r.scenario_id], "rendered")
                    and not set(scenarios[r.scenario_id].required_answer_atoms)
                    <= set(r.answer_atoms)
                    for r in group
                ),
                "unsupported_answers": sum(bool(r.unsupported_atoms) for r in group),
                "incorrect_actions": sum(
                    r.action != scenarios[r.scenario_id].expected_action for r in group
                ),
                "p50_ms": times[math.ceil(len(times) * 0.50) - 1],
                "p95_ms": p95,
                "input_tokens": sum(r.input_tokens for r in group),
                "output_tokens": sum(r.output_tokens for r in group),
                "cost_usd": sum(r.cost_usd for r in group),
                "noise_items": sum(
                    r.rendered_noise_items
                    if r.rendered_noise_items is not None
                    else len(set(r.rendered) - set(scenarios[r.scenario_id].required_evidence))
                    for r in group
                ),
            }
        )
    return {
        "version": bundle.version,
        "activation_eligible": True,
        "ci_method": "paired normal approximation, 95 percent",
        "scenarios": len(scenarios),
        "observations": len(rows),
        "cells": cells,
    }


def _read_bounded(path: Path) -> bytes:
    # Avoid unbounded allocation and blocking on FIFOs/devices or following links.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        _require(
            stat.S_ISREG(info.st_mode) and info.st_size <= _MAX_BYTES,
            "evidence must be a bounded regular file",
        )
        raw = handle.read(_MAX_BYTES + 1)
        _require(len(raw) <= _MAX_BYTES, "evidence file exceeds size limit")
        return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def bind_semantic_evidence(
    root: Path, path: Path, expected: RecallReleaseIdentity
) -> Callable[[], bool]:
    """Bind reviewed evidence to independently supplied active dependencies.

    The composition, never tool/model input, supplies ``expected``. This function
    verifies source/scorer bytes locally as well. Any observed replacement or
    removal latches withdrawal until a new explicit binding validates the bundle.
    """
    expected = RecallReleaseIdentity.model_validate(expected.model_dump())
    raw = _read_bounded(path)
    bundle = RecallReleaseBundle.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    _require(
        bundle.identity == expected
        and expected.implementation_sha256 == implementation_digest(root)
        and expected.scorer_sha256 == scorer_digest(root),
        "active dependency tuple mismatch",
    )
    score_release(bundle)
    bound_digest = hashlib.sha256(raw).digest()
    revoked = False

    def admitted() -> bool:
        nonlocal revoked
        if not revoked:
            try:
                revoked = hashlib.sha256(_read_bounded(path)).digest() != bound_digest
            except (OSError, ValueError):
                revoked = True
        return not revoked

    return admitted
