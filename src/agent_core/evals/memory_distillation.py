"""Offline corpus and publication types for formation@9 comparison."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.config import (
    MEMORY_DISTILLATION_CORPUS_PATH,
    MEMORY_DISTILLATION_HOLDOUT_DIGEST_PATH,
    MEMORY_DISTILLATION_HOLDOUT_PATH,
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
    DistillationRunMetrics,
    MemoryAuthority,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryDistillationEvidence,
    MemoryLongevity,
)
from agent_core.memory.distillation import DISTILLATION_CALLS_PER_SEGMENT, assigned_longevity
from agent_core.memory.equivalence import (
    DISTILLATION_SCORER_VERSION,
    is_generic_subject,
    normalized_statement,
    statement_matches_claim,
    statement_supports_clause,
    subject_matches,
)
from agent_core.policy.scopes import PLATFORM_SCOPES

CORPUS_PATH = MEMORY_DISTILLATION_CORPUS_PATH
HOLDOUT_PATH = MEMORY_DISTILLATION_HOLDOUT_PATH
HOLDOUT_DIGEST_PATH = MEMORY_DISTILLATION_HOLDOUT_DIGEST_PATH
MINIMUM_HOLDOUT_CASES = 30
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
    # Kinds a correct belief could reasonably carry instead of the primary
    # kind: a stated training history is a skill to the label author and a
    # project fact to the model. A belief under a compatible kind matches
    # only with the longevity local policy assigns that kind, so the
    # loosening is taxonomy alone and never reaches the memory's lifecycle.
    compatible_kinds: list[MemoryClaimKind] = Field(default_factory=list, max_length=3)
    subjects: list[str] = Field(min_length=1)
    statements: list[str] = Field(min_length=1)
    evidence_text: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def subjects_are_specific(self) -> ExpectedDistilledCandidate:
        if any(is_generic_subject(subject) for subject in self.subjects):
            raise ValueError("expected subjects must name a specific conflict key, not the user")
        if self.claim_kind in self.compatible_kinds:
            raise ValueError("compatible kinds must not repeat the primary kind")
        if len(set(self.compatible_kinds)) != len(self.compatible_kinds):
            raise ValueError("compatible kinds must be unique")
        return self

    def accepts(self, claim_kind: MemoryClaimKind, longevity: MemoryLongevity) -> bool:
        """Whether a belief's kind and longevity satisfy this expectation."""

        if claim_kind is self.claim_kind:
            return longevity is self.longevity
        return claim_kind in self.compatible_kinds and longevity is assigned_longevity(
            claim_kind, self.derivation
        )


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
    # Exact user clauses a seeded prior belief already asserts. The provider
    # must mark each `represented` by an anticipation attributed to that
    # belief; a case may only label them when it runs against a seed pool.
    represented_text: list[str] = Field(default_factory=list)

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
        if self.represented_text and self.prior_beliefs_pool is None:
            raise ValueError("represented text requires a seed pool that represents it")
        if any(text not in user_text for text in self.represented_text):
            raise ValueError("represented text is not an exact user substring")
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
            case.scenario == "rich-conversation"
            and case.prior_beliefs_pool is not None
            and len(self.seed_pools[case.prior_beliefs_pool]) >= MINIMUM_SEED_POOL_SIZE
            for case in positives
        ):
            raise ValueError("the rich-conversation scenario must run against a populated store")
        # A populated store proves nothing about anticipation unless some case
        # restates what a seed already asserts and the provider must say so.
        if not any(
            case.represented_text
            and case.prior_beliefs_pool is not None
            and all(
                any(
                    statement_supports_clause(seed.statement, text)
                    for seed in self.seed_pools[case.prior_beliefs_pool]
                )
                for text in case.represented_text
            )
            for case in seeded_positives
        ):
            raise ValueError(
                "a seeded positive case must label a clause that one of its seeds represents"
            )
        return self

    def seeds_for(self, case: MemoryDistillationCase) -> list[SeedBelief]:
        if case.prior_beliefs_pool is None:
            return []
        return list(self.seed_pools[case.prior_beliefs_pool])


class MemoryDistillationHoldout(BaseModel):
    """The frozen case set: authored before its first run, never edited after.

    The development corpus may be tuned freely; the holdout is what an
    artifact's independent numbers come from, so it carries a freeze date, its
    digest is recorded beside it in the tree, and any edit shows as a digest
    change in review and invalidates every artifact bound to the old digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    frozen_on: date
    authored_by: str = Field(min_length=1)
    seed_pools: dict[str, list[SeedBelief]] = Field(default_factory=dict)
    cases: list[MemoryDistillationCase] = Field(min_length=MINIMUM_HOLDOUT_CASES)

    @model_validator(mode="after")
    def holdout_has_declared_coverage(self) -> MemoryDistillationHoldout:
        identifiers = [case.id for case in self.cases]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("holdout case identifiers must be unique")
        positives = [case for case in self.cases if case.label != "must_not_form"]
        if len(positives) * 10 < len(self.cases) * 6:
            raise ValueError("holdout must be at least sixty percent positive")
        if not any(case.label == "must_not_form" for case in self.cases):
            raise ValueError("holdout must carry must-not-form cases")
        kinds = {expected.claim_kind for case in positives for expected in case.expected}
        if kinds != set(MemoryClaimKind):
            raise ValueError("holdout must expect every claim kind")
        derivations = {expected.derivation for case in positives for expected in case.expected}
        if derivations != {MemoryDerivation.DIRECT, MemoryDerivation.HYPOTHESIS}:
            raise ValueError("holdout must expect direct and hypothesis claims")
        for case in self.cases:
            pool = case.prior_beliefs_pool
            if pool is not None and pool not in self.seed_pools:
                raise ValueError(f"{case.id} references an undeclared seed pool")
        represented = [
            case
            for case in positives
            if case.represented_text
            and case.prior_beliefs_pool is not None
            and len(self.seed_pools[case.prior_beliefs_pool]) >= MINIMUM_SEED_POOL_SIZE
            and all(
                any(
                    statement_supports_clause(seed.statement, text)
                    for seed in self.seed_pools[case.prior_beliefs_pool]
                )
                for text in case.represented_text
            )
        ]
        # One represented case is one anticipation call's chance; several make
        # the aggregate gate a measurement rather than a coin flip.
        if len(represented) < 3:
            raise ValueError("holdout must carry at least three seeded cases that restate a seed")
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

    scorer_version: Literal["distillation-scorer@7"] = DISTILLATION_SCORER_VERSION
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
    # Which expected claims matched, in the case's order, so repeated runs can
    # be judged per expected memory rather than all-or-nothing per run.
    expectation_matches: list[bool] = Field(default_factory=list)


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
    # Candidates the provider withheld because an anticipation attributed to a
    # live memory already asserted them, and the labelled represented clauses
    # with how many of them the verification upheld.
    attributed_redundancies: int = Field(default=0, ge=0)
    represented_units: int = Field(default=0, ge=0)
    represented_units_verified: int = Field(default=0, ge=0)
    evaluated_at: datetime


class DistillationCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    label: Literal["must_form", "reasonable_to_form", "must_not_form"]
    scenario: str
    arms: dict[PolicyVersion, DistillationArmResult]
    run_index: int = Field(default=0, ge=0)


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
    scorer_version: Literal["distillation-scorer@7"] = DISTILLATION_SCORER_VERSION
    cases: list[DistillationCaseResult]
    policies: dict[PolicyVersion, DistillationPolicyMetrics]
    holdout_cases: list[DistillationCaseResult] = Field(default_factory=list)
    holdout_policies: dict[PolicyVersion, DistillationPolicyMetrics] = Field(default_factory=dict)
    # A tuning run scores the development corpus alone: it never reads the
    # holdout, which every run against it spends, and never publishes.
    development_only: bool = False
    # How many times the whole evaluation ran; the gates pool every run.
    repeats: int = Field(default=1, ge=1)
    evidence: MemoryDistillationEvidence | None = None

    @model_validator(mode="after")
    def outcome_matches_evidence(self) -> MemoryDistillationEvaluationResult:
        if self.evidence is not None and (not self.passed or self.development_only):
            raise ValueError("only passing distillation evaluation may carry evidence")
        if self.passed and self.evidence is None and not self.development_only:
            raise ValueError(
                "a passing distillation evaluation publishes evidence unless it is development-only"
            )
        if self.development_only and (self.holdout_cases or self.holdout_policies):
            raise ValueError("a development-only evaluation carries no holdout results")
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

    Strict scoring requires the derivation, the closed claim kind or one the
    gold declares compatible with the longevity local policy assigns it, plus
    a specific subject and an equivalent statement. Lenient scoring compares
    the statement only, so a control policy that cannot express the closed
    fields is still credited for the memory it actually formed.
    """

    if closed_fields and (
        belief.derivation is not expected.derivation
        or not expected.accepts(belief.claim_kind, belief.longevity)
    ):
        return False
    statement_matches = any(
        statement_matches_claim(belief.statement, statement) for statement in expected.statements
    )
    if not statement_matches:
        return False
    if not closed_fields:
        return True
    return subject_matches(belief.subject, expected.subjects, expected.statements)


def _maximum_matching(adjacency: list[list[int]], belief_count: int) -> list[int | None]:
    """Assign each expected claim to at most one belief, maximizing the pairs.

    A greedy pass can strand a claim when an earlier claim takes the only
    belief it could use; augmenting paths find the largest valid pairing
    regardless of the order beliefs were formed in.
    """

    owner: list[int | None] = [None] * belief_count

    def augment(expected_index: int, seen: set[int]) -> bool:
        for belief_index in adjacency[expected_index]:
            if belief_index in seen:
                continue
            seen.add(belief_index)
            current = owner[belief_index]
            if current is None or augment(current, seen):
                owner[belief_index] = expected_index
                return True
        return False

    for expected_index in range(len(adjacency)):
        augment(expected_index, set())
    assignment: list[int | None] = [None] * len(adjacency)
    for belief_index, assigned_expected in enumerate(owner):
        if assigned_expected is not None:
            assignment[assigned_expected] = belief_index
    return assignment


def score_distillation_case(
    case: MemoryDistillationCase,
    beliefs: list[DistillationEvaluationBelief],
    *,
    closed_fields: bool = True,
    evidence_units: int = 0,
    evidence_units_formed: int = 0,
) -> DistillationCaseScore:
    """Match each expected semantic claim once and count every extra prediction."""

    adjacency = [
        [
            index
            for index, belief in enumerate(beliefs)
            if _belief_matches(belief, expected, closed_fields=closed_fields)
        ]
        for expected in case.expected
    ]
    assignment = _maximum_matching(adjacency, len(beliefs))
    occupied = {belief_index for belief_index in assignment if belief_index is not None}
    matched = len(occupied)
    direct_expected = 0
    direct_matched = 0
    hypothesis_expected = 0
    hypothesis_matched = 0
    for expected, belief_index in zip(case.expected, assignment, strict=True):
        if case.label != "must_form":
            continue
        if expected.derivation is MemoryDerivation.DIRECT:
            direct_expected += 1
            direct_matched += int(belief_index is not None)
        else:
            hypothesis_expected += 1
            hypothesis_matched += int(belief_index is not None)
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
        expectation_matches=[belief_index is not None for belief_index in assignment],
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


def _case_represented_units(
    case: MemoryDistillationCase,
    units: list[tuple[str, str]],
    dispositions: dict[str, str],
) -> tuple[int, int]:
    """Count clauses a seed already asserts, and how many the provider verifiably represented.

    Only the verified `represented` disposition counts: a label the local check
    could not uphold is recorded as `represented_unverified` and counts as a
    failure to represent, exactly as it does at runtime.
    """

    if not case.represented_text:
        return 0, 0
    represented = [
        unit_id
        for unit_id, text in units
        if any(labelled in text or text in labelled for labelled in case.represented_text)
    ]
    verified = sum(dispositions.get(unit_id) == "represented" for unit_id in represented)
    return len(represented), verified


def represented_case_count(results: list[DistillationCaseResult]) -> int:
    """Cases in which every labelled represented clause was verifiably represented."""

    return sum(
        result.arms["formation@9"].represented_units > 0
        and result.arms["formation@9"].represented_units_verified
        == result.arms["formation@9"].represented_units
        for result in results
    )


async def seed_prior_beliefs(
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
        seeded = await seed_prior_beliefs(composition, seeds or [], principal=principal)
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
        attributed_redundancies = 0
        represented_units = 0
        represented_units_verified = 0
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
            attributed_redundancies = int(
                getattr(audit, "prediction_attributed_redundancies", 0) or 0
            )
            represented_units, represented_units_verified = _case_represented_units(
                case, units, dispositions
            )
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
        attributed_redundancies=attributed_redundancies,
        represented_units=represented_units,
        represented_units_verified=represented_units_verified,
        evaluated_at=evaluated_at,
    )


def _git_output(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("the evaluated tree must be a git checkout so its build ref can be bound")
    return completed.stdout.strip()


def require_committed_tree(repository_root: Path, build_ref: str) -> None:
    """Refuse to label evidence with a commit the evaluated files do not match.

    The build reference is what a later reader reproduces from, so it must be
    the checked-out HEAD and no tracked file may differ from it.
    """

    head = _git_output(repository_root, "rev-parse", "HEAD")
    if head != build_ref:
        raise ValueError("build ref must name the checked-out HEAD of the evaluated tree")
    if _git_output(repository_root, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("the evaluated tree has uncommitted changes; commit before evaluating")


def load_distillation_corpus(repository_root: Path) -> tuple[MemoryDistillationCorpus, str]:
    root = repository_root.resolve()
    path = (root / CORPUS_PATH).resolve()
    path.relative_to(root)
    raw = path.read_bytes()
    return (
        MemoryDistillationCorpus.model_validate_json(raw),
        hashlib.sha256(raw).hexdigest(),
    )


def load_distillation_holdout(
    repository_root: Path,
) -> tuple[MemoryDistillationHoldout, str]:
    """Load the frozen holdout and confirm it is the one whose digest is recorded.

    The recorded digest is the freeze: a holdout whose content no longer
    matches it has been edited since it was frozen, and the evaluator refuses
    to score against it until the freeze is deliberately re-recorded.
    """

    root = repository_root.resolve()
    path = (root / HOLDOUT_PATH).resolve()
    path.relative_to(root)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    recorded = (root / HOLDOUT_DIGEST_PATH).read_text().strip()
    if recorded != digest:
        raise ValueError("the holdout has been edited since its digest was frozen")
    holdout = MemoryDistillationHoldout.model_validate_json(raw)
    corpus, _ = load_distillation_corpus(repository_root)
    overlap = {case.id for case in corpus.cases} & {case.id for case in holdout.cases}
    if overlap:
        raise ValueError(f"holdout case identifiers collide with the corpus: {sorted(overlap)}")
    return holdout, digest


def publish_distillation_evidence(
    path: Path,
    *,
    identity: dict[str, str],
    corpus_sha256: str,
    sample_count: int,
    positive_case_count: int,
    holdout_sha256: str,
    holdout_sample_count: int,
    holdout_positive_case_count: int,
    metrics: dict[str, float | int | str],
    evaluated_at: datetime,
    repeats: int = 1,
    run_metrics: list[DistillationRunMetrics] | None = None,
) -> MemoryDistillationEvidence:
    """Validate all gates before creating, never replacing, one artifact."""

    evidence = MemoryDistillationEvidence.model_validate(
        {
            **identity,
            "scorer_version": DISTILLATION_SCORER_VERSION,
            "corpus_sha256": corpus_sha256,
            "sample_count": sample_count,
            "positive_case_count": positive_case_count,
            "holdout_sha256": holdout_sha256,
            "holdout_sample_count": holdout_sample_count,
            "holdout_positive_case_count": holdout_positive_case_count,
            "evaluated_at": evaluated_at,
            "repeats": repeats,
            "run_metrics": run_metrics
            or [
                DistillationRunMetrics(
                    direct_must_form_recall=float(metrics["direct_must_form_recall"]),
                    hypothesis_must_form_recall=float(metrics["hypothesis_must_form_recall"]),
                    benign_precision=float(metrics["benign_precision"]),
                    holdout_direct_must_form_recall=float(
                        metrics["holdout_direct_must_form_recall"]
                    ),
                    holdout_hypothesis_must_form_recall=float(
                        metrics["holdout_hypothesis_must_form_recall"]
                    ),
                    holdout_benign_precision=float(metrics["holdout_benign_precision"]),
                    provider_cost_usd=str(metrics["provider_cost_usd"]),
                )
            ],
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
    development_only: bool = False,
    repeats: int = 1,
) -> MemoryDistillationEvaluationResult | None:
    """Evaluate all three frozen/new policies and publish only passing evidence.

    A development-only run scores the development corpus, reports its gates,
    and stops: the holdout is not loaded and nothing is published, so prompt
    and policy work can be measured without spending the holdout on it.

    With repeats, the whole evaluation runs that many times and the gates are
    decided over the pool of every run, because one run of this policy has
    been measured to move by up to a tenth on a gate against the next.
    """

    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        return None
    if not model_policy.strip() or not policy_profile.strip() or not build_ref.strip():
        raise ValueError("model policy, policy profile, and build ref must be non-empty")
    if repeats < 1:
        raise ValueError("repeats must be at least one")
    if _BUILD_REF.match(build_ref.strip()) is None:
        raise ValueError("build ref must be the full forty-character commit sha")
    require_committed_tree(repository_root, build_ref.strip())
    if output.resolve().exists():
        raise ValueError(f"refusing to overwrite existing evaluation evidence: {output.resolve()}")

    corpus, corpus_sha256 = load_distillation_corpus(repository_root)
    holdout: MemoryDistillationHoldout | None = None
    holdout_sha256: str | None = None
    if not development_only:
        holdout, holdout_sha256 = load_distillation_holdout(repository_root)
    base_settings = load_settings()
    results: list[DistillationCaseResult] = []
    holdout_results: list[DistillationCaseResult] = []
    identities: set[tuple[str, str, str]] = set()
    evaluated_at: datetime | None = None
    case_sets = [(corpus.cases, corpus.seeds_for, results)]
    if holdout is not None:
        case_sets.append((holdout.cases, holdout.seeds_for, holdout_results))
    with tempfile.TemporaryDirectory(prefix="agent-memory-distillation-eval-") as root:
        settings = _evaluation_settings(base_settings, Path(root) / "artifacts")
        for run_index in range(repeats):
            for case_set, seeds_for, sink in case_sets:
                for case in case_set:
                    arms: dict[PolicyVersion, DistillationArmResult] = {}
                    seeds = seeds_for(case)
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
                    sink.append(
                        DistillationCaseResult(
                            case_id=case.id,
                            label=case.label,
                            scenario=case.scenario,
                            arms=arms,
                            run_index=run_index,
                        )
                    )

    if len(identities) != 1:
        raise ValueError("distillation evaluation resolved more than one provider tuple")
    if evaluated_at is None:
        raise ValueError("memory-distillation corpus is empty")
    provider, model, compiled_policy = identities.pop()
    summaries = {policy: _policy_metrics(policy, results) for policy in _POLICIES}
    holdout_summaries = (
        {policy: _policy_metrics(policy, holdout_results) for policy in _POLICIES}
        if holdout is not None
        else {}
    )
    failures = [*evaluate_publication_gates(corpus, results, summaries, repeats=repeats)]
    if holdout is not None:
        failures.extend(evaluate_holdout_gates(holdout_results, holdout_summaries, repeats=repeats))
    run_metrics: list[DistillationRunMetrics] = []
    for run_index, run in enumerate(_by_run(results)):
        run_summary = _policy_metrics("formation@9", run)
        held_run = (
            _policy_metrics("formation@9", _by_run(holdout_results)[run_index])
            if holdout is not None
            else run_summary
        )
        run_metrics.append(
            DistillationRunMetrics(
                direct_must_form_recall=run_summary.direct_must_form_recall,
                hypothesis_must_form_recall=run_summary.hypothesis_must_form_recall,
                benign_precision=run_summary.benign_precision,
                holdout_direct_must_form_recall=held_run.direct_must_form_recall,
                holdout_hypothesis_must_form_recall=held_run.hypothesis_must_form_recall,
                holdout_benign_precision=held_run.benign_precision,
                provider_cost_usd=format(
                    Decimal(run_summary.provider_cost_usd)
                    + (Decimal(held_run.provider_cost_usd) if holdout is not None else 0),
                    "f",
                ),
            )
        )
    current = summaries["formation@9"]
    lift = (current.useful_recall - summaries["formation@8"].useful_recall) * 100
    correction_rate = _correction_rate(results, current)

    evidence = None
    if not failures and holdout is not None and holdout_sha256 is not None:
        held = holdout_summaries["formation@9"]
        holdout_lift = (held.useful_recall - holdout_summaries["formation@8"].useful_recall) * 100
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
            holdout_sha256=holdout_sha256,
            holdout_sample_count=len(holdout.cases),
            holdout_positive_case_count=sum(
                case.label != "must_not_form" for case in holdout.cases
            ),
            metrics={
                "holdout_direct_must_form_recall": held.direct_must_form_recall,
                "holdout_hypothesis_must_form_recall": held.hypothesis_must_form_recall,
                "holdout_benign_precision": held.benign_precision,
                "holdout_useful_recall_lift_percentage_points": holdout_lift,
                "holdout_evidence_disposition_precision": held.evidence_disposition_precision,
                "holdout_represented_case_count": represented_case_count(holdout_results),
                "direct_must_form_recall": current.direct_must_form_recall,
                "hypothesis_must_form_recall": current.hypothesis_must_form_recall,
                "benign_precision": current.benign_precision,
                "useful_recall_lift_percentage_points": lift,
                "correction_rate_per_hundred": correction_rate,
                "evidence_disposition_precision": current.evidence_disposition_precision,
                "provider_calls_per_segment": _calls_per_segment([*results, *holdout_results]),
                "provider_calls_measured": sum(
                    result.arms["formation@9"].provider_calls
                    for result in [*results, *holdout_results]
                ),
                "consolidations_measured": sum(
                    bool(result.arms["formation@9"].expected_provider_calls)
                    for result in [*results, *holdout_results]
                ),
                "provider_cost_usd": format(
                    Decimal(current.provider_cost_usd) + Decimal(held.provider_cost_usd), "f"
                ),
                "seeded_case_count": sum(
                    result.arms["formation@9"].seeded_beliefs > 0 for result in results
                ),
                "represented_case_count": represented_case_count(results),
                "boundary_failures": current.boundary_failures,
            },
            evaluated_at=evaluated_at,
            repeats=repeats,
            run_metrics=run_metrics,
        )
    return MemoryDistillationEvaluationResult(
        passed=not failures,
        failure_summary="; ".join(failures) if failures else None,
        cases=results,
        policies=summaries,
        holdout_cases=holdout_results,
        holdout_policies=holdout_summaries,
        development_only=development_only,
        repeats=repeats,
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


def evaluate_holdout_gates(
    results: list[DistillationCaseResult],
    summaries: dict[PolicyVersion, DistillationPolicyMetrics],
    *,
    repeats: int = 1,
) -> list[str]:
    """Every reason the frozen holdout denies publication.

    The holdout carries the same recall, precision, lift, disposition,
    boundary, call-count, and represented gates as the development corpus and
    none of the corpus's named-scenario gates, because its cases are not
    tuned toward anything.
    """

    current = summaries["formation@9"]
    lift = (current.useful_recall - summaries["formation@8"].useful_recall) * 100
    failures: list[str] = []
    if current.direct_must_form_recall < 0.95:
        failures.append(
            f"holdout direct must-form recall {current.direct_must_form_recall:.3f} is below 0.95"
        )
    if current.hypothesis_must_form_recall < 0.8:
        failures.append(
            "holdout hypothesis must-form recall "
            f"{current.hypothesis_must_form_recall:.3f} is below 0.80"
        )
    # The holdout's floor is 0.75, not the corpus's 0.90: its labels cannot
    # anticipate every true belief, and a personal agent is scored recall-first.
    if current.benign_precision < 0.75:
        failures.append(f"holdout benign precision {current.benign_precision:.3f} is below 0.75")
    if lift < 15:
        failures.append(f"holdout useful recall lift {lift:.1f}pp is below 15pp")
    if current.evidence_disposition_precision < MINIMUM_EVIDENCE_DISPOSITION_PRECISION:
        failures.append(
            "holdout evidence disposition precision "
            f"{current.evidence_disposition_precision:.3f} is below "
            f"{MINIMUM_EVIDENCE_DISPOSITION_PRECISION:.2f}"
        )
    if current.boundary_failures:
        failures.append(f"holdout trust-boundary failures {current.boundary_failures} is not zero")
    for result in results:
        arm = result.arms["formation@9"]
        if arm.provider_calls != arm.expected_provider_calls:
            failures.append(
                f"{result.case_id} made {arm.provider_calls} provider calls; "
                f"expected {arm.expected_provider_calls}"
            )
    if not _represented_in_majority(results, repeats):
        failures.append("no holdout seeded case demonstrated attributed representation")
    return failures


def _by_run(results: list[DistillationCaseResult]) -> list[list[DistillationCaseResult]]:
    runs: dict[int, list[DistillationCaseResult]] = {}
    for result in results:
        runs.setdefault(result.run_index, []).append(result)
    return [runs[index] for index in sorted(runs)]


def _core_passes(results: list[DistillationCaseResult], scenario: str, repeats: int) -> bool:
    """Every expected memory of every core case formed in a majority of runs.

    One run decides nothing on its own once the evaluation is repeated: a
    core memory that forms in two runs of three is formed, and one that
    forms in one of three is not. With a single run this is the old rule,
    every expected memory in that run.
    """

    tallies: dict[str, list[int]] = {}
    for result in results:
        if result.scenario != scenario:
            continue
        score = result.arms["formation@9"].score
        flags = score.expectation_matches or [score.matched == score.expected] * score.expected
        tally = tallies.setdefault(result.case_id, [0] * len(flags))
        for index, flag in enumerate(flags):
            tally[index] += int(flag)
    if not tallies:
        return False
    return all(count * 2 > repeats for tally in tallies.values() for count in tally)


def _represented_in_majority(results: list[DistillationCaseResult], repeats: int) -> bool:
    """At least one seeded case was verifiably represented in a majority of runs."""

    demonstrated = sum(represented_case_count(run) > 0 for run in _by_run(results))
    return demonstrated * 2 > repeats


def evaluate_publication_gates(
    corpus: MemoryDistillationCorpus,
    results: list[DistillationCaseResult],
    summaries: dict[PolicyVersion, DistillationPolicyMetrics],
    *,
    repeats: int = 1,
) -> list[str]:
    """Every reason the comparative run may not publish activation evidence.

    With repeats, `results` pools every run and `summaries` was computed over
    the pool, so recall, precision, lift, disposition, correction rate, and
    claim-kind coverage are aggregates; the cores need each expected memory
    in a majority of runs; the represented gate needs a majority of runs; and
    boundary failures and call counts fail on any run.
    """

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
    # One represented case is one anticipation call's chance, so the gate is
    # the aggregate over cases and, with repeats, over runs: at least one
    # seeded restatement was verifiably represented in a majority of runs.
    if not _represented_in_majority(results, repeats):
        failures.append("no seeded case demonstrated attributed representation")
    if not _core_passes(results, "personal-agent", repeats):
        failures.append("personal-agent direct and hypothesis core did not pass")
    if not _core_passes(results, "rich-conversation", repeats):
        failures.append("rich multi-turn conversation core did not pass completely")
    # Coverage counts the kind the provider formed, not the kind the label
    # names: matching a skill through a compatible project fact must not
    # certify that the policy ever forms a skill.
    cases_by_id = {case.id: case for case in corpus.cases}
    matched_kinds = {
        belief.claim_kind
        for result in results
        for expected in cases_by_id[result.case_id].expected
        for belief in result.arms["formation@9"].beliefs
        if _belief_matches(belief, expected, closed_fields=True)
    }
    if matched_kinds != set(MemoryClaimKind):
        missing = ",".join(sorted(kind.value for kind in set(MemoryClaimKind) - matched_kinds))
        failures.append(f"claim-kind coverage is incomplete: {missing}")
    correction_rate = _correction_rate(results, current)
    if correction_rate > 10:
        failures.append(f"synthetic correction rate {correction_rate:.1f} exceeds 10")
    return failures
