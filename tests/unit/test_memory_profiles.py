"""The memory lifecycle profile document and the frozen models behind it.

These pin the configuration surface specified by
docs/plan/memory-evaluation-and-lifecycle.md: the shipped document is the
defaults, unknown keys are refused, the interactive snapshot caps live in
`context/plan.yaml` alone, and the session idle boundary never becomes a knob.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from agent_core.config import PACKAGE_ROOT, SHIPPED_KNOB_PATHS, ConfigurationError
from agent_core.memory.formation import SESSION_IDLE_SECONDS
from agent_core.memory.profiles import (
    FormationProfile,
    MemoryProfiles,
    RankingWeights,
    SnapshotProfiles,
)

MEMORY_PROFILE_DOCUMENT = "memory/profiles.yaml"
CONTEXT_PLAN_DOCUMENT = "context/plan.yaml"


def _document(relative: str) -> dict[str, Any]:
    loaded: object = yaml.safe_load((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return {str(key): value for key, value in loaded.items()}


def test_shipped_profiles_document_validates_into_frozen_models_and_equals_defaults() -> None:
    profiles = MemoryProfiles.from_document(_document(MEMORY_PROFILE_DOCUMENT))

    assert profiles == MemoryProfiles()
    assert profiles.schema_version == 1
    with pytest.raises(ValidationError):
        profiles.retrieval.reciprocal_rank_fusion_k = 1


def test_unknown_profile_key_is_rejected() -> None:
    document = _document(MEMORY_PROFILE_DOCUMENT)
    retrieval = dict(document["retrieval"])
    retrieval["hallucinated_knob"] = 1

    with pytest.raises(ConfigurationError, match=re.escape("memory/profiles.yaml")):
        MemoryProfiles.from_document({**document, "retrieval": retrieval})


def test_interactive_snapshot_knobs_are_not_in_the_memory_profile() -> None:
    document = _document(MEMORY_PROFILE_DOCUMENT)

    assert set(document["snapshots"]) == {"async", "child"}
    assert set(SnapshotProfiles.model_fields) == {"async_", "child"}
    assert not [
        path for path in SHIPPED_KNOB_PATHS[MEMORY_PROFILE_DOCUMENT] if "interactive" in path
    ]
    interactive = _document(CONTEXT_PLAN_DOCUMENT)["classes"]["memory_snapshot"]
    assert interactive["max_items"] == 40
    assert interactive["max_tokens"] == 1500


def test_idle_seconds_is_not_a_profile_knob() -> None:
    serialized = (PACKAGE_ROOT / MEMORY_PROFILE_DOCUMENT).read_text(encoding="utf-8")

    assert "idle" not in serialized
    assert not [name for name in FormationProfile.model_fields if "idle" in name]
    assert not [path for path in SHIPPED_KNOB_PATHS[MEMORY_PROFILE_DOCUMENT] if "idle" in path]
    assert SESSION_IDLE_SECONDS == 30


@pytest.mark.parametrize("field", ["confidence", "authority"])
def test_ranking_profile_preserves_trust_sensitive_terms(field: str) -> None:
    """Trust-bearing score terms cannot be disabled by an operator overlay."""

    with pytest.raises(ValidationError, match="greater than 0"):
        RankingWeights.model_validate({field: 0.0})
