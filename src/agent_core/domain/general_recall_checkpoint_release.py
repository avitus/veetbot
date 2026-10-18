"""Separate ADR-0212 measurements; legacy activation never accepts this format."""

from typing import Literal

from pydantic import Field

from agent_core.domain.general_recall_release import (
    Digest,
    RecallGateReceipt,
    RecallObservation,
    RecallPreregistration,
    RecallReleaseIdentity,
    RecallScenario,
    ReleaseValue,
)


class CheckpointLineage(ReleaseValue):
    approval_sha256: Digest
    plan_sha256: Digest
    parent_archive_sha256: Digest
    parent_identity_sha256: Digest
    evaluated_inputs_sha256: Digest
    collector_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    collector_manifest_sha256: Digest
    scorer_sha256: Digest
    collection_manifest_sha256: Digest


class CheckpointReleaseBundle(ReleaseValue):
    version: Literal["general-recall-checkpoint-release@1"]
    identity: RecallReleaseIdentity
    preregistration: RecallPreregistration
    scenarios: tuple[RecallScenario, ...] = Field(min_length=400, max_length=400)
    observations: tuple[RecallObservation, ...] = Field(min_length=1600, max_length=1600)
    gates: tuple[RecallGateReceipt, ...] = Field(min_length=11, max_length=11)
    lineage: CheckpointLineage
