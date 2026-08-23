"""The bundled provider-memory release evidence is loadable and unambiguous.

Nothing else loads the shipped release-evidence directory in CI. Without this
module a bundled artifact can rot silently — as the `formation@4` artifact did
once its policy version moved — and nobody learns until a composition falls back
at runtime and writes a selection audit no test reads.

Two properties are checked, and neither of them is "the bundle matches the
current policy". That deliberately stays untested: the design allows an
interregnum in which the active policy has moved and the replacement artifact
has not yet been published, and asserting equality with
``PROVIDER_FORMATION_POLICY_VERSION`` would have been red for the whole
documented `formation@4` period. What must always hold is weaker and sharper:
every bundled file parses through the loader the runtime itself uses, and no two
files claim the same activation tuple, since a duplicate would leave the
activated policy version ambiguous.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_core.config import (
    PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT,
    ConfigurationError,
    load_provider_extraction_evidence,
)
from agent_core.domain.memory import ProviderExtractionEvaluationEvidence

ActivationTuple = tuple[str, str, str, str, str, str, str]


def _activation_tuple(evidence: ProviderExtractionEvaluationEvidence) -> ActivationTuple:
    """The seven fields startup validation matches an artifact against."""

    return (
        evidence.extractor_version,
        evidence.formation_policy_version,
        evidence.model_policy,
        evidence.provider,
        evidence.model,
        evidence.policy_profile,
        evidence.policy_version,
    )


def _bundled_artifacts(root: Path) -> list[Path]:
    """Every file the runtime would consider, in the order it considers them."""

    return sorted(root.glob("*.json"))


def _duplicate_activation_tuples(root: Path) -> dict[ActivationTuple, list[str]]:
    """Map each activation tuple claimed by more than one artifact to its files."""

    claimed: dict[ActivationTuple, list[str]] = {}
    for path in _bundled_artifacts(root):
        evidence = load_provider_extraction_evidence(path)
        claimed.setdefault(_activation_tuple(evidence), []).append(path.name)
    return {tuple_: names for tuple_, names in claimed.items() if len(names) > 1}


def test_release_evidence_root_is_a_directory_the_runtime_can_read() -> None:
    assert PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT.is_dir()


def test_every_bundled_artifact_parses_through_the_runtime_loader() -> None:
    artifacts = _bundled_artifacts(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT)

    for path in artifacts:
        evidence = load_provider_extraction_evidence(path)
        assert isinstance(evidence, ProviderExtractionEvaluationEvidence)
        assert all(field for field in _activation_tuple(evidence))


def test_bundled_activation_tuples_are_unique() -> None:
    duplicates = _duplicate_activation_tuples(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT)

    assert duplicates == {}, f"two bundled artifacts claim one activation tuple: {duplicates}"


def test_duplicate_activation_tuples_are_detected(tmp_path: Path) -> None:
    """The uniqueness check must fail when a tuple really is claimed twice.

    Without this the check above would pass just as happily against a bundle it
    was not actually reading.
    """

    artifacts = _bundled_artifacts(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT)
    if not artifacts:
        pytest.skip("no bundled artifact to duplicate")
    original = artifacts[0]
    shutil.copyfile(original, tmp_path / original.name)
    shutil.copyfile(original, tmp_path / f"copy-of-{original.name}")

    duplicates = _duplicate_activation_tuples(tmp_path)

    assert len(duplicates) == 1
    assert sorted(next(iter(duplicates.values()))) == sorted(
        [original.name, f"copy-of-{original.name}"]
    )


def test_an_unparseable_bundled_artifact_is_refused(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text('{"schema_version": 2}', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="did not pass"):
        _duplicate_activation_tuples(tmp_path)
