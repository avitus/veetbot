"""Real Git input comparisons; synthetic publication fixtures are not release evidence."""

import json
from pathlib import Path

import pytest

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.evals.general_recall_checkpoint import _digest, _json
from agent_core.evals.general_recall_checkpoint_binding import publish_checkpoint_binding
from agent_core.evals.general_recall_checkpoint_scoring import score_checkpoint_release
from agent_core.memory.recall_release_binding import capture_inputs
from tests.unit.test_general_recall_checkpoint_scoring import fixture
from tests.unit.test_recall_release_binding import git, small_root

pytestmark = pytest.mark.static


def published(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = small_root(tmp_path)
    git(root, "init")
    git(root, "config", "user.name", "Synthetic fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "Frozen source")
    revision = git(root, "rev-parse", "HEAD")
    value = fixture().model_dump(mode="json")
    value["identity"]["build_ref"] = revision
    inputs = capture_inputs(root, value["identity"])
    value["identity"]["evaluated_inputs_sha256"] = inputs.digest
    from agent_core.domain.general_recall_release import RecallReleaseIdentity

    identity = RecallReleaseIdentity.model_validate(value["identity"])
    identity_sha = _digest(identity.model_dump_json().encode())
    for row in (*value["observations"], *value["gates"]):
        row["identity_sha256"] = identity_sha
    collection = _json({"synthetic_collection": True})
    value["lineage"].update(
        parent_identity_sha256=identity_sha,
        evaluated_inputs_sha256=inputs.digest,
        collection_manifest_sha256=_digest(collection),
    )
    bundle = CheckpointReleaseBundle.model_validate(value)
    directory = tmp_path / "publication"
    directory.mkdir()
    release = bundle.model_dump_json().encode()
    (directory / "release.json").write_bytes(release)
    (directory / "identity.json").write_text(identity.model_dump_json())
    (directory / "collection.json").write_bytes(collection)
    (directory / "report.json").write_bytes(_json(score_checkpoint_release(bundle)))
    provenance = _json(
        {
            "version": "recall-checkpoint-publication@1",
            "activation_eligible": False,
            "release_sha256": _digest(release),
            "measurement_identity_sha256": identity_sha,
            "collection_manifest_sha256": _digest(collection),
            "collector_commit": bundle.lineage.collector_commit,
            "collector_manifest_sha256": bundle.lineage.collector_manifest_sha256,
        }
    )
    (directory / "provenance.json").write_bytes(provenance)
    return root, directory, revision, _digest(provenance)


def test_binding_preserves_measurement_identity_and_does_not_grant_activation(
    tmp_path: Path,
) -> None:
    root, publication, original, digest = published(tmp_path)
    before = (publication / "release.json").read_bytes()
    (root / "docs").mkdir()
    (root / "docs/release.md").write_text("Documentation only\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Documentation")
    target = git(root, "rev-parse", "HEAD")
    output = publish_checkpoint_binding(
        root,
        publication,
        provenance_sha256=digest,
        deployment_build_ref=target,
        output=tmp_path / "binding.json",
    )
    bound = json.loads(output.read_bytes())
    assert bound["version"] == "recall-checkpoint-input-binding@1"
    assert bound["activation_eligible"] is False
    assert bound["identity"]["build_ref"] == original != target
    assert bound["deployment_build_ref"] == target
    assert bound["publication_sha256"] == digest
    assert (publication / "release.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "runtime",
        "collector",
        "new_input",
        "dirty",
        "provenance",
        "release",
        "collection",
        "report",
        "identity",
        "existing",
    ],
)
def test_binding_rejects_changed_inputs_or_unpublished_evidence(
    tmp_path: Path, change: str
) -> None:
    root, publication, original, digest = published(tmp_path)
    output = tmp_path / "binding.json"
    if change in {"runtime", "collector", "new_input", "dirty"}:
        path = (
            root
            / {
                "runtime": "src/agent_core/memory/reader.py",
                "collector": "src/agent_core/evals/new_collector.py",
                "new_input": "new-input.py",
                "dirty": "uv.lock",
            }[change]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Changed input\n")
        if change != "dirty":
            git(root, "add", ".")
            git(root, "commit", "-m", "Changed evaluated inputs")
    elif change == "existing":
        output.write_text("Retained receipt")
    else:
        path = publication / f"{change}.json"
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        publish_checkpoint_binding(
            root,
            publication,
            provenance_sha256=digest,
            deployment_build_ref=git(root, "rev-parse", "HEAD"),
            output=output,
        )
    assert output.read_text() == "Retained receipt" if change == "existing" else not output.exists()
    assert original
