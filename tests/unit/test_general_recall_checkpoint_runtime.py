"""Synthetic frozen-input/deadline/archive boundaries, never paid admission."""

import hashlib
import io
import tarfile
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_core.evals.general_recall_checkpoint import CheckpointStore, ContinuationPlan
from agent_core.evals.general_recall_checkpoint_runtime import (
    ContinuationDeadline,
    VerifiedParentArchive,
    bind_frozen_preflight,
)
from tests.contract.support import NOW

pytestmark = pytest.mark.static


def archive_fixture(tmp_path: Path, *, extra: str = "") -> tuple[Path, Path, str]:
    directory = tmp_path / "parent"
    directory.mkdir()
    (directory / "observations").mkdir()
    (directory / "identity.json").write_bytes(b'{"identity":"unchanged"}\n')
    (directory / "observations/one.jsonl").write_bytes(b'{"answer":"wrong"}\n')
    archive = tmp_path / "parent.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(directory, arcname="parent")
        if extra:
            entry = tarfile.TarInfo(extra)
            entry.size = 1
            output.addfile(entry, io.BytesIO(b"x"))
    return directory, archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_complete_archive_is_verified_without_extracting_or_modifying(tmp_path: Path) -> None:
    directory, archive, digest = archive_fixture(tmp_path)
    before = archive.read_bytes()
    parent = VerifiedParentArchive(directory, archive, digest)
    assert parent.read("observations/one.jsonl") == b'{"answer":"wrong"}\n'
    parent.verify()
    assert archive.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["parent", "parent.tar.gz"]


@pytest.mark.parametrize("change", ["digest", "missing", "changed", "extra", "symlink"])
def test_parent_inventory_cannot_omit_or_change_any_original(tmp_path: Path, change: str) -> None:
    directory, archive, digest = archive_fixture(tmp_path)
    target = directory / "observations/one.jsonl"
    if change == "digest":
        digest = "0" * 64
    elif change == "missing":
        target.unlink()
    elif change == "changed":
        target.write_bytes(b'{"answer":"correct"}\n')
    elif change == "extra":
        (directory / "unreported.json").write_bytes(b"{}")
    else:
        target.rename(tmp_path / "outside.json")
        target.symlink_to(tmp_path / "outside.json")
    with pytest.raises(ValueError):
        VerifiedParentArchive(directory, archive, digest)


@pytest.mark.parametrize(
    "extra", ["parent/../escape", "/outside", "foreign/file", "parent/identity.json"]
)
def test_parent_archive_refuses_unsafe_or_duplicate_members(tmp_path: Path, extra: str) -> None:
    directory, archive, digest = archive_fixture(tmp_path, extra=extra)
    with pytest.raises(ValueError):
        VerifiedParentArchive(directory, archive, digest)


def test_parent_read_rechecks_bytes_and_refuses_path_traversal(tmp_path: Path) -> None:
    directory, archive, digest = archive_fixture(tmp_path)
    parent = VerifiedParentArchive(directory, archive, digest)
    with pytest.raises(ValueError):
        parent.read("../parent/identity.json")
    (directory / "identity.json").write_bytes(b"changed")
    with pytest.raises(ValueError):
        parent.read("identity.json")


def deadline_plan() -> ContinuationPlan:
    return ContinuationPlan(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        tuple(f"case-{i}" for i in range(400)),
        Decimal("135.90882425"),
        Decimal("150"),
    )


def test_deadline_is_absolute_across_segments_and_clock_rollback(tmp_path: Path) -> None:
    current, elapsed = [NOW], [0.0]
    with CheckpointStore(tmp_path / "state", deadline_plan(), create=True) as store:
        first = ContinuationDeadline(
            store,
            protocol_sha256="d" * 64,
            duration_seconds=100,
            create=True,
            now=lambda: current[0],
            monotonic=lambda: elapsed[0],
        )
        assert first.remaining() == 100
        original = (store.directory / "deadline.json").read_bytes()
        current[0] += timedelta(seconds=20)
        elapsed[0] += 20
        reopened = ContinuationDeadline(
            store,
            protocol_sha256="d" * 64,
            duration_seconds=100,
            now=lambda: current[0],
            monotonic=lambda: elapsed[0],
        )
        assert reopened.remaining() == 80
        current[0] -= timedelta(seconds=10)
        elapsed[0] += 30
        assert reopened.remaining() == 50
        elapsed[0] += 51
        with pytest.raises(TimeoutError):
            reopened.remaining()
        assert (store.directory / "deadline.json").read_bytes() == original


@pytest.mark.parametrize("change", ["duration", "protocol", "missing", "duplicate", "backdated"])
def test_deadline_cannot_be_renewed_or_collector_replaced(tmp_path: Path, change: str) -> None:
    with CheckpointStore(tmp_path / "state", deadline_plan(), create=True) as store:
        arguments: dict[str, Any] = {
            "protocol_sha256": "d" * 64,
            "duration_seconds": 100,
            "now": lambda: NOW,
            "monotonic": lambda: 0.0,
        }
        ContinuationDeadline(store, create=True, **arguments)
        if change == "duration":
            arguments["duration_seconds"] = 101
        elif change == "protocol":
            arguments["protocol_sha256"] = "e" * 64
        elif change == "missing":
            (store.directory / "deadline.json").unlink()
        elif change == "duplicate":
            arguments["create"] = True
        else:
            arguments["now"] = lambda: NOW - timedelta(seconds=1)
        with pytest.raises((ValueError, FileExistsError, FileNotFoundError)):
            ContinuationDeadline(store, **arguments)


def preflight_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from agent_core.domain.messages import ResolvedModel
    from agent_core.evals.general_recall_confirmation import (
        ConfirmedExecution,
        RecallExecutionManifest,
    )
    from agent_core.evals.general_recall_confirmatory import PreparedConfirmation
    from tests.contract.test_local_semantic_preparation import LOCAL
    from tests.unit.test_general_recall_confirmation import binding, capture
    from tests.unit.test_general_recall_confirmatory import options, scratch_settings
    from tests.unit.test_general_recall_release import fixture

    manifest = RecallExecutionManifest.model_validate(
        {**capture().model_dump(), "version": "recall-execution-manifest@2", "worker_count": 2}
    )
    original = binding(manifest)
    preregistration = fixture().preregistration
    original = ConfirmedExecution(
        original.identity.model_copy(
            update={
                "preregistration_sha256": hashlib.sha256(
                    preregistration.model_dump_json().encode()
                ).hexdigest()
            }
        ),
        manifest,
    )
    fresh_manifest = manifest.model_copy(update={"captured_at": NOW + timedelta(seconds=1)})
    fresh_prereg = preregistration.model_copy(
        update={"registered_at": preregistration.registered_at + timedelta(days=1)}
    )
    fresh_identity = original.identity.model_copy(
        update={
            "execution_manifest_sha256": fresh_manifest.digest,
            "evaluated_inputs_sha256": "f" * 64,
            "preregistration_sha256": hashlib.sha256(
                fresh_prereg.model_dump_json().encode()
            ).hexdigest(),
        }
    )
    inputs: dict[str, Any] = {
        "deployment": {"effective": "unchanged"},
        "schema": {"revision": "b005"},
        "frozen_controls": {},
        "review": {},
        "baseline_sha256": "b" * 64,
        "evaluated_inputs": {"digest": original.identity.evaluated_inputs_sha256},
    }
    workers = tuple(
        replace(
            scratch_settings(),
            database_url=f"postgresql+asyncpg://localhost/veetbot_m33_eval_new_{i}",
        )
        for i in (1, 2)
    )
    settings = workers[0]
    prepared = PreparedConfirmation(
        ConfirmedExecution(fresh_identity, fresh_manifest),
        fresh_prereg,
        (),
        LOCAL,
        tmp_path / "model",
        ResolvedModel(
            provider="fake",
            model="scripted",
            credential_ref="fake",
            policy_name="fake-balanced",
            resolved_at=NOW,
        ),
        settings,
        {**inputs, "evaluated_inputs": {"digest": "f" * 64}},
        {},
        workers,
    )
    old_options = options(tmp_path, parallel_scratch_database="veetbot_m33_eval_parent_2")
    new_options = old_options.model_copy(
        update={"output": tmp_path / "new", "parallel_scratch_database": "veetbot_m33_eval_new_2"}
    )
    monkeypatch.setattr(
        "agent_core.memory.recall_release_binding.capture_inputs",
        lambda *_args: type("Capture", (), {"digest": original.identity.evaluated_inputs_sha256})(),
    )
    return {
        "parent_identity": original.identity.model_dump(mode="json"),
        "parent_manifest": manifest.model_dump(mode="json"),
        "parent_preregistration": preregistration.model_dump(mode="json"),
        "parent_inputs": inputs,
        "parent_options": old_options.model_dump(mode="json"),
        "prepared": prepared,
        "options": new_options,
        "root": tmp_path,
        "forbidden_databases": ("veetbot_m33_eval_parent_1", "veetbot_m33_eval_parent_2"),
    }


def test_frozen_binding_keeps_original_measurement_and_fresh_worker_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = preflight_fixture(tmp_path, monkeypatch)
    bound = bind_frozen_preflight(**arguments)
    assert bound.confirmation.identity.model_dump(mode="json") == arguments["parent_identity"]
    assert bound.confirmation.manifest.model_dump(mode="json") == arguments["parent_manifest"]
    assert bound.preregistration.model_dump(mode="json") == arguments["parent_preregistration"]
    assert bound.worker_settings == arguments["prepared"].worker_settings
    assert bound.snapshots == arguments["parent_inputs"]
    assert arguments["prepared"].confirmation.identity != bound.confirmation.identity


@pytest.mark.parametrize(
    "change",
    [
        "runtime",
        "scorer",
        "manifest",
        "threshold",
        "capture",
        "option",
        "worker",
        "duplicate",
        "count",
        "evaluated",
    ],
)
def test_frozen_binding_refuses_drift_before_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from agent_core.evals.general_recall_confirmation import ConfirmedExecution

    arguments = preflight_fixture(tmp_path, monkeypatch)
    prepared = arguments["prepared"]
    if change in {"runtime", "scorer"}:
        field = "implementation_sha256" if change == "runtime" else "scorer_sha256"
        arguments["prepared"] = replace(
            prepared,
            confirmation=ConfirmedExecution(
                prepared.confirmation.identity.model_copy(update={field: "9" * 64}),
                prepared.confirmation.manifest,
            ),
        )
    elif change == "manifest":
        arguments["prepared"] = replace(
            prepared,
            confirmation=ConfirmedExecution(
                prepared.confirmation.identity,
                prepared.confirmation.manifest.model_copy(
                    update={"maximum_cost_usd": Decimal("101")}
                ),
            ),
        )
    elif change == "threshold":
        arguments["prepared"] = replace(
            prepared,
            preregistration=prepared.preregistration.model_copy(update={"context_tokens": 3000}),
        )
    elif change == "capture":
        arguments["prepared"] = replace(
            prepared, snapshots={**prepared.snapshots, "schema": {"revision": "changed"}}
        )
    elif change == "option":
        arguments["options"] = arguments["options"].model_copy(update={"noise": 1})
    elif change == "worker":
        arguments["forbidden_databases"] = (
            *arguments["forbidden_databases"],
            "veetbot_m33_eval_new_1",
        )
    elif change == "duplicate":
        arguments["prepared"] = replace(
            prepared, worker_settings=(prepared.settings, prepared.settings)
        )
    elif change == "count":
        arguments["prepared"] = replace(prepared, worker_settings=(prepared.settings,))
    else:
        monkeypatch.setattr(
            "agent_core.memory.recall_release_binding.capture_inputs",
            lambda *_args: type("Capture", (), {"digest": "0" * 64})(),
        )
    with pytest.raises(ValueError):
        bind_frozen_preflight(**arguments)
