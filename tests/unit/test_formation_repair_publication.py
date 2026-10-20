"""Mixed runtime cohorts require explicit publication provenance, never retagging."""

from typing import Any

import pytest

from agent_core.domain.general_recall_checkpoint_release import CheckpointReleaseBundle
from agent_core.evals.general_recall_formation_repair import validate_repair_lineage
from tests.unit.test_general_recall_checkpoint_scoring import fixture

pytestmark = pytest.mark.static


def repaired_bundle() -> CheckpointReleaseBundle:
    data = fixture().model_dump()
    data["version"] = "general-recall-checkpoint-release@2"
    data["lineage"]["formation_repair_sha256"] = "a" * 64
    return CheckpointReleaseBundle.model_validate(data)


def test_repair_publication_is_separate_and_preserves_base_observations() -> None:
    bundle = repaired_bundle()
    validate_repair_lineage(
        bundle, {"version": "recall-checkpoint-collection@2", "formation_repair_sha256": "a" * 64}
    )
    assert bundle.observations == fixture().observations
    data = bundle.model_dump()
    data["version"] = "general-recall-checkpoint-release@1"
    with pytest.raises(ValueError):
        CheckpointReleaseBundle.model_validate(data)


@pytest.mark.parametrize(
    "manifest",
    [
        {"version": "recall-checkpoint-collection@1"},
        {"version": "recall-checkpoint-collection@2", "formation_repair_sha256": "b" * 64},
    ],
)
def test_repaired_bundle_cannot_claim_unchanged_or_other_runtime(manifest: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_repair_lineage(repaired_bundle(), manifest)


def test_legacy_bundle_cannot_publish_repaired_collection() -> None:
    with pytest.raises(ValueError):
        validate_repair_lineage(
            fixture(),
            {"version": "recall-checkpoint-collection@2", "formation_repair_sha256": "a" * 64},
        )


def test_raw_compiler_rejects_unadmitted_repair_marker(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from agent_core.evals.general_recall_checkpoint import CheckpointStore, _json
    from agent_core.evals.general_recall_checkpoint_collection import compile_checkpoint_collection
    from tests.unit.test_general_recall_checkpoint_collection import setup_collection

    original = CheckpointStore.complete

    def complete(self: Any, key: Any, raw: bytes, **kwargs: Any) -> Any:
        row = json.loads(raw)
        row["execution_repair"] = {"unadmitted": True}
        return original(self, key, _json(row), **kwargs)

    monkeypatch.setattr(CheckpointStore, "complete", complete)
    store, parent, parents = setup_collection(tmp_path, monkeypatch)
    with store, pytest.raises(ValueError, match="provenance"):
        compile_checkpoint_collection(store, parent, parents)
