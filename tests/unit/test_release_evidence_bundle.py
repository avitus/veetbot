"""The bundled provider-memory release evidence is loadable and unambiguous.

Nothing else loads the shipped release-evidence directory in CI. Without this
module a bundled artifact can rot silently — as the `formation@4` artifact did
once its policy version moved — and nobody learns until a composition falls back
at runtime and writes a selection audit no test reads.

Three properties are checked. Every bundled file parses through the loader the
runtime itself uses; no two files claim the same activation tuple, since a
duplicate would leave the activated policy version ambiguous; and every file
is bound to the compiled policy version this tree ships. The third used to be
deliberately untested so that a policy change could merge before its evidence
was republished. That interregnum is exactly how the first `formation@9`
deploy on 2026-09-03 silently ran deterministic formation, so ADR-0086 closes
it: a policy change regenerates its evidence on the same tree or withdraws the
artifact and records the gap in project state, and this module is where that
becomes a red pull request instead of a post-deploy audit.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from agent_core.config import (
    PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT,
    ConfigurationError,
    load_memory_release_evidence,
    shipped_policy_version,
)
from agent_core.domain.memory import (
    MemoryDistillationEvidence,
    ProviderExtractionEvaluationEvidence,
)
from agent_core.evals.memory_distillation import load_distillation_corpus
from agent_core.evals.memory_formation import load_corpus as load_formation_corpus
from agent_core.memory.equivalence import DISTILLATION_SCORER_VERSION

ActivationTuple = tuple[str, str, str, str, str, str, str]


def _activation_tuple(
    evidence: ProviderExtractionEvaluationEvidence | MemoryDistillationEvidence,
) -> ActivationTuple:
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
        evidence = load_memory_release_evidence(path)
        claimed.setdefault(_activation_tuple(evidence), []).append(path.name)
    return {tuple_: names for tuple_, names in claimed.items() if len(names) > 1}


def test_every_bundled_artifact_parses_through_the_runtime_loader() -> None:
    artifacts = _bundled_artifacts(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT)

    assert artifacts, "the release bundle holds no artifact for the runtime to load"
    for path in artifacts:
        evidence = load_memory_release_evidence(path)
        assert isinstance(
            evidence,
            (ProviderExtractionEvaluationEvidence, MemoryDistillationEvidence),
        )
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


def test_bundled_distillation_evidence_matches_the_corpus_and_scorer_it_claims() -> None:
    """A formation@9 artifact activates production, so its provenance must be live.

    The corpus digest must be the digest of the checked-in corpus, the scorer
    version must be the scorer this tree runs, and the build ref must be a
    commit rather than an operator-typed label; otherwise the numbers in the
    artifact describe an evaluation nobody can rerun.
    """

    repository_root = Path(__file__).resolve().parents[2]
    _corpus, corpus_sha256 = load_distillation_corpus(repository_root)
    for path in _bundled_artifacts(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT):
        evidence = load_memory_release_evidence(path)
        if not isinstance(evidence, MemoryDistillationEvidence):
            continue
        assert evidence.corpus_sha256 == corpus_sha256, path.name
        assert evidence.scorer_version == DISTILLATION_SCORER_VERSION, path.name
        assert re.fullmatch(r"[0-9a-f]{40}", evidence.build_ref), path.name
        assert evidence.seeded_case_count >= 1, path.name


def _policy_binding_errors(root: Path) -> list[str]:
    """Name every bundled artifact bound to a policy this tree no longer compiles."""

    errors: list[str] = []
    for path in _bundled_artifacts(root):
        evidence = load_memory_release_evidence(path)
        compiled = shipped_policy_version(evidence.policy_profile)
        if evidence.policy_version != compiled:
            errors.append(
                f"{path.name} is bound to {evidence.policy_version} but this tree "
                f"compiles {compiled}; regenerate the evidence on this tree or withdraw "
                "the artifact and record the interregnum in project state"
            )
    return errors


def test_every_bundled_artifact_is_bound_to_the_policy_this_tree_compiles() -> None:
    """A policy change must fail here, not in a post-deploy selection audit.

    The activation tuple includes the compiled policy version, so an unrelated
    policy rule silently drops production to deterministic formation the
    moment it merges, unless the evidence is regenerated on the same tree.
    That happened once on 2026-09-03; this is the guard that makes it a red
    pull request instead.
    """

    assert _policy_binding_errors(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT) == []


def test_a_stale_policy_binding_is_detected(tmp_path: Path) -> None:
    artifacts = _bundled_artifacts(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT)
    if not artifacts:
        pytest.skip("no bundled artifact to age")
    original = artifacts[0]
    evidence = load_memory_release_evidence(original)
    stale = evidence.model_copy(update={"policy_version": "default@000000000000+h00000000"})
    (tmp_path / original.name).write_text(stale.model_dump_json(), encoding="utf-8")

    errors = _policy_binding_errors(tmp_path)

    assert len(errors) == 1
    assert original.name in errors[0]
    assert "default@000000000000+h00000000" in errors[0]


def test_bundled_provider_evidence_matches_the_corpus_and_store_it_claims() -> None:
    """A formation@10 artifact must come from this corpus, a populated store, and a commit."""

    repository_root = Path(__file__).resolve().parents[2]
    _corpus, corpus_sha256 = load_formation_corpus(repository_root)
    for path in _bundled_artifacts(PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT):
        evidence = load_memory_release_evidence(path)
        if not isinstance(evidence, ProviderExtractionEvaluationEvidence):
            continue
        assert evidence.formation_policy_version == "formation@10", (
            f"{path.name}: the frozen formation@8 control no longer ships an artifact"
        )
        assert evidence.corpus_sha256 == corpus_sha256, path.name
        assert evidence.seeded_case_count >= 1, path.name
        assert re.fullmatch(r"[0-9a-f]{40}", evidence.build_ref), path.name
