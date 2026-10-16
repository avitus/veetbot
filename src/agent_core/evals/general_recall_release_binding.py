"""Bind an unchanged, measured input set to a clean deployment commit.

Run this trusted offline release step after publication. It never edits evidence,
reviews, observation identities or deployment settings, and never creates models.
"""

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

from agent_core.domain.general_recall_release import RecallReleaseBundle, RecallReleaseIdentity
from agent_core.evals.memory_distillation import require_committed_tree
from agent_core.memory.general_recall_evidence import _read_bounded, _unique_object, score_release
from agent_core.memory.recall_release_binding import (
    InputFile,
    RecallDeploymentBinding,
    RecallInputManifest,
    capture_inputs,
    input_dependencies,
    input_path,
)


def committed_inputs(
    root: Path, revision: str, identity: RecallReleaseIdentity
) -> RecallInputManifest:
    """Read immutable Git objects, independently of the supplied manifest's paths."""
    # Both revisions are validated full SHA values, never shell fragments or refs.
    RecallReleaseIdentity.model_validate(identity.model_dump() | {"build_ref": revision})
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    files: dict[str, InputFile] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tree:
        for entry in tree:
            name = entry.name.rstrip("/")
            if not input_path(name) or entry.isdir():
                continue
            if not entry.isfile() or entry.size > 16 * 1024 * 1024:
                raise ValueError("Recall Git inputs must be bounded regular files")
            handle = tree.extractfile(entry)
            assert handle is not None
            files[name] = InputFile(
                sha256=hashlib.sha256(handle.read()).hexdigest(),
                executable=bool(entry.mode & 0o111),
            )
    return RecallInputManifest(files=files, dependencies=input_dependencies(identity.model_dump()))


def publish_binding(root: Path, evidence: Path, *, deployment_build_ref: str, output: Path) -> Path:
    """A trusted release process supplies the original published evidence bytes."""
    if output.exists():
        raise ValueError("Retain existing deployment binding attempts")
    raw = _read_bounded(evidence)
    bundle = RecallReleaseBundle.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    identity = bundle.identity
    require_committed_tree(root, deployment_build_ref)
    measured = committed_inputs(root, identity.build_ref, identity)
    deployed = committed_inputs(root, deployment_build_ref, identity)
    if (
        identity.evaluated_inputs_sha256 is None
        or measured.digest != identity.evaluated_inputs_sha256
        or measured != deployed
        or capture_inputs(root, identity.model_dump()) != deployed
    ):
        raise ValueError("Measured and deployed recall inputs differ")
    score_release(bundle)
    receipt = RecallDeploymentBinding(
        version="recall-deployment-binding@1",
        identity=identity,
        deployment_build_ref=deployment_build_ref,
        evidence_sha256=hashlib.sha256(raw).hexdigest(),
        inputs=measured,
    )
    require_committed_tree(root, deployment_build_ref)
    if _read_bounded(evidence) != raw or capture_inputs(root, identity.model_dump()) != deployed:
        raise ValueError("Recall release inputs changed during binding")
    with output.open("xb") as stream:
        stream.write(receipt.model_dump_json(indent=2).encode() + b"\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--deployment-build-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        publish_binding(
            args.root,
            args.evidence,
            deployment_build_ref=args.deployment_build_ref,
            output=args.output,
        )
    )


if __name__ == "__main__":
    main()
