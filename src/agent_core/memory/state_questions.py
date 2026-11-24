"""Subject grounding for provisional state observations in current recall."""

import re

from agent_core.domain.memory import (
    MemoryAuthority,
    MemoryClaimKind,
    MemoryStatus,
    RecalledBelief,
    RecallProfile,
    RecallQuery,
    fold_word,
)

# Read only an atomic present-tense question or assertion. Longer explanations,
# comparisons and compound predicates carry context this rule cannot resolve.
_STATE = r"(?:(?:currently|now|still|not)\s+)*(?P<state>[\w-]+)"
_QUESTION = re.compile(rf"(?:is|are)\s+(?P<subject>[\w .:'\u2019\-]+?)\s+{_STATE}\s*\??", re.I)
_ASSERTION = re.compile(rf"(?P<subject>[\w .:'\u2019\-]+?)\s+(?:is|are)\s+{_STATE}[.!]?", re.I)
_DETERMINER = re.compile(r"^(?:the|an?|my|our|your|[\w-]+['\u2019]s)\s+", re.I)
_AMBIGUOUS_SUBJECT = re.compile(
    r"\b(?:and|or|versus|vs|if|than|which|who|whose|that|of|for|in|on|with|"
    r"all|any|both|each|either|every|many|most|neither|several|some|there|these|those)\b",
    re.I,
)


def _name(subject: str) -> tuple[str, ...]:
    return tuple(
        fold_word(word) for word in re.findall(r"\w+", _DETERMINER.sub("", subject).casefold())
    )


def grounded_state_beliefs(
    query: RecallQuery, beliefs: list[RecalledBelief]
) -> list[RecalledBelief]:
    """Keep a shared category noun from lending another subject's status to this one."""
    if (
        query.candidate_selection != "relevance"
        or query.profile is RecallProfile.CORE
        or query.as_of is not None
        or query.known_at is not None
        or query.include_superseded
        or query.include_ids is not None
        or query.people_scope is not None
        or query.relationship_group is not None
        or not query.text
        or len(query.text) > 512
    ):
        return beliefs
    question = _QUESTION.fullmatch(query.text.strip())
    if not question or _AMBIGUOUS_SUBJECT.search(question["subject"]):
        return beliefs
    requested = _name(question["subject"])
    if len(requested) < 2:
        return beliefs
    protected = {partner for item in beliefs for partner in item.conflict_with}
    kept = []
    for item in beliefs:
        if (
            item.status is MemoryStatus.PROVISIONAL
            and item.authority is MemoryAuthority.INFERRED
            and item.claim_kind is MemoryClaimKind.RECURRING_STATE
            and not item.conflict_with
            and item.belief_id not in protected
        ):
            name = _name(item.subject)
            if (
                len(name) >= 2
                and name[-1] == requested[-1]
                and set(name[:-1]).isdisjoint(requested[:-1])
                and (claim := _ASSERTION.fullmatch(item.statement.strip()))
                and _name(claim["subject"]) == name
                and claim["state"].casefold() == question["state"].casefold()
            ):
                continue
        kept.append(item)
    return kept
