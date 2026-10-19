"""Offline deployment-input proof for a published checkpoint comparison.

This receipt does not authorize activation or teach the legacy runtime a new format.
"""

import json
from pathlib import Path

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.evals.general_recall_checkpoint import _digest, _json, _read, _write
from agent_core.evals.general_recall_checkpoint_scoring import score_checkpoint_release
from agent_core.evals.general_recall_release_binding import committed_inputs
from agent_core.evals.memory_distillation import require_committed_tree
from agent_core.memory.general_recall_evidence import _unique_object
from agent_core.memory.recall_release_binding import capture_inputs


def publish_checkpoint_binding(
    root: Path,
    publication: Path,
    *,
    provenance_sha256: str,
    deployment_build_ref: str,
    output: Path,
) -> Path:
    """Compare all evaluated inputs; trust only an independently pinned publication.

    The caller obtains the provenance digest from the completed, human-reviewed
    publication step. A self-supplied digest does not authenticate a publication.
    Collector additions are not silently excluded from the input comparison.
    """
    if output.exists() or output.is_symlink() or publication.is_symlink():
        raise ValueError("Retain existing binding attempts; require a safe publication")
    names = ("release.json", "provenance.json", "identity.json", "collection.json", "report.json")
    raw = {name: _read(publication / name) for name in names}
    bundle = CheckpointReleaseBundle.model_validate(
        json.loads(raw["release.json"], object_pairs_hook=_unique_object)
    )
    lineage, identity = bundle.lineage, bundle.identity
    provenance = json.loads(raw["provenance.json"], object_pairs_hook=_unique_object)
    if (
        _digest(raw["provenance.json"]) != provenance_sha256
        or provenance.get("version") != "recall-checkpoint-publication@1"
        or provenance.get("activation_eligible") is not False
        or provenance.get("release_sha256") != _digest(raw["release.json"])
        or provenance.get("measurement_identity_sha256") != lineage.parent_identity_sha256
        or provenance.get("collection_manifest_sha256") != lineage.collection_manifest_sha256
        or provenance.get("collector_commit") != lineage.collector_commit
        or provenance.get("collector_manifest_sha256") != lineage.collector_manifest_sha256
        or raw["identity.json"] != identity.model_dump_json().encode()
        or _digest(raw["collection.json"]) != lineage.collection_manifest_sha256
        or raw["report.json"] != _json(score_checkpoint_release(bundle))
    ):
        raise ValueError("Checkpoint publication is missing, changed or not independently pinned")
    require_committed_tree(root, deployment_build_ref)
    measured = committed_inputs(root, identity.build_ref, identity)
    deployed = committed_inputs(root, deployment_build_ref, identity)
    if (
        measured.digest != identity.evaluated_inputs_sha256
        or measured != deployed
        or capture_inputs(root, identity.model_dump()) != deployed
    ):
        raise ValueError("Measured and deployed checkpoint inputs differ")
    receipt = {
        "version": "recall-checkpoint-input-binding@1",
        "activation_eligible": False,
        "identity": identity.model_dump(mode="json"),
        "deployment_build_ref": deployment_build_ref,
        "evidence_sha256": _digest(raw["release.json"]),
        "publication_sha256": provenance_sha256,
        "inputs": measured.model_dump(mode="json"),
    }
    require_committed_tree(root, deployment_build_ref)
    if capture_inputs(root, identity.model_dump()) != deployed or any(
        _read(publication / name) != data for name, data in raw.items()
    ):
        raise ValueError("Checkpoint publication or deployment inputs changed during binding")
    _write(output, _json(receipt))
    return output
