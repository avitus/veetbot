"""Compile the full reviewed-corpus matrix from retained runtime artifacts."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from agent_core.domain.general_recall_release import (
    Digest,
    RecallObservation,
    RecallPreregistration,
    RecallReleaseIdentity,
    RecallScenario,
    ReleaseValue,
)
from agent_core.evals.general_recall_composition import system_clock
from agent_core.evals.general_recall_confirmation import ConfirmedExecution, RecallExecutionManifest
from agent_core.evals.general_recall_corpus import RecallFixture, corpus_digest, validate_corpus
from agent_core.evals.general_recall_journal import verify_provider_journal
from agent_core.evals.general_recall_observation import ProviderAttempt, compile_runtime_observation
from agent_core.evals.general_recall_stages import authored_source_evidence
from agent_core.memory.general_recall_evidence import _read_bounded, _unique_object


class CollectionArtifact(ReleaseValue):
    role: Literal["corpus", "observations", "journal", "bank", "confirmation"]
    path: str = Field(min_length=1, max_length=4096)
    sha256: Digest


class CollectionManifest(ReleaseValue):
    version: Literal["recall-collection@1", "recall-collection@2"]
    identity_sha256: Digest
    maximum_cost_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    artifacts: tuple[CollectionArtifact, ...] = Field(min_length=3, max_length=20000)


@dataclass(frozen=True)
class CollectedObservations:
    scenarios: tuple[RecallScenario, ...]
    observations: tuple[RecallObservation, ...]
    actual_cost_usd: Decimal
    manifest_sha256: str
    artifacts: tuple[tuple[Path, str], ...] = ()
    confirmed: bool = False


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Recall collection: {message}")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _lines(raw: bytes) -> list[dict[str, Any]]:
    values = []
    for line in raw.splitlines():
        value = json.loads(line, object_pairs_hook=_unique_object)
        _require(isinstance(value, dict), "artifact contains a non-object row")
        values.append(value)
    return values


def scenario_manifest(cases: tuple[RecallFixture, ...]) -> tuple[RecallScenario, ...]:
    """Derive scoring-only metadata from the same histories the runtime consumes."""
    return tuple(
        RecallScenario(
            id=case.id,
            family=case.family,
            split=case.split,
            scenario_lineage=case.scenario_lineage,
            query_lineage=case.query_lineage,
            history_sha256=_digest(
                json.dumps([(t.speaker, t.scope, t.content) for t in case.history]).encode()
            ),
            required_evidence=tuple(label.id for label in case.evidence if label.required),
            required_answer_atoms=tuple(
                sorted(k for k, values in case.expected_values.items() if values)
            ),
            expected_action=case.expected_action,
            implicit_task=case.implicit_task,
            known_incident=case.known_incident,
            evidence_sources=authored_source_evidence(case),
        )
        for case in cases
    )


def compile_collection(
    manifest: Path, expected: RecallReleaseIdentity, preregistration: RecallPreregistration
) -> CollectedObservations:
    """Recompute complete observations and original charges, without granting admission.

    Artifacts are bounded regular files confined to the manifest's directory.
    Failed or partial collections are never replaced or omitted. Publication still
    requires independent human reviews, current deployment dependencies and every
    passing gate. The retained raw inputs remain the operator's provenance trust
    boundary; content hashes do not authenticate instrumentation.
    """
    manifest_bytes = _read_bounded(manifest)
    declared = CollectionManifest.model_validate(
        json.loads(manifest_bytes, object_pairs_hook=_unique_object)
    )
    identity = _digest(expected.model_dump_json().encode())
    _require(declared.identity_sha256 == identity, "dependency identity changed")
    _require(
        _digest(preregistration.model_dump_json().encode()) == expected.preregistration_sha256,
        "preregistration changed",
    )
    base = manifest.parent.resolve()
    artifacts: dict[str, list[tuple[Path, str]]] = {
        role: [] for role in ("corpus", "observations", "journal", "bank", "confirmation")
    }
    paths = set()
    for entry in declared.artifacts:
        path = Path(entry.path)
        _require(not path.is_absolute() and ".." not in path.parts, "artifact escapes collection")
        actual = base / path
        _require(actual.resolve().is_relative_to(base), "artifact escapes collection")
        _require(actual.resolve() not in paths, "artifact is duplicated")
        paths.add(actual.resolve())
        _require(_digest(_read_bounded(actual)) == entry.sha256, "artifact changed")
        artifacts[entry.role].append((actual, entry.sha256))
    _require(
        len(artifacts["corpus"]) == len(artifacts["journal"]) == 1
        and bool(artifacts["observations"]),
        "exactly one corpus and journal and observed rows are required",
    )
    confirmation = None
    if (
        expected.evaluated_inputs_sha256 is not None
        or expected.execution_manifest_sha256 is not None
        or declared.version == "recall-collection@2"
    ):
        _require(
            declared.version == "recall-collection@2" and len(artifacts["confirmation"]) == 1,
            "original confirmation inputs are required",
        )
        captured = RecallExecutionManifest.model_validate(
            json.loads(
                _read_bounded(artifacts["confirmation"][0][0]), object_pairs_hook=_unique_object
            )
        )
        _require(
            captured.captured_at <= preregistration.registered_at
            and captured.maximum_cost_usd == declared.maximum_cost_usd,
            "confirmation inputs or spend ceiling changed after registration",
        )
        confirmation = ConfirmedExecution(expected, captured)
        confirmation.validate(captured.variants[0].configuration)
    else:
        _require(not artifacts["confirmation"], "legacy collection cannot claim confirmation")
    cases = tuple(
        RecallFixture.model_validate(row)
        for row in _lines(_read_bounded(artifacts["corpus"][0][0]))
    )
    validate_corpus(cases, release=True)
    scenarios = scenario_manifest(cases)
    _require(
        corpus_digest(cases) == expected.corpus_sha256
        and _digest(b"".join(s.model_dump_json().encode() + b"\n" for s in scenarios))
        == expected.scenario_manifest_sha256,
        "corpus or scenario manifest differs from the reviewed identity",
    )
    by_id = {case.id: case for case in cases}
    banks = {digest: path for path, digest in artifacts["bank"]}
    _require(len(banks) == len(artifacts["bank"]), "formation bank is duplicated")
    seen: set[tuple[str, str, str, str]] = set()
    observations: list[RecallObservation] = []
    attempts: list[ProviderAttempt] = []
    now = system_clock().now()
    for path, _ in artifacts["observations"]:
        for raw in _lines(_read_bounded(path)):
            key = tuple(raw.get(k) for k in ("scenario_id", "path", "pipeline", "channel"))
            _require(
                len(observations) < len(cases) * 12
                and all(isinstance(part, str) for part in key)
                and key not in seen
                and key[0] in by_id,
                "duplicate, foreign or excess observation",
            )
            case = by_id[str(key[0])]
            if confirmation is not None:
                confirmation.validate_observation(raw)
            observed = compile_runtime_observation(
                case,
                raw,
                identity_sha256=identity,
                formation_bank=banks.get(str(raw.get("formation_bank_sha256"))),
                **(
                    {"principal": confirmation.manifest.principal.principal()}
                    if confirmation
                    else {}
                ),
            )
            _require(
                preregistration.registered_at <= observed.observed_at <= now,
                "observation predates registration or is in the future",
            )
            seen.add((observed.scenario_id, observed.path, observed.pipeline, observed.channel))
            observations.append(observed)
            attempts.extend(ProviderAttempt.model_validate(a) for a in raw["provider_attempts"])
    required = {
        (case.id, path, pipeline, channel)
        for case in cases
        for path in ("automatic", "deliberate")
        for pipeline in ("seeded", "formation")
        for channel in ("sparse", "dense", "hybrid")
    }
    _require(
        seen == required and len(observations) == len(required), "incomplete experiment matrix"
    )
    spent = verify_provider_journal(
        _read_bounded(artifacts["journal"][0][0]), attempts, maximum=declared.maximum_cost_usd
    )
    _require(
        _read_bounded(manifest) == manifest_bytes
        and all(
            _digest(_read_bounded(path)) == digest
            for group in artifacts.values()
            for path, digest in group
        ),
        "input changed during collection",
    )
    return CollectedObservations(
        scenarios,
        tuple(observations),
        spent,
        _digest(manifest_bytes),
        tuple(item for group in artifacts.values() for item in group),
        confirmed=confirmation is not None,
    )
