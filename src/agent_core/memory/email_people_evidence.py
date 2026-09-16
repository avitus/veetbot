"""Fresh model, code and schema evidence is required before email People activation."""

import hashlib
import json
from pathlib import Path

from agent_core.domain.email_people import email_people_schema
from agent_core.domain.email_people_evidence import EmailPeopleEvidence
from agent_core.memory.people_evidence import implementation_digest, reviewed_corpora, schema_digest

EMAIL_CORPUS = Path("evals/capability/email-people.v1.json")
EMAIL_HOLDOUT = Path("evals/capability/email-people.v1-holdout.json")


def email_evidence_policy(path: Path) -> str | None:
    from agent_core.config import ConfigurationError

    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("evidence must be an object")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError("email evaluation evidence could not be read") from exc
    policy = value.get("policy_version")
    return policy if isinstance(policy, str) else None


def email_people_schema_digest() -> str:
    return hashlib.sha256(
        json.dumps(email_people_schema(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def email_people_implementation_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(implementation_digest().encode())
    for relative in (
        "domain/email_people.py",
        "domain/email_people_evidence.py",
        "domain/email.py",
        "domain/email_semantics.py",
        "memory/email_people.py",
        "memory/email_people_evidence.py",
        "memory/people_correspondence.py",
        "memory/email_semantics.py",
        "runtime/email_tasks.py",
        "runtime/email_assessment.py",
        "evals/email_people.py",
        "evals/email_people_execution.py",
        "evals/email_people_ordinary.py",
        "evals/email_people_release.py",
        "evals/email_quality.py",
        "runtime/email_state.py",
        "adapters/persistence/email.py",
        "application/email.py",
    ):
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def reviewed_email_corpora() -> bool:
    from agent_core.config import REPOSITORY_ROOT

    try:
        return all(
            isinstance(value, dict) and value.get("review_status") == "reviewed"
            for value in (
                json.loads((REPOSITORY_ROOT / relative).read_bytes())
                for relative in (EMAIL_CORPUS, EMAIL_HOLDOUT)
            )
        )
    except (OSError, UnicodeError, ValueError):
        return False


def matches_email_people(evidence: EmailPeopleEvidence, *, provider: str, model: str) -> bool:
    from agent_core.memory.email_semantics import semantic_implementation_sha256

    return (
        reviewed_corpora()
        and reviewed_email_corpora()
        and evidence.provider == provider
        and evidence.model == model
        and evidence.schema_sha256 == email_people_schema_digest()
        and evidence.implementation_sha256 == email_people_implementation_digest()
        and evidence.ordinary_email.baseline_implementation_sha256
        == semantic_implementation_sha256()
        and evidence.people.schema_sha256 == schema_digest()
        and evidence.people.implementation_sha256 == implementation_digest()
    )


def load_email_people_evidence(
    path: Path,
    *,
    provider: str,
    model: str,
    build_ref: str | None,
    model_policy: str,
    policy_profile: str,
    policy_version: str,
) -> EmailPeopleEvidence:
    from agent_core.config import (
        MEMORY_DISTILLATION_CORPUS_PATH,
        MEMORY_DISTILLATION_HOLDOUT_PATH,
        ConfigurationError,
        shipped_corpus_sha256,
    )
    from agent_core.memory.people_evidence import PEOPLE_CORPUS_PATH, PEOPLE_HOLDOUT_PATH

    try:
        evidence = EmailPeopleEvidence.model_validate_json(path.read_text())
        if (
            not matches_email_people(evidence, provider=provider, model=model)
            or evidence.build_ref != build_ref
            or evidence.people.model_policy != model_policy
            or evidence.people.policy_profile != policy_profile
            or evidence.people.policy_version != policy_version
            or evidence.people.reasoning_configuration != "provider-default"
            or evidence.people.corpus_sha256 != shipped_corpus_sha256(PEOPLE_CORPUS_PATH)
            or evidence.people.holdout_sha256 != shipped_corpus_sha256(PEOPLE_HOLDOUT_PATH)
            or evidence.people.ordinary_corpus_sha256
            != shipped_corpus_sha256(MEMORY_DISTILLATION_CORPUS_PATH)
            or evidence.people.ordinary_holdout_sha256
            != shipped_corpus_sha256(MEMORY_DISTILLATION_HOLDOUT_PATH)
            or evidence.corpus_sha256 != shipped_corpus_sha256(EMAIL_CORPUS)
            or evidence.holdout_sha256 != shipped_corpus_sha256(EMAIL_HOLDOUT)
        ):
            raise ValueError("email People evidence differs from this release")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError("email People evaluation evidence did not pass") from exc
    return evidence
