"""Governed deterministic first implementation of memory formation."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.events import EventEnvelope, NewEvent, ProcessEvent
from agent_core.domain.memory import (
    BeliefRejection,
    BeliefType,
    ConsolidationResult,
    ConsolidationRun,
    MemoryAuthority,
    MemoryCandidate,
    MemoryDiagnosis,
    MemoryEdit,
    MemoryExtractionResult,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RecallTrace,
    RejectionKind,
    Sensitivity,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.memory.profiles import DEFAULT_FORMATION_PROFILE, FormationProfile
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.memory import MemoryCandidateExtractor
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

FORMATION_POLICY_VERSION = "formation@5"
MAX_AUTOMATIC_CANDIDATES = 12
MAX_EXTRACTOR_PROPOSALS = 256
MAX_INFERRED_CONFIDENCE = 0.55
SESSION_IDLE_SECONDS = 30
PROVIDER_RETRY_BACKOFF_SECONDS = (60, 300)
PROVIDER_MAX_ATTEMPTS = 1 + len(PROVIDER_RETRY_BACKOFF_SECONDS)
_SECRET = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential|bearer)\s*[:=]\s*\S+",
    re.I,
)
_INJECTION = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"<\s*/?\s*(?:system|memory|untrusted)|override\s+(?:policy|instructions))",
    re.I,
)
_TRANSIENT = re.compile(r"\b(?:right now|this turn|temporary|today only)\b", re.I)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;\r\n]+|,\s+(?:and\s+)?(?=(?:i|we)\b)|\s+and\s+(?=(?:i|we)\b)",
    re.I,
)
_ITEM_BOUNDARY = re.compile(r"\s*(?:,\s*(?:and\s+)?|\s+and\s+)\s*", re.I)
_OWNERSHIP_VERBS = {
    "have": "has",
    "own": "owns",
    "use": "uses",
    "wear": "wears",
    "drive": "drives",
}
_POSSESSIVE_ENTITY = re.compile(
    r"\bmy\s+((?:[A-Z][A-Za-z0-9'-]*|[A-Z0-9][A-Za-z0-9'-]*)"
    r"(?:\s+(?:[A-Z][A-Za-z0-9'-]*|[A-Z0-9][A-Za-z0-9'-]*)){0,3})"
)
_GROUNDING_TOKEN = re.compile(r"\d+(?:\.\d+)+|[a-z0-9]+(?:['-][a-z0-9]+)*")
_STARTED_ACTIVITY = re.compile(
    r"(?:i['\u2019]ve|i\s+have)\s+started\s+(?P<verb>[a-z]+ing)\s+"
    r"(?:the\s+)?(?P<object>.+?)(?:\s+after\s+(?P<prior>.+))?",
    re.I,
)
_PRIOR_EXPERIENCE = re.compile(
    r"(?P<duration>.+?)\s+of\s+(?P<verb>[a-z]+ing)\s+(?:the\s+)?(?P<object>.+)",
    re.I,
)
_RECURRING_SYMPTOM = re.compile(
    r"(?:on\s+(?:the\s+)?(?P<context>.+?)\s+)?my\s+(?P<body>.+?)\s+is\s+"
    r"(?P<frequency>often|frequently|repeatedly)\s+"
    r"(?P<symptom>hurting|aching|tingling)\s+after\s+"
    r"(?P<duration>.+?)(?:\s+of\s+(?P<activity>.+))?",
    re.I,
)
_SYMPTOM_VERBS = {"hurting": "hurts", "aching": "aches", "tingling": "tingles"}


def contains_memory_injection(value: str) -> bool:
    """Return whether memory-shaped text contains a prompt-injection marker."""

    return _INJECTION.search(value) is not None


def contains_automatic_memory_hazard(value: str) -> bool:
    """Return whether authoritative source text is unsafe for automatic formation."""

    return (
        _SECRET.search(value) is not None
        or contains_memory_injection(value)
        or _TRANSIENT.search(value) is not None
    )


def grounding_tokens(value: str) -> set[str]:
    """Normalize words and dotted numbers for memory-source grounding checks."""

    tokens = set(_GROUNDING_TOKEN.findall(value.casefold()))
    tokens.update(token[:-2] for token in tuple(tokens) if token.endswith("'s"))
    return tokens


def _event_text(event: EventEnvelope) -> str:
    content = event.payload.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for raw in content:
            if not isinstance(raw, dict):
                continue
            try:
                part = TextPart.model_validate(raw)
            except ValueError:
                continue
            texts.append(part.text)
    return "\n".join(texts).strip()


def _clean_object(value: str) -> tuple[str, str] | None:
    clean = value.strip(" \t\n\r,.:;!?")
    if not clean or re.match(r"^(?:it|this|that|something|anything)$", clean, re.I):
        return None
    article = ""
    match = re.match(r"^(a|an|the|my)\s+(.+)$", clean, re.I)
    if match is not None:
        article = match.group(1).casefold()
        clean = match.group(2).strip()
    if not clean or clean.casefold().startswith(("question", "problem")):
        return None
    if article in {"a", "an"}:
        rendered = f"{article} {clean}"
    elif clean[0].casefold() in {"a", "e", "i", "o", "u"}:
        rendered = f"an {clean}"
    else:
        rendered = f"a {clean}"
    return clean, rendered


def _preference_subject(value: str) -> str:
    lowered = value.casefold()
    if "metric" in lowered or "imperial" in lowered:
        return "measurement units"
    if "concise" in lowered or "detailed" in lowered or "answer" in lowered:
        return "answer style"
    if "dark mode" in lowered or "light mode" in lowered or "theme" in lowered:
        return "interface theme"
    if "tabs" in lowered or "spaces" in lowered or "indent" in lowered:
        return "indentation style"
    words = re.findall(r"[a-z0-9'-]+", lowered)
    topic = words[-1] if words else "general"
    if topic.endswith("ies") and len(topic) > 3:
        topic = f"{topic[:-3]}y"
    elif topic.endswith("s") and not topic.endswith("ss") and len(topic) > 3:
        topic = topic[:-1]
    return f"{topic} preference"


class DeterministicCandidateExtractor:
    """Bounded first-person extractor used before model-assisted extraction is enabled."""

    name = "deterministic-formation-v2"

    def __init__(self, maximum_candidates: int = MAX_EXTRACTOR_PROPOSALS) -> None:
        if maximum_candidates < 1:
            raise ValueError("maximum memory candidates must be positive")
        if maximum_candidates > MAX_EXTRACTOR_PROPOSALS:
            raise ValueError(f"maximum memory candidates must not exceed {MAX_EXTRACTOR_PROPOSALS}")
        self._maximum_candidates = maximum_candidates

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        seen: set[tuple[str, str, int]] = set()
        for event in events:
            if (
                event.event_type != "user.message.created"
                or event.actor_type != "principal"
                or event.actor_id != principal.principal_id
            ):
                continue
            text = _event_text(event)
            for candidate in self._from_text(event.sequence, text, scope):
                key = (
                    candidate.subject.casefold(),
                    candidate.statement.casefold(),
                    event.sequence,
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
                if len(candidates) >= self._maximum_candidates:
                    return candidates
        return candidates

    def _from_text(self, sequence: int, text: str, scope: str) -> list[MemoryCandidate]:
        proposed: list[MemoryCandidate] = []
        retracted_subjects: set[str] = set()
        proposed_subjects: set[str] = set()
        recent_activity_subject: str | None = None

        def add(
            *,
            subject: str,
            statement: str,
            belief_type: BeliefType,
            portability: Portability | None = None,
            sensitivity: Sensitivity = Sensitivity.INTERNAL,
            confidence: float = 0.72,
            polarity: Polarity = Polarity.ASSERT,
        ) -> None:
            proposed_subjects.add(subject.casefold())
            proposed.append(
                MemoryCandidate(
                    belief_type=belief_type,
                    subject=subject,
                    statement=statement,
                    polarity=polarity,
                    source_event_ids=[sequence],
                    model_confidence=confidence,
                    proposed_scope=scope,
                    proposed_portability=portability or portability_ceiling(belief_type),
                    sensitivity_guess=sensitivity,
                )
            )

        stripped = text.strip()
        if stripped.casefold().startswith("remember that "):
            add(
                subject="user",
                statement=stripped[len("remember that ") :].strip(),
                belief_type=BeliefType.FACT,
                confidence=0.9,
            )

        for raw_clause in _CLAUSE_BOUNDARY.split(stripped):
            clause = re.sub(r"^and\s+", "", raw_clause.strip(), flags=re.I)
            if not clause:
                continue

            started_activity = _STARTED_ACTIVITY.fullmatch(clause)
            if started_activity is not None:
                verb = started_activity.group("verb").casefold()
                activity_object = started_activity.group("object").strip(" ,.:;!?")
                recent_activity_subject = activity_object
                add(
                    subject=activity_object,
                    statement=f"User started {verb} {activity_object}.",
                    belief_type=BeliefType.USER_MODEL_ATTR,
                    confidence=0.8,
                )
                prior = started_activity.group("prior")
                prior_experience = None if prior is None else _PRIOR_EXPERIENCE.fullmatch(prior)
                if prior_experience is not None:
                    duration = prior_experience.group("duration").strip(" ,.:;!?")
                    prior_verb = prior_experience.group("verb").casefold()
                    prior_object = prior_experience.group("object").strip(" ,.:;!?")
                    add(
                        subject=f"{prior_object} experience",
                        statement=(
                            f"User has {duration} of experience {prior_verb} {prior_object}."
                        ),
                        belief_type=BeliefType.USER_MODEL_ATTR,
                        confidence=0.82,
                    )
                continue

            recurring_symptom = _RECURRING_SYMPTOM.fullmatch(clause)
            if recurring_symptom is not None:
                body = recurring_symptom.group("body").strip(" ,.:;!?")
                frequency = recurring_symptom.group("frequency").casefold()
                symptom = recurring_symptom.group("symptom").casefold()
                duration = recurring_symptom.group("duration").strip(" ,.:;!?")
                activity = recurring_symptom.group("activity")
                context = recurring_symptom.group("context")
                activity_phrase = "" if activity is None else activity.strip(" ,.:;!?")
                if recent_activity_subject is not None and context is not None:
                    normalized_context = context.strip(" ,.:;!?").casefold()
                    normalized_recent = recent_activity_subject.casefold()
                    if normalized_context in normalized_recent.split():
                        if not activity_phrase:
                            activity_phrase = f"using {recent_activity_subject}"
                        elif activity_phrase.casefold() == "playing":
                            activity_phrase = f"playing {recent_activity_subject}"
                suffix = "" if not activity_phrase else f" of {activity_phrase}"
                subject_suffix = "" if not activity_phrase else f" while {activity_phrase}"
                add(
                    subject=f"{body} pain{subject_suffix}",
                    statement=(
                        f"User's {body} {frequency} {_SYMPTOM_VERBS[symptom]} after "
                        f"{duration}{suffix}."
                    ),
                    belief_type=BeliefType.USER_MODEL_ATTR,
                    sensitivity=Sensitivity.SENSITIVE,
                    confidence=0.82,
                )
                continue

            preference = re.fullmatch(r"(?:i|we)\s+(?:really\s+)?prefer\s+(.+)", clause, re.I)
            if preference is not None:
                value = preference.group(1).strip(" ,.:;!?")
                add(
                    subject=_preference_subject(value),
                    statement=f"User prefers {value}.",
                    belief_type=BeliefType.PREFERENCE,
                    confidence=0.82,
                )
                continue

            retraction = re.fullmatch(
                r"i\s+(?:no\s+longer|do\s+not|don't)\s+(have|own|use|wear|drive)\s+(.+)",
                clause,
                re.I,
            )
            if retraction is not None:
                rendered_verb = _OWNERSHIP_VERBS[retraction.group(1).casefold()]
                for raw_item in _ITEM_BOUNDARY.split(retraction.group(2)):
                    cleaned = _clean_object(raw_item)
                    if cleaned is None:
                        continue
                    subject, rendered = cleaned
                    retracted_subjects.add(subject.casefold())
                    add(
                        subject=subject,
                        statement=f"User no longer {rendered_verb} {rendered}.",
                        belief_type=BeliefType.USER_MODEL_ATTR,
                        confidence=0.85,
                        polarity=Polarity.RETRACT,
                    )
                continue

            ownership = re.fullmatch(r"i\s+(have|own|use|wear|drive)\s+(.+)", clause, re.I)
            if ownership is not None:
                rendered_verb = _OWNERSHIP_VERBS[ownership.group(1).casefold()]
                for raw_item in _ITEM_BOUNDARY.split(ownership.group(2)):
                    cleaned = _clean_object(raw_item)
                    if cleaned is None:
                        continue
                    subject, rendered = cleaned
                    add(
                        subject=subject,
                        statement=f"User {rendered_verb} {rendered}.",
                        belief_type=BeliefType.USER_MODEL_ATTR,
                    )
                continue

            location = re.fullmatch(r"i\s+live\s+in\s+(.+)", clause, re.I)
            if location is not None:
                value = location.group(1).strip(" ,.:;!?")
                add(
                    subject="home location",
                    statement=f"User lives in {value}.",
                    belief_type=BeliefType.USER_MODEL_ATTR,
                    sensitivity=Sensitivity.SENSITIVE,
                    confidence=0.8,
                )
                continue

            employment = re.fullmatch(r"i\s+work\s+(?:at|for)\s+(.+)", clause, re.I)
            if employment is not None:
                value = employment.group(1).strip(" ,.:;!?")
                add(
                    subject="employment",
                    statement=f"User works at {value}.",
                    belief_type=BeliefType.USER_MODEL_ATTR,
                    sensitivity=Sensitivity.SENSITIVE,
                    confidence=0.8,
                )
                continue

            relationship = re.fullmatch(
                r"my\s+(spouse|wife|husband|partner|mother|father|son|daughter)\s+is\s+(.+)",
                clause,
                re.I,
            )
            if relationship is None:
                relationship = re.match(
                    r"my\s+(?:(?:\d{1,3})(?:\s+|-)years?(?:\s+|-)old(?:\s+|-))?"
                    r"(spouse|wife|husband|partner|mother|father|son|daughter)"
                    r"\s*,\s*([^,.;!?]+?)\s*,",
                    clause,
                    re.I,
                )
            if relationship is not None:
                relation = relationship.group(1).casefold()
                value = relationship.group(2).strip(" ,.:;!?")
                if not re.match(
                    r"(?:[A-Z][A-Za-z0-9'-]*|\d{1,3}(?:\s+|-)years?\s+old)\b",
                    value,
                ):
                    statement = (
                        f"User has at least one {relation}."
                        if relation in {"daughter", "son"}
                        else f"User has a {relation}."
                    )
                    add(
                        subject=relation,
                        statement=statement,
                        belief_type=BeliefType.RELATIONSHIP,
                        sensitivity=Sensitivity.SENSITIVE,
                        confidence=0.8,
                    )
                    continue
                add(
                    subject=relation,
                    statement=f"User's {relation} is {value}.",
                    belief_type=BeliefType.RELATIONSHIP,
                    sensitivity=Sensitivity.SENSITIVE,
                    confidence=0.8,
                )
                continue

            decision = re.fullmatch(r"we\s+(?:decided|agreed|chose)\s+(.+)", clause, re.I)
            if decision is not None:
                value = decision.group(1).strip(" ,.:;!?")
                add(
                    subject="project decision",
                    statement=f"The team decided {value}.",
                    belief_type=BeliefType.FACT,
                    portability=Portability.LOCAL,
                )
                continue

            outcome = re.fullmatch(
                r"we\s+(shipped|launched|completed|finished)\s+(.+)", clause, re.I
            )
            if outcome is not None:
                verb = outcome.group(1).casefold()
                value = outcome.group(2).strip(" ,.:;!?")
                add(
                    subject="task outcome",
                    statement=f"The team {verb} {value}.",
                    belief_type=BeliefType.FACT,
                    portability=Portability.LOCAL,
                )

        plural_children = re.compile(r"\bboth\s+of\s+my\s+(daughters|sons)\b", re.I)
        for match in plural_children.finditer(stripped):
            plural = match.group(1).casefold()
            singular = plural[:-1]
            if singular in proposed_subjects or plural in proposed_subjects:
                continue
            add(
                subject=plural,
                statement=f"User has at least two {plural}.",
                belief_type=BeliefType.RELATIONSHIP,
                sensitivity=Sensitivity.SENSITIVE,
                confidence=0.8,
            )

        relation_mentions = re.compile(
            r"\bmy\s+(spouse|wife|husband|partner|mother|father|son|daughter)"
            r"(?:['\u2019]s)?\b",
            re.I,
        )
        for match in relation_mentions.finditer(stripped):
            relation = match.group(1).casefold()
            if relation in proposed_subjects:
                continue
            statement = (
                f"User has at least one {relation}."
                if relation in {"daughter", "son"}
                else f"User has a {relation}."
            )
            add(
                subject=relation,
                statement=statement,
                belief_type=BeliefType.RELATIONSHIP,
                sensitivity=Sensitivity.SENSITIVE,
                confidence=0.8,
            )

        for match in _POSSESSIVE_ENTITY.finditer(stripped):
            cleaned = _clean_object(match.group(1))
            if cleaned is None:
                continue
            subject, rendered = cleaned
            normalized_subject = subject.casefold()
            if normalized_subject in retracted_subjects or normalized_subject in proposed_subjects:
                continue
            add(
                subject=subject,
                statement=f"User has {rendered}.",
                belief_type=BeliefType.USER_MODEL_ATTR,
                confidence=0.68,
            )
        return proposed


class DeterministicSalience:
    def eligible(self, statement: str, *, explicit: bool) -> bool:
        value = statement.strip()
        if not value or _SECRET.search(value) is not None or contains_memory_injection(value):
            return False
        if not explicit and _TRANSIENT.search(value) is not None:
            return False
        return explicit or len(value.split()) >= 3


class DeterministicConflictResolver:
    @staticmethod
    def _normalized(value: str) -> str:
        return " ".join(value.casefold().split())

    def relationship(
        self, existing: MemoryRecord, statement: str, source_event_ids: list[int]
    ) -> str:
        if set(source_event_ids).issubset(existing.source_event_ids):
            return "same_source"
        if self._normalized(existing.statement) == self._normalized(statement):
            return "duplicate"
        return "contradiction"


def portability_ceiling(belief_type: BeliefType) -> Portability:
    if belief_type in {
        BeliefType.PREFERENCE,
        BeliefType.USER_MODEL_ATTR,
        BeliefType.PROCEDURE_POINTER,
    }:
        return Portability.PORTABLE
    return Portability.CONTEXTUAL


def _portability_allowed(proposed: Portability, ceiling: Portability) -> bool:
    order = {Portability.LOCAL: 0, Portability.CONTEXTUAL: 1, Portability.PORTABLE: 2}
    return order[proposed] <= order[ceiling]


class GovernedMemoryService:
    """Formation service and management surface over the structured store."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        *,
        salience: DeterministicSalience | None = None,
        resolver: DeterministicConflictResolver | None = None,
        extractor: MemoryCandidateExtractor | None = None,
        policy_version: str = FORMATION_POLICY_VERSION,
        formation_profile: FormationProfile = DEFAULT_FORMATION_PROFILE,
    ) -> None:
        if not policy_version:
            raise ValueError("memory formation policy version must not be empty")
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._salience = salience or DeterministicSalience()
        self._resolver = resolver or DeterministicConflictResolver()
        self._extractor = extractor or DeterministicCandidateExtractor()
        self._policy_version = policy_version
        self._profile = formation_profile

    @property
    def formation_profile(self) -> FormationProfile:
        """Expose the formation profile the composition wired in."""

        return self._profile

    @property
    def extractor_name(self) -> str:
        """Name the configured candidate extractor, as a consolidation records it."""

        return self._extractor.name

    async def remember(
        self,
        *,
        session_id: UUID,
        run_id: UUID | None,
        statement: str,
        subject: str,
        scope: str,
        belief_type: BeliefType = BeliefType.FACT,
        portability: Portability | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        source_event_ids: list[int] | None = None,
        origin_trust: TrustLevel = TrustLevel.USER,
        explicit: bool = True,
        authority: MemoryAuthority = MemoryAuthority.USER,
        polarity: Polarity = Polarity.ASSERT,
        confidence: float | None = None,
        trigger: str = "explicit",
    ) -> MemoryRecord:
        record, _action = await self._remember(
            session_id=session_id,
            run_id=run_id,
            statement=statement,
            subject=subject,
            scope=scope,
            belief_type=belief_type,
            portability=portability,
            sensitivity=sensitivity,
            source_event_ids=source_event_ids,
            origin_trust=origin_trust,
            explicit=explicit,
            authority=authority,
            polarity=polarity,
            confidence=confidence,
            valid_from=None,
            expires_at=None,
            trigger=trigger,
            record_audit=True,
        )
        return record

    async def _remember(
        self,
        *,
        session_id: UUID,
        run_id: UUID | None,
        statement: str,
        subject: str,
        scope: str,
        belief_type: BeliefType,
        portability: Portability | None,
        sensitivity: Sensitivity,
        source_event_ids: list[int] | None,
        origin_trust: TrustLevel,
        explicit: bool,
        authority: MemoryAuthority,
        polarity: Polarity,
        confidence: float | None,
        valid_from: datetime | None,
        expires_at: datetime | None,
        trigger: str,
        record_audit: bool,
        existing_uow: RepositoryUnitOfWork | None = None,
        audit_id: UUID | None = None,
    ) -> tuple[MemoryRecord, str]:
        if origin_trust not in {TrustLevel.USER, TrustLevel.MEMORY} or (
            origin_trust is TrustLevel.MEMORY and not explicit
        ):
            raise ToolTrustRejectedError("external content cannot directly write persistent memory")
        clean_statement = " ".join(statement.split())
        clean_subject = " ".join(subject.split())
        if not clean_subject or not self._salience.eligible(clean_statement, explicit=explicit):
            raise ToolValidationError("memory candidate failed eligibility and safety gates")
        effective_portability = portability or portability_ceiling(belief_type)
        if not _portability_allowed(effective_portability, portability_ceiling(belief_type)):
            raise ToolValidationError("memory portability exceeds the belief type ceiling")

        async def apply(uow: RepositoryUnitOfWork) -> tuple[MemoryRecord, str]:
            sources = source_event_ids or await self._latest_user_source(uow, session_id)
            await self._validate_sources(uow, session_id, sources)
            rejections = await uow.memories.outstanding_rejections(
                self._principal.tenant_id, self._principal.principal_id
            )
            statement_hash = hashlib.sha256(clean_statement.casefold().encode()).hexdigest()
            if any(rejection.statement_sha256 == statement_hash for rejection in rejections):
                raise ConflictError("a user deletion or correction blocks this memory")
            formation_run = ConsolidationRun(
                id=audit_id or self._ids.new_id(),
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                trigger=trigger,
                scope=scope,
                session_id=session_id,
                watermark_before=min(sources) - 1,
                watermark_after=max(sources),
                model=self._extractor.name,
                policy_version=self._policy_version,
                candidates_proposed=1,
                committed=0,
                reinforced=0,
                superseded=0,
                rejected=0,
                started_at=self._clock.now(),
            )
            related = await uow.memories.related(
                self._principal.tenant_id,
                self._principal.principal_id,
                clean_subject,
                belief_type,
            )
            for current in sorted(related, key=lambda item: item.store_position, reverse=True):
                relation = self._resolver.relationship(current, clean_statement, sources)
                if relation == "same_source":
                    return current, "unchanged"
                if relation == "duplicate":
                    position = await uow.memories.next_position()
                    origin_scopes = list(dict.fromkeys([*current.origin_scopes, scope]))
                    promoted = (
                        current.portability is not Portability.LOCAL and len(origin_scopes) >= 2
                    )
                    reinforced = current.model_copy(
                        update={
                            "scope": "user" if promoted else current.scope,
                            "origin_scopes": origin_scopes,
                            "source_event_ids": sorted(
                                set(current.source_event_ids).union(sources)
                            ),
                            "corroboration_count": current.corroboration_count + 1,
                            "confidence": min(1.0, current.confidence + 0.1),
                            "status": MemoryStatus.ACTIVE,
                            "last_reinforced_at": self._clock.now(),
                            "store_position": position,
                            "updated_at": self._clock.now(),
                        },
                        deep=True,
                    )
                    stored = await uow.memories.reinforce(reinforced)
                    await self._append_event(
                        uow,
                        session_id,
                        run_id,
                        "memory.promoted" if promoted else "memory.reinforced",
                        stored,
                    )
                    if record_audit:
                        await uow.memories.record_consolidation(
                            formation_run.model_copy(
                                update={
                                    "reinforced": 1,
                                    "finished_at": self._clock.now(),
                                }
                            )
                        )
                    return stored, "reinforced"
                if relation == "contradiction":
                    replacement = await self._new_record(
                        uow,
                        formation_run.id,
                        clean_statement,
                        clean_subject,
                        scope,
                        belief_type,
                        effective_portability,
                        sensitivity,
                        session_id,
                        sources,
                        explicit,
                        authority,
                        polarity,
                        confidence,
                        valid_from,
                        expires_at,
                    )
                    position = await uow.memories.next_position()
                    superseded = current.model_copy(
                        update={
                            "status": MemoryStatus.SUPERSEDED,
                            "valid_to": self._clock.now(),
                            "superseded_by": replacement.id,
                            "store_position": position,
                            "updated_at": self._clock.now(),
                        },
                        deep=True,
                    )
                    _old, stored = await uow.memories.supersede(superseded, replacement)
                    await self._append_event(uow, session_id, run_id, "memory.superseded", stored)
                    if record_audit:
                        await uow.memories.record_consolidation(
                            formation_run.model_copy(
                                update={
                                    "committed": 1,
                                    "superseded": 1,
                                    "finished_at": self._clock.now(),
                                }
                            )
                        )
                    return stored, "superseded"
            record = await self._new_record(
                uow,
                formation_run.id,
                clean_statement,
                clean_subject,
                scope,
                belief_type,
                effective_portability,
                sensitivity,
                session_id,
                sources,
                explicit,
                authority,
                polarity,
                confidence,
                valid_from,
                expires_at,
            )
            stored = await uow.memories.upsert_belief(record)
            await self._append_event(uow, session_id, run_id, "memory.formed", stored)
            if record_audit:
                await uow.memories.record_consolidation(
                    formation_run.model_copy(
                        update={"committed": 1, "finished_at": self._clock.now()}
                    )
                )
            return stored, "committed"

        if existing_uow is not None:
            return await apply(existing_uow)
        async with self._uow_factory() as uow:
            return await apply(uow)

    async def run(
        self,
        *,
        trigger: str,
        scope: str,
        session_id: UUID | None,
        since_watermark: int | None = None,
    ) -> ConsolidationResult:
        started_at = self._clock.now()
        if session_id is None:
            run = ConsolidationRun(
                id=self._ids.new_id(),
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                trigger=trigger,
                scope=scope,
                watermark_before=0,
                watermark_after=0,
                model=self._extractor.name,
                policy_version=self._policy_version,
                candidates_proposed=0,
                committed=0,
                reinforced=0,
                superseded=0,
                rejected=0,
                started_at=started_at,
                finished_at=self._clock.now(),
            )
            async with self._uow_factory() as uow:
                await uow.memories.record_consolidation(run)
            return ConsolidationResult(run=run)
        async with self._uow_factory() as uow:
            watermark = (
                await uow.memories.consolidation_watermark(session_id, self._principal)
                if since_watermark is None
                else since_watermark
            )
            events = await uow.events.list_after(session_id, watermark, self._principal)
        extracted = await self._extractor.extract(
            events,
            principal=self._principal,
            scope=scope,
        )
        provider_failure = (
            extracted.provider_failure if isinstance(extracted, MemoryExtractionResult) else None
        )
        candidates = extracted[:MAX_AUTOMATIC_CANDIDATES]
        trusted_user_sources = {
            event.sequence
            for event in events
            if event.event_type == "user.message.created"
            and event.actor_type == "principal"
            and event.actor_id == self._principal.principal_id
        }
        by_sequence = {event.sequence: event for event in events}
        after = max((event.sequence for event in events), default=watermark)
        formation_requests = [
            event for event in events if event.event_type == "memory.formation.requested"
        ]
        latest_request = formation_requests[-1] if formation_requests else None
        current_attempt = 1
        effective_trigger = trigger
        if latest_request is not None and latest_request.payload.get("trigger") == "provider_retry":
            effective_trigger = "provider_retry"
            raw_attempt = latest_request.payload.get("attempt_number")
            if isinstance(raw_attempt, int) and 1 <= raw_attempt <= PROVIDER_MAX_ATTEMPTS:
                current_attempt = raw_attempt
        should_retry = (
            since_watermark is None
            and provider_failure is not None
            and provider_failure.retryable
            and current_attempt < PROVIDER_MAX_ATTEMPTS
        )

        def no_work(at_watermark: int) -> ConsolidationResult:
            return ConsolidationResult(
                run=ConsolidationRun(
                    id=self._ids.new_id(),
                    tenant_id=self._principal.tenant_id,
                    principal_id=self._principal.principal_id,
                    trigger=trigger,
                    scope=scope,
                    session_id=session_id,
                    watermark_before=at_watermark,
                    watermark_after=at_watermark,
                    model=self._extractor.name,
                    policy_version=self._policy_version,
                    candidates_proposed=0,
                    committed=0,
                    reinforced=0,
                    superseded=0,
                    rejected=0,
                    started_at=started_at,
                    finished_at=self._clock.now(),
                )
            )

        async with self._uow_factory() as uow:
            acquired = await uow.maintenance.acquire_memory_session(self._principal, session_id)
            if not acquired:
                return no_work(watermark)
            try:
                current_watermark = await uow.memories.consolidation_watermark(
                    session_id, self._principal
                )
                if since_watermark is None and current_watermark != watermark:
                    return no_work(current_watermark)
                consolidation_id = self._ids.new_id()
                beliefs: list[MemoryRecord] = []
                rejected = len(extracted) - len(candidates)
                committed = 0
                reinforced = 0
                superseded = 0
                for candidate in candidates:
                    if (
                        candidate.proposed_scope != scope
                        or not set(candidate.source_event_ids) <= trusted_user_sources
                    ):
                        rejected += 1
                        continue
                    source_text = "\n".join(
                        _event_text(by_sequence[sequence])
                        for sequence in candidate.source_event_ids
                    )
                    if contains_automatic_memory_hazard(source_text):
                        rejected += 1
                        continue
                    source_event = by_sequence[candidate.source_event_ids[0]]
                    try:
                        belief, action = await self._remember(
                            session_id=session_id,
                            run_id=source_event.run_id,
                            statement=candidate.statement,
                            subject=candidate.subject,
                            scope=candidate.proposed_scope,
                            belief_type=candidate.belief_type,
                            portability=candidate.proposed_portability,
                            sensitivity=candidate.sensitivity_guess,
                            source_event_ids=candidate.source_event_ids,
                            origin_trust=TrustLevel.USER,
                            explicit=False,
                            authority=MemoryAuthority.INFERRED,
                            polarity=candidate.polarity,
                            confidence=candidate.model_confidence,
                            valid_from=candidate.valid_from,
                            expires_at=candidate.expires_hint,
                            trigger=effective_trigger,
                            record_audit=False,
                            existing_uow=uow,
                            audit_id=consolidation_id,
                        )
                    except (ConflictError, ToolValidationError):
                        rejected += 1
                    else:
                        if action == "unchanged":
                            rejected += 1
                            continue
                        beliefs.append(belief)
                        if action == "reinforced":
                            reinforced += 1
                        else:
                            committed += 1
                            if action == "superseded":
                                superseded += 1
                watermark_after = watermark if should_retry else after
                if should_retry:
                    assert provider_failure is not None
                    next_attempt = current_attempt + 1
                    retry_at = self._clock.now() + timedelta(
                        seconds=PROVIDER_RETRY_BACKOFF_SECONDS[current_attempt - 1]
                    )
                    await uow.events.append(
                        NewEvent(
                            session_id=session_id,
                            run_id=next(
                                (
                                    event.run_id
                                    for event in reversed(events)
                                    if event.run_id is not None
                                ),
                                None,
                            ),
                            event_type="memory.formation.requested",
                            actor_type="memory_maintenance",
                            actor_id=self._principal.principal_id,
                            payload={
                                "trigger": "provider_retry",
                                "attempt_number": next_attempt,
                                "not_before": retry_at.isoformat(),
                                "source_watermark_before": watermark,
                                "source_watermark_after": after,
                                "failure_kind": provider_failure.failure_kind,
                            },
                            derivation_key=(
                                "memory.formation.provider_retry:"
                                f"{session_id}:{after}:{next_attempt}"
                            ),
                        )
                    )
                else:
                    await uow.memories.set_consolidation_watermark(
                        session_id, self._principal, after
                    )
                    if (
                        since_watermark is None
                        and provider_failure is not None
                        and provider_failure.retryable
                        and current_attempt >= PROVIDER_MAX_ATTEMPTS
                    ):
                        await uow.process_events.append(
                            ProcessEvent(
                                id=self._ids.new_id(),
                                event_type="memory.provider_extraction.retry_exhausted",
                                actor_type="memory_maintenance",
                                actor_id=self._principal.principal_id,
                                payload={
                                    "session_id": str(session_id),
                                    "tenant_id": self._principal.tenant_id,
                                    "principal_id": self._principal.principal_id,
                                    "attempt_number": current_attempt,
                                    "failure_kind": provider_failure.failure_kind,
                                    "provider_code": provider_failure.provider_code,
                                    "http_status": provider_failure.http_status,
                                    "provider_parameter": (provider_failure.provider_parameter),
                                    "stream_had_output": (provider_failure.stream_had_output),
                                },
                                derivation_key=(
                                    "memory.provider_extraction.retry_exhausted:"
                                    f"{session_id}:{after}:{current_attempt}"
                                ),
                                created_at=self._clock.now(),
                            )
                        )
                audit = ConsolidationRun(
                    id=consolidation_id,
                    tenant_id=self._principal.tenant_id,
                    principal_id=self._principal.principal_id,
                    trigger=effective_trigger,
                    scope=scope,
                    session_id=session_id,
                    watermark_before=watermark,
                    watermark_after=watermark_after,
                    model=self._extractor.name,
                    policy_version=self._policy_version,
                    candidates_proposed=len(extracted),
                    committed=committed,
                    reinforced=reinforced,
                    superseded=superseded,
                    rejected=rejected,
                    started_at=started_at,
                    finished_at=self._clock.now(),
                )
                await uow.memories.record_consolidation(audit)
            finally:
                await uow.maintenance.release_memory_session(self._principal, session_id)
        return ConsolidationResult(run=audit, beliefs=beliefs)

    async def diagnose(self, session_id: UUID) -> MemoryDiagnosis:
        """Return content-free formation evidence and governed beliefs for one session."""

        async with self._uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, self._principal)
            watermark = await uow.memories.consolidation_watermark(session_id, self._principal)
            consolidations = await uow.memories.list_consolidations(
                self._principal,
                session_id=session_id,
                limit=100,
            )
            beliefs = await uow.memories.list_memories(
                self._principal,
                include_inactive=True,
                session_id=session_id,
                limit=200,
            )
            completed_events = await uow.process_events.list("memory.provider_extraction.completed")
            failed_events = await uow.process_events.list("memory.provider_extraction.failed")
            selection_events = await uow.process_events.list("memory.provider_extraction.selection")
            process_events = sorted(
                [*completed_events, *failed_events, *selection_events],
                key=lambda event: (event.created_at, event.id.int),
            )
        formation_requests = [
            event for event in events if event.event_type == "memory.formation.requested"
        ]
        provider_attempts = [
            event
            for event in process_events
            if event.event_type
            in {"memory.provider_extraction.completed", "memory.provider_extraction.failed"}
            and event.payload.get("tenant_id") == self._principal.tenant_id
            and event.payload.get("principal_id") == self._principal.principal_id
            and event.payload.get("session_id") == str(session_id)
        ]
        selections = [
            event
            for event in process_events
            if event.event_type == "memory.provider_extraction.selection"
            and event.payload.get("tenant_id") == self._principal.tenant_id
            and event.payload.get("principal_id") == self._principal.principal_id
        ]
        latest_request = formation_requests[-1] if formation_requests else None
        return MemoryDiagnosis(
            session_id=session_id,
            watermark=watermark,
            formation_requests=formation_requests,
            provider_selection=selections[-1] if selections else None,
            provider_attempts=provider_attempts,
            consolidations=consolidations,
            beliefs=beliefs,
            pending_retry=(
                latest_request is not None
                and latest_request.sequence > watermark
                and latest_request.payload.get("trigger") == "provider_retry"
            ),
        )

    async def replay(self, session_id: UUID) -> ConsolidationResult:
        """Reprocess original session evidence without manufacturing memory provenance."""

        diagnosis = await self.diagnose(session_id)
        scope = diagnosis.consolidations[-1].scope if diagnosis.consolidations else "general"
        return await self.run(
            trigger="operator_replay",
            scope=scope,
            session_id=session_id,
            since_watermark=0,
        )

    async def list_memories(
        self,
        *,
        include_inactive: bool = False,
        session_id: UUID | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        async with self._uow_factory() as uow:
            return await uow.memories.list_memories(
                self._principal,
                include_inactive=include_inactive,
                session_id=session_id,
                limit=limit,
            )

    async def get_memory(self, belief_id: UUID) -> MemoryRecord:
        async with self._uow_factory() as uow:
            return await uow.memories.get(belief_id, self._principal)

    async def list_consolidations(
        self, *, session_id: UUID | None = None, limit: int = 100
    ) -> list[ConsolidationRun]:
        async with self._uow_factory() as uow:
            return await uow.memories.list_consolidations(
                self._principal,
                session_id=session_id,
                limit=limit,
            )

    async def get_recall_trace(self, trace_id: UUID) -> RecallTrace:
        async with self._uow_factory() as uow:
            return await uow.traces.get(trace_id, self._principal)

    async def edit(self, belief_id: UUID, edit: MemoryEdit) -> MemoryRecord:
        async with self._uow_factory() as uow:
            current = await uow.memories.get(belief_id, self._principal)
            position = await uow.memories.next_position()
            updated = current.model_copy(
                update={
                    "statement": edit.statement,
                    "sensitivity": edit.sensitivity or current.sensitivity,
                    "portability": edit.portability or current.portability,
                    "scope": edit.scope or current.scope,
                    "authority": MemoryAuthority.USER,
                    "source_event_ids": current.source_event_ids,
                    "store_position": position,
                    "updated_at": self._clock.now(),
                    "last_reinforced_at": self._clock.now(),
                },
                deep=True,
            )
            stored = await uow.memories.edit(belief_id, self._principal, edit, updated)
            await self._append_event(uow, _source_session(current), None, "memory.edited", stored)
            return stored

    async def delete(self, belief_id: UUID, *, trace_id: UUID | None = None) -> None:
        async with self._uow_factory() as uow:
            current = await uow.memories.get(belief_id, self._principal)
            tombstone = BeliefRejection(
                id=self._ids.new_id(),
                tenant_id=current.tenant_id,
                principal_id=current.principal_id,
                belief_id=current.id,
                kind=RejectionKind.DELETED,
                subject=current.subject,
                statement=None,
                statement_sha256=hashlib.sha256(current.statement.casefold().encode()).hexdigest(),
                belief_type=current.belief_type,
                scope=current.scope,
                trace_id=trace_id,
                created_at=self._clock.now(),
            )
            await uow.memories.delete(belief_id, self._principal, tombstone)
            await self._append_event(uow, _source_session(current), None, "memory.deleted", current)

    async def reject(
        self,
        belief_id: UUID,
        kind: RejectionKind,
        *,
        replacement_statement: str | None = None,
        trace_id: UUID | None = None,
    ) -> MemoryRecord:
        if kind is RejectionKind.DELETED:
            await self.delete(belief_id, trace_id=trace_id)
            raise NotFoundError("memory was deleted")
        async with self._uow_factory() as uow:
            current = await uow.memories.get(belief_id, self._principal)
            position = await uow.memories.next_position()
            update: dict[str, object] = {
                "flagged_for_review": True,
                "confidence": max(0, current.confidence - 0.2),
                "store_position": position,
                "updated_at": self._clock.now(),
            }
            if kind is RejectionKind.UNTRUE:
                update.update({"status": MemoryStatus.RETIRED, "valid_to": self._clock.now()})
            elif kind is RejectionKind.NOT_HERE:
                update["portability"] = Portability.LOCAL
            elif kind is RejectionKind.CHANGED:
                if replacement_statement is None:
                    raise ToolValidationError("changed rejection requires replacement text")
                update.update({"status": MemoryStatus.SUPERSEDED, "valid_to": self._clock.now()})
            updated = current.model_copy(update=update, deep=True)
            rejection = BeliefRejection(
                id=self._ids.new_id(),
                tenant_id=current.tenant_id,
                principal_id=current.principal_id,
                belief_id=current.id,
                kind=kind,
                subject=current.subject,
                statement=current.statement,
                statement_sha256=hashlib.sha256(current.statement.casefold().encode()).hexdigest(),
                belief_type=current.belief_type,
                scope=current.scope,
                trace_id=trace_id,
                created_at=self._clock.now(),
            )
            stored = await uow.memories.reject(rejection, updated)
            await self._append_event(uow, _source_session(current), None, "memory.rejected", stored)
        if kind is RejectionKind.CHANGED and replacement_statement is not None:
            replacement = await self.remember(
                session_id=_source_session(current),
                run_id=None,
                statement=replacement_statement,
                subject=current.subject,
                scope=current.scope,
                belief_type=current.belief_type,
                portability=current.portability,
                sensitivity=current.sensitivity,
                source_event_ids=current.source_event_ids,
                origin_trust=TrustLevel.USER,
                explicit=True,
            )
            linked = stored.model_copy(
                update={"superseded_by": replacement.id, "updated_at": self._clock.now()},
                deep=True,
            )
            linked_rejection = rejection.model_copy(update={"replacement_id": replacement.id})
            async with self._uow_factory() as uow:
                await uow.memories.reject(linked_rejection, linked)
            return replacement
        return stored

    async def expire(self) -> list[MemoryRecord]:
        async with self._uow_factory() as uow:
            return await uow.memories.expire(self._principal)

    async def _new_record(
        self,
        uow: RepositoryUnitOfWork,
        formation_run_id: UUID,
        statement: str,
        subject: str,
        scope: str,
        belief_type: BeliefType,
        portability: Portability,
        sensitivity: Sensitivity,
        source_session_id: UUID,
        source_event_ids: list[int],
        explicit: bool,
        authority: MemoryAuthority,
        polarity: Polarity,
        confidence: float | None,
        valid_from: datetime | None,
        expires_at: datetime | None,
    ) -> MemoryRecord:
        now = self._clock.now()
        effective_confidence = confidence if confidence is not None else 0.9
        if not explicit:
            effective_confidence = min(effective_confidence, MAX_INFERRED_CONFIDENCE)
        return MemoryRecord(
            id=self._ids.new_id(),
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            scope=scope,
            subject=subject,
            statement=statement,
            source_session_id=source_session_id,
            source_event_ids=sorted(set(source_event_ids)),
            confidence=effective_confidence,
            sensitivity=sensitivity,
            valid_from=valid_from or now,
            expires_at=expires_at,
            status=MemoryStatus.ACTIVE if explicit else MemoryStatus.PROVISIONAL,
            belief_type=belief_type,
            polarity=polarity,
            portability=portability,
            origin_scopes=[scope],
            last_reinforced_at=now,
            flagged_for_review=(
                not explicit and sensitivity in {Sensitivity.SENSITIVE, Sensitivity.RESTRICTED}
            ),
            formation_run_id=formation_run_id,
            consolidation_policy_version=self._policy_version,
            authority=authority,
            store_position=await uow.memories.next_position(),
            created_at=now,
            updated_at=now,
        )

    async def _latest_user_source(self, uow: RepositoryUnitOfWork, session_id: UUID) -> list[int]:
        events = await uow.events.list_after(session_id, 0, self._principal)
        user_events = [
            event.sequence for event in events if event.event_type == "user.message.created"
        ]
        if not user_events:
            raise ToolValidationError("memory write requires a user-source event")
        return [user_events[-1]]

    async def _validate_sources(
        self, uow: RepositoryUnitOfWork, session_id: UUID, sources: list[int]
    ) -> None:
        if not sources:
            raise ToolValidationError("memory provenance cannot be empty")
        existing = await uow.events.existing_sequences(session_id, set(sources), self._principal)
        if existing != set(sources):
            raise ToolValidationError("memory provenance names missing source events")

    async def _append_event(
        self,
        uow: RepositoryUnitOfWork,
        session_id: UUID,
        run_id: UUID | None,
        event_type: str,
        belief: MemoryRecord,
    ) -> None:
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=run_id,
                event_type=event_type,
                actor_type="principal" if belief.authority is MemoryAuthority.USER else "memory",
                actor_id=self._principal.principal_id,
                payload={"belief": belief.model_dump(mode="json")},
            )
        )


def _source_session(record: MemoryRecord) -> UUID:
    return record.source_session_id
