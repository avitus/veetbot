"""Duplicate evidence between two people, before anything is merged (ADR-0125).

Names alone never merge people automatically: two people can share a name
(gate P01). These rules only rank how strongly two identities' names agree,
which decides whether the owner is asked about a possible duplicate, and
which identity survives a merge.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

NameReason = Literal["same_name", "first_name", "nickname"]

_RANK: dict[NameReason, int] = {"nickname": 1, "first_name": 2, "same_name": 3}
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
# A nickname must be at least this long to count as the start of a longer name.
_NICKNAME_MINIMUM = 3


@dataclass(frozen=True)
class Standing:
    """What decides which of two identities survives their merge."""

    person_id: UUID
    state: str
    pinned: bool
    # Supported by the owner's own words, such as a Chat statement.
    owner_stated: bool
    history: int
    created_at: datetime


def name_tokens(label: str) -> tuple[str, ...]:
    """Comparable words of a personal name: case-folded and accent-free, without
    initials or generational suffixes. Hyphenated and apostrophe names stay whole."""
    decomposed = unicodedata.normalize("NFKD", label)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    words = re.findall(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*", plain)
    return tuple(word for word in words if len(word) > 1 and word not in _SUFFIXES)


def name_match(
    first: Sequence[tuple[str, ...]], second: Sequence[tuple[str, ...]]
) -> NameReason | None:
    """The strongest agreement between any name of one identity and any of the other."""
    best: NameReason | None = None
    for left in first:
        for right in second:
            reason = _agreement(left, right)
            if reason is not None and (best is None or _RANK[reason] > _RANK[best]):
                best = reason
    return best


def name_key(tokens: tuple[str, ...]) -> str:
    """A key two names share whenever they can agree.

    Every agreement needs equal first words, or a nickname of at least
    `_NICKNAME_MINIMUM` letters that starts the other first word, so comparing
    only names with equal keys finds every match.
    """
    return tokens[0][:_NICKNAME_MINIMUM]


def _agreement(left: tuple[str, ...], right: tuple[str, ...]) -> NameReason | None:
    if not left or not right:
        return None
    if len(left) >= 2 and len(right) >= 2:
        # Full names agree, allowing a differing middle name.
        if left == right or (left[0] == right[0] and left[-1] == right[-1]):
            return "same_name"
        return None
    if len(left) == 1 and len(right) == 1:
        return "first_name" if left == right else None
    short, full = (left, right) if len(left) == 1 else (right, left)
    if short[0] == full[0]:
        return "first_name"
    if len(short[0]) >= _NICKNAME_MINIMUM and full[0].startswith(short[0]):
        return "nickname"
    return None


def family_surnames(references: frozenset[str]) -> frozenset[str]:
    """Surnames the owner's own addresses suggest.

    An address such as ``avitus@…`` usually spells an initial and a surname, so
    both ``avitus`` and ``vitus`` count. This only labels a suggestion as sharing
    the owner's family name; it never merges anyone.
    """
    found: set[str] = set()
    for reference in references:
        kind, _, value = reference.partition(":")
        local = value.partition("@")[0] if kind == "email" else value if kind == "handle" else ""
        for word in re.split(r"[^a-z]+", local.casefold()):
            if len(word) >= 4:
                found.add(word)
            if len(word) >= 5:
                found.add(word[1:])
    return frozenset(found)


def shares_family_name(tokens: tuple[str, ...], surnames: frozenset[str]) -> bool:
    return len(tokens) >= 2 and tokens[-1] in surnames


def merge_direction(first: Standing, second: Standing) -> tuple[UUID, UUID]:
    """(source, target): the weaker identity merges into the stronger one.

    Strength is, in order: confirmed by the owner, pinned, supported by the
    owner's own words, more recorded history, then the older identity.
    """

    def strength(standing: Standing) -> tuple[bool, bool, bool, int, float, str]:
        return (
            standing.state == "active",
            standing.pinned,
            standing.owner_stated,
            standing.history,
            -standing.created_at.timestamp(),
            str(standing.person_id),
        )

    target, source = sorted((first, second), key=strength, reverse=True)
    return source.person_id, target.person_id
