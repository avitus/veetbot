"""Paired live evaluation for provider-assisted memory formation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.config import (
    AuthMode,
    DeploymentMode,
    MemoryProviderExtractionMode,
    SandboxMechanism,
    Settings,
    load_settings,
)
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import ProviderExtractionEvaluationEvidence
from agent_core.memory.provider_extraction import (
    PROVIDER_EXTRACTOR_VERSION,
    PROVIDER_FORMATION_POLICY_VERSION,
)
from agent_core.policy.scopes import PLATFORM_SCOPES

CORPUS_PATH = Path("evals/capability/memory-formation.v1.json")
EVALUATION_SCOPE = "memory-formation-evaluation"


class EvaluationBelief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_type: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class ExpectedBelief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_type: str = Field(min_length=1)
    subjects: list[str] = Field(min_length=1)
    statements: list[str] = Field(min_length=1)


class MemoryFormationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9]+(?:-[a-z0-9]+)*-\d{3}$")
    episodes: list[str] = Field(min_length=1)
    expected: list[ExpectedBelief]
    must_remain_empty: bool = False

    @model_validator(mode="after")
    def labeled_outcome(self) -> MemoryFormationCase:
        if not self.expected and not self.must_remain_empty:
            raise ValueError("empty expected beliefs require must_remain_empty")
        if self.expected and self.must_remain_empty:
            raise ValueError("must_remain_empty cases cannot expect beliefs")
        return self


class MemoryFormationCorpus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    cases: list[MemoryFormationCase] = Field(min_length=20)

    @model_validator(mode="after")
    def unique_case_ids(self) -> MemoryFormationCorpus:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("memory-formation case ids must be unique")
        return self


class FormationScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    supported_candidates: int = Field(ge=0)
    fabricated_candidates: int = Field(ge=0)
    policy_failures: int = Field(ge=0)


def score_case(case: MemoryFormationCase, beliefs: list[EvaluationBelief]) -> FormationScore:
    occupied: set[int] = set()
    supported = 0
    for expected in case.expected:
        expected_subjects = {_normalized(value) for value in expected.subjects}
        expected_statements = {_normalized(value) for value in expected.statements}
        for index, belief in enumerate(beliefs):
            if index in occupied:
                continue
            if (
                belief.belief_type.casefold() == expected.belief_type.casefold()
                and _normalized(belief.subject) in expected_subjects
                and _normalized(belief.statement) in expected_statements
            ):
                occupied.add(index)
                supported += 1
                break
    fabricated = len(beliefs) - len(occupied)
    return FormationScore(
        supported_candidates=supported,
        fabricated_candidates=fabricated,
        policy_failures=fabricated if case.must_remain_empty else 0,
    )


def load_corpus(repository_root: Path) -> tuple[MemoryFormationCorpus, str]:
    root = repository_root.resolve()
    path = (root / CORPUS_PATH).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("memory-formation corpus path escapes the repository") from exc
    raw = path.read_bytes()
    corpus = MemoryFormationCorpus.model_validate_json(raw)
    return corpus, hashlib.sha256(raw).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().strip().rstrip(".!?").split())


def _evaluation_settings(settings: Settings, artifact_root: Path) -> Settings:
    if settings.deployment_mode is DeploymentMode.PRODUCTION:
        raise ValueError("provider memory extraction evaluation is unavailable in production")
    return replace(
        settings,
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        memory_provider_extraction_mode=MemoryProviderExtractionMode.OFF,
        memory_provider_extraction_evidence=None,
        artifact_root=artifact_root,
    )


async def _evaluate_case(
    settings: Settings,
    case: MemoryFormationCase,
    *,
    model_policy: str,
    policy_profile: str,
    provider_assisted: bool,
) -> tuple[FormationScore, tuple[str, str, str] | None, datetime]:
    principal = Principal(
        tenant_id="evaluation",
        principal_id="memory-formation-evaluator",
        roles={"evaluator"},
        scopes=set(PLATFORM_SCOPES),
    )
    bootstrap: Any = __import__("agent_core.bootstrap", fromlist=["build"])
    async with bootstrap.build(
        settings=settings,
        storage="memory",
        principal=principal,
        policy_profile=policy_profile,
        model_policy=model_policy,
        memory_provider_evaluation_mode=provider_assisted,
    ) as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            for episode in case.episodes:
                await uow.events.append(
                    NewEvent(
                        session_id=session_id,
                        run_id=None,
                        event_type="user.message.created",
                        actor_type="principal",
                        actor_id=principal.principal_id,
                        payload={"content": episode},
                    )
                )
        result = await composition.memory.run(
            trigger="evaluation",
            scope=EVALUATION_SCOPE,
            session_id=session_id,
        )
        identity = None
        if provider_assisted:
            async with composition.uow_factory() as uow:
                selections = await uow.process_events.list("memory.provider_extraction.selection")
            if len(selections) != 1:
                raise ValueError("provider evaluation did not record one model selection")
            payload = selections[0].payload
            provider = payload.get("provider")
            model = payload.get("model")
            policy_version = payload.get("policy_version")
            if (
                not isinstance(provider, str)
                or not isinstance(model, str)
                or not isinstance(policy_version, str)
            ):
                raise ValueError("provider evaluation recorded an incomplete model selection")
            identity = (provider, model, policy_version)
        evaluated_at = composition.clock.now()
    beliefs = [
        EvaluationBelief(
            belief_type=belief.belief_type.value,
            subject=belief.subject,
            statement=belief.statement,
        )
        for belief in result.beliefs
    ]
    return score_case(case, beliefs), identity, evaluated_at


def _write_evidence(output: Path, evidence: ProviderExtractionEvaluationEvidence) -> None:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(evidence.model_dump_json(indent=2))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise ValueError(
                f"refusing to overwrite existing evaluation evidence: {output}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


async def run_live_evaluation(
    repository_root: Path,
    *,
    model_policy: str,
    policy_profile: str,
    build_ref: str,
    output: Path,
) -> ProviderExtractionEvaluationEvidence | None:
    """Run paired cases and atomically publish only evidence that passes the gate."""

    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        return None
    if not model_policy.strip() or not policy_profile.strip() or not build_ref.strip():
        raise ValueError("model policy, policy profile, and build ref must be non-empty")
    if output.resolve().exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {output.resolve()}")

    corpus, corpus_sha256 = load_corpus(repository_root)
    base_settings = load_settings()
    deterministic_supported = 0
    deterministic_policy_failures = 0
    provider_supported = 0
    provider_fabricated = 0
    provider_policy_failures = 0
    identities: set[tuple[str, str, str]] = set()
    fabricated_case_ids: list[str] = []
    policy_regression_case_ids: list[str] = []
    missing_expected_case_ids: list[str] = []
    evaluated_at: datetime | None = None
    with tempfile.TemporaryDirectory(prefix="agent-memory-eval-") as temporary_root:
        settings = _evaluation_settings(base_settings, Path(temporary_root) / "artifacts")
        for case in corpus.cases:
            deterministic, _, _ = await _evaluate_case(
                settings,
                case,
                model_policy=model_policy,
                policy_profile=policy_profile,
                provider_assisted=False,
            )
            provider, identity, evaluated_at = await _evaluate_case(
                settings,
                case,
                model_policy=model_policy,
                policy_profile=policy_profile,
                provider_assisted=True,
            )
            if identity is None:
                raise ValueError("provider evaluation did not resolve a model")
            identities.add(identity)
            deterministic_supported += deterministic.supported_candidates
            deterministic_policy_failures += deterministic.policy_failures
            provider_supported += provider.supported_candidates
            provider_fabricated += provider.fabricated_candidates
            provider_policy_failures += provider.policy_failures
            if provider.fabricated_candidates:
                fabricated_case_ids.append(case.id)
            if provider.policy_failures > deterministic.policy_failures:
                policy_regression_case_ids.append(case.id)
            if provider.supported_candidates < len(case.expected):
                missing_expected_case_ids.append(case.id)

    if len(identities) != 1:
        raise ValueError("provider evaluation resolved more than one provider/model tuple")
    if evaluated_at is None:
        raise ValueError("provider evaluation corpus is empty")
    provider_name, model_name, policy_version = identities.pop()
    failures: list[str] = []
    if provider_supported <= deterministic_supported:
        failures.append(
            "no formation lift "
            f"(deterministic={deterministic_supported}, provider={provider_supported}, "
            f"missing_cases={','.join(missing_expected_case_ids) or 'none'})"
        )
    if provider_fabricated:
        failures.append(
            f"fabricated candidates={provider_fabricated} (cases={','.join(fabricated_case_ids)})"
        )
    if provider_policy_failures > deterministic_policy_failures:
        failures.append(
            "policy regression "
            f"(deterministic={deterministic_policy_failures}, "
            f"provider={provider_policy_failures}, "
            f"cases={','.join(policy_regression_case_ids)})"
        )
    if failures:
        raise ValueError("provider memory formation did not pass: " + "; ".join(failures))
    evidence = ProviderExtractionEvaluationEvidence(
        extractor_version=PROVIDER_EXTRACTOR_VERSION,
        formation_policy_version=PROVIDER_FORMATION_POLICY_VERSION,
        model_policy=model_policy,
        provider=provider_name,
        model=model_name,
        policy_profile=policy_profile,
        policy_version=policy_version,
        build_ref=build_ref,
        corpus_sha256=corpus_sha256,
        sample_count=len(corpus.cases),
        deterministic_supported_candidates=deterministic_supported,
        provider_supported_candidates=provider_supported,
        fabricated_candidates=provider_fabricated,
        deterministic_policy_failures=deterministic_policy_failures,
        provider_policy_failures=provider_policy_failures,
        evaluated_at=evaluated_at,
    )
    _write_evidence(output, evidence)
    return evidence
