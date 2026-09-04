"""Governed deterministic first implementation of memory formation."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from contextlib import suppress
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.context import WorkingState
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.events import EventEnvelope, NewEvent, ProcessEvent
from agent_core.domain.hazards import INJECTION_PATTERN, SECRET_PATTERN
from agent_core.domain.memory import (
    LIFECYCLE_POLICY_VERSION,
    MEMORY_SUBJECT_MAX_LENGTH,
    SENSITIVITY_ORDER,
    BeliefRejection,
    BeliefType,
    ConsolidationResult,
    ConsolidationRun,
    DecayResult,
    EvidenceSpan,
    MemoryAuthority,
    MemoryCandidate,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryDiagnosis,
    MemoryEdit,
    MemoryExtractionResult,
    MemoryLongevity,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RecallTrace,
    RejectionKind,
    Sensitivity,
    UsageFeedback,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.persona import (
    PERSONA_ENTRY_MAX_CHARS,
    PersonaNomination,
    PersonaNominationState,
)
from agent_core.domain.policies import TrustLevel
from agent_core.memory.equivalence import content_terms
from agent_core.memory.profiles import (
    DEFAULT_FORMATION_PROFILE,
    DEFAULT_RETRIEVAL_PROFILE,
    DecayTauDays,
    FormationProfile,
    PersonaNominationProfile,
    UsageDeltas,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.memory import MemoryCandidateExtractor
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

FORMATION_POLICY_VERSION = "formation@7"
NEMORI_FORMATION_POLICY_VERSION = "formation@9"
HIGH_RECALL_EXTRACTOR_VERSION = "nemori-deterministic-fallback-v1"
MAX_AUTOMATIC_CANDIDATES = 12
MAX_NEMORI_AUTOMATIC_CANDIDATES = 32
MAX_NEMORI_CANDIDATES_PER_SOURCE = 6
MAX_EXTRACTOR_PROPOSALS = 256
MAX_INFERRED_CONFIDENCE = 0.55
SESSION_IDLE_SECONDS = 30
# How many recall traces one maintenance pass may strip of their operator tier.
TRACE_EXPIRY_SWEEP_LIMIT = 500
PROVIDER_RETRY_BACKOFF_SECONDS = (60, 300)
PROVIDER_MAX_ATTEMPTS = 1 + len(PROVIDER_RETRY_BACKOFF_SECONDS)


def _provider_repass_floor(requests: list[EventEnvelope], watermark: int) -> int | None:
    """The watermark a pending provider re-pass must read from, if it names one below."""

    if not requests:
        return None
    latest = requests[-1]
    floor = latest.payload.get("source_watermark_before")
    if (
        latest.payload.get("trigger") == "provider_retry"
        and isinstance(floor, int)
        and not isinstance(floor, bool)
        and 0 <= floor < watermark
    ):
        return floor
    return None


WORKING_STATE_EVENT = "context.working_state.updated"
# Who is speaking, in the order resolution trusts them: what the user said
# outranks what the agent concluded, which outranks what an extractor guessed.
_AUTHORITY_RANK = {
    MemoryAuthority.INFERRED: 0,
    MemoryAuthority.AFFIRMED: 1,
    MemoryAuthority.USER: 2,
}

_FUTURE_USEFULNESS = {
    MemoryClaimKind.ONGOING_PROJECT: 1.0,
    MemoryClaimKind.GOAL: 0.95,
    MemoryClaimKind.CONSTRAINT: 0.92,
    MemoryClaimKind.ROLE: 0.9,
    MemoryClaimKind.RELATIONSHIP: 0.88,
    MemoryClaimKind.PREFERENCE: 0.86,
    MemoryClaimKind.SKILL: 0.84,
    MemoryClaimKind.HABIT: 0.8,
    MemoryClaimKind.RECURRING_STATE: 0.78,
    MemoryClaimKind.INTEREST: 0.72,
    MemoryClaimKind.RESOURCE: 0.62,
    MemoryClaimKind.PROJECT_FACT: 0.55,
}
_BELIEF_TYPE_BY_CLAIM_KIND = {
    MemoryClaimKind.RELATIONSHIP: BeliefType.RELATIONSHIP,
    MemoryClaimKind.PREFERENCE: BeliefType.PREFERENCE,
    MemoryClaimKind.RESOURCE: BeliefType.PROCEDURE_POINTER,
    MemoryClaimKind.PROJECT_FACT: BeliefType.FACT,
}

logger = logging.getLogger(__name__)


_DIRECT_LONGEVITY_BY_CLAIM_KIND: dict[MemoryClaimKind, MemoryLongevity] = {
    MemoryClaimKind.ONGOING_PROJECT: MemoryLongevity.ONGOING,
    MemoryClaimKind.GOAL: MemoryLongevity.ONGOING,
    MemoryClaimKind.ROLE: MemoryLongevity.DURABLE,
    MemoryClaimKind.SKILL: MemoryLongevity.DURABLE,
    MemoryClaimKind.INTEREST: MemoryLongevity.DURABLE,
    MemoryClaimKind.HABIT: MemoryLongevity.ONGOING,
    MemoryClaimKind.CONSTRAINT: MemoryLongevity.DURABLE,
    MemoryClaimKind.RECURRING_STATE: MemoryLongevity.ONGOING,
    MemoryClaimKind.RELATIONSHIP: MemoryLongevity.DURABLE,
    MemoryClaimKind.PREFERENCE: MemoryLongevity.DURABLE,
    MemoryClaimKind.RESOURCE: MemoryLongevity.DURABLE,
    MemoryClaimKind.PROJECT_FACT: MemoryLongevity.ONGOING,
}


def _belief_type_for_claim_kind(claim_kind: MemoryClaimKind) -> BeliefType:
    return _BELIEF_TYPE_BY_CLAIM_KIND.get(claim_kind, BeliefType.USER_MODEL_ATTR)


# The citation form the renderer emits, read back exactly as it is written:
# eight lower-case hex digits of the belief identifier (retrieval.py:576).
_CITED_BELIEF = re.compile(r"\[m:([0-9a-f]{8})\]")
_SECRET = SECRET_PATTERN
_INJECTION = INJECTION_PATTERN
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
# Subject derivation for an established fact. The trim set is the punctuation
# that can sit around a word; the terminator set is the punctuation that ends
# an entity span; the openers are the words a sentence capitalizes without
# naming anything.
_SUBJECT_TRIM = " \t\"'\u2018\u2019\u201c\u201d()[]{}.,:;!?"
_SUBJECT_TERMINATORS = ".,:;!?\"'\u2019)]}"
_SUBJECT_LIMIT = 512
_SENTENCE_OPENERS = frozenset(
    {
        "a",
        "after",
        "all",
        "an",
        "and",
        "any",
        "as",
        "at",
        "before",
        "both",
        "but",
        "by",
        "during",
        "each",
        "every",
        "for",
        "he",
        "her",
        "here",
        "his",
        "i",
        "if",
        "in",
        "it",
        "its",
        "my",
        "no",
        "not",
        "of",
        "on",
        "one",
        "our",
        "she",
        "some",
        "that",
        "the",
        "their",
        "them",
        "these",
        "they",
        "this",
        "those",
        "to",
        "us",
        "user",
        "we",
        "when",
        "while",
        "with",
        "you",
        "your",
    }
)


def contains_secret_material(value: str) -> bool:
    """True when a statement carries credential-shaped material.

    The system prompt must not contain secrets, and the persona is system
    prompt: every persona write surface refuses on this check before
    persistence (persona-surface.md).
    """

    return _SECRET.search(value) is not None


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


def _select_nemori_candidates(
    proposals: list[tuple[MemoryCandidate, MemoryAuthority]],
) -> list[tuple[MemoryCandidate, MemoryAuthority]]:
    """Choose a broad, useful formation@9 batch without silent truncation."""

    ranked = sorted(
        enumerate(proposals),
        key=lambda indexed: (
            indexed[1][0].derivation is MemoryDerivation.HYPOTHESIS,
            -_FUTURE_USEFULNESS[indexed[1][0].claim_kind],
            min(indexed[1][0].source_event_ids),
            indexed[1][0].subject.casefold(),
            indexed[1][0].claim_kind.value,
            indexed[1][0].statement.casefold(),
            indexed[0],
        ),
    )
    chosen: list[tuple[MemoryCandidate, MemoryAuthority]] = []
    chosen_indices: set[int] = set()
    source_counts: dict[int, int] = {}
    subjects: set[str] = set()
    kinds: set[MemoryClaimKind] = set()

    def take(index: int, proposal: tuple[MemoryCandidate, MemoryAuthority]) -> bool:
        candidate, _authority = proposal
        if len(chosen) >= MAX_NEMORI_AUTOMATIC_CANDIDATES or any(
            source_counts.get(source_id, 0) >= MAX_NEMORI_CANDIDATES_PER_SOURCE
            for source_id in candidate.source_event_ids
        ):
            return False
        chosen.append(proposal)
        chosen_indices.add(index)
        subjects.add(candidate.subject.casefold())
        kinds.add(candidate.claim_kind)
        for source_id in candidate.source_event_ids:
            source_counts[source_id] = source_counts.get(source_id, 0) + 1
        return True

    # Diversity pass: a novel subject or category earns one slot before the
    # rank-ordered fill. The rank still keeps direct observations ahead of
    # hypotheses and useful durable/ongoing claims ahead of incidental facts.
    for index, proposal in ranked:
        candidate, _authority = proposal
        if candidate.subject.casefold() not in subjects or candidate.claim_kind not in kinds:
            take(index, proposal)
    for index, proposal in ranked:
        if index not in chosen_indices:
            take(index, proposal)
    return chosen


def _nemori_displacement_counts(
    proposals: list[tuple[MemoryCandidate, MemoryAuthority]],
    selected: list[tuple[MemoryCandidate, MemoryAuthority]],
) -> Counter[str]:
    """Classify each capacity displacement by the limit that excluded it."""

    remaining = list(selected)
    displaced: list[tuple[MemoryCandidate, MemoryAuthority]] = []
    for proposal in proposals:
        selected_index = next(
            (
                index
                for index, current in enumerate(remaining)
                if current[0] is proposal[0] and current[1] is proposal[1]
            ),
            None,
        )
        if selected_index is None:
            displaced.append(proposal)
        else:
            remaining.pop(selected_index)
    if not displaced:
        return Counter()
    if len(selected) >= MAX_NEMORI_AUTOMATIC_CANDIDATES:
        return Counter({"displaced_global": len(displaced)})
    source_counts: Counter[int] = Counter(
        source_id for candidate, _authority in selected for source_id in candidate.source_event_ids
    )
    reasons: Counter[str] = Counter()
    for candidate, _authority in displaced:
        reason = (
            "displaced_per_source"
            if any(
                source_counts[source_id] >= MAX_NEMORI_CANDIDATES_PER_SOURCE
                for source_id in candidate.source_event_ids
            )
            else "displaced_global"
        )
        reasons[reason] += 1
    return reasons


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


def _fact_subject(statement: str) -> str:
    """Name the subject of one established fact, from the statement alone.

    The subject is the first capitalized entity span in the statement. A word
    that only opens the sentence is capitalized by grammar rather than by
    being a name, so the closed set below is skipped in that position and
    nowhere else. A statement with no entity span falls back to its first
    three words, which keeps the derivation total and dependent on nothing but
    the text, so the same fact always reaches the same belief.
    """

    tokens = statement.split()
    for index, token in enumerate(tokens):
        bare = token.strip(_SUBJECT_TRIM)
        if not bare[:1].isupper():
            continue
        if index == 0 and bare.casefold() in _SENTENCE_OPENERS:
            continue
        span = [bare]
        cursor = index
        while (
            cursor + 1 < len(tokens)
            and tokens[cursor].strip(_SUBJECT_TERMINATORS) == tokens[cursor]
        ):
            following = tokens[cursor + 1].strip(_SUBJECT_TRIM)
            if not following[:1].isupper():
                break
            span.append(following)
            cursor += 1
        return " ".join(span)[:_SUBJECT_LIMIT]
    return " ".join(token.strip(_SUBJECT_TRIM) for token in tokens[:3]).strip()[:_SUBJECT_LIMIT]


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
                    evidence_spans=[EvidenceSpan(source_event_id=sequence, text=statement)],
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
                    context_terms = set(re.findall(r"[a-z0-9]+", context.casefold())) - {"the"}
                    recent_terms = set(re.findall(r"[a-z0-9]+", recent_activity_subject.casefold()))
                    if context_terms and context_terms <= recent_terms:
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


_ONGOING_BUILD = re.compile(
    r"\b(?:i\s+am|i['\u2019]m|we\s+are|we['\u2019]re)\s+"
    r"(?P<activity>building|creating|developing|working\s+on)\s+"
    r"(?P<object>.{1,160}?)"
    r"(?=\s+(?:and|but|because|while)\s+(?:i|we)\b|[.!?]|$)",
    re.IGNORECASE,
)
_SOFTWARE_PROJECT_CUE = re.compile(
    r"\b(?:ai\s+agent|agent|api|app|application|code|platform|service|software|website)\b",
    re.IGNORECASE,
)
# A clause ends at sentence punctuation, or at a comma or "and" that begins a
# new first-person clause. A period is a boundary only before whitespace or the
# end of the text and never after a common abbreviation, so a path, a version
# number, a decimal, or "e.g." stays inside its clause. Shared with the
# distiller's coverage ledger so both sides count the same clauses.
SOURCE_CLAUSE_BOUNDARY = re.compile(
    r"[!?;\r\n]+"
    r"|(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bdr)(?<!\bmr)(?<!\bms)(?<!\bmrs)"
    r"\.(?=\s|$)"
    r"|,\s+(?:and\s+)?(?=(?:i|we)\b)|\s+and\s+(?=(?:i|we)\b)",
    re.IGNORECASE,
)
_CLAUSE_STRIP = " \t\n\r,.:;!?"
_MAX_CLAUSE_CHARS = 512
_FREQUENCY = (
    r"(?:(?:\d{1,2}(?:\s*[-\u2013]\s*\d{1,2})?|once|twice|one|two|three|four|five|six|seven)"
    r"\s+(?:times?\s+)?(?:per|a|each|every)\s+(?:week|day|month|year|weekend)"
    r"|(?:every|each)\s+(?:day|morning|afternoon|evening|night|week|weekend|other\s+day"
    r"|weekday|sunday|monday|tuesday|wednesday|thursday|friday|saturday)"
    r"|daily|weekly|monthly|most\s+(?:days|mornings|evenings|weekends))"
)
_ACTIVITY_VERBS = frozenset(
    {
        "bake",
        "bike",
        "box",
        "climb",
        "code",
        "cook",
        "cycle",
        "dance",
        "draw",
        "fish",
        "garden",
        "golf",
        "hike",
        "jog",
        "journal",
        "kayak",
        "knit",
        "lift",
        "meditate",
        "paddle",
        "paint",
        "practice",
        "read",
        "row",
        "run",
        "sail",
        "sew",
        "sing",
        "skate",
        "ski",
        "sketch",
        "sprint",
        "stretch",
        "study",
        "surf",
        "swim",
        "train",
        "walk",
        "write",
    }
)
_ACTIVITY_VERB_ALTERNATION = "|".join(sorted(_ACTIVITY_VERBS, key=len, reverse=True))
_WEEKLY_ROUTINE = re.compile(
    r"\bi\s+(?:currently\s+|still\s+|usually\s+)?(?P<verb>do|follow|practice|perform|attend|take)"
    r"\s+(?:the\s+|a\s+|an\s+|my\s+)?(?P<routine>[^,;]{1,80}?)\s+(?P<frequency>"
    + _FREQUENCY
    + r")\b",
    re.IGNORECASE,
)
_ACTIVITY_FREQUENCY = re.compile(
    r"\bi\s+(?:currently\s+|still\s+|usually\s+)?(?P<verb>" + _ACTIVITY_VERB_ALTERNATION + r")"
    r"(?:\s+(?P<object>[^,;]{1,40}?))?\s+(?P<frequency>" + _FREQUENCY + r")\b",
    re.IGNORECASE,
)
_LEADING_FREQUENCY_HABIT = re.compile(
    r"\b(?P<frequency>" + _FREQUENCY + r")\s*,?\s*i\s+(?:usually\s+)?"
    r"(?P<verb>" + _ACTIVITY_VERB_ALTERNATION + r")(?:\s+(?P<object>[^,;]{1,40}?))?\s*$",
    re.IGNORECASE,
)
_ACTIVITY_LIST = re.compile(
    r"(?:\b(?P<context>(?:the\s+)?rest\s+of\s+(?:the\s+)?days|most\s+days|on\s+(?:the\s+)?"
    r"other\s+days|on\s+off\s+days|on\s+weekends|most\s+(?:mornings|evenings))\s*,?\s*)?"
    r"\bi\s+(?P<items>[a-z]+(?:\s*,\s*[a-z]+){0,6}\s*,?\s*(?:or|and)\s+[a-z]+)\b"
    r"(?P<tail>\s+most\s+(?:days|mornings|evenings)|\s+on\s+(?:the\s+)?(?:other|off)\s+days)?",
    re.IGNORECASE,
)
_FREQUENT_HABIT = re.compile(
    r"\bi\s+(?P<verb>" + _ACTIVITY_VERB_ALTERNATION + r"|review|check|go)\s+"
    r"(?P<value>[^,;]{0,80}?\b(?:every|each)\s+[^,;]{1,60})",
    re.IGNORECASE,
)
_TRAINED_REGULARLY = re.compile(
    r"\bi(?:['\u2019]ve|\s+have)\s+(?P<activity>trained|exercised|swum|run|cycled|lifted"
    r"|practiced|competed)\s+regularly\s+(?:for\s+)?(?P<span>most\s+of\s+my\s+life"
    r"|all\s+(?:of\s+)?my\s+life|my\s+whole\s+life|for\s+years|for\s+decades"
    r"|since\s+[^,;]{1,40})",
    re.IGNORECASE,
)
_PROGRESS_STATE = re.compile(
    r"\b(?:my\s+)?(?P<subject>progress|strength|weight|squat|bench|deadlift|mileage|pace"
    r"|endurance)\s+(?P<state>has\s+not\s+stalled|has\s+stalled|is\s+improving|is\s+stuck"
    r"|has\s+plateaued|is\s+stalling|is\s+increasing|is\s+decreasing)(?P<yet>\s+yet)?\b",
    re.IGNORECASE,
)
_RESTARTED_ACTIVITY = re.compile(
    r"\bi\s+(?:restarted|resumed|went\s+back\s+to|got\s+back\s+into)\s+"
    r"(?P<activity>[^,;]{1,40}?)\s+"
    r"(?P<when>(?:about\s+|around\s+)?(?:a|an|one|two|three|\d{1,2})\s+(?:year|month|week)s?"
    r"\s+ago)(?:\s+after\s+(?P<prior>[^,;]{1,80}))?",
    re.IGNORECASE,
)
_PRIOR_ACTIVITY_LEAD = re.compile(
    r"^(?:many|several|a\s+few|some|\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+(?:years?|months?|decades?)\s+of\s+",
    re.IGNORECASE,
)
_TRAINING_ACTIVITY_CUE = re.compile(
    r"\b(?:routine|program|programme|training|calisthenics|yoga|pilates|crossfit|5x5"
    r"|lifting|swimming|running|cycling|rowing|climbing|karate|judo|boxing|gymnastics"
    r"|gymnastic|martial|sport|drills|class|course|method|plan|diet|practice|regimen"
    r"|schedule|meditation|lessons?)\b",
    re.IGNORECASE,
)
_PAST_ACTIVITY_DURATION = re.compile(
    r"\bi\s+(?P<verb>did|practiced|trained\s+with|followed|used|played)\s+"
    r"(?:the\s+|a\s+|an\s+)?(?P<activity>[^,;]{1,60}?)\s+for\s+"
    r"(?P<duration>(?:about|around|approximately|roughly|nearly|almost|over)?\s*"
    r"(?:a|an|\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|several|many"
    r"|a\s+few)\s+(?:year|month|week|decade)s?)"
    r"(?:\s*\((?P<uncertainty>[^)]{1,80})\))?",
    re.IGNORECASE,
)
_UNCERTAINTY_CUE = re.compile(
    r"\b(?:forget|forgot|not\s+sure|unsure|don't\s+remember|do\s+not\s+remember|roughly)\b",
    re.IGNORECASE,
)
_FALSE_POSSESSION_PRESENT_PERFECT = re.compile(
    r"\bi\s+have\s+(?:trained|worked|studied|practiced|exercised|lived|run|swum|cycled)\b",
    re.IGNORECASE,
)
_FALSE_POSSESSION_EXPERIENCE_SUBJECT = re.compile(
    r"^(?:(?:[a-z]+|\d+)\s+years?\s+of\s+.+\s+experience)$",
    re.IGNORECASE,
)
_PRESENT_PERFECT_ACTIVITY_SUBJECT = re.compile(
    r"^(?:trained|worked|studied|practiced|exercised|lived|run|swum|cycled)\b",
    re.IGNORECASE,
)
_EXPLICIT_EXPERIENCE = re.compile(
    r"\bi\s+have\s+(?P<duration>(?:[a-z]+|\d{1,2})\s+years?\s+of\s+"
    r"(?P<skill>[^,;]{1,60}?)\s+experience)\b",
    re.IGNORECASE,
)
_AUTOMATIC_CORRECTION_CUE = re.compile(
    r"\b(?:no\s+longer|do\s+not|don't|never|stopped|quit|gave\s+up|given\s+up)\s+"
    r"(?:have|own|use|wear|drive|live|work|like|want|need|play|eat|drink|smoke|run|swim"
    r"|bike|train|go|take|attend|practice|do|follow|teach|coach|study|read|watch|visit"
    r"|cook|bake|lift|climb|ride|cycle|meditate|volunteer|commute)\b"
    r"|\b(?:stopped|quit|gave\s+up|given\s+up)\s+[a-z]+ing\b"
    r"|\b(?:not|don't|no\s+longer)\b[^.!?;]{0,40}\banymore\b"
    r"|\b(?:old\s+memory|memory\s+saying)\b.*\b(?:wrong|incorrect)\b",
    re.IGNORECASE,
)
# A stated end to an activity: "I gave up swimming", "I stopped attending yoga",
# "I no longer take meetings on Fridays". Ownership verbs are left to the
# completed extractor, whose retraction the fallback passes through.
_RETRACTION_LEAD = (
    r"\bi\s+(?:no\s+longer|don't|do\s+not|have\s+stopped|stopped|have\s+quit|quit"
    r"|gave\s+up|have\s+given\s+up)\s+"
)
_RETRACTED_ACTIVITY = re.compile(
    _RETRACTION_LEAD
    + r"(?P<verb>[a-z]+)(?:\s+(?P<object>[^,;]{1,80}?))?"
    + r"(?:\s+(?:anymore|any\s+more|now|these\s+days))?\s*$",
    re.IGNORECASE,
)
_RETRACTION_TRAILER = re.compile(r"\s+(?:anymore|any\s+more)(?=\.?$)", re.IGNORECASE)
_NON_RETRACTABLE_VERBS = frozenset(
    {
        "believe",
        "care",
        "expect",
        "feel",
        "get",
        "guess",
        "know",
        "like",
        "mean",
        "mind",
        "need",
        "remember",
        "see",
        "suppose",
        "think",
        "understand",
        "want",
        *_OWNERSHIP_VERBS,
    }
)
_DETERMINERS = frozenset({"a", "an", "the", "my", "our", "this", "that", "these", "those", "it"})
_RETRACTABLE_VERB_BASES = frozenset(
    {
        *_ACTIVITY_VERBS,
        "attend",
        "commute",
        "coach",
        "do",
        "drink",
        "eat",
        "follow",
        "go",
        "jog",
        "meditate",
        "play",
        "smoke",
        "take",
        "teach",
        "travel",
        "visit",
        "volunteer",
        "watch",
        "work",
    }
)
_LEADS_TEAM = re.compile(
    r"\bi\s+lead\s+(?:the|an?)\s+(?P<team>[^,;]{1,80}?\s+team)\b",
    re.IGNORECASE,
)
_CLUB_ROLE = re.compile(
    r"\bi(?:['\u2019]m|\s+am)\s+the\s+(?P<role>[^,;]{1,60}?)\s+for\s+"
    r"(?:our|the|a)\s+(?P<club>[^,;]{1,60}?\s+club)\b",
    re.IGNORECASE,
)
_PERSONAL_CONSTRAINT = re.compile(
    r"\bi\s+(?:cannot|can't|can\s+not)\s+(?P<value>[^,;]{1,120})",
    re.IGNORECASE,
)
_HELP_REQUEST_VERB = re.compile(
    r"^(?:open|find|see|get|figure|understand|access|log|load|remember|reproduce|make|tell"
    r"|seem|believe|wait|reach|connect|install|run|start|stop|fix|parse|read|download"
    r"|upload|view|locate|recall|think|decide)\b",
    re.IGNORECASE,
)
_REQUEST_SHAPED = re.compile(
    r"\b(?:can\s+you|could\s+you|would\s+you|please|help(?:\s+me)?|what\s+time|how\s+do\s+i"
    r"|right\s+now|today|tonight|tomorrow|yesterday|this\s+(?:pdf|file|error|question|message"
    r"|email|page|link|issue|bug|meeting|call|chat|conversation|recommendation)|that\s+(?:file"
    r"|error|question|message)|you|your|before\s+you)\b",
    re.IGNORECASE,
)
_UNIVERSAL_CONSTRAINT = re.compile(
    r"\ball\s+(?P<object>[^,;]{1,60}?)\s+must\s+(?P<requirement>[^,;]{1,120})",
    re.IGNORECASE,
)
_OWNED_RESOURCE_LOCATION = re.compile(
    r"\bour\s+(?P<resource>[^,;]{1,60}?)\s+(?:is|are|lives?)\s+in\s+"
    r"(?P<location>[A-Za-z][\w./:~-]{0,120})(?!\s*(?:minutes?|hours?|days?|weeks?|months?))",
    re.IGNORECASE,
)
_KEPT_RESOURCE = re.compile(
    r"\bi\s+keep\s+(?!(?:getting|having|seeing|running|trying|forgetting|hearing|thinking"
    r"|saying|telling|wondering|finding|losing|meaning)\b)"
    r"(?P<resource>[^,;]{1,60}?)\s+in\s+(?P<location>[^,;]{1,80})",
    re.IGNORECASE,
)
_IMPERATIVE_RESOURCE = re.compile(
    r"^(?:please\s+)?use\s+the\s+(?P<resource>[^,;]{1,60}?)\s+for\s+(?P<purpose>[^,;]{1,80})$",
    re.IGNORECASE,
)
_DIRECT_GOAL = re.compile(r"\bi\s+want\s+to\s+(?P<value>[^,;]{1,120})", re.IGNORECASE)
_NAMED_GOAL = re.compile(r"\bmy\s+goal\s+is\s+to\s+(?P<value>[^,;]{1,120})", re.IGNORECASE)
_TRANSIENT_GOAL_VERB = re.compile(
    r"^(?:know|understand|ask|check|see|find\s+out|make\s+sure|confirm|be\s+able|figure"
    r"|clarify|double[- ]check|verify|hear|talk|discuss|try|test)\b",
    re.IGNORECASE,
)
_IMPROVEMENT_QUESTION = re.compile(
    r"\b(?:what|how)\s+(?:would|could|can|should|might|do\s+i)\s+(?:be\s+)?(?:the\s+)?"
    r"(?:best\s+)?(?:way|modification|change|adjustment|tweak)?\s*(?:to\s+)?improve\s+"
    r"(?P<object>that|it|this|my\s+[^,;?]{1,60}|the\s+[^,;?]{1,60})",
    re.IGNORECASE,
)
_FACTOR_QUESTION = re.compile(
    r"\b(?:are|do|did|can|could|will|would|have)\s+you\s+"
    r"(?:factor(?:ing|ed)?|consider(?:ing|ed)?|account(?:ing|ed)?\s+for|tak(?:e|ing|en))"
    r"\s+(?:in\s+|into\s+account\s+)?my\s+"
    r"(?P<factor>age|budget|schedule|injur(?:y|ies)|experience|level|goals?|constraints?"
    r"|health|weight|height|location|timezone|time\s+zone|diet)\b",
    re.IGNORECASE,
)
_PROFESSIONAL_SKILL = re.compile(
    r"\bi\s+work\s+professionally\s+as\s+(?P<role>[^,;]{1,80})", re.IGNORECASE
)
_DIRECT_INTEREST = re.compile(
    r"\bi(?:['\u2019]m|\s+am)\s+(?:deeply\s+|really\s+|very\s+)?interested\s+in\s+"
    r"(?P<topic>[^,;]{1,80})",
    re.IGNORECASE,
)
_LEARNING_INTEREST = re.compile(
    r"\bi\s+love\s+learning\s+about\s+(?P<topic>[^,;]{1,80})", re.IGNORECASE
)
_BODY_RECURRING_STATE = re.compile(
    r"\bmy\s+(?P<body>[^,;]{1,40}?)\s+(?P<frequency>often|frequently|regularly)\s+"
    r"(?P<state>hurts|aches|tingles)\s+(?P<context>after\s+[^,;]{1,60}?)(?=\s+and\s+my\b|$)",
    re.IGNORECASE,
)
_SYSTEM_RECURRING_STATE = re.compile(
    r"\bthe\s+(?P<subject>[^,;]{1,40}?)\s+(?P<frequency>often|frequently|regularly)\s+"
    r"(?P<state>slows(?:\s+down)?|times\s+out)\s+(?P<context>after\s+[^,;]{1,60})",
    re.IGNORECASE,
)
_IMPLIED_PREFERENCE = re.compile(
    r"^(?P<value>[^,;]{1,80}?)\s+works\s+better\s+for\s+me\b", re.IGNORECASE
)
_PROJECT_USES = re.compile(r"\bthe\s+project\s+uses\s+(?P<technology>[^,;]{1,80})", re.IGNORECASE)
_PRODUCTION_DEPLOYS = re.compile(
    r"\bproduction\s+deploys\s+happen\s+through\s+(?P<system>[^,;]{1,80})",
    re.IGNORECASE,
)
_RESIDUAL_FIRST_PERSON = re.compile(
    r"\b(?:i|i'm|i've|i'd|i'll|my|me|mine|myself|you|your|yours|yourself|we|us|our|ours)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
    "10": "ten",
}


def split_source_clauses(text: str) -> list[str]:
    """Bounded first-person clauses of one user turn, each an exact substring."""

    clauses: list[str] = []
    for raw_clause in SOURCE_CLAUSE_BOUNDARY.split(text):
        clause = raw_clause.strip(_CLAUSE_STRIP)
        if clause:
            clauses.append(clause)
    return clauses


def _clause_starts(text: str, clauses: list[str]) -> list[int]:
    """The offset at which each clause begins, in order; each is an exact substring."""

    starts: list[int] = []
    position = 0
    for clause in clauses:
        found = text.find(clause, position)
        if found < 0:  # pragma: no cover - clauses are exact substrings of the text
            found = position
        starts.append(found)
        position = found + len(clause)
    return starts


def _clause_ordinal(clause_starts: list[int], offset: int) -> int:
    """The clause containing an offset; the first clause when nothing precedes it."""

    ordinal = 0
    for index, start in enumerate(clause_starts):
        if start <= offset:
            ordinal = index
    return ordinal


def _frequency_words(value: str) -> str:
    compact = " ".join(value.split())
    range_match = re.fullmatch(
        r"(\d{1,2})\s*[-\u2013]\s*(\d{1,2})\s+(times?\s+(?:per|a|each|every)\s+\w+)",
        compact,
        re.IGNORECASE,
    )
    if range_match is not None:
        lower, upper, rest = range_match.groups()
        return f"{_NUMBER_WORDS.get(lower, lower)} to {_NUMBER_WORDS.get(upper, upper)} {rest}"
    return re.sub(
        r"^(\d{1,2})\b",
        lambda match: _NUMBER_WORDS.get(match.group(1), match.group(1)),
        compact,
    )


def _third_person_verb(value: str) -> str:
    irregular = {"do": "does", "go": "goes", "have": "has", "be": "is"}
    lowered = value.casefold()
    if lowered in irregular:
        return irregular[lowered]
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        return f"{lowered}es"
    if lowered.endswith("y") and len(lowered) > 1 and lowered[-2] not in "aeiou":
        return f"{lowered[:-1]}ies"
    return f"{lowered}s"


def _gerund(value: str) -> str:
    lowered = value.casefold()
    irregular = {"run": "running", "swim": "swimming", "row": "rowing", "ski": "skiing"}
    if lowered in irregular:
        return irregular[lowered]
    if lowered.endswith("ie"):
        return f"{lowered[:-2]}ying"
    if lowered.endswith("e") and not lowered.endswith("ee"):
        return f"{lowered[:-1]}ing"
    if (
        len(lowered) >= 3
        and lowered[-1] not in "aeiouwxy"
        and lowered[-2] in "aeiou"
        and lowered[-3] not in "aeiou"
    ):
        return f"{lowered}{lowered[-1]}ing"
    return f"{lowered}ing"


_PRONOUN_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bi\s+am\b", re.IGNORECASE), "they are"),
    (re.compile(r"\bi'm\b", re.IGNORECASE), "they're"),
    (re.compile(r"\bi've\b", re.IGNORECASE), "they've"),
    (re.compile(r"\bi'd\b", re.IGNORECASE), "they'd"),
    (re.compile(r"\bi'll\b", re.IGNORECASE), "they'll"),
    (re.compile(r"\bi\b", re.IGNORECASE), "they"),
    (re.compile(r"\bmyself\b", re.IGNORECASE), "themselves"),
    (re.compile(r"\bmy\b", re.IGNORECASE), "their"),
    (re.compile(r"\bmine\b", re.IGNORECASE), "theirs"),
    (re.compile(r"\bme\b", re.IGNORECASE), "them"),
    (re.compile(r"\bwe\s+are\b", re.IGNORECASE), "they are"),
    (re.compile(r"\bwe're\b", re.IGNORECASE), "they're"),
    (re.compile(r"\bwe\b", re.IGNORECASE), "they"),
    (re.compile(r"\bourselves\b", re.IGNORECASE), "themselves"),
    (re.compile(r"\bour\b", re.IGNORECASE), "their"),
    (re.compile(r"\bours\b", re.IGNORECASE), "theirs"),
    (re.compile(r"\bus\b", re.IGNORECASE), "them"),
)


def _gerund_base(word: str) -> str | None:
    """The base verb of a gerund ("attending" gives "attend"), or nothing."""

    lowered = word.casefold()
    known = {_gerund(verb): verb for verb in _RETRACTABLE_VERB_BASES}
    if lowered in known:
        return known[lowered]
    if not lowered.endswith("ing") or len(lowered) < 6:
        return None
    base = lowered[:-3]
    if base[-1] == base[-2] and base[-1] not in "aeiou":
        base = base[:-1]
    return base


def _third_person(value: str) -> str:
    """Rewrite first-person pronouns so a rendered statement is about the user."""

    rendered = " ".join(value.split())
    for pattern, replacement in _PRONOUN_REWRITES:
        rendered = pattern.sub(replacement, rendered)
    return rendered


def _article(phrase: str) -> str:
    return "an" if phrase[:1].casefold() in "aeiou" else "a"


def _exact_evidence(event: EventEnvelope, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(
        source_event_id=event.sequence,
        text=_event_text(event)[start:end].strip(_CLAUSE_STRIP)[:8192],
    )


def _clause_evidence(event: EventEnvelope, clause: str, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(
        source_event_id=event.sequence,
        text=clause[start:end].strip(_CLAUSE_STRIP)[:8192],
    )


def contains_automatic_memory_correction(value: str) -> bool:
    """Identify correction/retraction text that may update but never create memory."""

    return _AUTOMATIC_CORRECTION_CUE.search(value) is not None


def _legacy_evidence_clause(event: EventEnvelope, candidate: MemoryCandidate) -> str:
    """The clause of the source turn that best supports a legacy candidate.

    The legacy extractor records provenance by event, not by span. Citing the
    whole turn would make every belief cite unrelated content, so the clause
    sharing the most content with the candidate is cited instead.
    """

    text = _event_text(event)
    clauses = split_source_clauses(text)
    if not clauses:
        return text[:_MAX_CLAUSE_CHARS]
    wanted = content_terms(f"{candidate.subject} {candidate.statement}")
    best = max(clauses, key=lambda clause: len(wanted & content_terms(clause)))
    return best[:_MAX_CLAUSE_CHARS]


def _render_retraction(
    event: EventEnvelope, clause: str, match: re.Match[str], scope: str
) -> MemoryCandidate | None:
    """Render "I gave up swimming" as a retraction of the swimming habit.

    The statement is the user's own claim negated, the subject is the thing
    given up, and the polarity tells consolidation to update rather than
    create. A lead followed by a determiner or a verb of thought ("I don't
    think", "I quit my job") is not an activity the fallback can name safely
    and renders nothing.
    """

    verb = match.group("verb").casefold()
    raw_object = " ".join((match.group("object") or "").split()).strip(_CLAUSE_STRIP)
    if verb in _NON_RETRACTABLE_VERBS or verb in _DETERMINERS or verb in _DIRECTION_WORDS:
        return None
    base = _gerund_base(verb)
    if base is None and verb.endswith("ing"):
        return None
    if base is None:
        if verb not in _RETRACTABLE_VERB_BASES:
            return None
        base = verb
    if raw_object and raw_object.split()[0].casefold() in _DETERMINERS - {"it"}:
        subject = " ".join(raw_object.split()[1:])
    else:
        subject = raw_object
    if raw_object and raw_object.casefold() in {"it", "that", "this"}:
        return None
    if not subject:
        subject = _gerund(base)
    rendered_object = f" {_third_person(raw_object)}" if raw_object else ""
    statement = f"User no longer {_third_person_verb(base)}{rendered_object}."
    body = statement.removeprefix("User ")
    if _RESIDUAL_FIRST_PERSON.search(body) is not None or _REQUEST_SHAPED.search(body) is not None:
        return None
    try:
        return MemoryCandidate(
            belief_type=_belief_type_for_claim_kind(MemoryClaimKind.HABIT),
            subject=subject[:MEMORY_SUBJECT_MAX_LENGTH],
            statement=statement[:8192],
            polarity=Polarity.RETRACT,
            source_event_ids=[event.sequence],
            model_confidence=0.65,
            proposed_scope=scope,
            proposed_portability=portability_ceiling(
                _belief_type_for_claim_kind(MemoryClaimKind.HABIT)
            ),
            sensitivity_guess=Sensitivity.INTERNAL,
            claim_kind=MemoryClaimKind.HABIT,
            derivation=MemoryDerivation.DIRECT,
            longevity=_DIRECT_LONGEVITY_BY_CLAIM_KIND[MemoryClaimKind.HABIT],
            evidence_spans=[
                EvidenceSpan(
                    source_event_id=event.sequence,
                    text=clause[match.start() : match.end()].strip(_CLAUSE_STRIP)[:8192],
                )
            ],
        )
    except ValueError:
        return None


_DIRECTION_WORDS = frozenset({"to", "for", "with", "at", "in", "on", "of", "up", "out"})


class HighRecallCandidateExtractor:
    """Deterministic formation@9 fallback with broad ongoing-activity recall.

    The completed deterministic extractor remains untouched and runs first.
    This additive fallback recognizes stated routines, activities, histories,
    roles, goals, constraints, resources, and recurring states as direct claims
    rendered from the user's own words, and emits inferences, such as a skill
    implied by a project or a preference implied by a question, only as
    tentative hypotheses. It is a safety net behind the provider, so it must
    never fabricate: every rendering is bounded, grounded in one clause, and
    dropped if it still speaks in the first or second person.
    """

    name = HIGH_RECALL_EXTRACTOR_VERSION

    def __init__(self, maximum_candidates: int = MAX_EXTRACTOR_PROPOSALS) -> None:
        if maximum_candidates < 1 or maximum_candidates > MAX_EXTRACTOR_PROPOSALS:
            raise ValueError(
                f"maximum memory candidates must be between 1 and {MAX_EXTRACTOR_PROPOSALS}"
            )
        self._maximum_candidates = maximum_candidates
        self._legacy = DeterministicCandidateExtractor(maximum_candidates=maximum_candidates)

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate]:
        by_sequence = {event.sequence: event for event in events}
        legacy = list(await self._legacy.extract(events, principal=principal, scope=scope))
        proposed: list[MemoryCandidate] = []
        for candidate in legacy:
            source = next(
                (
                    by_sequence[source_id]
                    for source_id in candidate.source_event_ids
                    if source_id in by_sequence
                ),
                None,
            )
            if source is None:
                continue
            clause = _legacy_evidence_clause(source, candidate)
            if candidate.polarity is Polarity.RETRACT:
                # A retraction is the one thing a correction clause may
                # produce: it updates the belief it names and, at commit,
                # never creates one. The completed extractor's rendering
                # keeps the user's "anymore"; the fallback drops it.
                proposed.append(
                    candidate.model_copy(
                        update={
                            "subject": _RETRACTION_TRAILER.sub("", candidate.subject),
                            "statement": _RETRACTION_TRAILER.sub("", candidate.statement),
                            "evidence_spans": [
                                EvidenceSpan(source_event_id=source.sequence, text=clause)
                            ],
                        }
                    )
                )
                continue
            if contains_automatic_memory_correction(clause):
                continue
            if candidate.statement.startswith("User has a ") and (
                (
                    _PRESENT_PERFECT_ACTIVITY_SUBJECT.match(candidate.subject) is not None
                    or _FALSE_POSSESSION_EXPERIENCE_SUBJECT.match(candidate.subject) is not None
                )
                and (
                    _FALSE_POSSESSION_PRESENT_PERFECT.search(_event_text(source)) is not None
                    or _EXPLICIT_EXPERIENCE.search(_event_text(source)) is not None
                )
            ):
                continue
            proposed.append(
                candidate.model_copy(
                    update={
                        "claim_kind": {
                            BeliefType.PREFERENCE: MemoryClaimKind.PREFERENCE,
                            BeliefType.RELATIONSHIP: MemoryClaimKind.RELATIONSHIP,
                            BeliefType.PROCEDURE_POINTER: MemoryClaimKind.RESOURCE,
                        }.get(candidate.belief_type, candidate.claim_kind),
                        "longevity": (
                            MemoryLongevity.DURABLE
                            if candidate.belief_type
                            in {
                                BeliefType.PREFERENCE,
                                BeliefType.RELATIONSHIP,
                                BeliefType.PROCEDURE_POINTER,
                            }
                            else candidate.longevity
                        ),
                        "evidence_spans": [
                            EvidenceSpan(source_event_id=source.sequence, text=clause)
                        ],
                    }
                )
            )
        seen = {
            (
                candidate.subject.casefold(),
                candidate.statement.casefold(),
                tuple(candidate.source_event_ids),
            )
            for candidate in proposed
        }

        def append(candidate: MemoryCandidate | None) -> bool:
            if candidate is None:
                return len(proposed) >= self._maximum_candidates
            key = (
                candidate.subject.casefold(),
                candidate.statement.casefold(),
                tuple(candidate.source_event_ids),
            )
            if key not in seen:
                seen.add(key)
                proposed.append(candidate)
            return len(proposed) >= self._maximum_candidates

        def render(
            event: EventEnvelope,
            *,
            subject: str,
            statement: str,
            claim_kind: MemoryClaimKind,
            evidence_spans: list[EvidenceSpan],
            derivation: MemoryDerivation = MemoryDerivation.DIRECT,
            polarity: Polarity = Polarity.ASSERT,
        ) -> MemoryCandidate | None:
            """Build one grounded candidate, or nothing when the rendering is unsafe."""

            compact_statement = " ".join(statement.split())
            compact_subject = " ".join(subject.split()).strip(_CLAUSE_STRIP)
            body = compact_statement.removeprefix("User's ").removeprefix("User ")
            if (
                not compact_subject
                or _RESIDUAL_FIRST_PERSON.search(body) is not None
                or _REQUEST_SHAPED.search(body) is not None
                or len(compact_statement.split()) < 3
            ):
                return None
            hypothesis = derivation is MemoryDerivation.HYPOTHESIS
            belief_type = _belief_type_for_claim_kind(claim_kind)
            try:
                return MemoryCandidate(
                    belief_type=belief_type,
                    subject=compact_subject[:MEMORY_SUBJECT_MAX_LENGTH],
                    statement=compact_statement[:8192],
                    polarity=polarity,
                    source_event_ids=[event.sequence],
                    model_confidence=0.35 if hypothesis else 0.65,
                    proposed_scope=scope,
                    proposed_portability=portability_ceiling(belief_type),
                    sensitivity_guess=Sensitivity.INTERNAL,
                    claim_kind=claim_kind,
                    derivation=derivation,
                    longevity=(
                        MemoryLongevity.TENTATIVE
                        if hypothesis
                        else _DIRECT_LONGEVITY_BY_CLAIM_KIND[claim_kind]
                    ),
                    evidence_spans=evidence_spans,
                )
            except ValueError:
                return None

        for event in events:
            if (
                event.event_type != "user.message.created"
                or event.actor_type != "principal"
                or event.actor_id != principal.principal_id
            ):
                continue
            text = _event_text(event)
            clauses = split_source_clauses(text)
            clause_starts = _clause_starts(text, clauses)
            # Subjects are recorded with the clause that stated them so that a
            # pronoun resolves only to the clause it follows: "that" after an
            # unrecognized sentence refers to that sentence, not to whatever
            # the fallback last happened to recognize.
            event_subjects: list[tuple[MemoryClaimKind, str, int]] = []
            current_clause = [-1]

            def note(
                candidate: MemoryCandidate | None,
                event_subjects: list[tuple[MemoryClaimKind, str, int]] = event_subjects,
                current_clause: list[int] = current_clause,
            ) -> bool:
                if candidate is not None:
                    event_subjects.append(
                        (candidate.claim_kind, candidate.subject, current_clause[0])
                    )
                return append(candidate)

            def adjacent_subjects(
                kinds: frozenset[MemoryClaimKind],
                event_subjects: list[tuple[MemoryClaimKind, str, int]] = event_subjects,
                current_clause: list[int] = current_clause,
            ) -> str | None:
                """The latest subject of a wanted kind from this clause or the one before."""

                return next(
                    (
                        subject
                        for claim_kind, subject, ordinal in reversed(event_subjects)
                        if claim_kind in kinds and ordinal >= current_clause[0] - 1
                    ),
                    None,
                )

            for match in _ONGOING_BUILD.finditer(text):
                activity = " ".join(match.group("activity").casefold().split())
                raw_object = match.group("object").strip(_CLAUSE_STRIP)
                if not raw_object or contains_automatic_memory_correction(match.group(0)):
                    continue
                evidence = _exact_evidence(event, match.start("activity"), match.end("object"))
                current_clause[0] = _clause_ordinal(clause_starts, match.start("activity"))
                subject = re.sub(r"^(?:a|an|the)\s+", "", raw_object, flags=re.IGNORECASE)
                rendered_activity = "building" if activity == "working on" else activity
                if note(
                    render(
                        event,
                        subject=subject,
                        statement=f"User is {rendered_activity} {_third_person(raw_object)}.",
                        claim_kind=MemoryClaimKind.ONGOING_PROJECT,
                        evidence_spans=[evidence],
                    )
                ):
                    return proposed
                if _SOFTWARE_PROJECT_CUE.search(raw_object) is not None and note(
                    render(
                        event,
                        subject="software-development experience",
                        statement="User likely has software-development experience.",
                        claim_kind=MemoryClaimKind.SKILL,
                        evidence_spans=[evidence],
                        derivation=MemoryDerivation.HYPOTHESIS,
                    )
                ):
                    return proposed

            for clause_ordinal, clause in enumerate(clauses):
                current_clause[0] = clause_ordinal
                retraction_match = _RETRACTED_ACTIVITY.fullmatch(clause)
                if retraction_match is not None:
                    retracted = _render_retraction(event, clause, retraction_match, scope)
                    if retracted is not None and append(retracted):
                        return proposed
                    # Whether or not it rendered, a correction clause forms
                    # nothing else.
                    continue
                if contains_automatic_memory_correction(clause):
                    continue

                def span(
                    match: re.Match[str],
                    group: str | int = 0,
                    event: EventEnvelope = event,
                    clause: str = clause,
                ) -> EvidenceSpan:
                    return _clause_evidence(event, clause, match.start(group), match.end(group))

                for match in _LEADS_TEAM.finditer(clause):
                    team = " ".join(match.group("team").split())
                    if note(
                        render(
                            event,
                            subject=f"{team} role",
                            statement=f"User leads {_article(team)} {_third_person(team)}.",
                            claim_kind=MemoryClaimKind.ROLE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _CLUB_ROLE.finditer(clause):
                    role = " ".join(match.group("role").split()).casefold()
                    club = " ".join(match.group("club").split())
                    if note(
                        render(
                            event,
                            subject=f"{club} role",
                            statement=f"User is {role} for a {_third_person(club)}.",
                            claim_kind=MemoryClaimKind.ROLE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _PERSONAL_CONSTRAINT.finditer(clause):
                    value = " ".join(match.group("value").split()).strip(_CLAUSE_STRIP)
                    if _HELP_REQUEST_VERB.match(value) is not None:
                        continue
                    if note(
                        render(
                            event,
                            subject=_third_person(value),
                            statement=f"User cannot {_third_person(value)}.",
                            claim_kind=MemoryClaimKind.CONSTRAINT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _UNIVERSAL_CONSTRAINT.finditer(clause):
                    object_ = " ".join(match.group("object").split()).casefold()
                    requirement = " ".join(match.group("requirement").split())
                    if note(
                        render(
                            event,
                            subject=f"{object_} requirement",
                            statement=(
                                f"User requires all {object_} to {_third_person(requirement)}."
                            ),
                            claim_kind=MemoryClaimKind.CONSTRAINT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _OWNED_RESOURCE_LOCATION.finditer(clause):
                    resource = " ".join(match.group("resource").split()).casefold()
                    location = match.group("location").rstrip(".")
                    if note(
                        render(
                            event,
                            subject=resource,
                            statement=f"The user's {resource} is in {location}.",
                            claim_kind=MemoryClaimKind.RESOURCE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _KEPT_RESOURCE.finditer(clause):
                    resource = " ".join(match.group("resource").split())
                    location = " ".join(match.group("location").split())
                    if note(
                        render(
                            event,
                            subject=_third_person(resource).casefold(),
                            statement=(
                                f"User keeps {_third_person(resource)} in "
                                f"{_third_person(location)}."
                            ),
                            claim_kind=MemoryClaimKind.RESOURCE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _IMPERATIVE_RESOURCE.finditer(clause):
                    resource = " ".join(match.group("resource").split())
                    purpose = " ".join(match.group("purpose").split())
                    if note(
                        render(
                            event,
                            subject=purpose.casefold(),
                            statement=f"The {resource} is used for {purpose}.",
                            claim_kind=MemoryClaimKind.RESOURCE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in list(_DIRECT_GOAL.finditer(clause)) + list(
                    _NAMED_GOAL.finditer(clause)
                ):
                    value = " ".join(match.group("value").split()).strip(_CLAUSE_STRIP)
                    if _TRANSIENT_GOAL_VERB.match(value) is not None:
                        continue
                    referent = adjacent_subjects(frozenset({MemoryClaimKind.ONGOING_PROJECT}))
                    if referent is not None:
                        value = re.sub(
                            r"\bit\b", f"the {referent}", value, count=1, flags=re.IGNORECASE
                        )
                    rendered = _third_person(value)
                    if note(
                        render(
                            event,
                            subject=rendered,
                            statement=f"User wants to {rendered}.",
                            claim_kind=MemoryClaimKind.GOAL,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _IMPROVEMENT_QUESTION.finditer(clause):
                    raw_object = match.group("object").strip(_CLAUSE_STRIP)
                    if raw_object.casefold() in {"that", "it", "this"}:
                        referent = adjacent_subjects(
                            frozenset(
                                {
                                    MemoryClaimKind.HABIT,
                                    MemoryClaimKind.ONGOING_PROJECT,
                                    MemoryClaimKind.RECURRING_STATE,
                                    MemoryClaimKind.RESOURCE,
                                    MemoryClaimKind.PROJECT_FACT,
                                }
                            )
                        )
                        if referent is None:
                            continue
                        target = f"their {referent}"
                    else:
                        target = _third_person(raw_object)
                    if note(
                        render(
                            event,
                            subject=(
                                f"{target.removeprefix('their ').removeprefix('the ')} improvement"
                            ),
                            statement=f"User wants to improve {target}.",
                            claim_kind=MemoryClaimKind.GOAL,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _FACTOR_QUESTION.finditer(clause):
                    factor = " ".join(match.group("factor").casefold().split())
                    if note(
                        render(
                            event,
                            subject=f"{factor}-aware recommendations",
                            statement=(
                                f"User wants recommendations that account for their {factor}."
                            ),
                            claim_kind=MemoryClaimKind.PREFERENCE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _PROFESSIONAL_SKILL.finditer(clause):
                    role = " ".join(match.group("role").split()).strip(_CLAUSE_STRIP)
                    if note(
                        render(
                            event,
                            subject=re.sub(r"^(?:a|an)\s+", "", role, flags=re.IGNORECASE),
                            statement=f"User works professionally as {_third_person(role)}.",
                            claim_kind=MemoryClaimKind.SKILL,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in list(_DIRECT_INTEREST.finditer(clause)) + list(
                    _LEARNING_INTEREST.finditer(clause)
                ):
                    topic = " ".join(match.group("topic").split()).strip(_CLAUSE_STRIP)
                    if note(
                        render(
                            event,
                            subject=_third_person(topic),
                            statement=f"User is interested in {_third_person(topic)}.",
                            claim_kind=MemoryClaimKind.INTEREST,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _WEEKLY_ROUTINE.finditer(clause):
                    verb = match.group("verb").casefold()
                    routine = " ".join(match.group("routine").split()).strip(_CLAUSE_STRIP)
                    frequency = _frequency_words(match.group("frequency"))
                    determiner = "the " if _TRAINING_ACTIVITY_CUE.search(routine) else ""
                    if note(
                        render(
                            event,
                            subject=_third_person(routine),
                            statement=(
                                f"User {_third_person_verb(verb)} {determiner}"
                                f"{_third_person(routine)} {frequency}."
                            ),
                            claim_kind=MemoryClaimKind.HABIT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                frequent_verbs: set[str] = set()
                for match in _FREQUENT_HABIT.finditer(clause):
                    verb = match.group("verb").casefold()
                    value = _third_person(match.group("value"))
                    object_ = re.split(r"\b(?:every|each)\b", value, maxsplit=1)[0].strip()
                    subject = f"{_gerund(verb)} {object_}".strip()
                    frequent_verbs.add(verb)
                    if note(
                        render(
                            event,
                            subject=subject,
                            statement=f"User {_third_person_verb(verb)} {value}.",
                            claim_kind=MemoryClaimKind.HABIT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _ACTIVITY_FREQUENCY.finditer(clause):
                    verb = match.group("verb").casefold()
                    if verb in frequent_verbs:
                        continue
                    object_ = " ".join((match.group("object") or "").split()).strip(_CLAUSE_STRIP)
                    frequency = _frequency_words(match.group("frequency"))
                    activity = f"{_third_person_verb(verb)} {_third_person(object_)}".strip()
                    if note(
                        render(
                            event,
                            subject=f"{_gerund(verb)} {_third_person(object_)}".strip(),
                            statement=f"User {activity} {frequency}.",
                            claim_kind=MemoryClaimKind.HABIT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _LEADING_FREQUENCY_HABIT.finditer(clause):
                    verb = match.group("verb").casefold()
                    if verb in frequent_verbs:
                        continue
                    object_ = " ".join((match.group("object") or "").split()).strip(_CLAUSE_STRIP)
                    frequency = _frequency_words(match.group("frequency"))
                    activity = f"{_third_person_verb(verb)} {_third_person(object_)}".strip()
                    if note(
                        render(
                            event,
                            subject=f"{_gerund(verb)} {_third_person(object_)}".strip(),
                            statement=f"User {activity} {frequency}.",
                            claim_kind=MemoryClaimKind.HABIT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _ACTIVITY_LIST.finditer(clause):
                    items = [
                        item.strip().casefold()
                        for item in re.split(
                            r"\s*,\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+", match.group("items")
                        )
                        if item.strip()
                    ]
                    if len(items) < 2 or not all(item in _ACTIVITY_VERBS for item in items):
                        continue
                    context = match.group("context") or (match.group("tail") or "").strip()
                    context = " ".join(context.split()).casefold()
                    if context.startswith("rest of") or context.startswith("the rest of"):
                        context = "on the rest of the days"
                    elif (
                        context
                        and not context.startswith("on ")
                        and not context.startswith("most ")
                    ):
                        context = f"on {context}"
                    list_evidence = span(match)
                    for item in items:
                        statement = (
                            f"User {_third_person_verb(item)} {context}."
                            if context
                            else f"User regularly {_third_person_verb(item)}."
                        )
                        if note(
                            render(
                                event,
                                subject=_gerund(item),
                                statement=statement,
                                claim_kind=MemoryClaimKind.HABIT,
                                evidence_spans=[list_evidence],
                            )
                        ):
                            return proposed

                for match in _TRAINED_REGULARLY.finditer(clause):
                    activity = match.group("activity").casefold()
                    span_text = _third_person(match.group("span"))
                    if not span_text.startswith(("for ", "since ")):
                        span_text = f"for {span_text}"
                    gerund = {"run": "running", "swum": "swimming"}.get(
                        activity, re.sub(r"(?:ed|d)$", "ing", activity)
                    )
                    if note(
                        render(
                            event,
                            subject=f"{gerund} history",
                            statement=f"User has {activity} regularly {span_text}.",
                            claim_kind=MemoryClaimKind.SKILL,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _PROGRESS_STATE.finditer(clause):
                    subject = match.group("subject").casefold()
                    state = " ".join(match.group("state").casefold().split())
                    yet = " yet" if match.group("yet") else ""
                    if note(
                        render(
                            event,
                            subject=f"training {subject}" if subject == "progress" else subject,
                            statement=f"User's {subject} {state}{yet}.",
                            claim_kind=MemoryClaimKind.RECURRING_STATE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _RESTARTED_ACTIVITY.finditer(clause):
                    activity = " ".join(match.group("activity").split()).strip(_CLAUSE_STRIP)
                    when = " ".join(match.group("when").casefold().split())
                    if note(
                        render(
                            event,
                            subject=f"{activity} history",
                            statement=f"User restarted {_third_person(activity)} {when}.",
                            claim_kind=MemoryClaimKind.SKILL,
                            evidence_spans=[
                                _clause_evidence(event, clause, match.start(), match.end("when"))
                            ],
                        )
                    ):
                        return proposed
                    prior = match.group("prior")
                    if prior:
                        prior = " ".join(prior.split()).strip(_CLAUSE_STRIP)
                        prior_activity = _PRIOR_ACTIVITY_LEAD.sub("", prior).strip()
                        if prior_activity and note(
                            render(
                                event,
                                subject=f"{prior_activity.casefold()} experience",
                                statement=(
                                    f"User did {_third_person(prior)} before restarting "
                                    f"{_third_person(activity)}."
                                ),
                                claim_kind=MemoryClaimKind.SKILL,
                                evidence_spans=[
                                    _clause_evidence(
                                        event, clause, match.start(), match.end("prior")
                                    )
                                ],
                            )
                        ):
                            return proposed

                for match in _PAST_ACTIVITY_DURATION.finditer(clause):
                    activity = " ".join(match.group("activity").split()).strip(_CLAUSE_STRIP)
                    if _TRAINING_ACTIVITY_CUE.search(activity) is None:
                        continue
                    verb = " ".join(match.group("verb").casefold().split())
                    duration = " ".join(match.group("duration").split()).strip(_CLAUSE_STRIP)
                    uncertainty = match.group("uncertainty")
                    statement = f"User {verb} the {_third_person(activity)} for {duration}"
                    duration_spans = [
                        _clause_evidence(event, clause, match.start(), match.end("duration"))
                    ]
                    if uncertainty and _UNCERTAINTY_CUE.search(uncertainty) is not None:
                        statement += " but is unsure exactly how long"
                        duration_spans.append(span(match, "uncertainty"))
                    subject = re.sub(
                        r"\s+(?:routine|program|programme|plan|class)$",
                        "",
                        activity,
                        flags=re.IGNORECASE,
                    ).casefold()
                    if note(
                        render(
                            event,
                            subject=subject,
                            statement=f"{statement}.",
                            claim_kind=MemoryClaimKind.SKILL,
                            evidence_spans=duration_spans,
                        )
                    ):
                        return proposed

                for match in _EXPLICIT_EXPERIENCE.finditer(clause):
                    duration = " ".join(match.group("duration").split())
                    skill = " ".join(match.group("skill").split())
                    if note(
                        render(
                            event,
                            subject=skill,
                            statement=f"User has {duration}.",
                            claim_kind=MemoryClaimKind.SKILL,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _BODY_RECURRING_STATE.finditer(clause):
                    body = " ".join(match.group("body").split())
                    frequency = match.group("frequency").casefold()
                    state = match.group("state").casefold()
                    context = " ".join(match.group("context").split())
                    if note(
                        render(
                            event,
                            subject=f"{body} {state}",
                            statement=(
                                f"User's {body} {frequency} {state} {_third_person(context)}."
                            ),
                            claim_kind=MemoryClaimKind.RECURRING_STATE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _SYSTEM_RECURRING_STATE.finditer(clause):
                    subject = " ".join(match.group("subject").split())
                    frequency = match.group("frequency").casefold()
                    state = re.sub(r"\s+down$", "", match.group("state"), flags=re.IGNORECASE)
                    context = " ".join(match.group("context").split())
                    if note(
                        render(
                            event,
                            subject=f"{subject} state",
                            statement=(
                                f"The user's {subject} {frequency} {state.casefold()} "
                                f"{_third_person(context)}."
                            ),
                            claim_kind=MemoryClaimKind.RECURRING_STATE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _IMPLIED_PREFERENCE.finditer(clause):
                    value = " ".join(match.group("value").split())
                    value = f"{value[:1].lower()}{value[1:]}"
                    if note(
                        render(
                            event,
                            subject=_third_person(value),
                            statement=f"User prefers {_third_person(value)}.",
                            claim_kind=MemoryClaimKind.PREFERENCE,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _PROJECT_USES.finditer(clause):
                    technology = " ".join(match.group("technology").split())
                    if note(
                        render(
                            event,
                            subject="project technology",
                            statement=f"The user's project uses {technology}.",
                            claim_kind=MemoryClaimKind.PROJECT_FACT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed

                for match in _PRODUCTION_DEPLOYS.finditer(clause):
                    system = " ".join(match.group("system").split())
                    if note(
                        render(
                            event,
                            subject="production deployment",
                            statement=f"The user's production deploys happen through {system}.",
                            claim_kind=MemoryClaimKind.PROJECT_FACT,
                            evidence_spans=[span(match)],
                        )
                    ):
                        return proposed
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
        self,
        existing: MemoryRecord,
        statement: str,
        source_event_ids: list[int],
        *,
        authority: MemoryAuthority = MemoryAuthority.USER,
        polarity: Polarity = Polarity.ASSERT,
        session_id: UUID | None = None,
        at: datetime | None = None,
    ) -> str:
        """Classify incoming evidence against one existing belief.

        The order is same source, duplicate, conflict, contradiction. Only the
        last one supersedes, and it is reached only when the incoming evidence
        outranks the existing belief or something orders the two in time.
        Polarity is accepted so a resolver may record what kind of statement it
        judged, and is deliberately never consulted: a retraction is a later
        statement about the same subject, so it is ordered by the same rules as
        any other, and treating disagreement itself as a conflict would leave
        every ordinary correction unresolved.
        """

        # Event sequences are allocated per session, so the same number means
        # the same episode only within one session. A caller that cannot name
        # the consolidating session keeps the legacy sequence-only comparison.
        same_session = session_id is None or existing.source_session_id == session_id
        if same_session and set(source_event_ids).issubset(existing.source_event_ids):
            return "same_source"
        if self._normalized(existing.statement) == self._normalized(statement):
            return "duplicate"
        if _AUTHORITY_RANK[authority] < _AUTHORITY_RANK[existing.authority]:
            return "conflict"
        if _AUTHORITY_RANK[authority] == _AUTHORITY_RANK[existing.authority] and not self._ordered(
            existing, source_event_ids, same_session=same_session, at=at
        ):
            return "conflict"
        return "contradiction"

    @staticmethod
    def _ordered(
        existing: MemoryRecord,
        source_event_ids: list[int],
        *,
        same_session: bool,
        at: datetime | None,
    ) -> bool:
        """Say whether the incoming evidence is later than the existing belief.

        Inside one session the event log orders the two, so a later sequence is
        later evidence. Across sessions only the clock can, and the two instants
        it compares are both evidence instants: the incoming one is when the
        statement was made, not when it is being consolidated, and the existing
        one is `valid_from`, when the belief's own evidence arrived. `updated_at`
        cannot stand in for it, because usage feedback, decay, and conflict
        linkage all write it long after the evidence landed. A caller that
        cannot name an instant keeps the ordering it had before this rule
        existed rather than turning every cross-session statement into a
        conflict.
        """

        if same_session:
            return max(source_event_ids) > max(existing.source_event_ids)
        return at is None or existing.valid_from < at


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
        decay_tau_days: DecayTauDays = DEFAULT_RETRIEVAL_PROFILE.decay_tau_days,
        usage: UsageDeltas = DEFAULT_RETRIEVAL_PROFILE.usage,
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
        # The time constants are the ranker's table: the age at which a belief
        # stops counting as reinforced is the age at which decay begins.
        self._decay_tau_days = decay_tau_days
        # Usage moves utility only. The deltas are the ranker's too, so what a
        # citation is worth is stated once, in the retrieval profile.
        self._usage = usage

    @property
    def formation_profile(self) -> FormationProfile:
        """Expose the formation profile the composition wired in."""

        return self._profile

    @property
    def extractor_name(self) -> str:
        """Name the configured candidate extractor, as a consolidation records it."""

        return self._extractor.name

    @property
    def extractor_audit(self) -> object | None:
        """The extractor's content-free audit of its latest extraction, if it keeps one."""

        return getattr(self._extractor, "last_audit", None)

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
        claim_kind: MemoryClaimKind | None = None,
        derivation: MemoryDerivation = MemoryDerivation.DIRECT,
        longevity: MemoryLongevity = MemoryLongevity.DURABLE,
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
            claim_kind=claim_kind,
            derivation=derivation,
            longevity=longevity,
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
        claim_kind: MemoryClaimKind | None = None,
        derivation: MemoryDerivation = MemoryDerivation.DIRECT,
        longevity: MemoryLongevity = MemoryLongevity.DURABLE,
        evidence_at: datetime | None = None,
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
        effective_claim_kind = claim_kind or {
            BeliefType.PREFERENCE: MemoryClaimKind.PREFERENCE,
            BeliefType.RELATIONSHIP: MemoryClaimKind.RELATIONSHIP,
            BeliefType.PROCEDURE_POINTER: MemoryClaimKind.RESOURCE,
        }.get(belief_type, MemoryClaimKind.PROJECT_FACT)
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
            classified = [
                (
                    current,
                    self._resolver.relationship(
                        current,
                        clean_statement,
                        sources,
                        authority=authority,
                        polarity=polarity,
                        session_id=session_id,
                        at=evidence_at or self._clock.now(),
                    ),
                )
                for current in sorted(related, key=lambda item: item.store_position, reverse=True)
            ]
            # A replay is a no-op whichever related belief the ordering reaches
            # first, so it is decided over all of them before any is acted on.
            # A conflict leaves both halves of the pair live and lifts the
            # existing belief above the replacement it just wrote, so
            # re-consolidating the same evidence meets the belief that was
            # contradicted before it meets the record that evidence produced.
            replay = next(
                (current for current, relation in classified if relation == "same_source"),
                None,
            )
            if replay is not None:
                return replay, "unchanged"
            promotable = next(
                (
                    current
                    for current in sorted(
                        related, key=lambda item: item.store_position, reverse=True
                    )
                    if current.derivation is MemoryDerivation.HYPOTHESIS
                    and derivation is MemoryDerivation.DIRECT
                    and current.polarity is Polarity.ASSERT
                    and polarity is Polarity.ASSERT
                    and _AUTHORITY_RANK[authority] >= _AUTHORITY_RANK[current.authority]
                ),
                None,
            )
            if promotable is not None:
                evidence_instant = evidence_at or self._clock.now()
                all_sources = sorted(set(promotable.source_event_ids).union(sources))
                promoted_expiry = expires_at
                if not explicit and promoted_expiry is None:
                    if longevity is MemoryLongevity.TENTATIVE:
                        promoted_expiry = evidence_instant + timedelta(days=30)
                    elif longevity is MemoryLongevity.ONGOING:
                        promoted_expiry = evidence_instant + timedelta(days=90)
                position = await uow.memories.next_position()
                promoted = promotable.model_copy(
                    update={
                        "statement": clean_statement,
                        "source_event_ids": all_sources,
                        "corroboration_count": promotable.corroboration_count + 1,
                        "claim_kind": effective_claim_kind,
                        "derivation": MemoryDerivation.DIRECT,
                        "longevity": longevity,
                        "last_evidence_at": max(promotable.last_evidence_at, evidence_instant),
                        "evidence_count": len(all_sources),
                        "lifecycle_policy_version": LIFECYCLE_POLICY_VERSION,
                        "confidence": min(
                            1.0,
                            max(
                                promotable.confidence + 0.2,
                                confidence or promotable.confidence,
                            ),
                        ),
                        "status": MemoryStatus.ACTIVE,
                        "expires_at": promoted_expiry,
                        "authority": authority,
                        "last_reinforced_at": self._clock.now(),
                        "store_position": position,
                        "updated_at": self._clock.now(),
                    },
                    deep=True,
                )
                stored = await uow.memories.reinforce(promoted)
                await self._append_event(uow, session_id, run_id, "memory.promoted", stored)
                if record_audit:
                    await uow.memories.record_consolidation(
                        formation_run.model_copy(
                            update={"reinforced": 1, "finished_at": self._clock.now()}
                        )
                    )
                return stored, "promoted"
            for current, relation in classified:
                if relation == "duplicate":
                    position = await uow.memories.next_position()
                    origin_scopes = list(dict.fromkeys([*current.origin_scopes, scope]))
                    portable_scope_promoted = (
                        current.portability is not Portability.LOCAL and len(origin_scopes) >= 2
                    )
                    reinforced = current.model_copy(
                        update={
                            "scope": "user" if portable_scope_promoted else current.scope,
                            "origin_scopes": origin_scopes,
                            "source_event_ids": sorted(
                                set(current.source_event_ids).union(sources)
                            ),
                            "corroboration_count": current.corroboration_count + 1,
                            "claim_kind": effective_claim_kind,
                            "derivation": (
                                MemoryDerivation.DIRECT
                                if derivation is MemoryDerivation.DIRECT
                                else current.derivation
                            ),
                            "longevity": (
                                longevity
                                if derivation is MemoryDerivation.DIRECT
                                else current.longevity
                            ),
                            "last_evidence_at": max(
                                current.last_evidence_at,
                                evidence_at or self._clock.now(),
                            ),
                            "evidence_count": len(set(current.source_event_ids).union(sources)),
                            "lifecycle_policy_version": LIFECYCLE_POLICY_VERSION,
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
                        ("memory.promoted" if portable_scope_promoted else "memory.reinforced"),
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
                if relation == "conflict":
                    # Nothing here is resolved by guessing. The incoming belief
                    # is committed beside the one it contradicts, both are
                    # linked and flagged, and the user is asked which holds.
                    proposed = await self._new_record(
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
                        effective_claim_kind,
                        derivation,
                        longevity,
                        evidence_at,
                    )
                    replacement = proposed.model_copy(
                        update={"flagged_for_review": True, "conflicts_with": [current.id]},
                        deep=True,
                    )
                    stored = await uow.memories.upsert_belief(replacement)
                    # The belief that was already stored has changed state, so
                    # it takes a fresh position: the next turn's recall delta
                    # is how the user learns that it is now disputed.
                    position = await uow.memories.next_position()
                    linked = current.model_copy(
                        update={
                            "conflicts_with": list(
                                dict.fromkeys([*current.conflicts_with, stored.id])
                            ),
                            "flagged_for_review": True,
                            "store_position": position,
                            "updated_at": self._clock.now(),
                        },
                        deep=True,
                    )
                    await uow.memories.reinforce(linked)
                    await self._append_event(uow, session_id, run_id, "memory.formed", stored)
                    await self._append_event(
                        uow, session_id, run_id, "memory.needs_confirmation", stored
                    )
                    if record_audit:
                        await uow.memories.record_consolidation(
                            formation_run.model_copy(
                                update={"committed": 1, "finished_at": self._clock.now()}
                            )
                        )
                    return stored, "conflicted"
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
                        effective_claim_kind,
                        derivation,
                        longevity,
                        evidence_at,
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
                effective_claim_kind,
                derivation,
                longevity,
                evidence_at,
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
            pending_requests = [
                event for event in events if event.event_type == "memory.formation.requested"
            ]
            # A formation@9 consolidation completes with its fallback and moves
            # the watermark, so a provider re-pass names the range it must read
            # again; the request itself sits above the watermark and is what
            # made the session due.
            repass_floor = _provider_repass_floor(pending_requests, watermark)
            if since_watermark is None and repass_floor is not None:
                events = await uow.events.list_after(session_id, repass_floor, self._principal)
        extracted = await self._extractor.extract(
            events,
            principal=self._principal,
            scope=scope,
        )
        provider_failure = (
            extracted.provider_failure if isinstance(extracted, MemoryExtractionResult) else None
        )
        trusted_user_sources = {
            event.sequence
            for event in events
            if event.event_type == "user.message.created"
            and event.actor_type == "principal"
            and event.actor_id == self._principal.principal_id
        }
        # Working state is formation's second input. Its facts are the agent's
        # own conclusions, so they enter ahead of the extractor's guesses and
        # may displace them; the displaced proposals are counted, not dropped.
        proposals: list[tuple[MemoryCandidate, MemoryAuthority]] = [
            (candidate, MemoryAuthority.AFFIRMED)
            for candidate in self._established_fact_candidates(events, scope, trusted_user_sources)
        ]
        proposals.extend((candidate, MemoryAuthority.INFERRED) for candidate in extracted)
        if self._policy_version == NEMORI_FORMATION_POLICY_VERSION:
            candidates = _select_nemori_candidates(proposals)
            displacement_counts = _nemori_displacement_counts(proposals, candidates)
        else:
            candidates = proposals[:MAX_AUTOMATIC_CANDIDATES]
            displaced_count = len(proposals) - len(candidates)
            displacement_counts = (
                Counter({"displaced_global": displaced_count}) if displaced_count else Counter()
            )
        by_sequence = {event.sequence: event for event in events}
        after = max((event.sequence for event in events), default=watermark)
        retry_attempts = [
            attempt
            for event in pending_requests
            if event.payload.get("trigger") == "provider_retry"
            and isinstance((attempt := event.payload.get("attempt_number")), int)
            and 1 <= attempt <= PROVIDER_MAX_ATTEMPTS
        ]
        current_attempt = 1
        effective_trigger = trigger
        if retry_attempts:
            effective_trigger = "provider_retry"
            current_attempt = max(retry_attempts)
        retryable_failure = (
            since_watermark is None
            and provider_failure is not None
            and provider_failure.retryable
            and current_attempt < PROVIDER_MAX_ATTEMPTS
        )
        # The completed provider policies hold their watermark and retry the
        # whole consolidation. formation@9 instead completes with its audited
        # fallback now and schedules a bounded re-pass over the same evidence,
        # so an outage delays the provider's contribution without discarding
        # either the fallback's memories or the evidence the provider missed.
        should_retry = retryable_failure and self._policy_version != NEMORI_FORMATION_POLICY_VERSION
        should_repass = (
            retryable_failure and self._policy_version == NEMORI_FORMATION_POLICY_VERSION
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
                displaced = len(proposals) - len(candidates)
                rejected = displaced
                decisions: Counter[str] = displacement_counts.copy()
                committed = 0
                reinforced = 0
                superseded = 0
                conflicted = 0
                for candidate, authority in [] if should_retry else candidates:
                    if (
                        candidate.proposed_scope != scope
                        or not set(candidate.source_event_ids) <= trusted_user_sources
                    ):
                        rejected += 1
                        decisions["rejected_provenance"] += 1
                        continue
                    if (
                        self._policy_version == NEMORI_FORMATION_POLICY_VERSION
                        and authority is MemoryAuthority.INFERRED
                        and any(
                            span.text not in _event_text(by_sequence[span.source_event_id])
                            for span in candidate.evidence_spans
                        )
                    ):
                        rejected += 1
                        decisions["rejected_provenance"] += 1
                        continue
                    source_text = "\n".join(
                        _event_text(by_sequence[sequence])
                        for sequence in candidate.source_event_ids
                    )
                    if contains_automatic_memory_hazard(source_text):
                        rejected += 1
                        decisions[
                            "rejected_credential"
                            if _SECRET.search(source_text) is not None
                            else "rejected_injection"
                        ] += 1
                        continue
                    if (
                        self._policy_version == NEMORI_FORMATION_POLICY_VERSION
                        and candidate.polarity is Polarity.RETRACT
                        and authority is not MemoryAuthority.USER
                    ):
                        # A correction may update the belief it names and
                        # never create one: with nothing live to retract,
                        # the candidate is counted and dropped.
                        related = await uow.memories.related(
                            self._principal.tenant_id,
                            self._principal.principal_id,
                            " ".join(candidate.subject.split()),
                            candidate.belief_type,
                        )
                        if not any(current.polarity is Polarity.ASSERT for current in related):
                            rejected += 1
                            decisions["skipped_unmatched_retraction"] += 1
                            continue
                    source_events = [
                        event
                        for sequence in candidate.source_event_ids
                        if (event := by_sequence.get(sequence)) is not None
                    ]
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
                            authority=authority,
                            polarity=candidate.polarity,
                            confidence=candidate.model_confidence,
                            valid_from=candidate.valid_from,
                            expires_at=candidate.expires_hint,
                            trigger=effective_trigger,
                            record_audit=False,
                            claim_kind=candidate.claim_kind,
                            derivation=candidate.derivation,
                            longevity=candidate.longevity,
                            # A consolidation may run at any distance from the
                            # evidence it reads - a replay re-reads a session
                            # from watermark zero long afterwards - so recency
                            # is judged on when the statement was made, never on
                            # when it is being read.
                            evidence_at=max(
                                (event.created_at for event in source_events),
                                default=None,
                            ),
                            existing_uow=uow,
                            audit_id=consolidation_id,
                        )
                    except ConflictError:
                        rejected += 1
                        decisions["rejected_correction"] += 1
                    except ToolValidationError:
                        rejected += 1
                        decisions["rejected_provenance"] += 1
                    else:
                        if action == "unchanged":
                            rejected += 1
                            decisions["redundant_attributed"] += 1
                            continue
                        beliefs.append(belief)
                        if action == "promoted":
                            reinforced += 1
                            decisions["promoted"] += 1
                        elif action == "reinforced":
                            reinforced += 1
                            decisions["reinforced"] += 1
                        else:
                            committed += 1
                            if action == "superseded":
                                superseded += 1
                                decisions["superseded"] += 1
                            elif action == "conflicted":
                                conflicted += 1
                                decisions["conflicted"] += 1
                            else:
                                decisions[
                                    "committed_hypothesis"
                                    if candidate.derivation is MemoryDerivation.HYPOTHESIS
                                    else "committed_direct"
                                ] += 1
                if not should_retry:
                    await self._nominate_persona_candidates(uow, beliefs, consolidation_id)
                watermark_after = watermark if should_retry else after

                async def schedule_provider_retry(
                    uow: RepositoryUnitOfWork, source_before: int
                ) -> None:
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
                                "source_watermark_before": source_before,
                                "source_watermark_after": after,
                                "failure_kind": provider_failure.failure_kind,
                            },
                            derivation_key=(
                                "memory.formation.provider_retry:"
                                f"{session_id}:{after}:{next_attempt}"
                            ),
                        )
                    )

                if should_retry:
                    await schedule_provider_retry(uow, watermark)
                else:
                    await uow.memories.set_consolidation_watermark(
                        session_id, self._principal, after
                    )
                    if should_repass:
                        await schedule_provider_retry(
                            uow, watermark if repass_floor is None else repass_floor
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
                extractor_audit = getattr(self._extractor, "last_audit", None)
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
                    candidates_proposed=len(proposals),
                    committed=committed,
                    reinforced=reinforced,
                    superseded=superseded,
                    rejected=rejected,
                    decision_counts=dict(decisions),
                    episode_count=int(getattr(extractor_audit, "episode_count", 0)),
                    provider_call_count=int(getattr(extractor_audit, "provider_calls", 0)),
                    fallback_stages=list(getattr(extractor_audit, "fallback_stages", [])),
                    provider_stage_metrics=dict(
                        getattr(extractor_audit, "provider_stage_metrics", {})
                    ),
                    started_at=started_at,
                    finished_at=self._clock.now(),
                )
                await uow.memories.record_consolidation(audit)
            finally:
                await uow.maintenance.release_memory_session(self._principal, session_id)
        return ConsolidationResult(run=audit, beliefs=beliefs, conflicted=conflicted)

    async def _nominate_persona_candidates(
        self,
        uow: RepositoryUnitOfWork,
        beliefs: list[MemoryRecord],
        consolidation_id: UUID,
    ) -> None:
        """Raise persona nominations for beliefs that clear the persona bar.

        Runs inside the consolidation's unit of work, only on a committing
        pass — never a retry or no-work path. Only this governed service
        nominates; affirmation stays a human act on another surface
        (persona-surface.md).
        """

        profile = self._profile.persona_nomination
        now = self._clock.now()
        all_rows = await uow.personas.list_nominations(self._principal)
        open_rows = [row for row in all_rows if row.state is PersonaNominationState.NOMINATED]
        # Decline is content-keyed as well as id-keyed: re-derivation mints
        # new belief identifiers, and a statement the owner has already
        # judged must not come back under one (persona-surface.md).
        resolved_statements = {
            row.statement.casefold()
            for row in all_rows
            if row.state in (PersonaNominationState.DECLINED, PersonaNominationState.AFFIRMED)
        }
        open_count = 0
        open_belief_ids: set[UUID] = set()
        for row in open_rows:
            try:
                source = await uow.memories.get(row.belief_id, self._principal)
            except NotFoundError:
                source = None
            if source is None or source.status is not MemoryStatus.ACTIVE:
                # The belief died before review; the slot frees and the
                # withdrawal records why the owner never saw it.
                await uow.personas.resolve_nomination(
                    row.id,
                    self._principal,
                    state=PersonaNominationState.WITHDRAWN,
                    resolved_at=now,
                )
                continue
            open_count += 1
            open_belief_ids.add(row.belief_id)
        active_document = await uow.personas.active(self._principal)
        already_affirmed = (
            set(active_document.affirmed_belief_ids) if active_document is not None else set()
        )
        for belief in beliefs:
            if open_count >= profile.max_open:
                return
            if belief.id in open_belief_ids or belief.id in already_affirmed:
                continue
            if belief.statement.casefold() in resolved_statements:
                continue
            if not self._persona_eligible(belief, profile):
                continue
            try:
                stored = await uow.personas.nominate(
                    PersonaNomination(
                        id=self._ids.new_id(),
                        tenant_id=self._principal.tenant_id,
                        principal_id=self._principal.principal_id,
                        belief_id=belief.id,
                        statement=belief.statement,
                        belief_type=belief.belief_type,
                        authority=belief.authority,
                        confidence=belief.confidence,
                        corroboration_count=belief.corroboration_count,
                        sensitivity=belief.sensitivity,
                        consolidation_run_id=consolidation_id,
                        nominated_at=now,
                    )
                )
            except ConflictError:
                # A durable decline or a standing affirmation: never again.
                continue
            open_count += 1
            open_belief_ids.add(stored.belief_id)

    @staticmethod
    def _persona_eligible(belief: MemoryRecord, profile: PersonaNominationProfile) -> bool:
        return (
            belief.status is MemoryStatus.ACTIVE
            and belief.derivation is MemoryDerivation.DIRECT
            and belief.belief_type in (BeliefType.PREFERENCE, BeliefType.USER_MODEL_ATTR)
            and belief.scope == "user"
            and belief.portability is not Portability.LOCAL
            and belief.polarity is Polarity.ASSERT
            and not belief.flagged_for_review
            and belief.confidence >= profile.min_confidence
            and belief.corroboration_count >= profile.min_corroboration
            and SENSITIVITY_ORDER[belief.sensitivity] <= SENSITIVITY_ORDER[Sensitivity.INTERNAL]
            and len(belief.statement) <= PERSONA_ENTRY_MAX_CHARS
            and not contains_automatic_memory_hazard(belief.statement)
        )

    def _established_fact_candidates(
        self,
        events: list[EventEnvelope],
        scope: str,
        trusted_user_sources: set[int],
    ) -> list[MemoryCandidate]:
        """Propose the working-state facts this window established from user events.

        Only the last working-state update in the window is read: it is the
        state the session ends holding, and the earlier ones are its drafts.
        The manager stamps every fact `EXTERNAL_UNTRUSTED` because a run never
        upgrades its own trust, so trust is derived here instead — a fact
        qualifies exactly when every event it cites is an owning-principal user
        message inside this window.
        """

        if not self._profile.established_facts_enabled:
            return []
        state_event = next(
            (event for event in reversed(events) if event.event_type == WORKING_STATE_EVENT),
            None,
        )
        if state_event is None:
            return []
        try:
            state = WorkingState.model_validate(state_event.payload.get("working_state"))
        except ValidationError:
            logger.warning(
                "memory_working_state_payload_invalid",
                extra={"event_sequence": state_event.sequence},
            )
            return []
        candidates: list[MemoryCandidate] = []
        for fact in state.established_facts:
            if not set(fact.source_event_ids) <= trusted_user_sources:
                continue
            subject = _fact_subject(fact.statement)
            if not subject:
                continue
            candidates.append(
                MemoryCandidate(
                    belief_type=BeliefType.FACT,
                    subject=subject,
                    statement=fact.statement,
                    polarity=Polarity.ASSERT,
                    source_event_ids=list(fact.source_event_ids),
                    model_confidence=MAX_INFERRED_CONFIDENCE,
                    proposed_scope=scope,
                    proposed_portability=portability_ceiling(BeliefType.FACT),
                    sensitivity_guess=Sensitivity.INTERNAL,
                    evidence_spans=[
                        EvidenceSpan(
                            source_event_id=fact.source_event_ids[0],
                            text=fact.statement,
                        )
                    ],
                )
            )
        return candidates

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
            attempt_events = await uow.process_events.list_filtered(
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                session_id=session_id,
                event_types=frozenset(
                    {
                        "memory.provider_extraction.completed",
                        "memory.provider_extraction.failed",
                    }
                ),
                limit=100,
            )
            selection_events = await uow.process_events.list_filtered(
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                session_id=None,
                event_types=frozenset({"memory.provider_extraction.selection"}),
                limit=100,
            )
            process_events = sorted(
                [*attempt_events, *selection_events],
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
        ]
        selections = [
            event
            for event in process_events
            if event.event_type == "memory.provider_extraction.selection"
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
                    "derivation": MemoryDerivation.DIRECT,
                    "longevity": MemoryLongevity.DURABLE,
                    "status": MemoryStatus.ACTIVE,
                    "expires_at": None,
                    "source_event_ids": current.source_event_ids,
                    "store_position": position,
                    "updated_at": self._clock.now(),
                    "last_evidence_at": self._clock.now(),
                    "last_reinforced_at": self._clock.now(),
                    "lifecycle_policy_version": LIFECYCLE_POLICY_VERSION,
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

    async def decay(self, *, now: datetime | None = None) -> DecayResult:
        """Take a step of confidence from live beliefs nothing has used.

        Eligible is provisional or below the maximum inferred confidence, idle
        for at least its belief type's time constant, and last written more
        than one sweep interval ago — the last of which is what stops two
        workers, or two passes inside one window, from decaying a belief twice.
        An explicit user statement is active at high confidence and is
        therefore never eligible.

        The window is the least recently reinforced beliefs past the shortest
        time constant any type carries, bounded by the per-sweep ceiling.
        Ordering it by idleness rather than by write position is what keeps the
        sweep working in a large store, and it is also what lets the lowered
        belief keep its position: the window never has to be re-found.

        A belief the step carries below the floor is retired with its validity
        closed at the sweep instant. Only that branch takes a fresh store
        position. A session reads a position above its snapshot watermark as a
        belief formed or corrected since, so a quiet loss of confidence keeps
        the position it had — republishing it would report a change the user
        never made — while being closed is exactly the change the next turn's
        correction lines exist to state. Both outcomes are written through the
        reinforcement path and announce themselves as events, and the sweep is
        bounded by the profile's per-sweep ceiling.
        """

        instant = now or self._clock.now()
        interval = timedelta(seconds=self._profile.scheduled_interval_seconds)
        decay = self._profile.decay
        decayed = 0
        retired = 0
        horizon = min(self._decay_tau_days.for_belief_type(kind) for kind in BeliefType)
        async with self._uow_factory() as uow:
            candidates = await uow.memories.list_idle(
                self._principal,
                evidence_before=instant - timedelta(days=horizon),
                decay_confidence_ceiling=MAX_INFERRED_CONFIDENCE,
                limit=decay.max_per_sweep,
            )
            for record in candidates:
                if not self._decays(record, instant, interval):
                    continue
                confidence = max(0.0, record.confidence - decay.step)
                retiring = confidence < decay.floor_confidence
                update: dict[str, object] = {
                    "confidence": confidence,
                    "updated_at": instant,
                }
                if retiring:
                    update.update(
                        {
                            "status": MemoryStatus.RETIRED,
                            "valid_to": instant,
                            "store_position": await uow.memories.next_position(),
                        }
                    )
                stored = await uow.memories.reinforce(record.model_copy(update=update, deep=True))
                await self._append_event(
                    uow,
                    _source_session(record),
                    None,
                    "memory.retired" if retiring else "memory.decayed",
                    stored,
                    actor_type="memory",
                )
                retired += int(retiring)
                decayed += int(not retiring)
        return DecayResult(decayed=decayed, retired=retired)

    def _decays(self, record: MemoryRecord, instant: datetime, interval: timedelta) -> bool:
        """Whether this belief is idle, uncertain, and unwritten long enough."""

        if record.status is not MemoryStatus.PROVISIONAL and (
            record.confidence >= MAX_INFERRED_CONFIDENCE
        ):
            return False
        tau = timedelta(days=self._decay_tau_days.for_belief_type(record.belief_type))
        if instant - record.last_evidence_at < tau:
            return False
        return record.updated_at < instant - interval

    async def record_usage(
        self,
        *,
        session_id: UUID,
        run_id: UUID,
        final_text: str,
        snapshot_trace_id: UUID | None = None,
        now: datetime | None = None,
    ) -> UsageFeedback:
        """Feed one completed run's citations back into the beliefs it read.

        The answer names beliefs in the eight-hex form the renderer emits, so
        the citations are read out of the final message and matched against
        what the run's traces actually returned: an identifier the recall never
        offered is an invention and moves nothing, and one that fits two of the
        returned beliefs is evidence about neither. A cited belief gains utility
        and a fresh usage instant without refreshing evidence; a belief returned
        and never used loses utility, so it stops winning the ranking it kept
        winning for nothing.

        Confidence is never touched in either direction
        (memory-retrieval-and-ranking.md:793): a wrong belief that happens to
        rank well would otherwise entrench itself by being retrieved, and
        evidence has to come from the world rather than from the retriever.

        One `memory.cited` event carries the run's identifier as its derivation
        key, so the re-entrant completion path finds the run already accounted
        for and changes nothing the second time.
        """

        instant = now or self._clock.now()
        short_ids = set(_CITED_BELIEF.findall(final_text))
        derivation_key = f"memory.cited:{run_id}"
        async with self._uow_factory() as uow:
            if await uow.events.get_by_derivation(derivation_key, self._principal) is not None:
                return UsageFeedback()
            traces = [
                trace
                for trace in await uow.traces.for_turn(run_id)
                if trace.tenant_id == self._principal.tenant_id
                and trace.principal_id == self._principal.principal_id
            ]
            if snapshot_trace_id is not None and not any(
                trace.id == snapshot_trace_id for trace in traces
            ):
                # A session whose snapshot is gone is still a run whose in-turn
                # recalls deserve their feedback.
                with suppress(NotFoundError):
                    traces.append(await uow.traces.get(snapshot_trace_id, self._principal))
            # Eight hex digits name a belief only while the run's returned set
            # holds one belief starting with them. A citation that fits two is
            # evidence about neither, so it credits neither and charges
            # neither: crediting both would manufacture usage the answer never
            # expressed, and the deterministic identifiers the evaluation
            # harness issues make every belief render `[m:00000000]`.
            matched: dict[str, set[UUID]] = {short: set() for short in short_ids}
            returned: set[UUID] = set()
            for trace in traces:
                returned.update(trace.returned)
                for belief_id in trace.returned:
                    candidates = matched.get(str(belief_id)[:8])
                    if candidates is not None:
                        candidates.add(belief_id)
            cited = {
                next(iter(candidates)) for candidates in matched.values() if len(candidates) == 1
            }
            unresolved = [candidates for candidates in matched.values() if len(candidates) > 1]
            ambiguous = {belief_id for candidates in unresolved for belief_id in candidates}
            if not returned:
                # Nothing was recalled, so there is no feedback to record and
                # nothing a repeated completion could double-count.
                return UsageFeedback(traces=len(traces))
            for trace in traces:
                fresh = [
                    belief_id
                    for belief_id in trace.returned
                    if belief_id in cited and belief_id not in set(trace.cited)
                ]
                if fresh:
                    await uow.traces.mark_cited(trace.id, self._principal, fresh)
            # A belief the answer cited is never also charged for going unused,
            # however many of the run's traces returned it, and neither is one
            # an ambiguous citation may have meant.
            uncited = returned - cited - ambiguous
            moved_cited = await self._move_utility(
                uow, sorted(cited, key=str), self._usage.cited_utility_delta, instant, cited=True
            )
            moved_uncited = await self._move_utility(
                uow,
                sorted(uncited, key=str),
                self._usage.uncited_utility_delta,
                instant,
                cited=False,
            )
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="memory.cited",
                    actor_type="memory",
                    actor_id=self._principal.principal_id,
                    payload={
                        "trace_ids": [str(trace.id) for trace in traces],
                        "cited": [str(belief_id) for belief_id in sorted(cited, key=str)],
                        "uncited": [str(belief_id) for belief_id in sorted(uncited, key=str)],
                        "ambiguous": len(unresolved),
                    },
                    derivation_key=derivation_key,
                )
            )
        return UsageFeedback(
            cited=moved_cited,
            uncited=moved_uncited,
            traces=len(traces),
            ambiguous=len(unresolved),
        )

    async def _move_utility(
        self,
        uow: RepositoryUnitOfWork,
        belief_ids: list[UUID],
        delta: float,
        instant: datetime,
        *,
        cited: bool,
    ) -> int:
        """Add one signed usage delta to each belief, bounded by [-1, 1].

        A citation moves `last_used_at`, never `last_evidence_at` or the legacy
        `last_reinforced_at`. Retrieval is evidence that a belief was useful,
        not evidence that it remains true, so a belief cannot perpetuate itself
        by winning recall.

        Neither side takes a fresh store position. The recall delta reads a
        position above the session's snapshot watermark as a belief formed or
        corrected since the snapshot, so republishing a belief to the next turn
        for having been read would report a change that never happened. A move
        the clamp flattens to nothing is not written at all.
        """

        moved = 0
        for belief_id in belief_ids:
            try:
                record = await uow.memories.get(belief_id, self._principal)
            except NotFoundError:
                # The belief was deleted between the recall and the completion.
                continue
            update: dict[str, object] = {"utility": min(1.0, max(-1.0, record.utility + delta))}
            if cited:
                update.update({"last_used_at": instant, "updated_at": instant})
            updated = record.model_copy(update=update, deep=True)
            if updated == record:
                continue
            await uow.memories.reinforce(updated)
            moved += 1
        return moved

    async def expire_traces(
        self, now: datetime | None = None, *, limit: int = TRACE_EXPIRY_SWEEP_LIMIT
    ) -> int:
        """Null the operator tier of recall traces past their operator expiry.

        The user-safe tier is untouched, so this is retention rather than
        deletion; the bound keeps one sweep's write set small enough that the
        maintenance pass stays predictable however many traces are due.
        """

        async with self._uow_factory() as uow:
            return await uow.traces.expire_operator_fields(now or self._clock.now(), limit)

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
        claim_kind: MemoryClaimKind,
        derivation: MemoryDerivation,
        longevity: MemoryLongevity,
        evidence_at: datetime | None,
    ) -> MemoryRecord:
        now = self._clock.now()
        effective_confidence = confidence if confidence is not None else 0.9
        if not explicit:
            ceiling = (
                0.4
                if derivation is MemoryDerivation.HYPOTHESIS
                else (
                    0.65
                    if self._policy_version == NEMORI_FORMATION_POLICY_VERSION
                    else MAX_INFERRED_CONFIDENCE
                )
            )
            effective_confidence = min(effective_confidence, ceiling)
        evidence_instant = evidence_at or valid_from or now
        effective_expiry = expires_at
        if not explicit and effective_expiry is None:
            if longevity is MemoryLongevity.TENTATIVE:
                effective_expiry = evidence_instant + timedelta(days=30)
            elif longevity is MemoryLongevity.ONGOING:
                effective_expiry = evidence_instant + timedelta(days=90)
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
            expires_at=effective_expiry,
            status=MemoryStatus.ACTIVE if explicit else MemoryStatus.PROVISIONAL,
            belief_type=belief_type,
            polarity=polarity,
            portability=portability,
            origin_scopes=[scope],
            claim_kind=claim_kind,
            derivation=derivation,
            longevity=longevity,
            last_evidence_at=evidence_instant,
            evidence_count=len(set(source_event_ids)),
            lifecycle_policy_version=LIFECYCLE_POLICY_VERSION,
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
        *,
        actor_type: str | None = None,
    ) -> None:
        # A belief's authority names who is speaking when the write follows a
        # statement; a background sweep is the memory itself acting, whatever
        # authority the belief it touches carries.
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=run_id,
                event_type=event_type,
                actor_type=actor_type
                or ("principal" if belief.authority is MemoryAuthority.USER else "memory"),
                actor_id=self._principal.principal_id,
                payload={"belief": belief.model_dump(mode="json")},
            )
        )


def _source_session(record: MemoryRecord) -> UUID:
    return record.source_session_id
