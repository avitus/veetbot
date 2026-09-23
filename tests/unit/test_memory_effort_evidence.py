"""ADR-0118: formation@9 evidence binds the reasoning effort it was evaluated at."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.config import PACKAGE_ROOT
from agent_core.domain.memory import MemoryDistillationEvidence
from agent_core.domain.messages import ReasoningEffort, ResolvedModel
from agent_core.memory.distillation import distillation_evidence_matches

SOL_ARTIFACT = (
    PACKAGE_ROOT / "memory/release_evidence/openai-balanced-gpt-5.6-sol-default-formation9.json"
)


def _artifact(**changes: Any) -> dict[str, Any]:
    return {**json.loads(Path(SOL_ARTIFACT).read_text(encoding="utf-8")), **changes}


def _sol() -> ResolvedModel:
    return ResolvedModel(
        provider="openai",
        model="gpt-5.6-sol",
        policy_name="balanced",
        resolved_at=datetime(2026, 9, 23, tzinfo=UTC),
    )


def test_a_schema_seven_artifact_is_provider_default_evidence() -> None:
    evidence = MemoryDistillationEvidence.model_validate(_artifact())
    matches = [
        distillation_evidence_matches(
            evidence,
            _sol(),
            evidence.policy_profile,
            evidence.policy_version,
            reasoning_effort=effort,
        )
        for effort in (None, ReasoningEffort.MEDIUM)
    ]

    assert evidence.schema_version == 7
    assert evidence.reasoning_effort is None
    assert matches == [True, False]


def test_a_schema_eight_artifact_matches_only_its_own_effort() -> None:
    evidence = MemoryDistillationEvidence.model_validate(
        _artifact(schema_version=8, reasoning_effort="medium")
    )
    matches = {
        effort: distillation_evidence_matches(
            evidence,
            _sol(),
            evidence.policy_profile,
            evidence.policy_version,
            reasoning_effort=effort,
        )
        for effort in (None, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH)
    }

    assert matches == {None: False, ReasoningEffort.MEDIUM: True, ReasoningEffort.HIGH: False}


def test_schema_seven_cannot_claim_an_effort() -> None:
    with pytest.raises(ValidationError, match="effort"):
        MemoryDistillationEvidence.model_validate(_artifact(reasoning_effort="medium"))
