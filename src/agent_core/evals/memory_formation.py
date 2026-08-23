"""Paired live evaluation for provider-assisted memory formation."""

from __future__ import annotations

import hashlib
import importlib
import os
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Literal

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
from agent_core.domain.memory import (
    ProviderExtractionEvaluationEvidence,
    minimum_supported_case_count,
)
from agent_core.memory.provider_extraction import (
    PROVIDER_EXTRACTOR_VERSION,
    PROVIDER_FORMATION_POLICY_VERSION,
)
from agent_core.policy.scopes import PLATFORM_SCOPES

CORPUS_PATH = Path("evals/capability/memory-formation.v2.json")
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


class ProviderExtractionDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: str = Field(min_length=1)
    error_class: str | None = None
    candidate_count: int = Field(ge=0)
    grounded_candidate_count: int = Field(ge=0)


class FormationArmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score: FormationScore
    beliefs: list[EvaluationBelief]
    candidates_proposed: int = Field(ge=0)
    committed: int = Field(ge=0)
    reinforced: int = Field(ge=0)
    superseded: int = Field(ge=0)
    rejected: int = Field(ge=0)
    identity: tuple[str, str, str] | None = None
    extraction: ProviderExtractionDiagnostics | None = None
    evaluated_at: datetime


class MemoryFormationCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    expected_count: int = Field(ge=0)
    must_remain_empty: bool
    deterministic: FormationArmResult
    provider: FormationArmResult
    shared_beliefs: list[EvaluationBelief]
    provider_added_beliefs: list[EvaluationBelief]


class MemoryFormationEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    failure_summary: str | None
    cases: list[MemoryFormationCaseResult]
    evidence: ProviderExtractionEvaluationEvidence | None = None

    @model_validator(mode="after")
    def outcome_matches_evidence(self) -> MemoryFormationEvaluationResult:
        if self.passed != (self.evidence is not None):
            raise ValueError("only a passing memory evaluation may carry activation evidence")
        if self.passed and self.failure_summary is not None:
            raise ValueError("passing memory evaluation must not carry a failure summary")
        if not self.passed and not self.failure_summary:
            raise ValueError("failed memory evaluation requires a failure summary")
        return self


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


def _belief_key(belief: EvaluationBelief) -> tuple[str, str, str]:
    return (
        belief.belief_type.casefold(),
        _normalized(belief.subject),
        _normalized(belief.statement),
    )


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
) -> FormationArmResult:
    principal = Principal(
        tenant_id="evaluation",
        principal_id="memory-formation-evaluator",
        roles={"evaluator"},
        scopes=set(PLATFORM_SCOPES),
    )
    # Defer the composition-root import to avoid an evaluation/bootstrap cycle.
    bootstrap = importlib.import_module("agent_core.bootstrap")
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
        extraction = None
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
            async with composition.uow_factory() as uow:
                completed = await uow.process_events.list("memory.provider_extraction.completed")
                failed = await uow.process_events.list("memory.provider_extraction.failed")
            audits = [*completed, *failed]
            if len(audits) != 1:
                raise ValueError("provider evaluation did not record one extraction audit")
            audit = audits[0].payload
            outcome = audit.get("outcome")
            error_class = audit.get("error_class")
            candidate_count = audit.get("candidate_count")
            grounded_candidate_count = audit.get("grounded_candidate_count")
            if (
                not isinstance(outcome, str)
                or (error_class is not None and not isinstance(error_class, str))
                or not isinstance(candidate_count, int)
                or not isinstance(grounded_candidate_count, int)
            ):
                raise ValueError("provider evaluation recorded incomplete extraction diagnostics")
            extraction = ProviderExtractionDiagnostics(
                outcome=outcome,
                error_class=error_class,
                candidate_count=candidate_count,
                grounded_candidate_count=grounded_candidate_count,
            )
        evaluated_at = composition.clock.now()
    beliefs = [
        EvaluationBelief(
            belief_type=belief.belief_type.value,
            subject=belief.subject,
            statement=belief.statement,
        )
        for belief in result.beliefs
    ]
    return FormationArmResult(
        score=score_case(case, beliefs),
        beliefs=beliefs,
        candidates_proposed=result.run.candidates_proposed,
        committed=result.run.committed,
        reinforced=result.run.reinforced,
        superseded=result.run.superseded,
        rejected=result.run.rejected,
        identity=identity,
        extraction=extraction,
        evaluated_at=evaluated_at,
    )


def _write_evidence(output: Path, evidence: BaseModel) -> None:
    """Publish one evaluation document, never overwriting an existing file."""

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
) -> MemoryFormationEvaluationResult | None:
    """Run paired cases and atomically publish only evidence that passes the gate."""

    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        return None
    if not model_policy.strip() or not policy_profile.strip() or not build_ref.strip():
        raise ValueError("model policy, policy profile, and build ref must be non-empty")
    if output.resolve().exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {output.resolve()}")

    corpus, corpus_sha256 = load_corpus(repository_root)
    base_settings = load_settings()
    positive_case_count = sum(bool(case.expected) for case in corpus.cases)
    supported_case_floor = minimum_supported_case_count(positive_case_count)
    deterministic_supported_cases = 0
    deterministic_supported = 0
    deterministic_fabricated = 0
    deterministic_policy_failures = 0
    provider_supported_cases = 0
    provider_supported = 0
    provider_fabricated = 0
    provider_policy_failures = 0
    identities: set[tuple[str, str, str]] = set()
    deterministic_fabricated_case_ids: list[str] = []
    provider_fabricated_case_ids: list[str] = []
    policy_regression_case_ids: list[str] = []
    missing_expected_case_ids: list[str] = []
    case_results: list[MemoryFormationCaseResult] = []
    evaluated_at: datetime | None = None
    with tempfile.TemporaryDirectory(prefix="agent-memory-eval-") as temporary_root:
        settings = _evaluation_settings(base_settings, Path(temporary_root) / "artifacts")
        for case in corpus.cases:
            deterministic = await _evaluate_case(
                settings,
                case,
                model_policy=model_policy,
                policy_profile=policy_profile,
                provider_assisted=False,
            )
            provider = await _evaluate_case(
                settings,
                case,
                model_policy=model_policy,
                policy_profile=policy_profile,
                provider_assisted=True,
            )
            if provider.identity is None:
                raise ValueError("provider evaluation did not resolve a model")
            identities.add(provider.identity)
            evaluated_at = provider.evaluated_at
            deterministic_supported += deterministic.score.supported_candidates
            deterministic_fabricated += deterministic.score.fabricated_candidates
            deterministic_policy_failures += deterministic.score.policy_failures
            provider_supported += provider.score.supported_candidates
            provider_fabricated += provider.score.fabricated_candidates
            provider_policy_failures += provider.score.policy_failures
            if case.expected and deterministic.score.supported_candidates == len(case.expected):
                deterministic_supported_cases += 1
            if case.expected and provider.score.supported_candidates == len(case.expected):
                provider_supported_cases += 1
            if deterministic.score.fabricated_candidates:
                deterministic_fabricated_case_ids.append(case.id)
            if provider.score.fabricated_candidates:
                provider_fabricated_case_ids.append(case.id)
            if provider.score.policy_failures > deterministic.score.policy_failures:
                policy_regression_case_ids.append(case.id)
            if provider.score.supported_candidates < len(case.expected):
                missing_expected_case_ids.append(case.id)
            deterministic_keys = {_belief_key(belief) for belief in deterministic.beliefs}
            shared = [
                belief for belief in provider.beliefs if _belief_key(belief) in deterministic_keys
            ]
            added = [
                belief
                for belief in provider.beliefs
                if _belief_key(belief) not in deterministic_keys
            ]
            case_results.append(
                MemoryFormationCaseResult(
                    case_id=case.id,
                    expected_count=len(case.expected),
                    must_remain_empty=case.must_remain_empty,
                    deterministic=deterministic,
                    provider=provider,
                    shared_beliefs=shared,
                    provider_added_beliefs=added,
                )
            )

    if len(identities) != 1:
        raise ValueError("provider evaluation resolved more than one provider/model tuple")
    if evaluated_at is None:
        raise ValueError("provider evaluation corpus is empty")
    provider_name, model_name, policy_version = identities.pop()
    failures: list[str] = []
    if provider_supported_cases < supported_case_floor:
        failures.append(
            f"positive coverage {provider_supported_cases}/{positive_case_count} "
            f"(minimum={supported_case_floor}, "
            f"missing_cases={','.join(missing_expected_case_ids) or 'none'})"
        )
    if provider_supported <= deterministic_supported:
        failures.append(
            "no formation lift "
            f"(deterministic={deterministic_supported}, provider={provider_supported}, "
            f"missing_cases={','.join(missing_expected_case_ids) or 'none'})"
        )
    if deterministic_fabricated or provider_fabricated:
        failures.append(
            "fabricated candidates "
            f"(deterministic={deterministic_fabricated}, "
            f"provider={provider_fabricated}, "
            "deterministic_cases="
            f"{','.join(deterministic_fabricated_case_ids) or 'none'}, "
            f"provider_cases={','.join(provider_fabricated_case_ids) or 'none'})"
        )
    if provider_policy_failures > deterministic_policy_failures:
        failures.append(
            "policy regression "
            f"(deterministic={deterministic_policy_failures}, "
            f"provider={provider_policy_failures}, "
            f"cases={','.join(policy_regression_case_ids)})"
        )
    if failures:
        return MemoryFormationEvaluationResult(
            passed=False,
            failure_summary="; ".join(failures),
            cases=case_results,
        )
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
        positive_case_count=positive_case_count,
        minimum_supported_case_count=supported_case_floor,
        deterministic_supported_case_count=deterministic_supported_cases,
        provider_supported_case_count=provider_supported_cases,
        deterministic_supported_candidates=deterministic_supported,
        provider_supported_candidates=provider_supported,
        deterministic_fabricated_candidates=deterministic_fabricated,
        provider_fabricated_candidates=provider_fabricated,
        deterministic_policy_failures=deterministic_policy_failures,
        provider_policy_failures=provider_policy_failures,
        evaluated_at=evaluated_at,
    )
    _write_evidence(output, evidence)
    return MemoryFormationEvaluationResult(
        passed=True,
        failure_summary=None,
        cases=case_results,
        evidence=evidence,
    )
