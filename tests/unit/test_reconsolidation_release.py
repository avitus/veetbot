"""Activation follows exact source/model/policy bytes and revokes on file changes."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from agent_core.domain.reconsolidation_release import (
    ReconsolidationEvidence,
    ReconsolidationQuality,
)
from agent_core.memory.reconsolidation_evidence import (
    bind_evidence,
    file_digest,
    implementation_digest,
    model_digest,
    scorer_digest,
)
from agent_core.memory.reconsolidation_policy import privacy_digest
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION
from tests.contract.reconsolidation_admission_cases import model

ROOT = Path(__file__).resolve().parents[2]


def certificate() -> ReconsolidationEvidence:
    return ReconsolidationEvidence(
        build_ref="a" * 40,
        implementation_sha256=implementation_digest(ROOT),
        corpus_sha256=file_digest(ROOT / "evals/capability/memory-reconsolidation.v1.json"),
        scorer_sha256=scorer_digest(ROOT),
        report_sha256="0" * 64,
        recall_build_ref="a" * 40,
        recall_report_sha256="0" * 64,
        recall_corpus_sha256=file_digest(ROOT / "evals/capability/memory-benchmark.v1.json"),
        provider=model().provider,
        model=model().model,
        model_configuration_sha256=model_digest(model()),
        privacy_sha256=privacy_digest(model().provider),
        formation_policy="formation@9",
        retrieval_policy=RETRIEVAL_POLICY_VERSION,
        upstream_build_ref="b" * 40,
        upstream_corpus_sha256="c" * 64,
        quality=tuple(
            ReconsolidationQuality(
                split=cast(Literal["development", "holdout"], split),
                false_merges=0,
                lost_facts=0,
                boundary_failures=0,
                failures=0,
                duplicate_coverage=0.8,
                hypothesis_precision=0.8,
                hypothesis_recall=0.6,
                answer_coverage_lift=0.1,
                merge_answer_regressions=0,
            )
            for split in ("development", "holdout")
        ),
    )


def bind(path: Path, **changes: Any) -> Callable[[], bool]:
    kwargs: dict[str, Any] = {
        "release_sha": "a" * 40,
        "formation_policy": "formation@9",
        "upstream_build_ref": "b" * 40,
        "upstream_corpus_sha256": "c" * 64,
        "residency_provider": model().provider,
    }
    kwargs.update(changes)
    return bind_evidence(ROOT, path, model(), **kwargs)


def test_exact_binding_revokes_after_replacement_or_removal(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(certificate().model_dump_json())
    admitted = bind(path)
    assert admitted()
    path.write_text(path.read_text() + " ")
    assert not admitted()
    path.unlink()
    assert not admitted()


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_sha", "d" * 40),
        ("formation_policy", "formation@11"),
        ("upstream_build_ref", None),
        ("upstream_corpus_sha256", "e" * 64),
        ("residency_provider", None),
    ],
)
def test_stale_dependency_tuple_is_not_admitted(
    tmp_path: Path, field: str, value: str | None
) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(certificate().model_dump_json())
    with pytest.raises(ValueError, match="tuple"):
        bind(path, **{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "implementation_sha256",
        "corpus_sha256",
        "scorer_sha256",
        "model_configuration_sha256",
        "privacy_sha256",
    ],
)
def test_changed_release_inputs_are_not_admitted(tmp_path: Path, field: str) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(certificate().model_copy(update={field: "f" * 64}).model_dump_json())
    with pytest.raises(ValueError, match="tuple"):
        bind(path)


def test_activation_requires_current_recall_floor_evidence() -> None:
    from pydantic import ValidationError

    payload = certificate().model_dump()
    for field in ("recall_report_sha256", "recall_corpus_sha256", "recall_build_ref"):
        payload.pop(field, None)
    with pytest.raises(ValidationError):
        ReconsolidationEvidence.model_validate(payload)
