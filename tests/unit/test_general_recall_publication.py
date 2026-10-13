"""Fabricated publication inputs test the compiler, never qualify a deployment."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import pytest

from agent_core.domain.general_recall_release import (
    RecallPreregistration,
    RecallReleaseBundle,
    RecallReleaseIdentity,
)
from agent_core.evals import general_recall_publication as publication
from agent_core.evals.general_recall_collection import CollectedObservations
from agent_core.memory.general_recall_evidence import score_release
from tests.unit.test_general_recall_source_scoring import recovered_bundle

CollectionCompiler = Callable[
    [Path, RecallReleaseIdentity, RecallPreregistration], CollectedObservations
]


@pytest.fixture(autouse=True)
def isolate_raw_projection(monkeypatch: pytest.MonkeyPatch) -> CollectionCompiler:
    # Full-matrix collection and actual runtime projection have their own suites.
    # This suite fabricates metrics only to exercise the publication boundary.
    def compile_fixture(path: Path, *_: object) -> CollectedObservations:
        bundle = RecallReleaseBundle.model_validate_json((path.parent / "bundle.json").read_bytes())
        return CollectedObservations(
            bundle.scenarios,
            bundle.observations,
            Decimal(0),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            confirmed=True,
        )

    monkeypatch.setattr(publication, "compile_collection", compile_fixture, raising=False)
    return compile_fixture


def test_publication_refuses_unconfirmed_development_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolate_raw_projection: CollectionCompiler,
) -> None:
    arguments = inputs(tmp_path)
    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None)

    def unconfirmed(*args: object) -> CollectedObservations:
        path = arguments["collection_manifest"]
        return replace(
            isolate_raw_projection(
                path,
                arguments["expected"],
                RecallPreregistration(
                    **json.loads(arguments["bundle_path"].read_bytes())["preregistration"]
                ),
            ),
            confirmed=False,
        )

    monkeypatch.setattr(publication, "compile_collection", unconfirmed)
    with pytest.raises(ValueError, match="confirmation"):
        publication.publish_release(**arguments)


def write_json(path: Path, value: object) -> str:
    content = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


class PublicationInputs(TypedDict):
    root: Path
    bundle_path: Path
    expected: RecallReleaseIdentity
    independence_review: Path
    failure_review: Path
    gate_reports: tuple[Path, ...]
    collection_manifest: Path
    output: Path


def inputs(directory: Path, evaluated_inputs_sha256: str | None = None) -> PublicationInputs:
    raw = recovered_bundle().model_dump(mode="json")
    independence = directory / "independence.json"
    raw["identity"]["independence_review_sha256"] = write_json(
        independence,
        {
            "version": "recall-independence-review@1",
            "reviewer": "Fabricated unit-test reviewer",
            "reviewed_at": "2026-10-10T00:00:00Z",
            "decision": "accepted",
            "corpus_sha256": raw["identity"]["corpus_sha256"],
            "scenario_manifest_sha256": raw["identity"]["scenario_manifest_sha256"],
            "reviewed_scenarios": [s["id"] for s in raw["scenarios"]],
        },
    )
    if evaluated_inputs_sha256 is not None:
        raw["identity"]["evaluated_inputs_sha256"] = evaluated_inputs_sha256
    expected = RecallReleaseIdentity.model_validate(raw["identity"])
    identity = hashlib.sha256(expected.model_dump_json().encode()).hexdigest()
    for row in [*raw["observations"], *raw["gates"]]:
        row["identity_sha256"] = identity
    reports = []
    for receipt in raw["gates"]:
        report = directory / f"gate-{receipt['gate']}.json"
        receipt["report_sha256"] = write_json(
            report,
            {
                "version": "recall-gate-report@1",
                "identity_sha256": identity,
                "gate": receipt["gate"],
                "checks": [{"id": "Fabricated regression", "passed": True}],
            },
        )
        reports.append(report)
    bundle = RecallReleaseBundle.model_validate(raw)
    failures = directory / "failures.json"
    write_json(
        failures,
        {
            "version": "recall-failure-review@1",
            "reviewer": "Fabricated unit-test reviewer",
            "reviewed_at": "2026-10-12T01:00:00Z",
            "decision": "accepted",
            "identity_sha256": identity,
            "observations_sha256": hashlib.sha256(
                b"".join(row.model_dump_json().encode() + b"\n" for row in bundle.observations)
            ).hexdigest(),
            "reviewed_scenarios": [s.id for s in bundle.scenarios],
        },
    )
    source = directory / "bundle.json"
    source.write_text(bundle.model_dump_json())
    collection = directory / "collection.json"
    write_json(collection, {"fabricated_unit_fixture": True})
    return {
        "root": Path(__file__).resolve().parents[2],
        "bundle_path": source,
        "expected": expected,
        "independence_review": independence,
        "failure_review": failures,
        "gate_reports": tuple(reports),
        "collection_manifest": collection,
        "output": directory / "published",
    }


def test_publisher_writes_a_recomputed_bound_bundle_without_creating_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = inputs(tmp_path)
    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None, raising=False)
    result = publication.publish_release(**arguments)
    assert result == tmp_path / "published" / "release.json"
    assert result.read_bytes() == (tmp_path / "bundle.json").read_bytes()
    report = json.loads((result.parent / "report.json").read_text())
    assert report["activation_eligible"] and report["observations"] == 4800


def test_publication_rejects_an_unverified_evaluated_input_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = inputs(tmp_path, evaluated_inputs_sha256="0" * 64)
    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None)
    with pytest.raises(ValueError, match="evaluated inputs changed"):
        publication.publish_release(**arguments)
    assert not (tmp_path / "published").exists()


def test_publication_rejects_metrics_that_differ_from_original_runtime_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolate_raw_projection: CollectionCompiler,
) -> None:
    arguments = inputs(tmp_path)
    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None)
    original = isolate_raw_projection

    def changed(
        path: Path, identity: RecallReleaseIdentity, preregistration: RecallPreregistration
    ) -> CollectedObservations:
        result = original(path, identity, preregistration)
        return CollectedObservations(
            result.scenarios,
            (result.observations[0].model_copy(update={"cost_usd": 0.5}), *result.observations[1:]),
            result.actual_cost_usd,
            result.manifest_sha256,
        )

    monkeypatch.setattr(publication, "compile_collection", changed)
    with pytest.raises(ValueError, match="original runtime"):
        publication.publish_release(**arguments)
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_review",
        "different_review",
        "unreviewed_failures",
        "future_review",
        "gate_change",
        "wrong_identity",
        "duplicate_gate",
    ],
)
def test_incomplete_changed_or_unbound_inputs_never_create_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    arguments = inputs(tmp_path)
    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None)
    if failure == "missing_review":
        (tmp_path / "independence.json").unlink()
    elif failure == "different_review":
        path = tmp_path / "independence.json"
        review = json.loads(path.read_text())
        review["reviewed_scenarios"][0] = "foreign-scenario"
        write_json(path, review)
    elif failure in {"unreviewed_failures", "future_review"}:
        path = tmp_path / "failures.json"
        review = json.loads(path.read_text())
        if failure == "unreviewed_failures":
            review["reviewed_scenarios"] = []
        else:
            review["reviewed_at"] = "2099-01-01T00:00:00Z"
        write_json(path, review)
    elif failure == "gate_change":
        path = tmp_path / "gate-1.json"
        report = json.loads(path.read_text())
        report["checks"][0]["passed"] = False
        write_json(path, report)
    elif failure == "wrong_identity":
        arguments["expected"] = arguments["expected"].model_copy(
            update={"reader_configuration_sha256": "f" * 64}
        )
    else:
        arguments["gate_reports"] = (tmp_path / "gate-1.json",) * 11
    with pytest.raises((OSError, ValueError)):
        publication.publish_release(**arguments)
    assert not (tmp_path / "published").exists()


def test_publication_preserves_an_existing_or_failed_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = inputs(tmp_path)
    output = tmp_path / "published"
    output.mkdir()
    (output / "retained").write_text("Earlier failed evidence")
    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None)
    with pytest.raises(ValueError, match="output already exists"):
        publication.publish_release(**arguments)
    assert (output / "retained").read_text() == "Earlier failed evidence"


def test_review_change_during_scoring_is_detected_before_any_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = inputs(tmp_path)
    original = score_release

    def score_and_change(bundle: RecallReleaseBundle) -> dict[str, object]:
        report = original(bundle)
        (tmp_path / "failures.json").write_text("Changed after read")
        return report

    monkeypatch.setattr(publication, "require_committed_tree", lambda *_: None)
    monkeypatch.setattr(publication, "score_release", score_and_change)
    with pytest.raises(ValueError, match="input changed during publication"):
        publication.publish_release(**arguments)
    assert not (tmp_path / "published").exists()
