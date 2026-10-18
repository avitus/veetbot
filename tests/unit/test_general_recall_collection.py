"""Fabricated matrix artifacts test collection, never provide release evidence."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.general_recall_release import (
    RecallPreregistration,
    RecallReleaseIdentity,
    RecallScenario,
)
from agent_core.evals import general_recall_collection as collection
from agent_core.evals.general_recall_corpus import corpus_digest
from tests.unit.test_general_recall_confirmation import binding, capture
from tests.unit.test_general_recall_corpus import fixture as case_fixture
from tests.unit.test_general_recall_journal import attempt, encode, entries
from tests.unit.test_general_recall_release import fixture as release_fixture


def inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, RecallReleaseIdentity, RecallPreregistration]:
    bundle = release_fixture()
    cases, scenarios, rows = [], [], []
    case = case_fixture()
    for original in bundle.scenarios:
        history = case.history[0].model_copy(update={"content": original.id})
        actual = case.model_copy(
            update={
                **original.model_dump(
                    include={
                        "id",
                        "family",
                        "split",
                        "scenario_lineage",
                        "query_lineage",
                        "known_incident",
                    }
                ),
                "history": (history,),
            }
        )
        cases.append(actual)
        scenarios.append(
            RecallScenario(
                **original.model_dump(
                    exclude={"history_sha256", "required_evidence", "required_answer_atoms"}
                ),
                history_sha256=hashlib.sha256(
                    json.dumps([(history.speaker, history.scope, history.content)]).encode()
                ).hexdigest(),
                required_evidence=(),
                required_answer_atoms=("route",),
            )
        )
    expected = bundle.identity.model_copy(
        update={
            "corpus_sha256": corpus_digest(tuple(cases)),
            "scenario_manifest_sha256": hashlib.sha256(
                b"".join(s.model_dump_json().encode() + b"\n" for s in scenarios)
            ).hexdigest(),
        }
    )
    identity = hashlib.sha256(expected.model_dump_json().encode()).hexdigest()
    projected = {}
    journal: list[dict[str, Any]] = []
    for index, observation in enumerate(bundle.observations, 1):
        row = {
            "scenario_id": observation.scenario_id,
            "path": observation.path,
            "pipeline": observation.pipeline,
            "channel": observation.channel,
            "observed_at": observation.observed_at.isoformat(),
            "provider_attempts": [attempt().model_dump(mode="json")],
            "formation_bank_sha256": None,
        }
        rows.append(row)
        projected[
            (observation.scenario_id, observation.path, observation.pipeline, observation.channel)
        ] = observation.model_copy(update={"identity_sha256": identity})
        journal.extend({**entry, "index": index} for entry in entries())
    # Runtime projection is independently covered by real execution tests. This
    # fixture isolates complete coverage, original artifact identity and costs.
    monkeypatch.setattr(
        collection,
        "compile_runtime_observation",
        lambda case, row, **kwargs: projected[
            (case.id, row["path"], row["pipeline"], row["channel"])
        ],
        raising=False,
    )
    payloads = {
        "corpus": b"".join(c.model_dump_json().encode() + b"\n" for c in cases),
        "observations": b"".join(json.dumps(row).encode() + b"\n" for row in rows),
        "journal": encode(journal),
    }
    artifacts = []
    for role, payload in payloads.items():
        path = tmp_path / f"{role}.jsonl"
        path.write_bytes(payload)
        artifacts.append(
            {"role": role, "path": path.name, "sha256": hashlib.sha256(payload).hexdigest()}
        )
    manifest = tmp_path / "collection.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "recall-collection@1",
                "identity_sha256": identity,
                "maximum_cost_usd": "100",
                "artifacts": artifacts,
            }
        )
    )
    return manifest, expected, bundle.preregistration


def test_collection_recomputes_the_complete_matrix_from_bound_runtime_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = collection.compile_collection(*inputs(tmp_path, monkeypatch))
    assert result is not None
    assert len(result.scenarios) == 400 and len(result.observations) == 4800
    assert str(result.actual_cost_usd) == "96.00"


def test_development_collection_cannot_be_retagged_as_verified_input_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, expected, registration = inputs(tmp_path, monkeypatch)
    expected = expected.model_copy(update={"evaluated_inputs_sha256": "e" * 64})
    manifest = json.loads(path.read_bytes())
    manifest["identity_sha256"] = hashlib.sha256(expected.model_dump_json().encode()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="confirmation"):
        collection.compile_collection(path, expected, registration)


def confirmed_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, RecallReleaseIdentity, RecallPreregistration]:
    path, expected, registration = inputs(tmp_path, monkeypatch)
    captured = capture().model_copy(update={"captured_at": registration.registered_at})
    confirmation = binding(captured)
    expected = expected.model_copy(
        update={
            "execution_manifest_sha256": captured.digest,
            "evaluated_inputs_sha256": "e" * 64,
        }
    )
    confirmation = type(confirmation)(expected, captured)
    manifest = json.loads(path.read_bytes())
    manifest["version"] = "recall-collection@2"
    manifest["identity_sha256"] = confirmation.observation_fields()["identity_sha256"]
    capture_path = tmp_path / "confirmation.json"
    capture_path.write_text(captured.model_dump_json())
    manifest["artifacts"].append(
        {
            "role": "confirmation",
            "path": capture_path.name,
            "sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
        }
    )
    rows_path = tmp_path / "observations.jsonl"
    rows = []
    variants = {v.key: v for v in captured.variants}
    for encoded in rows_path.read_bytes().splitlines():
        row = json.loads(encoded)
        key = tuple(row[k] for k in ("pipeline", "path", "channel"))
        row.update(confirmation.observation_fields(), configuration_sha256=variants[key].digest)
        rows.append(json.dumps(row) + "\n")
    rows_path.write_text("".join(rows))
    manifest["artifacts"][1]["sha256"] = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    return path, expected, registration


def test_confirmed_collection_checks_original_identity_and_selected_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = confirmed_inputs(tmp_path, monkeypatch)
    original = vars(collection)["compile_runtime_observation"]
    seen = []

    def project(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["principal"])
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "compile_runtime_observation", project)
    result = collection.compile_collection(*arguments)
    assert result.confirmed and len(result.observations) == len(seen) == 4800
    assert all(owner.principal_id == "selected" for owner in seen)


@pytest.mark.parametrize("change", ["identity", "configuration", "unbound", "late", "budget"])
def test_confirmation_collection_rejects_retagging_and_registration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path, expected, registration = confirmed_inputs(tmp_path, monkeypatch)
    manifest = json.loads(path.read_bytes())
    if change in {"late", "budget"}:
        captured = capture().model_copy(
            update={"captured_at": registration.registered_at + timedelta(seconds=1)}
        )
        if change == "budget":
            manifest["maximum_cost_usd"] = "101"
            captured = captured.model_copy(update={"captured_at": registration.registered_at})
        file = tmp_path / "confirmation.json"
        file.write_text(captured.model_dump_json())
        entry = manifest["artifacts"][-1]
        expected = expected.model_copy(update={"execution_manifest_sha256": captured.digest})
        manifest["identity_sha256"] = hashlib.sha256(
            expected.model_dump_json().encode()
        ).hexdigest()
    else:
        file = tmp_path / "observations.jsonl"
        rows = file.read_bytes().splitlines(keepends=True)
        row = json.loads(rows[0])
        if change == "unbound":
            row.pop("identity_sha256")
        else:
            row[f"{change}_sha256"] = "0" * 64
        rows[0] = json.dumps(row).encode() + b"\n"
        file.write_bytes(b"".join(rows))
        entry = manifest["artifacts"][1]
    entry["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="confirmation"):
        collection.compile_collection(path, expected, registration)


@pytest.mark.parametrize(
    "change", ["changed", "missing", "duplicate", "foreign", "journal", "escape"]
)
def test_incomplete_changed_or_unsettled_collection_cannot_supply_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    arguments = inputs(tmp_path, monkeypatch)
    manifest = json.loads(arguments[0].read_bytes())
    rows_path = tmp_path / "observations.jsonl"
    if change == "escape":
        manifest["artifacts"][0]["path"] = "../outside.jsonl"
    elif change == "journal":
        path = tmp_path / "journal.jsonl"
        path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
        manifest["artifacts"][2]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        rows = rows_path.read_bytes().splitlines(keepends=True)
        if change in {"changed", "missing"}:
            rows.pop()
        elif change == "duplicate":
            rows[-1] = rows[0]
        else:
            row = json.loads(rows[-1])
            row["scenario_id"] = "foreign"
            rows[-1] = json.dumps(row).encode() + b"\n"
        rows_path.write_bytes(b"".join(rows))
        if change != "changed":
            manifest["artifacts"][1]["sha256"] = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    arguments[0].write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        collection.compile_collection(*arguments)
