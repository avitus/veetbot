"""Release input equivalence preserves original evidence; fixtures are not reviews."""

import hashlib
import json
from pathlib import Path

import pytest

from agent_core.memory.semantic_deployment import bind_index_manifest
from tests.unit.test_general_recall_release import ROOT
from tests.unit.test_semantic_index_composition import release_files


def test_identical_merge_inputs_can_bind_without_retagging_observations(tmp_path: Path) -> None:
    manifest, evidence, dependencies = release_files(tmp_path)
    original = evidence.read_bytes()
    identity = json.loads(manifest.read_bytes())
    # A receipt without the measured input commitment must remain ineligible.
    # This first regression reaches the old binder's existing public boundary.
    from agent_core.memory import recall_release_binding as binding

    inputs = binding.capture_inputs(ROOT, identity)
    identity["evaluated_inputs_sha256"] = inputs.digest
    bundle = json.loads(original)
    bundle["identity"] = identity
    identity_digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()
    for row in (*bundle["observations"], *bundle["gates"]):
        row["identity_sha256"] = identity_digest
    evidence.write_text(json.dumps(bundle))
    measured = evidence.read_bytes()
    receipt = {
        "version": "recall-deployment-binding@1",
        "identity": identity,
        "deployment_build_ref": "b" * 40,
        "evidence_sha256": hashlib.sha256(measured).hexdigest(),
        "inputs": inputs.model_dump(mode="json"),
    }
    manifest.write_text(json.dumps(receipt))
    admitted = bind_index_manifest(
        ROOT, manifest, evidence, "20261012-120000-bbbbbbb", dependencies
    )
    assert admitted()
    assert evidence.read_bytes() == measured
    assert json.loads(evidence.read_bytes())["identity"]["build_ref"] == "a" * 40


def small_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    for name in (
        "src/agent_core/domain/general_recall_release.py",
        "src/agent_core/memory/general_recall_evidence.py",
        "src/agent_core/memory/reader.py",
        "pyproject.toml",
        "uv.lock",
        "migrations/versions/schema.py",
        "deploy/app/release.sh",
        ".circleci/config.yml",
        "evals/capability/corpus.json",
        "evals/fixtures/controls.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Original input\n")
    return root


def measured_bundle(root: Path, evidence: Path, build_ref: str = "a" * 40) -> dict[str, str]:
    from agent_core.domain.general_recall_release import RecallReleaseIdentity
    from agent_core.memory.general_recall_evidence import scorer_digest
    from agent_core.memory.recall_release_binding import capture_inputs
    from agent_core.memory.reconsolidation_evidence import implementation_digest
    from tests.unit.test_general_recall_release import fixture

    bundle = fixture()
    identity = bundle.identity.model_copy(
        update={
            "build_ref": build_ref,
            "implementation_sha256": implementation_digest(root),
            "scorer_sha256": scorer_digest(root),
        }
    )
    inputs = capture_inputs(root, identity.model_dump())
    identity = RecallReleaseIdentity.model_validate(
        identity.model_dump() | {"evaluated_inputs_sha256": inputs.digest}
    )
    identity_hash = hashlib.sha256(identity.model_dump_json().encode()).hexdigest()
    bundle = bundle.model_copy(
        update={
            "identity": identity,
            "observations": tuple(
                row.model_copy(update={"identity_sha256": identity_hash})
                for row in bundle.observations
            ),
            "gates": tuple(
                row.model_copy(update={"identity_sha256": identity_hash}) for row in bundle.gates
            ),
        }
    )
    evidence.write_text(bundle.model_dump_json())
    return {
        field: getattr(identity, field)
        for field in (
            "configuration_sha256",
            "embedding_configuration_sha256",
            "formation_configuration_sha256",
            "reader_configuration_sha256",
        )
    }


def receipt_for(root: Path, evidence: Path, receipt: Path) -> None:
    from agent_core.domain.general_recall_release import RecallReleaseIdentity
    from agent_core.memory.recall_release_binding import RecallDeploymentBinding, capture_inputs

    raw = evidence.read_bytes()
    identity = RecallReleaseIdentity.model_validate(json.loads(raw)["identity"])
    receipt.write_text(
        RecallDeploymentBinding(
            version="recall-deployment-binding@1",
            identity=identity,
            deployment_build_ref="b" * 40,
            evidence_sha256=hashlib.sha256(raw).hexdigest(),
            inputs=capture_inputs(root, identity.model_dump()),
        ).model_dump_json()
    )


@pytest.mark.parametrize(
    "changed",
    [
        "src/agent_core/memory/reader.py",
        "src/agent_core/memory/general_recall_evidence.py",
        "uv.lock",
        "pyproject.toml",
        "migrations/versions/schema.py",
        "deploy/app/release.sh",
        ".circleci/config.yml",
        "evals/capability/corpus.json",
        "evals/fixtures/controls.json",
        "new-input.py",
    ],
)
def test_changed_input_refuses_binding_and_latches_withdrawal(tmp_path: Path, changed: str) -> None:
    root = small_root(tmp_path)
    evidence, receipt = tmp_path / "evidence.json", tmp_path / "receipt.json"
    dependencies = measured_bundle(root, evidence)
    receipt_for(root, evidence, receipt)
    admitted = bind_index_manifest(root, receipt, evidence, "20261012-120000-bbbbbbb", dependencies)
    assert admitted()
    target = root / changed
    original = target.read_bytes() if target.exists() else None
    target.write_text("Changed evaluated input\n")
    assert not admitted()
    with pytest.raises(ValueError, match="evaluated input"):
        bind_index_manifest(root, receipt, evidence, "20261012-120000-bbbbbbb", dependencies)
    if original is None:
        target.unlink()
    else:
        target.write_bytes(original)
    assert not admitted(), "Restoring files cannot renew observed revocation"


@pytest.mark.parametrize(
    "mutation", ["missing", "symlink", "mode", "omit", "forged", "version", "receipt", "evidence"]
)
def test_missing_forged_or_replaced_binding_is_refused(tmp_path: Path, mutation: str) -> None:
    root = small_root(tmp_path)
    evidence, receipt = tmp_path / "evidence.json", tmp_path / "receipt.json"
    dependencies = measured_bundle(root, evidence)
    receipt_for(root, evidence, receipt)
    admitted = bind_index_manifest(root, receipt, evidence, "20261012-120000-bbbbbbb", dependencies)
    target = root / "deploy/app/release.sh"
    if mutation == "missing":
        target.unlink()
    elif mutation == "symlink":
        target.unlink()
        target.symlink_to(root / "uv.lock")
    elif mutation == "mode":
        target.chmod(0o755)
    elif mutation == "evidence":
        evidence.write_bytes(evidence.read_bytes() + b" ")
    else:
        value = json.loads(receipt.read_bytes())
        if mutation == "omit":
            value["inputs"]["files"].pop("deploy/app/release.sh")
        elif mutation == "forged":
            value["identity"]["evaluated_inputs_sha256"] = "0" * 64
        elif mutation == "version":
            value["inputs"]["version"] = "unknown-exclusions@1"
        else:
            value["deployment_build_ref"] = "c" * 40
        receipt.write_text(json.dumps(value))
    assert not admitted()
    with pytest.raises((ValueError, OSError)):
        bind_index_manifest(root, receipt, evidence, "20261012-120000-bbbbbbb", dependencies)


def test_legacy_identity_serialization_has_no_added_null_field() -> None:
    from tests.unit.test_general_recall_release import fixture

    assert "evaluated_inputs_sha256" not in fixture().identity.model_dump()
    assert "evaluated_inputs_sha256" not in fixture().identity.model_dump_json()


@pytest.mark.parametrize("generated", [".owner-scopes.env", "website/out/index.html"])
def test_deployment_generated_outputs_do_not_change_evaluated_inputs(
    tmp_path: Path, generated: str
) -> None:
    root = small_root(tmp_path)
    evidence, receipt = tmp_path / "evidence.json", tmp_path / "receipt.json"
    dependencies = measured_bundle(root, evidence)
    receipt_for(root, evidence, receipt)
    target = root / generated
    target.parent.mkdir(parents=True, exist_ok=True)
    # release.sh generates the owner scope file; hosted checks build website/out.
    # Effective authority remains bound independently by the composition digest.
    target.write_text("Generated runtime or website output\n")
    admitted = bind_index_manifest(root, receipt, evidence, "20261012-120000-bbbbbbb", dependencies)
    assert admitted()
    for dependency in dependencies:
        with pytest.raises(ValueError, match="active dependency mismatch"):
            bind_index_manifest(
                root,
                receipt,
                evidence,
                "20261012-120000-bbbbbbb",
                dependencies | {dependency: "0" * 64},
            )


def git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_release_step_independently_compares_both_git_trees(tmp_path: Path) -> None:
    from agent_core.evals.general_recall_release_binding import publish_binding

    root = small_root(tmp_path)
    git(root, "init")
    git(root, "config", "user.name", "Test fixture")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "Measured implementation")
    source = git(root, "rev-parse", "HEAD")
    evidence = tmp_path / "evidence.json"
    dependencies = measured_bundle(root, evidence, source)
    original = evidence.read_bytes()
    (root / "docs").mkdir()
    (root / "docs/release.md").write_text("Documentation-only follow-up\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Documentation changes the commit, not evaluated inputs")
    deployed = git(root, "rev-parse", "HEAD")
    assert source != deployed
    receipt = publish_binding(
        root, evidence, deployment_build_ref=deployed, output=tmp_path / "receipt.json"
    )
    assert evidence.read_bytes() == original
    admitted = bind_index_manifest(
        root, receipt, evidence, f"20261012-120000-{deployed[:7]}", dependencies
    )
    assert admitted()
    assert json.loads(receipt.read_bytes())["identity"]["build_ref"] == source
    (root / "migrations/versions/schema.py").write_text("Different schema\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Change a non-source-code input")
    with pytest.raises(ValueError, match="inputs differ"):
        publish_binding(
            root,
            evidence,
            deployment_build_ref=git(root, "rev-parse", "HEAD"),
            output=tmp_path / "rejected.json",
        )
    assert not (tmp_path / "rejected.json").exists()
    assert evidence.read_bytes() == original
