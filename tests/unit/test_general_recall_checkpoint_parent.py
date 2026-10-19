"""Parent projection fixtures isolate provenance; they are not release evidence."""

import hashlib
import json
import tarfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals import general_recall_checkpoint_parent as parent_module
from agent_core.evals.general_recall_checkpoint import CheckpointStore, ContinuationPlan
from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent
from agent_core.evals.general_recall_checkpoint_runtime import VerifiedParentArchive
from agent_core.evals.general_recall_collection import scenario_manifest
from agent_core.evals.general_recall_confirmation import ConfirmedExecution, RecallExecutionManifest
from agent_core.evals.general_recall_corpus import corpus_digest
from tests.unit.test_general_recall_confirmation import capture
from tests.unit.test_general_recall_confirmatory import options, scratch_settings
from tests.unit.test_general_recall_corpus import fixture as case_fixture
from tests.unit.test_general_recall_release import fixture

pytestmark = pytest.mark.static


def parent_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str = ""
) -> tuple[VerifiedParentArchive, ContinuationPlan, list[dict[str, Any]]]:
    directory = tmp_path / "original"
    directory.mkdir()
    (directory / "banks").mkdir()
    (directory / "observations").mkdir()
    bundle = fixture()
    template = case_fixture()
    cases = tuple(
        template.model_copy(
            update={
                **s.model_dump(
                    include={
                        "id",
                        "family",
                        "split",
                        "scenario_lineage",
                        "query_lineage",
                        "known_incident",
                    }
                ),
                "history": (template.history[0].model_copy(update={"content": s.id}),),
            }
        )
        for s in bundle.scenarios
    )
    scenarios = scenario_manifest(cases)
    captured = capture()
    variants = tuple(
        v.model_copy(
            update={"configuration": {**v.configuration, "formation_policy": "formation@11"}}
        )
        for v in captured.variants
    )
    captured = RecallExecutionManifest.model_validate(
        {
            **captured.model_dump(),
            "variants": [v.model_dump() for v in variants],
            "version": "recall-execution-manifest@2",
            "worker_count": 2,
        }
    )
    identity = bundle.identity.model_copy(
        update={
            "corpus_sha256": corpus_digest(cases),
            "execution_manifest_sha256": captured.digest,
            "evaluated_inputs_sha256": "e" * 64,
            "scenario_manifest_sha256": hashlib.sha256(
                b"".join(s.model_dump_json().encode() + b"\n" for s in scenarios)
            ).hexdigest(),
            "preregistration_sha256": hashlib.sha256(
                bundle.preregistration.model_dump_json().encode()
            ).hexdigest(),
        }
    )
    confirmation = ConfirmedExecution(identity, captured)
    bank_identity = hashlib.sha256(
        json.dumps(
            {
                "case": cases[0].model_dump(mode="json"),
                "formation_policy": "formation@11",
                "storage": "postgres",
                "formation": {},
                "implementation": identity.implementation_sha256,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    bank_bytes = json.dumps(
        {"identity": bank_identity, "state_sha256": "a" * 64, "calls": []}
    ).encode()
    if change == "bank_identity":
        bank_bytes = bank_bytes.replace(bank_identity.encode(), b"0" * 64)
    bank_name = hashlib.sha256(cases[0].id.encode()).hexdigest() + ".json"
    (directory / "banks" / bank_name).write_bytes(bank_bytes)
    rows = [
        {
            "scenario_id": cases[0].id,
            "path": v.path,
            "pipeline": v.pipeline,
            "channel": v.channel,
            "configuration_sha256": v.digest,
            **confirmation.observation_fields(),
            "observed_at": bundle.observations[0].observed_at.isoformat(),
            "formation_bank_sha256": hashlib.sha256(bank_bytes).hexdigest()
            if v.pipeline == "formation"
            else None,
            "answer": "unsuccessful" if v.channel == "sparse" else "successful",
        }
        for v in captured.variants
    ]
    if change == "duplicate":
        rows.append(rows[0])
    elif change == "foreign":
        rows[0] = {**rows[0], "scenario_id": "foreign"}
    elif change == "row_identity":
        rows[0] = {**rows[0], "identity_sha256": "0" * 64}
    elif change == "bank_hash":
        rows[6] = {**rows[6], "formation_bank_sha256": "0" * 64}
    elif change == "corpus":
        cases = cases[:-1]
    elif change == "pending":
        (directory / "banks" / bank_name).unlink()
        (directory / "banks" / (bank_name + ".pending.jsonl")).write_text(
            json.dumps({"identity": bank_identity, "state": "recording"}) + "\n"
        )
        rows = [r for r in rows if r["pipeline"] == "seeded"]
    (directory / "observations/first.jsonl").write_bytes(
        b"".join(json.dumps(r).encode() + b"\n" for r in rows)
    )
    (directory / "corpus.jsonl").write_bytes(
        b"".join(c.model_dump_json().encode() + b"\n" for c in cases)
    )
    (directory / "identity.json").write_text(identity.model_dump_json())
    (directory / "confirmation.json").write_text(captured.model_dump_json())
    (directory / "preregistration.json").write_text(bundle.preregistration.model_dump_json())
    (directory / "inputs.json").write_text(json.dumps({"frozen": "unchanged"}))
    (directory / "run.json").write_text(
        json.dumps({"state": "failed", "options": options(tmp_path).model_dump(mode="json")})
    )
    archive = tmp_path / "parent.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(directory, arcname="original")
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    verified = VerifiedParentArchive(directory, archive, archive_sha)
    plan = ContinuationPlan(
        confirmation.observation_fields()["identity_sha256"],
        "e" * 64,
        archive_sha,
        tuple(c.id for c in cases),
        Decimal("135.90882425"),
        Decimal("150"),
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        parent_module, "load_config_document", lambda *_args: {"formation": {}}, raising=False
    )

    def compile_observation(case: Any, row: dict[str, Any], **kwargs: Any) -> None:
        assert case.id == row["scenario_id"]
        assert kwargs["formation_bank"].read_bytes() == bank_bytes
        calls.append(row)
        if change == "compiler":
            raise ValueError("Mechanical compiler refused this row")

    monkeypatch.setattr(
        parent_module, "compile_runtime_observation", compile_observation, raising=False
    )
    return verified, plan, calls


def test_parent_import_keeps_all_selected_answers_exact_and_is_crash_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, plan, calls = parent_fixture(tmp_path, monkeypatch)
    parent = CheckpointParent(archive, scratch_settings())
    assert len(calls) == 4
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        report = parent.import_into(store)
        assert report["selected_rows"] == 4
        assert len(store.completed()) == 4
        retained = [p.read_bytes() for p in (store.directory / "results").iterdir()]
        assert any(b'"answer": "unsuccessful"' in r for r in retained)
        originals = archive.read("observations/first.jsonl").splitlines(keepends=True)
        assert all(r in originals for r in retained)
        assert parent.bank_path(store, plan.scenarios[0]).is_relative_to(archive.directory)
        assert parent.import_into(store) == report
    with CheckpointStore(tmp_path / "state", plan) as reopened:
        assert parent.import_into(reopened) == report
        assert len(reopened.completed()) == 4


@pytest.mark.parametrize(
    "change",
    ["duplicate", "foreign", "row_identity", "bank_hash", "bank_identity", "corpus", "compiler"],
)
def test_parent_invalid_rows_fail_all_or_nothing_without_durable_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    archive, _plan, _calls = parent_fixture(tmp_path, monkeypatch, change)
    with pytest.raises(ValueError):
        CheckpointParent(archive, scratch_settings())
    assert not (tmp_path / "state").exists()


def test_parent_pending_bank_cannot_replay_or_silently_regenerate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, plan, _calls = parent_fixture(tmp_path, monkeypatch, "pending")
    parent = CheckpointParent(archive, scratch_settings())
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        parent.import_into(store)
        with pytest.raises(ValueError, match="replacement"):
            parent.bank_path(store, plan.scenarios[0])
        other = parent.bank_path(store, plan.scenarios[1])
        assert other.is_relative_to(store.directory) and not other.exists()


def test_partial_parent_import_reopens_exact_intent_without_repeating_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, plan, _calls = parent_fixture(tmp_path, monkeypatch)
    parent = CheckpointParent(archive, scratch_settings())
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        complete = store.import_result
        count = 0

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("Simulated import interruption")
            return complete(*args, **kwargs)

        monkeypatch.setattr(store, "import_result", interrupted)
        with pytest.raises(RuntimeError):
            parent.import_into(store)
        assert len(store.completed()) == 1
    with CheckpointStore(tmp_path / "state", plan) as resumed:
        assert parent.import_into(resumed)["selected_rows"] == 4
        assert len(resumed.completed()) == 4


def replacement_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CheckpointParent, ContinuationPlan, bytes, bytes]:
    original, plan, _calls = parent_fixture(tmp_path, monkeypatch, "pending")
    directory = original.directory
    pending = next((directory / "banks").iterdir())
    pending.write_bytes(
        pending.read_bytes()
        + json.dumps({"state": "reserved", "request_sha256": "b" * 64}).encode()
        + b"\n"
    )
    call = {
        "index": 1,
        "provider": "openai",
        "model": "model",
        "request_sha256": "c" * 64,
        "reservation_usd": "2",
        "outcome": "uncertain",
        "error_class": "CancelledError",
    }
    ledger = (
        json.dumps({**call, "outcome": "reserved", "error_class": None}).encode()
        + b"\n"
        + json.dumps(call).encode()
        + b"\n"
    )
    (directory / "provider-ledger.jsonl").write_bytes(ledger)
    run = json.loads((directory / "run.json").read_bytes())
    run["finished_at"] = "2026-10-17T20:38:48+00:00"
    (directory / "run.json").write_text(json.dumps(run))
    path = tmp_path / "with-failure.tar.gz"
    with tarfile.open(path, "w:gz") as output:
        output.add(directory, arcname="original")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    from dataclasses import replace

    plan = replace(plan, parent_archive_sha256=digest)
    parent = CheckpointParent(VerifiedParentArchive(directory, path, digest), scratch_settings())
    diagnostic = json.dumps({"primary_archive_sha256": digest, "provider_call": call}).encode()
    approval = json.dumps(
        {
            "version": "recall-parent-preparation-approval@1",
            "scenario_id": plan.scenarios[0],
            "parent_archive_sha256": digest,
            "pending_bank_sha256": hashlib.sha256(pending.read_bytes()).hexdigest(),
            "ledger_file": "provider-ledger.jsonl",
            "ledger_sha256": hashlib.sha256(ledger).hexdigest(),
            "call_index": 1,
            "error_class": "CancelledError",
            "finished_at": run["finished_at"],
            "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
            "owner_authorization_sha256": "d" * 64,
            "assessment": "cancelled-pre-answer-preparation",
            "linkage": "operator-assessed-terminal-context",
        }
    ).encode()
    return parent, plan, approval, diagnostic


def test_original_preparation_replacement_has_no_invented_started_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, plan, raw, diagnostic = replacement_fixture(tmp_path, monkeypatch)
    digest = hashlib.sha256(raw).hexdigest()
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        parent.import_into(store)
        parent.admit_preparation_replacement(
            store, raw, expected_sha256=digest, diagnostic=diagnostic
        )
        events = [
            json.loads(p.read_bytes()) for p in sorted((store.directory / "events").iterdir())
        ]
        assert [e["action"] for e in events] == ["parent_failed", "replacement"]
        assert events[0]["source_sha256"] == digest
        bank = parent.bank_path(store, plan.scenarios[0])
        assert bank.name.endswith(".replacement.json") and not bank.exists()
        assert len(store.pending()) == 1600
        parent.admit_preparation_replacement(
            store, raw, expected_sha256=digest, diagnostic=diagnostic
        )
        assert len(list((store.directory / "events").iterdir())) == 2
    with CheckpointStore(tmp_path / "state", plan) as reopened:
        assert plan.scenarios[0] in reopened._replaced
        assert len(reopened.completed()) == 0
        assert reopened.begin(reopened.pending()[0])
        reopened.fail(
            reopened.pending()[0]
            if reopened.pending()[0][0] == plan.scenarios[0]
            else (plan.scenarios[0], "automatic", "formation", "sparse"),
            stage="formation",
            error_class="CancelledError",
            ledger_sha256="f" * 64,
        )
        with pytest.raises(ValueError, match="eligible"):
            reopened.replace_formation((plan.scenarios[0], "automatic", "formation", "sparse"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_archive_sha256", "0" * 64),
        ("pending_bank_sha256", "0" * 64),
        ("ledger_sha256", "0" * 64),
        ("diagnostic_sha256", "0" * 64),
        ("scenario_id", "foreign"),
        ("call_index", 2),
        ("error_class", "ModelProtocolError"),
        ("finished_at", "2026-10-16T20:38:48+00:00"),
        ("linkage", "exact-request-key"),
        ("ledger_file", "../provider-ledger.jsonl"),
    ],
)
def test_original_preparation_admission_refuses_changed_or_unproven_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    parent, plan, raw, diagnostic = replacement_fixture(tmp_path, monkeypatch)
    changed = json.loads(raw)
    changed[field] = value
    raw = json.dumps(changed).encode()
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        parent.import_into(store)
        with pytest.raises(ValueError):
            parent.admit_preparation_replacement(
                store, raw, expected_sha256=hashlib.sha256(raw).hexdigest(), diagnostic=diagnostic
            )
        assert not list((store.directory / "events").iterdir())
        assert not store._replaced


def test_replacement_refuses_a_continuation_answer_already_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, plan, raw, diagnostic = replacement_fixture(tmp_path, monkeypatch)
    with CheckpointStore(tmp_path / "state", plan, create=True) as store:
        parent.import_into(store)
        store.begin((plan.scenarios[0], "deliberate", "formation", "hybrid"))
        with pytest.raises(ValueError):
            parent.admit_preparation_replacement(
                store, raw, expected_sha256=hashlib.sha256(raw).hexdigest(), diagnostic=diagnostic
            )
        assert not store._replaced


def test_reopened_parent_failure_cannot_follow_replaced_evidence_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, plan, raw, diagnostic = replacement_fixture(tmp_path, monkeypatch)
    directory = tmp_path / "state"
    with CheckpointStore(directory, plan, create=True) as store:
        parent.import_into(store)
        parent.admit_preparation_replacement(
            store, raw, expected_sha256=hashlib.sha256(raw).hexdigest(), diagnostic=diagnostic
        )
    (directory / "failures").rename(tmp_path / "elsewhere")
    (directory / "failures").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(ValueError, match=r"unsafe|Unsafe|eligible"):
        CheckpointStore(directory, plan)
