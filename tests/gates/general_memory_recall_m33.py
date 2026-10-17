"""Explicit M33 release checks over original, independently reviewed evidence.

This file deliberately lacks the ``test_`` prefix: it is selected by the gate
registry when M33 is evaluated, not by implementation-only CI before measurement.
An explicit invocation without complete evidence fails; it never skips or treats
mechanism fixtures as release observations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import pytest
from pydantic import Field

from agent_core.domain.general_recall_release import (
    RecallReleaseIdentity,
    ReleaseValue,
)
from agent_core.evals.general_recall_publication import publish_release
from agent_core.memory.general_recall_evidence import _read_bounded, _unique_object

ROOT = Path(__file__).resolve().parents[2]


class GateInputs(ReleaseValue):
    """Operator-supplied paths; none of these files is manufactured by a gate."""

    version: Literal["recall-gate-inputs@1"]
    identity: Path
    bundle: Path
    independence_review: Path
    failure_review: Path
    collection_manifest: Path
    gate_reports: tuple[Path, ...] = Field(min_length=11, max_length=11)


def verify_release_inputs(manifest_path: Path, output: Path) -> None:
    """Recompile original observations and verify every review and gate receipt.

    Gate-report provenance remains the trusted operator's responsibility, as in
    publication. A passing unit test or an invented receipt is not evidence.
    Requiring the whole package for each named gate is deliberately conservative:
    no M33 release gate turns green while another release prerequisite is absent.
    """
    raw = _read_bounded(manifest_path)
    inputs = GateInputs.model_validate(json.loads(raw, object_pairs_hook=_unique_object))

    def source(path: Path) -> Path:
        return path if path.is_absolute() else manifest_path.parent / path

    identity_path = source(inputs.identity)
    identity_raw = _read_bounded(identity_path)
    expected = RecallReleaseIdentity.model_validate(
        json.loads(identity_raw, object_pairs_hook=_unique_object)
    )
    publish_release(
        ROOT,
        source(inputs.bundle),
        expected,
        independence_review=source(inputs.independence_review),
        failure_review=source(inputs.failure_review),
        gate_reports=tuple(source(path) for path in inputs.gate_reports),
        collection_manifest=source(inputs.collection_manifest),
        output=output,
    )
    if _read_bounded(manifest_path) != raw or _read_bounded(identity_path) != identity_raw:
        raise ValueError("M33 gate inputs changed during verification")


@pytest.fixture(scope="session")
def reviewed_release(tmp_path_factory: pytest.TempPathFactory) -> None:
    configured = os.environ.get("VEETBOT_M33_GATE_INPUTS")
    if not configured:
        raise ValueError("M33 requires VEETBOT_M33_GATE_INPUTS with complete reviewed evidence")
    output = tmp_path_factory.mktemp("m33-gate-verification") / "recompiled"
    verify_release_inputs(Path(configured), output)


@pytest.mark.parametrize("gate", range(1, 13))
def test_reviewed_release(gate: int, reviewed_release: None) -> None:
    """Require the actual complete publication boundary for every named gate."""
    assert 1 <= gate <= 12
