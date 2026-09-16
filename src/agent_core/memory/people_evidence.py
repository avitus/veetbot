"""Bind People activation to the exact code, schema, model and frozen corpora."""

import hashlib
import json
from pathlib import Path

from agent_core.domain.messages import ResolvedModel
from agent_core.domain.people_evidence import PeopleFormationEvidence
from agent_core.memory.distillation import PeopleAssistedCandidateExtractor

PEOPLE_CORPUS_PATH = Path("evals/capability/people.v1.json")
PEOPLE_HOLDOUT_PATH = Path("evals/capability/people.v1-holdout.json")


def reviewed_corpora() -> bool:
    """Never activate against draft labels, even with a matching artifact hash."""
    from agent_core.config import REPOSITORY_ROOT

    try:
        return all(
            isinstance(value, dict) and value.get("review_status") == "reviewed"
            for value in (
                json.loads((REPOSITORY_ROOT / relative).read_bytes())
                for relative in (PEOPLE_CORPUS_PATH, PEOPLE_HOLDOUT_PATH)
            )
        )
    except (OSError, UnicodeError, ValueError):
        return False


def schema_digest() -> str:
    return hashlib.sha256(
        json.dumps(
            PeopleAssistedCandidateExtractor._distillation_schema(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def implementation_digest() -> str:
    """Read current code so resumable imports cannot reuse a stale process-cached binding."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    paths = {
        "memory/distillation.py",
        "memory/equivalence.py",
        "evals/memory_distillation.py",
        "memory/formation.py",
        "memory/retrieval.py",
        "memory/communication_sources.py",
        "context/builder.py",
        "domain/memory.py",
        "domain/erasure.py",
        "adapters/memory/in_memory.py",
        "adapters/persistence/memory.py",
        "adapters/persistence/memory_repositories.py",
        "adapters/persistence/repositories.py",
        "adapters/persistence/sqlalchemy_models.py",
        "bootstrap.py",
    }
    paths.update(str(path.relative_to(root)) for path in root.rglob("people*.py"))
    for relative in sorted(paths):
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def people_evidence_matches(
    evidence: PeopleFormationEvidence,
    model: ResolvedModel,
    policy_profile: str,
    policy_version: str,
    *,
    corpus_sha256: str,
    holdout_sha256: str,
    ordinary_corpus_sha256: str,
    ordinary_holdout_sha256: str,
) -> bool:
    return (
        reviewed_corpora()
        and evidence.provider == model.provider
        and evidence.model == model.model
        and evidence.reasoning_configuration == "provider-default"
        and evidence.model_policy == model.policy_name
        and evidence.policy_profile == policy_profile
        and evidence.policy_version == policy_version
        and evidence.schema_sha256 == schema_digest()
        and evidence.implementation_sha256 == implementation_digest()
        and evidence.corpus_sha256 == corpus_sha256
        and evidence.holdout_sha256 == holdout_sha256
        and evidence.ordinary_corpus_sha256 == ordinary_corpus_sha256
        and evidence.ordinary_holdout_sha256 == ordinary_holdout_sha256
    )
