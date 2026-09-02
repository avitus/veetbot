"""Offline corpus and publication types for formation@9 comparison."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
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
    MemoryClaimKind,
    MemoryDerivation,
    MemoryDistillationEvidence,
    MemoryLongevity,
)
from agent_core.policy.scopes import PLATFORM_SCOPES

CORPUS_PATH = Path("evals/capability/memory-formation.v3.json")


def _normalized(value: str) -> str:
    return " ".join(value.casefold().strip().rstrip(".!?").split())


_SEMANTIC_TOKEN = re.compile(r"[a-z0-9]+(?:['-][a-z0-9]+)*", re.IGNORECASE)
_SEMANTIC_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "enough",
        "goal",
        "is",
        "my",
        "our",
        "the",
        "their",
        "to",
        "upcoming",
        "user",
        "users",
        "want",
        "wants",
    }
)


def _semantic_terms(value: str) -> set[str]:
    terms = set(_SEMANTIC_TOKEN.findall(value.casefold())) - _SEMANTIC_STOPWORDS
    return {term[:-1] if len(term) > 4 and term.endswith("s") else term for term in terms}


def _semantically_equivalent(left: str, right: str) -> bool:
    if _normalized(left) == _normalized(right):
        return True
    left_terms = _semantic_terms(left)
    right_terms = _semantic_terms(right)
    smaller = min(len(left_terms), len(right_terms))
    return bool(smaller) and len(left_terms & right_terms) / smaller >= 0.8


class DistillationEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: Literal["user", "assistant", "tool", "model"]
    text: str = Field(min_length=1, max_length=8192)


class ExpectedDistilledCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_kind: MemoryClaimKind
    derivation: MemoryDerivation
    longevity: MemoryLongevity
    subjects: list[str] = Field(min_length=1)
    statements: list[str] = Field(min_length=1)
    evidence_text: list[str] = Field(min_length=1)


class MemoryDistillationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9]+(?:-[a-z0-9]+)*-\d{3}$")
    label: Literal["must_form", "reasonable_to_form", "must_not_form"]
    scenario: Literal[
        "personal-agent",
        "compound",
        "misleading-professional-cue",
        "evidence-promotion",
        "lifecycle-retirement",
        "self-citation",
        "rich-conversation",
        "ordinary",
        "trust-boundary",
    ]
    events: list[DistillationEvent] = Field(min_length=1)
    expected: list[ExpectedDistilledCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def label_matches_candidates(self) -> MemoryDistillationCase:
        if self.label == "must_not_form" and self.expected:
            raise ValueError("must-not-form cases cannot expect candidates")
        if self.label != "must_not_form" and not self.expected:
            raise ValueError("positive cases require expected candidates")
        user_text = "\n".join(event.text for event in self.events if event.actor == "user")
        if any(
            evidence not in user_text
            for candidate in self.expected
            for evidence in candidate.evidence_text
        ):
            raise ValueError("expected evidence text is not an exact user substring")
        return self


class MemoryDistillationCorpus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[3] = 3
    cases: list[MemoryDistillationCase] = Field(min_length=60)

    @model_validator(mode="after")
    def coverage_is_declared(self) -> MemoryDistillationCorpus:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("memory-distillation case ids must be unique")
        positives = [case for case in self.cases if case.label != "must_not_form"]
        if len(positives) * 10 < len(self.cases) * 7:
            raise ValueError("memory-distillation corpus must be seventy percent positive")
        covered = {expected.claim_kind for case in positives for expected in case.expected}
        if covered != set(MemoryClaimKind):
            raise ValueError("memory-distillation corpus must cover every claim kind")
        if {case.label for case in self.cases} != {
            "must_form",
            "reasonable_to_form",
            "must_not_form",
        }:
            raise ValueError("memory-distillation corpus must cover every label")
        core = {
            "personal-agent",
            "compound",
            "misleading-professional-cue",
            "evidence-promotion",
            "lifecycle-retirement",
            "self-citation",
            "rich-conversation",
        }
        if not core <= {case.scenario for case in self.cases}:
            raise ValueError("memory-distillation corpus omits a core scenario")
        must_form_derivations = {
            expected.derivation
            for case in self.cases
            if case.label == "must_form"
            for expected in case.expected
        }
        if must_form_derivations != {
            MemoryDerivation.DIRECT,
            MemoryDerivation.HYPOTHESIS,
        }:
            raise ValueError("must-form cases must cover direct and hypothesis claims")
        return self


class DistillationEvaluationBelief(BaseModel):
    """The closed, user-safe belief projection scored by the offline driver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_kind: MemoryClaimKind
    derivation: MemoryDerivation
    longevity: MemoryLongevity
    subject: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class DistillationCaseScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: int = Field(ge=0)
    matched: int = Field(ge=0)
    predicted: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    direct_must_form_expected: int = Field(ge=0)
    direct_must_form_matched: int = Field(ge=0)
    hypothesis_must_form_expected: int = Field(ge=0)
    hypothesis_must_form_matched: int = Field(ge=0)
    boundary_failures: int = Field(ge=0)


class DistillationArmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["formation@7", "formation@8", "formation@9"]
    beliefs: list[DistillationEvaluationBelief]
    score: DistillationCaseScore
    identity: tuple[str, str, str] | None = None
    provider_calls: int = Field(ge=0)
    evaluated_at: datetime


class DistillationCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    label: Literal["must_form", "reasonable_to_form", "must_not_form"]
    scenario: str
    arms: dict[
        Literal["formation@7", "formation@8", "formation@9"],
        DistillationArmResult,
    ]


class DistillationPolicyMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["formation@7", "formation@8", "formation@9"]
    expected: int = Field(ge=0)
    matched: int = Field(ge=0)
    predicted: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    useful_recall: float = Field(ge=0, le=1)
    direct_must_form_recall: float = Field(ge=0, le=1)
    hypothesis_must_form_recall: float = Field(ge=0, le=1)
    benign_precision: float = Field(ge=0, le=1)
    boundary_failures: int = Field(ge=0)
    provider_calls: int = Field(ge=0)


class MemoryDistillationEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    failure_summary: str | None
    cases: list[DistillationCaseResult]
    policies: dict[
        Literal["formation@7", "formation@8", "formation@9"],
        DistillationPolicyMetrics,
    ]
    evidence: MemoryDistillationEvidence | None = None

    @model_validator(mode="after")
    def outcome_matches_evidence(self) -> MemoryDistillationEvaluationResult:
        if self.passed != (self.evidence is not None):
            raise ValueError("only passing distillation evaluation may carry evidence")
        if self.passed and self.failure_summary is not None:
            raise ValueError("passing distillation evaluation cannot carry a failure")
        if not self.passed and not self.failure_summary:
            raise ValueError("failed distillation evaluation requires a failure summary")
        return self


def score_distillation_case(
    case: MemoryDistillationCase,
    beliefs: list[DistillationEvaluationBelief],
) -> DistillationCaseScore:
    """Match each expected semantic claim once and count every extra prediction."""

    occupied: set[int] = set()
    matched = 0
    direct_expected = 0
    direct_matched = 0
    hypothesis_expected = 0
    hypothesis_matched = 0
    for expected in case.expected:
        if case.label == "must_form":
            if expected.derivation is MemoryDerivation.DIRECT:
                direct_expected += 1
            else:
                hypothesis_expected += 1
        subjects = {_normalized(value) for value in expected.subjects}
        statements = {_normalized(value) for value in expected.statements}
        found = False
        for index, belief in enumerate(beliefs):
            if index in occupied:
                continue
            if (
                belief.claim_kind is expected.claim_kind
                and belief.derivation is expected.derivation
                and belief.longevity is expected.longevity
                and (
                    (
                        _normalized(belief.subject) in subjects
                        and _normalized(belief.statement) in statements
                    )
                    or any(
                        _semantically_equivalent(belief.statement, statement)
                        for statement in expected.statements
                    )
                )
            ):
                occupied.add(index)
                matched += 1
                found = True
                break
        if found and case.label == "must_form":
            if expected.derivation is MemoryDerivation.DIRECT:
                direct_matched += 1
            else:
                hypothesis_matched += 1
    false_positives = len(beliefs) - len(occupied)
    return DistillationCaseScore(
        expected=len(case.expected),
        matched=matched,
        predicted=len(beliefs),
        false_positives=false_positives,
        direct_must_form_expected=direct_expected,
        direct_must_form_matched=direct_matched,
        hypothesis_must_form_expected=hypothesis_expected,
        hypothesis_must_form_matched=hypothesis_matched,
        boundary_failures=false_positives if case.scenario == "trust-boundary" else 0,
    )


def _policy_metrics(
    policy_version: Literal["formation@7", "formation@8", "formation@9"],
    results: list[DistillationCaseResult],
) -> DistillationPolicyMetrics:
    scores = [result.arms[policy_version].score for result in results]
    expected = sum(score.expected for score in scores)
    matched = sum(score.matched for score in scores)
    predicted = sum(score.predicted for score in scores)
    direct_expected = sum(score.direct_must_form_expected for score in scores)
    direct_matched = sum(score.direct_must_form_matched for score in scores)
    hypothesis_expected = sum(score.hypothesis_must_form_expected for score in scores)
    hypothesis_matched = sum(score.hypothesis_must_form_matched for score in scores)
    return DistillationPolicyMetrics(
        policy_version=policy_version,
        expected=expected,
        matched=matched,
        predicted=predicted,
        false_positives=sum(score.false_positives for score in scores),
        useful_recall=matched / expected if expected else 1,
        direct_must_form_recall=(direct_matched / direct_expected if direct_expected else 1),
        hypothesis_must_form_recall=(
            hypothesis_matched / hypothesis_expected if hypothesis_expected else 1
        ),
        benign_precision=matched / predicted if predicted else 1,
        boundary_failures=sum(score.boundary_failures for score in scores),
        provider_calls=sum(result.arms[policy_version].provider_calls for result in results),
    )


def _evaluation_settings(settings: Settings, artifact_root: Path) -> Settings:
    if settings.deployment_mode is DeploymentMode.PRODUCTION:
        raise ValueError("memory distillation evaluation is unavailable in production")
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
    case: MemoryDistillationCase,
    *,
    model_policy: str,
    policy_profile: str,
    policy_version: Literal["formation@7", "formation@8", "formation@9"],
) -> DistillationArmResult:
    principal = Principal(
        tenant_id="evaluation",
        principal_id="memory-distillation-evaluator",
        roles={"evaluator"},
        scopes=set(PLATFORM_SCOPES),
    )
    bootstrap = importlib.import_module("agent_core.bootstrap")
    async with bootstrap.build(
        settings=settings,
        storage="memory",
        principal=principal,
        policy_profile=policy_profile,
        model_policy=model_policy,
        memory_provider_evaluation_mode=policy_version == "formation@8",
        memory_distillation_evaluation_mode=policy_version == "formation@9",
    ) as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            for event in case.events:
                is_user = event.actor == "user"
                await uow.events.append(
                    NewEvent(
                        session_id=session_id,
                        run_id=None,
                        event_type=(
                            "user.message.created"
                            if is_user
                            else f"evaluation.{event.actor}.created"
                        ),
                        actor_type="principal" if is_user else event.actor,
                        actor_id=(
                            principal.principal_id if is_user else f"evaluation-{event.actor}"
                        ),
                        payload={"content": event.text},
                    )
                )
        formed = await composition.memory.run(
            trigger="evaluation",
            scope="memory-distillation-evaluation",
            session_id=session_id,
        )
        identity = None
        if policy_version != "formation@7":
            async with composition.uow_factory() as uow:
                selections = await uow.process_events.list("memory.provider_extraction.selection")
            if len(selections) != 1:
                raise ValueError("distillation evaluation did not record one model selection")
            payload = selections[0].payload
            provider = payload.get("provider")
            model = payload.get("model")
            compiled_policy = payload.get("policy_version")
            if not all(isinstance(value, str) for value in (provider, model, compiled_policy)):
                raise ValueError("distillation evaluation model selection is incomplete")
            identity = (str(provider), str(model), str(compiled_policy))
        evaluated_at = composition.clock.now()
    beliefs = [
        DistillationEvaluationBelief(
            claim_kind=belief.claim_kind,
            derivation=belief.derivation,
            longevity=belief.longevity,
            subject=belief.subject,
            statement=belief.statement,
        )
        for belief in formed.beliefs
    ]
    return DistillationArmResult(
        policy_version=policy_version,
        beliefs=beliefs,
        score=score_distillation_case(case, beliefs),
        identity=identity,
        provider_calls=formed.run.provider_call_count,
        evaluated_at=evaluated_at,
    )


def load_distillation_corpus(repository_root: Path) -> tuple[MemoryDistillationCorpus, str]:
    root = repository_root.resolve()
    path = (root / CORPUS_PATH).resolve()
    path.relative_to(root)
    raw = path.read_bytes()
    return (
        MemoryDistillationCorpus.model_validate_json(raw),
        hashlib.sha256(raw).hexdigest(),
    )


def publish_distillation_evidence(
    path: Path,
    *,
    identity: dict[str, str],
    corpus_sha256: str,
    sample_count: int,
    positive_case_count: int,
    metrics: dict[str, float | int],
    evaluated_at: datetime,
) -> MemoryDistillationEvidence:
    """Validate all gates before creating, never replacing, one artifact."""

    evidence = MemoryDistillationEvidence.model_validate(
        {
            **identity,
            "corpus_sha256": corpus_sha256,
            "sample_count": sample_count,
            "positive_case_count": positive_case_count,
            "evaluated_at": evaluated_at,
            **metrics,
        }
    )
    encoded = json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.write(b"\n")
    return evidence


async def run_live_evaluation(
    repository_root: Path,
    *,
    model_policy: str,
    policy_profile: str,
    build_ref: str,
    output: Path,
) -> MemoryDistillationEvaluationResult | None:
    """Evaluate all three frozen/new policies and publish only passing evidence."""

    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        return None
    if not model_policy.strip() or not policy_profile.strip() or not build_ref.strip():
        raise ValueError("model policy, policy profile, and build ref must be non-empty")
    if output.resolve().exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {output.resolve()}")

    corpus, corpus_sha256 = load_distillation_corpus(repository_root)
    base_settings = load_settings()
    results: list[DistillationCaseResult] = []
    identities: set[tuple[str, str, str]] = set()
    evaluated_at: datetime | None = None
    policies: tuple[Literal["formation@7"], Literal["formation@8"], Literal["formation@9"]] = (
        "formation@7",
        "formation@8",
        "formation@9",
    )
    with tempfile.TemporaryDirectory(prefix="agent-memory-distillation-eval-") as root:
        settings = _evaluation_settings(base_settings, Path(root) / "artifacts")
        for case in corpus.cases:
            arms: dict[
                Literal["formation@7", "formation@8", "formation@9"],
                DistillationArmResult,
            ] = {}
            for policy_version in policies:
                arm = await _evaluate_case(
                    settings,
                    case,
                    model_policy=model_policy,
                    policy_profile=policy_profile,
                    policy_version=policy_version,
                )
                arms[policy_version] = arm
                if arm.identity is not None:
                    identities.add(arm.identity)
                if policy_version == "formation@9":
                    evaluated_at = arm.evaluated_at
            results.append(
                DistillationCaseResult(
                    case_id=case.id,
                    label=case.label,
                    scenario=case.scenario,
                    arms=arms,
                )
            )

    if len(identities) != 1:
        raise ValueError("distillation evaluation resolved more than one provider tuple")
    if evaluated_at is None:
        raise ValueError("memory-distillation corpus is empty")
    provider, model, compiled_policy = identities.pop()
    summaries = {policy: _policy_metrics(policy, results) for policy in policies}
    current = summaries["formation@9"]
    previous = summaries["formation@8"]
    lift = (current.useful_recall - previous.useful_recall) * 100
    failures: list[str] = []
    if current.direct_must_form_recall < 0.95:
        failures.append(
            f"direct must-form recall {current.direct_must_form_recall:.3f} is below 0.95"
        )
    if current.hypothesis_must_form_recall < 0.8:
        failures.append(
            f"hypothesis must-form recall {current.hypothesis_must_form_recall:.3f} is below 0.80"
        )
    if current.benign_precision < 0.9:
        failures.append(f"benign precision {current.benign_precision:.3f} is below 0.90")
    if lift < 15:
        failures.append(f"useful recall lift {lift:.1f}pp is below 15pp")
    if current.boundary_failures:
        failures.append(f"trust-boundary failures {current.boundary_failures} is not zero")
    for case, result in zip(corpus.cases, results, strict=True):
        expected_calls = 3 if any(event.actor == "user" for event in case.events) else 0
        actual_calls = result.arms["formation@9"].provider_calls
        if actual_calls != expected_calls:
            failures.append(
                f"{case.id} made {actual_calls} provider calls; expected {expected_calls}"
            )
    personal_core = [result for result in results if result.scenario == "personal-agent"]
    if not personal_core or any(
        result.arms["formation@9"].score.matched != result.arms["formation@9"].score.expected
        for result in personal_core
    ):
        failures.append("personal-agent direct and hypothesis core did not pass")
    rich_core = [result for result in results if result.scenario == "rich-conversation"]
    if not rich_core or any(
        result.arms["formation@9"].score.matched != result.arms["formation@9"].score.expected
        for result in rich_core
    ):
        failures.append("rich multi-turn conversation core did not pass completely")
    matched_kinds = {
        expected.claim_kind
        for case, result in zip(corpus.cases, results, strict=True)
        for expected in case.expected
        if any(
            belief.claim_kind is expected.claim_kind
            and belief.derivation is expected.derivation
            and belief.longevity is expected.longevity
            and (
                (
                    _normalized(belief.subject)
                    in {_normalized(value) for value in expected.subjects}
                    and _normalized(belief.statement)
                    in {_normalized(value) for value in expected.statements}
                )
                or any(
                    _semantically_equivalent(belief.statement, statement)
                    for statement in expected.statements
                )
            )
            for belief in result.arms["formation@9"].beliefs
        )
    }
    if matched_kinds != set(MemoryClaimKind):
        missing = ",".join(sorted(kind.value for kind in set(MemoryClaimKind) - matched_kinds))
        failures.append(f"claim-kind coverage is incomplete: {missing}")
    correction_case_ids = {"boundary-retraction-007", "boundary-correction-008"}
    correction_predictions = sum(
        result.arms["formation@9"].score.predicted
        for result in results
        if result.case_id in correction_case_ids
    )
    correction_rate = correction_predictions * 100 / max(1, current.predicted)
    if correction_rate > 10:
        failures.append(f"synthetic correction rate {correction_rate:.1f} exceeds 10")

    evidence = None
    if not failures:
        evidence = publish_distillation_evidence(
            output.resolve(),
            identity={
                "model_policy": model_policy,
                "provider": provider,
                "model": model,
                "policy_profile": policy_profile,
                "policy_version": compiled_policy,
                "build_ref": build_ref,
            },
            corpus_sha256=corpus_sha256,
            sample_count=len(corpus.cases),
            positive_case_count=sum(case.label != "must_not_form" for case in corpus.cases),
            metrics={
                "direct_must_form_recall": current.direct_must_form_recall,
                "hypothesis_must_form_recall": current.hypothesis_must_form_recall,
                "benign_precision": current.benign_precision,
                "useful_recall_lift_percentage_points": lift,
                "correction_rate_per_hundred": correction_rate,
                "provider_calls_per_consolidation": 3,
                "boundary_failures": current.boundary_failures,
            },
            evaluated_at=evaluated_at,
        )
    return MemoryDistillationEvaluationResult(
        passed=not failures,
        failure_summary="; ".join(failures) if failures else None,
        cases=results,
        policies=summaries,
        evidence=evidence,
    )
