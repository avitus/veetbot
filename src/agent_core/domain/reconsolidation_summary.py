"""Extractive summary proposals, content-free lineage and separate projections."""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    BeliefType,
    MemoryAuthority,
    Portability,
    Sensitivity,
)
from agent_core.domain.reconsolidation import ReconValue, SourceVersion, compatible, utc_now
from agent_core.domain.reconsolidation_merge import (
    Digest,
    MergeDependency,
    MergeSource,
    normalize_claim,
    source_admissible,
    source_dependency,
)


class SummaryClause(ReconValue):
    text: str = Field(min_length=1, max_length=4000)
    source_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct_sources(self) -> "SummaryClause":
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("clause source IDs must be distinct and ordered")
        return self


class SummaryClauseRef(ReconValue):
    digest: Digest
    source_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)


class SummaryPlan(ReconValue):
    tenant_id: str
    principal_id: str
    member_ids: tuple[UUID, ...] = Field(min_length=2, max_length=32)
    dependencies: tuple[MergeDependency, ...] = Field(min_length=2, max_length=32)
    clauses: tuple[SummaryClauseRef, ...] = Field(min_length=1, max_length=4)
    omitted_source_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def complete_lineage(self) -> "SummaryPlan":
        if self.member_ids != tuple(sorted(set(self.member_ids))) or self.member_ids != tuple(
            item.source.belief_id for item in self.dependencies
        ):
            raise ValueError("summary dependencies must identify every ordered input")
        used = {key for clause in self.clauses for key in clause.source_ids}
        if not used <= set(self.member_ids) or self.omitted_source_ids != tuple(
            key for key in self.member_ids if key not in used
        ):
            raise ValueError("summary coverage must account for every input")
        return self


class PreparedSummary(ReconValue):
    kind: Literal["summary", "hypothesis"] = "summary"
    plan: SummaryPlan
    clauses: tuple[SummaryClause, ...] = Field(min_length=1, max_length=4)
    subject: Literal["Related memories", "Tentative connection"] = "Related memories"
    belief_types: tuple[BeliefType, ...]
    scope: str
    portability: Portability
    sensitivity: Sensitivity
    authority: MemoryAuthority
    confidence: float = Field(ge=0, le=1)
    last_evidence_at: AwareDatetime
    valid_from: AwareDatetime
    expires_at: AwareDatetime | None

    @property
    def rendered(self) -> str:
        return self.subject + ":\n" + "\n".join(f"- {clause.text}" for clause in self.clauses)


class SummaryMemory(ReconValue):
    id: UUID
    operation_id: UUID
    tenant_id: str
    principal_id: str
    kind: Literal["summary", "hypothesis"] = "summary"
    content: PreparedSummary
    created_at: AwareDatetime
    store_position: int = Field(gt=0)
    status: Literal["active"] = "active"
    revision: int = Field(default=1, ge=1)
    flagged_for_review: bool = True
    utility: float = Field(default=0, ge=-1, le=1)
    last_used_at: AwareDatetime | None = None


def prepare_summary(
    principal: Principal,
    expected: tuple[SourceVersion, ...],
    sources: tuple[MergeSource, ...],
    clauses: tuple[SummaryClause, ...],
    *,
    now: datetime,
) -> PreparedSummary | None:
    now = utc_now(now)
    if not 2 <= len(sources) <= 32 or not 1 <= len(clauses) <= 4:
        return None
    versions = {item.belief_id: item for item in expected}
    by_id = {item.record.id: item for item in sources}
    if len(versions) != len(expected) or len(by_id) != len(sources) or set(versions) != set(by_id):
        return None
    if any(
        not source_admissible(item, principal, now)
        or item.version != versions[item.record.id]
        or not compatible(item.record, sources[0].record)
        for item in sources
    ):
        return None
    selected: list[SummaryClause] = []
    seen: set[str] = set()
    length = len("Related memories:\n")
    for clause in clauses:
        text = normalize_claim(clause.text)
        if (
            not text
            or not set(clause.source_ids) <= by_id.keys()
            or any(
                normalize_claim(by_id[key].record.statement) != text for key in clause.source_ids
            )
        ):
            return None
        if text in seen:
            return None
        seen.add(text)
        size = 2 + len(text) + int(bool(selected))
        if length + size <= 1024:
            selected.append(SummaryClause(text=text, source_ids=clause.source_ids))
            length += size
    if not selected:
        return None
    records = [item.record for item in sources]
    used = {key for clause in selected for key in clause.source_ids}
    ends = [
        at for record in records for at in (record.valid_to, record.expires_at) if at is not None
    ]
    return PreparedSummary(
        plan=SummaryPlan(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            member_ids=tuple(sorted(by_id)),
            dependencies=tuple(source_dependency(by_id[key]) for key in sorted(by_id)),
            clauses=tuple(
                SummaryClauseRef(
                    digest=hashlib.sha256(c.text.encode()).hexdigest(), source_ids=c.source_ids
                )
                for c in selected
            ),
            omitted_source_ids=tuple(key for key in sorted(by_id) if key not in used),
        ),
        clauses=tuple(selected),
        belief_types=tuple(sorted({record.belief_type for record in records})),
        scope=records[0].scope,
        portability=max(
            (r.portability for r in records),
            key=lambda v: {
                Portability.PORTABLE: 0,
                Portability.CONTEXTUAL: 1,
                Portability.LOCAL: 2,
            }[v],
        ),
        sensitivity=max((r.sensitivity for r in records), key=SENSITIVITY_ORDER.__getitem__),
        authority=min(
            (r.authority for r in records),
            key=lambda v: {
                MemoryAuthority.INFERRED: 0,
                MemoryAuthority.AFFIRMED: 1,
                MemoryAuthority.USER: 2,
            }[v],
        ),
        confidence=min(r.confidence for r in records),
        last_evidence_at=max(r.last_evidence_at for r in records),
        valid_from=max(r.valid_from for r in records),
        expires_at=min(ends) if ends else None,
    )


# Conservative local consistency checks supplement, never replace, the verifier.
# Unknown entities, changed counts/polarity and sensitive-trait inference abstain.
_NAMES = re.compile(r"\b[A-Z][\w'-]*\b")
_COUNTS = re.compile(
    r"\b(?:\d+(?:[.,:]\d+)*|one|two|three|four|five|six|seven|eight|nine|ten)\b", re.I
)
_NEGATION = re.compile(r"\b(?:not|never|cannot|without|no|neither|don't|doesn't|can't)\b", re.I)
_TRAITS = re.compile(
    r"\b(?:race|racial|ethnicity|ethnic|religion|religious|muslim|christian|jewish|"
    r"sexual|gay|lesbian|bisexual|transgender|political(?:ly)?|democrat|republican|"
    r"diagnosis|diagnosed|depression|autism|autistic|adhd|pregnant|disability|disabled)\b",
    re.I,
)


def _unhedged_claim(text: str) -> str:
    """Recognize literal restatements, not semantic equivalence or new support."""
    text = re.sub(r"^The (?:owner|user)\b", "User", normalize_claim(text), flags=re.I)
    return normalize_claim(re.sub(r"\b(?:may|might|could|likely|tentatively)\b", "", text))


def prepare_connection(
    principal: Principal,
    expected: tuple[SourceVersion, ...],
    sources: tuple[MergeSource, ...],
    clauses: tuple[SummaryClause, ...],
    *,
    now: datetime,
    formed_at: datetime | None = None,
) -> PreparedSummary | None:
    """One tentative claim; all evidence remains original and independently identified."""
    if len(clauses) != 1 or len(sources) < 2:
        return None
    text = normalize_claim(clauses[0].text)
    owner_text = re.sub(r"^The (?:owner|user)\b", "User", text, flags=re.I)
    ids = tuple(sorted(s.record.id for s in sources))
    if clauses[0].source_ids != ids or len(text) > 980:
        return None
    # Reuse the exact same owner/scope/lifecycle and complete-lineage admission.
    seed = prepare_summary(
        principal,
        expected,
        sources,
        (SummaryClause(text=sources[0].record.statement, source_ids=(sources[0].record.id,)),),
        now=now,
    )
    if seed is None:
        return None
    leaves = {(s.record.source_session_id, n) for s in sources for n in s.record.source_event_ids}
    original_times = [s.original_evidence_at for s in sources]
    if len(leaves) < 2 or any(at is None or at > now for at in original_times):
        return None
    # Resolved person assignments must agree, and the claim must keep uncertainty.
    support = " ".join(s.record.statement for s in sources)
    if (
        not re.match(r"^User(?:['\u2019]s)?\b", owner_text)
        or not re.search(r"\b(?:may|might|could|likely|tentatively)\b", text)
        or len({s.attribution for s in sources}) != 1
        or _TRAITS.search(text)
        or contains_injection_pattern(text)
        or contains_secret_material(text)
        or not (set(_NAMES.findall(owner_text)) - {"User", "User's"})
        <= set(_NAMES.findall(support))
        or not set(_COUNTS.findall(text.casefold())) <= set(_COUNTS.findall(support.casefold()))
        or bool(_NEGATION.search(text)) != bool(_NEGATION.search(support))
        or len({normalize_claim(s.record.statement) for s in sources}) < 2
        or any(_unhedged_claim(text) == _unhedged_claim(s.record.statement) for s in sources)
    ):
        return None
    evidence_at = max(at for at in original_times if at is not None)
    expiry = min(
        evidence_at + timedelta(days=30), seed.expires_at or evidence_at + timedelta(days=30)
    )
    if expiry <= now:
        return None
    clause = SummaryClause(text=text, source_ids=ids)
    plan = seed.plan.model_copy(
        update={
            "clauses": (
                SummaryClauseRef(digest=hashlib.sha256(text.encode()).hexdigest(), source_ids=ids),
            ),
            "omitted_source_ids": (),
        }
    )
    return seed.model_copy(
        update={
            "kind": "hypothesis",
            "subject": "Tentative connection",
            "plan": plan,
            "clauses": (clause,),
            "authority": MemoryAuthority.INFERRED,
            "confidence": min(0.35, seed.confidence),
            "last_evidence_at": evidence_at,
            "valid_from": formed_at or now,
            "expires_at": expiry,
        }
    )
