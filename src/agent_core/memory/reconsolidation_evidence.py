"""Bind admission to the exact deployed tuple; removal or replacement revokes it."""

import hashlib
from collections.abc import Callable
from pathlib import Path

from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation_release import ReconsolidationEvidence
from agent_core.memory.reconsolidation_policy import privacy_digest
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION


def implementation_digest(root: Path) -> str:
    files = sorted(
        p
        for p in (root / "src" / "agent_core").rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".json"}
    )
    files += [root / "pyproject.toml", root / "uv.lock"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def model_digest(model: ResolvedModel) -> str:
    return hashlib.sha256(
        model.model_dump_json(exclude={"resolved_at", "credential_ref"}).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scorer_digest(root: Path) -> str:
    return hashlib.sha256(
        b"".join(
            (root / name).read_bytes()
            for name in (
                "src/agent_core/evals/memory_reconsolidation.py",
                "src/agent_core/evals/memory_benchmark.py",
            )
        )
    ).hexdigest()


def bind_evidence(
    root: Path,
    path: Path,
    model: ResolvedModel,
    *,
    release_sha: str,
    formation_policy: str,
    upstream_build_ref: str | None,
    upstream_corpus_sha256: str | None,
    residency_provider: str | None,
) -> Callable[[], bool]:
    raw = path.read_bytes()
    evidence = ReconsolidationEvidence.model_validate_json(raw)
    if (
        evidence.build_ref != release_sha
        or evidence.implementation_sha256 != implementation_digest(root)
        or evidence.corpus_sha256
        != file_digest(root / "evals/capability/memory-reconsolidation.v1.json")
        or evidence.recall_corpus_sha256
        != file_digest(root / "evals/capability/memory-benchmark.v1.json")
        or evidence.scorer_sha256 != scorer_digest(root)
        or (evidence.provider, evidence.model) != (model.provider, model.model)
        or evidence.model_configuration_sha256 != model_digest(model)
        or evidence.privacy_sha256 != privacy_digest(residency_provider)
        or residency_provider != model.provider
        or evidence.formation_policy != formation_policy
        or evidence.retrieval_policy != RETRIEVAL_POLICY_VERSION
        or evidence.upstream_build_ref != upstream_build_ref
        or evidence.upstream_corpus_sha256 != upstream_corpus_sha256
    ):
        raise ValueError("reconsolidation evidence does not match the active dependency tuple")
    expected = hashlib.sha256(raw).digest()

    def admitted() -> bool:
        try:
            return hashlib.sha256(path.read_bytes()).digest() == expected
        except OSError:
            return False

    return admitted
