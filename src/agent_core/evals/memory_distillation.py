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
from decimal import Decimal
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
    MemoryAuthority,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryDistillationEvidence,
    MemoryLongevity,
)
from agent_core.memory.distillation import DISTILLATION_CALLS_PER_SEGMENT
from agent_core.memory.equivalence import (
    DISTILLATION_SCORER_VERSION,
    is_generic_subject,
    normalized_statement,
    statements_equivalent,
    subject_matches,
)
from agent_core.policy.scopes import PLATFORM_SCOPES

CORPUS_PATH = Path("evals/capability/memory-formation.v3.json")
EVALUATION_SCOPE = "memory-distillation-evaluation"
MINIMUM_SEED_POOL_SIZE = 25
MINIMUM_EVIDENCE_DISPOSITION_PRECISION = 0.75

PolicyVersion = Literal["formation@7", "formation@8", "formation@9"]
_POLICIES: tuple[PolicyVersion, ...] = ("formation@7", "formation@8", "formation@9")

_BUILD_REF = re.compile(r"^[0-9a-f]{40}$")


def _normalized(value: str) -> str:
    return normalized_statement(value)


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

    @model_validator(mode="after")
    def subjects_are_specific(self) -> ExpectedDistilledCandidate:
        if any(is_generic_subject(subject) for subject in self.subjects):
            raise ValueError("expected subjects must name a specific conflict key, not the user")
        return self


class SeedBelief(BaseModel):
    """A realistic prior belief written to the store before a case consolidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_kind: MemoryClaimKind
    derivation: MemoryDerivation = MemoryDerivation.DIRECT
    longevity: MemoryLongevity = MemoryLongevity.DURABLE
    subject: str = Field(min_length=1, max_length=512)
    statement: str = Field(min_length=1, max_length=8192)


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
    prior_beliefs_pool: str | None = None

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
    seed_pools: dict[str, list[SeedBelief]] = Field(default_factory=dict)
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
        for case in self.cases:
            pool = case.prior_beliefs_pool
            if pool is not None and pool not in self.seed_pools:
                raise ValueError(f"{case.id} references an undeclared seed pool")
        seeded_positives = [
            case
            for case in positives
            if case.prior_beliefs_pool is not None
            and len(self.seed_pools[case.prior_beliefs_pool]) >= MINIMUM_SEED_POOL_SIZE
        ]
        if not seeded_positives:
            raise ValueError(
                "memory-distillation corpus must run at least one positive case against a "
                f"populated store of at least {MINIMUM_SEED_POOL_SIZE} prior beliefs"
            )
        if not any(
            sum(event.actor == "user" for event in case.events) >= 2 for case in seeded_positives
        ):
            raise ValueError("a seeded positive case must be a multi-event session")
        if not any(
            case.scenario == "rich-conversation" and case.prior_beliefs_pool is not None
            for case in positives
        ):
            raise ValueError("the rich-conversation scenario must run against a populated store")
        return self

    def seeds_for(self, case: MemoryDistillationCase) -> list[SeedBelief]:
        if case.prior_beliefs_pool is None:
            return []
        return list(self.seed_pools[case.prior_beliefs_pool])


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

    scorer_version: Literal["distillation-scorer@2"] = DISTILLATION_SCORER_VERSION
    scoring: Literal["strict", "lenient"] = "strict"
    expected: int = Field(ge=0)
    matched: int = Field(ge=0)
    predicted: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    direct_must_form_expected: int = Field(ge=0)
    direct_must_form_matched: int = Field(ge=0)
    hypothesis_must_form_expected: int = Field(ge=0)
    hypothesis_must_form_matched: int = Field(ge=0)
    boundary_failures: int = Field(ge=0)
    evidence_units: int = Field(default=0, ge=0)
    evidence_units_formed: int = Field(default=0, ge=0)


class DistillationArmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: PolicyVersion
    beliefs: list[DistillationEvaluationBelief]
    score: DistillationCaseScore
    identity: tuple[str, str, str] | None = None
    provider_calls: int = Field(ge=0)
    expected_provider_calls: int = Field(ge=0)
    provider_cost_usd: str = Field(default="0", pattern=r"^\d+(?:\.\d+)?$")
    seeded_beliefs: int = Field(default=0, ge=0)
    evaluated_at: datetime


class DistillationCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    label: Literal["must_form", "reasonable_to_form", "must_not_form"]
    scenario: str
    arms: dict[PolicyVersion, DistillationArmResult]


class DistillationPolicyMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: PolicyVersion
    scoring: Literal["strict", "lenient"]
    expected: int = Field(ge=0)
    matched: int = Field(ge=0)
    predicted: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    useful_recall: float = Field(ge=0, le=1)
    direct_must_form_recall: float = Field(ge=0, le=1)
    hypothesis_must_form_recall: float = Field(ge=0, le=1)
    benign_precision: float = Field(ge=0, le=1)
    evidence_disposition_precision: float = Field(ge=0, le=1)
    boundary_failures: int = Field(ge=0)
    provider_calls: int = Field(ge=0)
    provider_cost_usd: str = Field(pattern=r"^\d+(?:\.\d+)?$")


class MemoryDistillationEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    failure_summary: str | None
    scorer_version: Literal["distillation-scorer@2"] = DISTILLATION_SCORER_VERSION
    cases: list[DistillationCaseResult]
    policies: dict[PolicyVersion, DistillationPolicyMetrics]
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


def _belief_matches(
    belief: DistillationEvaluationBelief,
    expected: ExpectedDistilledCandidate,
    *,
    closed_fields: bool,
) -> bool:
    """One expected claim matches one belief.

    Strict scoring requires the closed claim kind, derivation, and longevity plus
    a specific subject and an equivalent statement. Lenient scoring compares the
    statement only, so a control policy that cannot express the closed fields is
    still credited for the memory it actually formed.
    """

    if closed_fields and (
        belief.claim_kind is not expected.claim_kind
        or belief.derivation is not expected.derivation
        or belief.longevity is not expected.longevity
    ):
        return False
    statement_matches = any(
        statements_equivalent(belief.statement, statement) for statement in expected.statements
    )
    if not statement_matches:
        return False
    if not closed_fields:
        return True
    return subject_matches(belief.subject, expected.subjects, expected.statements)


def score_distillation_case(
    case: MemoryDistillationCase,
    beliefs: list[DistillationEvaluationBelief],
    *,
    closed_fields: bool = True,
    evidence_units: int = 0,
    evidence_units_formed: int = 0,
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
        found = False
        for index, belief in enumerate(beliefs):
            if index in occupied:
                continue
            if _belief_matches(belief, expected, closed_fields=closed_fields):
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
        scoring="strict" if closed_fields else "lenient",
        expected=len(case.expected),
        matched=matched,
        predicted=len(beliefs),
        false_positives=false_positives,
        direct_must_form_expected=direct_expected,
        direct_must_form_matched=direct_matched,
        hypothesis_must_form_expected=hypothesis_expected,
        hypothesis_must_form_matched=hypothesis_matched,
        boundary_failures=false_positives if case.scenario == "trust-boundary" else 0,
        evidence_units=evidence_units,
        evidence_units_formed=evidence_units_formed,
    )


def _policy_metrics(
    policy_version: PolicyVersion,
    results: list[DistillationCaseResult],
) -> DistillationPolicyMetrics:
    arms = [result.arms[policy_version] for result in results]
    scores = [arm.score for arm in arms]
    expected = sum(score.expected for score in scores)
    matched = sum(score.matched for score in scores)
    predicted = sum(score.predicted for score in scores)
    direct_expected = sum(score.direct_must_form_expected for score in scores)
    direct_matched = sum(score.direct_must_form_matched for score in scores)
    hypothesis_expected = sum(score.hypothesis_must_form_expected for score in scores)
    hypothesis_matched = sum(score.hypothesis_must_form_matched for score in scores)
    evidence_units = sum(score.evidence_units for score in scores)
    evidence_formed = sum(score.evidence_units_formed for score in scores)
    cost = sum((Decimal(arm.provider_cost_usd) for arm in arms), Decimal(0))
    return DistillationPolicyMetrics(
        policy_version=policy_version,
        scoring="strict" if policy_version == "formation@9" else "lenient",
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
        evidence_disposition_precision=(evidence_formed / evidence_units if evidence_units else 1),
        boundary_failures=sum(score.boundary_failures for score in scores),
        provider_calls=sum(arm.provider_calls for arm in arms),
        provider_cost_usd=format(cost, "f"),
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


def _case_evidence_units(
    case: MemoryDistillationCase,
    units: list[tuple[str, str]],
    dispositions: dict[str, str],
) -> tuple[int, int]:
    """Count clauses that carry labeled evidence, and how many the provider formed.

    A clause the gold labels as evidence must be `formed` or `represented`;
    marking it transient, unsafe, or not_memory hides an omission behind a
    label. A stage that fell back has no ledger, so every evidence clause counts
    as unformed for that case.
    """

    evidence_texts = [evidence for expected in case.expected for evidence in expected.evidence_text]
    if not evidence_texts:
        return 0, 0
    evidence_units = [
        unit_id
        for unit_id, text in units
        if any(evidence in text or text in evidence for evidence in evidence_texts)
    ]
    formed = sum(
        dispositions.get(unit_id) in {"formed", "represented"} for unit_id in evidence_units
    )
    return len(evidence_units), formed


async def _seed_prior_beliefs(
    composition: object,
    seeds: list[SeedBelief],
    *,
    principal: Principal,
) -> int:
    """Write realistic prior beliefs before the case consolidates.

    Production stores are populated. The belief view, anticipation, and
    attributed redundancy only behave like production when the evaluation store
    is too, so seeds are written through the same governed service as any other
    memory, in a separate session the case consolidation never reads.
    """

    if not seeds:
        return 0
    seed_session = await composition.sessions.create()  # type: ignore[attr-defined]
    memory = composition.memory  # type: ignore[attr-defined]
    written = 0
    for seed in seeds:
        async with composition.uow_factory() as uow:  # type: ignore[attr-defined]
            await uow.events.append(
                NewEvent(
                    session_id=seed_session,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=principal.principal_id,
                    payload={"content": seed.statement},
                )
            )
        hypothesis = seed.derivation is MemoryDerivation.HYPOTHESIS
        await memory.remember(
            session_id=seed_session,
            run_id=None,
            statement=seed.statement,
            subject=seed.subject,
            scope=EVALUATION_SCOPE,
            belief_type=_belief_type_for(seed.claim_kind),
            explicit=False,
            authority=MemoryAuthority.INFERRED,
            confidence=0.35 if hypothesis else 0.55,
            trigger="evaluation_seed",
            claim_kind=seed.claim_kind,
            derivation=seed.derivation,
            longevity=seed.longevity,
        )
        written += 1
    return written


def _belief_type_for(claim_kind: MemoryClaimKind) -> object:
    distillation = importlib.import_module("agent_core.memory.distillation")
    return distillation.belief_type_for_claim_kind(claim_kind)


async def _evaluate_case(
    settings: Settings,
    case: MemoryDistillationCase,
    *,
    model_policy: str,
    policy_profile: str,
    policy_version: PolicyVersion,
    seeds: list[SeedBelief] | None = None,
) -> DistillationArmResult:
    principal = Principal(
        tenant_id="evaluation",
        principal_id="memory-distillation-evaluator",
        roles={"evaluator"},
        scopes=set(PLATFORM_SCOPES),
    )
    bootstrap = importlib.import_module("agent_core.bootstrap")
    distillation = importlib.import_module("agent_core.memory.distillation")
    async with bootstrap.build(
        settings=settings,
        storage="memory",
        principal=principal,
        policy_profile=policy_profile,
        model_policy=model_policy,
        memory_provider_evaluation_mode=policy_version == "formation@8",
        memory_distillation_evaluation_mode=policy_version == "formation@9",
    ) as composition:
        seeded = await _seed_prior_beliefs(composition, seeds or [], principal=principal)
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
            scope=EVALUATION_SCOPE,
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
        async with composition.uow_factory() as uow:
            session_events = await uow.events.list_after(session_id, 0, principal)
        owned = distillation.owned_user_events(session_events, principal)
        expected_calls = 0
        evidence_units = 0
        evidence_units_formed = 0
        cost = Decimal(0)
        if policy_version == "formation@9" and owned:
            expected_calls = 3 * len(distillation.plan_segments(owned))
            audit = composition.memory.extractor_audit
            dispositions = dict(getattr(audit, "coverage_dispositions", {}) or {})
            units = [
                (str(unit["coverage_unit_id"]), str(unit["text"]))
                for unit in distillation.coverage_units(owned)
            ]
            evidence_units, evidence_units_formed = _case_evidence_units(case, units, dispositions)
        for stage in formed.run.provider_stage_metrics.values():
            raw_cost = stage.get("cost_usd")
            if isinstance(raw_cost, str) and raw_cost:
                cost += Decimal(raw_cost)
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
        score=score_distillation_case(
            case,
            beliefs,
            closed_fields=policy_version == "formation@9",
            evidence_units=evidence_units,
            evidence_units_formed=evidence_units_formed,
        ),
        identity=identity,
        provider_calls=formed.run.provider_call_count,
        expected_provider_calls=expected_calls,
        provider_cost_usd=format(cost, "f"),
        seeded_beliefs=seeded,
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
    metrics: dict[str, float | int | str],
    evaluated_at: datetime,
) -> MemoryDistillationEvidence:
    """Validate all gates before creating, never replacing, one artifact."""

    evidence = MemoryDistillationEvidence.model_validate(
        {
            **identity,
            "scorer_version": DISTILLATION_SCORER_VERSION,
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
    if _BUILD_REF.match(build_ref.strip()) is None:
        raise ValueError("build ref must be the full forty-character commit sha")
    if output.resolve().exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {output.resolve()}")

    corpus, corpus_sha256 = load_distillation_corpus(repository_root)
    base_settings = load_settings()
    results: list[DistillationCaseResult] = []
    identities: set[tuple[str, str, str]] = set()
    evaluated_at: datetime | None = None
    with tempfile.TemporaryDirectory(prefix="agent-memory-distillation-eval-") as root:
        settings = _evaluation_settings(base_settings, Path(root) / "artifacts")
        for case in corpus.cases:
            arms: dict[PolicyVersion, DistillationArmResult] = {}
            seeds = corpus.seeds_for(case)
            for policy_version in _POLICIES:
                arm = await _evaluate_case(
                    settings,
                    case,
                    model_policy=model_policy,
                    policy_profile=policy_profile,
                    policy_version=policy_version,
                    seeds=seeds,
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
    summaries = {policy: _policy_metrics(policy, results) for policy in _POLICIES}
    failures = evaluate_publication_gates(corpus, results, summaries)
    current = summaries["formation@9"]
    lift = (current.useful_recall - summaries["formation@8"].useful_recall) * 100
    correction_rate = _correction_rate(results, current)

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
                "build_ref": build_ref.strip(),
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
                "evidence_disposition_precision": current.evidence_disposition_precision,
                "provider_calls_per_segment": _calls_per_segment(results),
                "provider_calls_measured": sum(
                    result.arms["formation@9"].provider_calls for result in results
                ),
                "consolidations_measured": sum(
                    bool(result.arms["formation@9"].expected_provider_calls) for result in results
                ),
                "provider_cost_usd": current.provider_cost_usd,
                "seeded_case_count": sum(
                    result.arms["formation@9"].seeded_beliefs > 0 for result in results
                ),
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


def _correction_rate(
    results: list[DistillationCaseResult],
    current: DistillationPolicyMetrics,
) -> float:
    correction_case_ids = {"boundary-retraction-007", "boundary-correction-008"}
    correction_predictions = sum(
        result.arms["formation@9"].score.predicted
        for result in results
        if result.case_id in correction_case_ids
    )
    return correction_predictions * 100 / max(1, current.predicted)


def _calls_per_segment(results: list[DistillationCaseResult]) -> int:
    """Three calls per planned segment, verified on every eligible consolidation.

    A consolidation that spans several segments makes three calls per segment,
    so the published literal is per segment and the artifact also carries the
    measured call and consolidation totals behind it.
    """

    eligible = [
        result.arms["formation@9"]
        for result in results
        if result.arms["formation@9"].expected_provider_calls
    ]
    if not eligible:
        raise ValueError("no eligible consolidation measured provider calls")
    if any(arm.provider_calls != arm.expected_provider_calls for arm in eligible):
        raise ValueError("provider calls per consolidation were not exactly three per segment")
    return DISTILLATION_CALLS_PER_SEGMENT


def evaluate_publication_gates(
    corpus: MemoryDistillationCorpus,
    results: list[DistillationCaseResult],
    summaries: dict[PolicyVersion, DistillationPolicyMetrics],
) -> list[str]:
    """Every reason the comparative run may not publish activation evidence."""

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
    if current.evidence_disposition_precision < MINIMUM_EVIDENCE_DISPOSITION_PRECISION:
        failures.append(
            "evidence disposition precision "
            f"{current.evidence_disposition_precision:.3f} is below "
            f"{MINIMUM_EVIDENCE_DISPOSITION_PRECISION:.2f}"
        )
    if current.boundary_failures:
        failures.append(f"trust-boundary failures {current.boundary_failures} is not zero")
    for result in results:
        arm = result.arms["formation@9"]
        if arm.provider_calls != arm.expected_provider_calls:
            failures.append(
                f"{result.case_id} made {arm.provider_calls} provider calls; "
                f"expected {arm.expected_provider_calls}"
            )
    if not any(result.arms["formation@9"].seeded_beliefs for result in results):
        failures.append("no case ran against a populated store")
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
            _belief_matches(belief, expected, closed_fields=True)
            for belief in result.arms["formation@9"].beliefs
        )
    }
    if matched_kinds != set(MemoryClaimKind):
        missing = ",".join(sorted(kind.value for kind in set(MemoryClaimKind) - matched_kinds))
        failures.append(f"claim-kind coverage is incomplete: {missing}")
    correction_rate = _correction_rate(results, current)
    if correction_rate > 10:
        failures.append(f"synthetic correction rate {correction_rate:.1f} exceeds 10")
    return failures
