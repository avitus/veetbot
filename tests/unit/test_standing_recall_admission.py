"""The standing owner admission holds across time and releases; file mutation withdraws it.

Synthetic owner statements only; nothing here is production authorization.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.config import ConfigurationError
from agent_core.memory.bounded_recall_admission import BoundedAdmission
from agent_core.memory.semantic_deployment import (
    IndexProfiles,
    active_index_dependencies,
    bind_index_manifest,
    compose_indexing,
)
from agent_core.memory.standing_recall_admission import (
    bind_standing_recall,
    write_standing_admission,
)
from tests.contract.support import principal
from tests.unit.test_bounded_recall_admission import NOW, Clock, digest, write

pytestmark = pytest.mark.static

INSTRUCTION = (
    "Synthetic owner direction: keep semantic recall on; it has been turned on and off "
    "for months without progress."
)
DEPENDENCIES = {
    "configuration_sha256": "1" * 64,
    "embedding_configuration_sha256": "2" * 64,
    "formation_configuration_sha256": "3" * 64,
    "reader_configuration_sha256": "4" * 64,
}


def standing_files(
    tmp_path: Path, dependencies: dict[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    dependencies = dict(dependencies or DEPENDENCIES)
    evidence = {
        "version": "recall-standing-evidence@1",
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "owner_kind": "human",
        "decided_at": (NOW - timedelta(hours=1)).isoformat(),
        "instruction": INSTRUCTION,
        "owner_response_ref": "synthetic-owner-message",
        "owner_statement_sha256": digest(INSTRUCTION.encode("utf-8")),
        "supersedes": "synthetic-trial",
        "observed_dependencies": dict(dependencies),
    }
    manifest = {
        "version": "recall-standing-admission@1",
        "evidence_sha256": "",
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "activated_at": NOW.isoformat(),
        "embedding_configuration_sha256": dependencies["embedding_configuration_sha256"],
        "rollback_build_ref": "b" * 40,
    }
    kwargs: dict[str, Any] = {
        "manifest": tmp_path / "standing-admission.json",
        "evidence": tmp_path / "standing-evidence.json",
        "dependencies": dependencies,
        "principal": principal(),
        "clock": Clock(),
    }
    write(kwargs, manifest, evidence)
    return kwargs, manifest, evidence


def test_standing_admission_holds_across_years_and_other_dependency_changes(
    tmp_path: Path,
) -> None:
    kwargs, _, _ = standing_files(tmp_path)
    admitted = bind_standing_recall(**kwargs)
    assert isinstance(admitted, BoundedAdmission)
    assert admitted()
    kwargs["clock"].value = NOW + timedelta(days=365)
    assert admitted(), "A standing admission never expires on the clock"
    # A policy, formation or reader change is not a withdrawal: only the
    # embedding tuple is bound.
    changed = {
        **DEPENDENCIES,
        "configuration_sha256": "5" * 64,
        "formation_configuration_sha256": "6" * 64,
        "reader_configuration_sha256": "7" * 64,
    }
    again = bind_standing_recall(**{**kwargs, "dependencies": changed})
    assert again()


def test_router_dispatches_standing_manifest_with_receipt_and_needs_owner(
    tmp_path: Path,
) -> None:
    kwargs, _, _ = standing_files(tmp_path)
    admitted = bind_index_manifest(root=tmp_path, release_id="release-aaaaaaa", **kwargs)
    assert isinstance(admitted, BoundedAdmission)
    assert admitted.receipt_sha256 == digest(kwargs["manifest"].read_bytes())
    assert admitted()
    without = {k: v for k, v in kwargs.items() if k != "clock"}
    with pytest.raises(ValueError, match="owner and clock"):
        bind_index_manifest(root=tmp_path, release_id="release-aaaaaaa", **without)


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "tenant",
        "evidence-owner",
        "version-pair",
        "evidence-sha",
        "statement-sha",
        "short-instruction",
        "blank-reference",
        "future-activation",
        "decided-after-activation",
        "embedding-mismatch",
        "missing-dependency",
        "extra-dependency",
        "duplicate-key",
    ],
)
def test_standing_admission_refuses_incomplete_or_mismatched_files(
    tmp_path: Path, change: str
) -> None:
    kwargs, manifest, evidence = standing_files(tmp_path)
    if change == "owner":
        manifest["principal_id"] = "someone-else"
    elif change == "tenant":
        manifest["tenant_id"] = "other-tenant"
    elif change == "evidence-owner":
        evidence["principal_id"] = "someone-else"
    elif change == "version-pair":
        evidence["version"] = "recall-bounded-evidence@3"
    elif change == "statement-sha":
        evidence["owner_statement_sha256"] = "9" * 64
    elif change == "short-instruction":
        evidence["instruction"] = "Too short."
        evidence["owner_statement_sha256"] = digest(b"Too short.")
    elif change == "blank-reference":
        evidence["owner_response_ref"] = "   "
    elif change == "future-activation":
        manifest["activated_at"] = (NOW + timedelta(minutes=1)).isoformat()
    elif change == "decided-after-activation":
        evidence["decided_at"] = (NOW + timedelta(minutes=1)).isoformat()
    elif change == "embedding-mismatch":
        manifest["embedding_configuration_sha256"] = "8" * 64
    elif change == "missing-dependency":
        kwargs["dependencies"].pop("reader_configuration_sha256")
    elif change == "extra-dependency":
        kwargs["dependencies"]["schema_sha256"] = "a" * 64
    write(kwargs, manifest, evidence)
    if change == "evidence-sha":
        kwargs["evidence"].write_bytes(kwargs["evidence"].read_bytes() + b"\n")
    if change == "duplicate-key":
        text = kwargs["manifest"].read_text()
        kwargs["manifest"].write_text(text[:-1] + ',"rollback_build_ref":"' + "c" * 40 + '"}')
    with pytest.raises(ValueError):
        bind_standing_recall(**kwargs)


def test_file_mutation_withdraws_permanently_and_restoring_bytes_does_not_revive(
    tmp_path: Path,
) -> None:
    kwargs, manifest, evidence = standing_files(tmp_path)
    admitted = bind_standing_recall(**kwargs)
    assert admitted()
    original = kwargs["evidence"].read_bytes()
    kwargs["evidence"].write_bytes(original + b" ")
    assert not admitted()
    kwargs["evidence"].write_bytes(original)
    assert not admitted(), "Restoring the bytes does not revive a withdrawn handle"
    (tmp_path / "second").mkdir()
    kwargs, manifest, evidence = standing_files(tmp_path / "second")
    admitted = bind_standing_recall(**kwargs)
    kwargs["manifest"].unlink()
    assert not admitted()
    write(kwargs, manifest, evidence)
    assert not admitted()


def test_writer_produces_binding_files_and_never_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "standing"
    evidence_path, manifest_path = write_standing_admission(
        out,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        instruction=INSTRUCTION,
        reference="synthetic-owner-message",
        rollback_build_ref="b" * 40,
        dependencies=DEPENDENCIES,
        activated_at=NOW,
        decided_at=NOW - timedelta(hours=1),
        supersedes="synthetic-trial",
    )
    assert (evidence_path.name, manifest_path.name) == (
        "standing-evidence.json",
        "standing-admission.json",
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["evidence_sha256"] == digest(evidence_path.read_bytes())
    assert manifest["embedding_configuration_sha256"] == "2" * 64
    facts = json.loads(evidence_path.read_text())
    assert facts["owner_statement_sha256"] == digest(INSTRUCTION.encode("utf-8"))
    assert facts["observed_dependencies"] == DEPENDENCIES
    admitted = bind_standing_recall(
        manifest_path,
        evidence_path,
        DEPENDENCIES,
        principal=principal(),
        clock=Clock(),
    )
    assert admitted()
    with pytest.raises(ValueError, match="already exist"):
        write_standing_admission(
            out,
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            instruction=INSTRUCTION,
            reference="synthetic-owner-message",
            rollback_build_ref="b" * 40,
            dependencies=DEPENDENCIES,
            activated_at=NOW,
        )
    with pytest.raises(ValueError):
        write_standing_admission(
            tmp_path / "other",
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            instruction="Too short.",
            reference="synthetic-owner-message",
            rollback_build_ref="b" * 40,
            dependencies=DEPENDENCIES,
            activated_at=NOW,
        )
    with pytest.raises(ValueError, match="decided after"):
        write_standing_admission(
            tmp_path / "late",
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            instruction=INSTRUCTION,
            reference="synthetic-owner-message",
            rollback_build_ref="b" * 40,
            dependencies=DEPENDENCIES,
            activated_at=NOW,
            decided_at=NOW + timedelta(hours=1),
        )
    assert not (tmp_path / "late").exists()


async def _standing_local_options(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    from tests.unit.test_local_semantic_composition import local_options

    kwargs, _, _, _ = await local_options(tmp_path)
    path = kwargs["settings"].config_dir / "memory" / "semantic-index.yaml"
    profile = IndexProfiles.model_validate(json.loads(path.read_text())).configurations[0]
    dependencies = active_index_dependencies(
        profile,
        configuration=kwargs["configuration"],
        formation=kwargs["formation"],
        reader=kwargs["reader"],
    )
    settings = kwargs["settings"]
    for stale in (settings.memory_semantic_index_manifest, settings.memory_semantic_index_evidence):
        stale.unlink()
    out = tmp_path / "standing"
    evidence_path, manifest_path = write_standing_admission(
        out,
        tenant_id=kwargs["principal"].tenant_id,
        principal_id=kwargs["principal"].principal_id,
        instruction=INSTRUCTION,
        reference="synthetic-owner-message",
        rollback_build_ref="b" * 40,
        dependencies=dependencies,
        activated_at=kwargs["clock"].now(),
    )
    from dataclasses import replace

    kwargs["settings"] = replace(
        settings,
        memory_semantic_index_manifest=manifest_path,
        memory_semantic_index_evidence=evidence_path,
    )
    return kwargs, manifest_path, evidence_path


async def test_composition_admits_standing_local_recall_with_interactive_queries(
    tmp_path: Path,
) -> None:
    kwargs, _, evidence_path = await _standing_local_options(tmp_path)
    services = await compose_indexing(**kwargs)
    assert services is not None
    assert isinstance(services.admitted, BoundedAdmission)
    assert services.queries is not None, "Standing admission keeps interactive owner queries"
    assert services.admitted()
    kwargs["clock"].advance(timedelta(days=400))
    assert services.admitted(), "No expiry on the clock"
    evidence_path.write_bytes(evidence_path.read_bytes() + b"\n")
    assert not services.admitted()
    await services.close()


async def test_composition_refuses_standing_admission_on_a_remote_profile(
    tmp_path: Path,
) -> None:
    kwargs, _, _ = await _standing_local_options(tmp_path)
    path = kwargs["settings"].config_dir / "memory" / "semantic-index.yaml"
    from tests.contract.semantic_indexing_cases import INDEX_CONFIG

    remote = INDEX_CONFIG.model_copy(
        update={
            "space": INDEX_CONFIG.space.model_copy(update={"provider": "openai"}),
            "residency_provider": "openai",
        }
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "configurations": [
                    {"credential_ref": "openai", "embedding": remote.model_dump(mode="json")}
                ],
            }
        )
    )
    with pytest.raises(ConfigurationError, match="Semantic index"):
        await compose_indexing(**kwargs)
