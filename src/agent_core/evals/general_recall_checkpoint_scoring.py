"""Score the approved matrix without granting publication or deployment authority."""

import hashlib
from pathlib import Path
from typing import Any

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.memory.general_recall_evidence import _score_measurements


def checkpoint_scorer_digest(root: Path) -> str:
    return hashlib.sha256(
        b"".join(
            (root / name).read_bytes()
            for name in (
                "src/agent_core/domain/general_recall_release.py",
                "src/agent_core/domain/general_recall_checkpoint_release.py",
                "src/agent_core/memory/general_recall_evidence.py",
                "src/agent_core/evals/general_recall_checkpoint_scoring.py",
            )
        )
    ).hexdigest()


def score_checkpoint_release(bundle: CheckpointReleaseBundle) -> dict[str, Any]:
    bundle = CheckpointReleaseBundle.model_validate(bundle.model_dump())
    identity = hashlib.sha256(bundle.identity.model_dump_json().encode()).hexdigest()
    if (
        bundle.lineage.parent_identity_sha256 != identity
        or bundle.identity.evaluated_inputs_sha256 != bundle.lineage.evaluated_inputs_sha256
    ):
        raise ValueError("Checkpoint measurement identity or evaluated inputs changed")
    report = _score_measurements(bundle)
    # Publication must still verify original bytes, all ledger lineage, independent
    # human review and the eventual deployment binding. Metrics grant no authority.
    report.update(
        activation_eligible=False,
        quality_gates_satisfied=True,
        measurement_identity_sha256=identity,
        lineage=bundle.lineage.model_dump(mode="json"),
    )
    return report
