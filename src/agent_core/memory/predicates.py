"""Predicate keys: which attribute of a subject a statement claims (ADR-0249).

Formation used to select conflict partners by ``(subject, belief_type)``, so
every lower-authority statement about "Andy Vitus" conflicted with the owner's
affirmed home address. A conflict is two claims about the *same attribute*
with different values. The key set here is closed and reviewed; a statement
that names no known attribute falls back to its own claim identity, never to
the bare subject; two such claims are still compared under the legacy subject
rule (so a free-text update supersedes), while a recognised attribute is
compared only with the same attribute. Preferences are deliberately not keyed:
their topic has no reliable surface form, and the M16 update scenarios need a
later preference to supersede the earlier one about the same subject.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from agent_core.domain.memory import BeliefType

PREDICATE_RULE_VERSION: Final = "predicate-key@1"

_SUBJECT = (
    r"(?:(?:the\s+)?(?:user|owner|he|she|they|his|her|their|my)"
    r"|[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*)(?:'s|\u2019s)?"
)
_ANY_VALUE = r"(?P<value>.+?)"

# Each attribute: (key, regex over the whole statement). The first match wins;
# the regexes are anchored to the statement and capture the attribute's value.
_ATTRIBUTES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (key, re.compile(pattern, re.I))
    for key, pattern in (
        (
            "home_address",
            rf"^(?:{_SUBJECT}\s+)?(?:home\s+|shipping\s+|mailing\s+|street\s+)?address(?:\s+is|\s*:|\s+for\s+{_SUBJECT}\s*(?:is|:))\s*{_ANY_VALUE}\.?$",
        ),
        ("home_address", rf"^{_SUBJECT}\s+(?:lives|resides)\s+at\s+{_ANY_VALUE}\.?$"),
        ("home_city", rf"^{_SUBJECT}\s+(?:lives|resides|is\s+based)\s+in\s+{_ANY_VALUE}\.?$"),
        (
            "phone",
            rf"^(?:{_SUBJECT}\s+)?(?:mobile|cell|phone)(?:\s+number)?(?:\s+is|\s*:)\s*{_ANY_VALUE}\.?$",
        ),
        ("email", rf"^(?:{_SUBJECT}\s+)?e-?mail(?:\s+address)?(?:\s+is|\s*:)\s*{_ANY_VALUE}\.?$"),
        (
            "employer",
            rf"^{_SUBJECT}\s+(?:works\s+(?:at|for)|is\s+employed\s+(?:at|by))\s+{_ANY_VALUE}\.?$",
        ),
        (
            "job_title",
            rf"^{_SUBJECT}\s+(?:is|serves\s+as|works\s+as)\s+(?:the\s+|a\s+|an\s+)?"
            rf"{_ANY_VALUE}\s+(?:of|at)\s+(?P<org>.+?)\.?$",
        ),
        (
            "birthday",
            rf"^(?:{_SUBJECT}\s+)?(?:birthday|date\s+of\s+birth)(?:\s+is|\s*:)\s*{_ANY_VALUE}\.?$",
        ),
        ("birthday", rf"^{_SUBJECT}\s+was\s+born\s+on\s+{_ANY_VALUE}\.?$"),
    )
)

_RELATIONSHIP: Final = re.compile(
    rf"^{_SUBJECT}\s+(?P<role>mother|father|mom|dad|wife|husband|spouse|partner|daughter|son|"
    rf"brother|sister|sibling|boss|manager|assistant|doctor|dentist|accountant|lawyer|landlord)"
    rf"\s+is\s+{_ANY_VALUE}\.?$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class PredicateClaim:
    """What a statement claims: an attribute key and, when recognised, its value."""

    key: str
    value: str | None

    @property
    def recognized(self) -> bool:
        return self.value is not None


def _normalize(text: str) -> str:
    return " ".join(text.casefold().replace("\u2019", "'").split()).rstrip(".")


def predicate_claim(statement: str, belief_type: BeliefType) -> PredicateClaim:
    """Derive the closed predicate key, and the value when the attribute is known."""
    clean = " ".join(statement.split())
    if belief_type is BeliefType.RELATIONSHIP:
        match = _RELATIONSHIP.match(clean)
        if match is not None:
            return PredicateClaim(
                f"relationship:{match.group('role').casefold()}", _normalize(match.group("value"))
            )
    for key, pattern in _ATTRIBUTES:
        match = pattern.match(clean)
        if match is not None:
            value = _normalize(match.group("value"))
            if key == "job_title":
                return PredicateClaim(f"job_title:{_normalize(match.group('org'))}", value)
            return PredicateClaim(key, value)
    return PredicateClaim(_claim_key(clean), None)


_CLAIM_KEY_INLINE_CHARS: Final = 200


def _claim_key(statement: str) -> str:
    """Claim identity: the normalized statement inline while short, a digest of
    it past 200 characters so the key always fits its 512-character column."""
    normalized = _normalize(statement)
    if len(normalized) <= _CLAIM_KEY_INLINE_CHARS:
        return f"claim:{normalized}"
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return f"claim:sha256:{digest}"


def predicate_key(statement: str, belief_type: BeliefType) -> str:
    return predicate_claim(statement, belief_type).key


def same_attribute(a: PredicateClaim, b: PredicateClaim) -> bool:
    """Two claims can agree or disagree only when they key the same attribute."""
    return a.key == b.key


def comparable(a: PredicateClaim, b: PredicateClaim) -> bool:
    """Whether formation may relate two claims about one subject at all.

    Two recognised claims compare only within one attribute. Two unrecognised
    claims keep the subject-and-type comparison formation always had, so a
    free-text update still supersedes the statement it replaces (the M16
    currency gates). A recognised claim and an unrecognised one are
    independent: an email fact about the owner is not a second home address.
    """
    if a.recognized and b.recognized:
        return a.key == b.key
    return not a.recognized and not b.recognized


def comparable_keys(incoming: PredicateClaim, existing_key: str) -> bool:
    """`comparable` when only the existing belief's stored key is at hand."""
    existing_recognized = not existing_key.startswith("claim:")
    if incoming.recognized and existing_recognized:
        return incoming.key == existing_key
    return not incoming.recognized and not existing_recognized


def restates(a: PredicateClaim, b: PredicateClaim) -> bool:
    """Same attribute, same value: corroboration, never a conflict."""
    return a.recognized and b.recognized and a.key == b.key and a.value == b.value
