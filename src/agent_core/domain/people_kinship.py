"""Owner kinship stated in the owner's own words (ADR-0259).

"Erin is my wife" names a person and the owner's tie to them in one clause.
The tie is read from the owner's message, never from a model's paraphrase,
and only from the plain present-tense forms: a negation, "ex-", "late" or a
past tense states no current tie, and a possessive chain ("my wife's friend")
ties the owner to someone else. A clause anywhere in the message that says
the tie ended ("we divorced", "she passed away") withdraws it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from uuid import UUID

from agent_core.domain.people import PeopleEndpoint, RelationshipPredicate

# Which end of the edge the owner holds: "owner" reads owner -> person, so a
# daughter is owner -parent-> daughter and a mother is mother -parent-> owner.
OwnerSide = Literal["owner", "person"]
OwnerKinship = tuple[RelationshipPredicate, OwnerSide]

_KIN: dict[str, OwnerKinship] = {
    "wife": ("spouse", "owner"),
    "husband": ("spouse", "owner"),
    "spouse": ("spouse", "owner"),
    "partner": ("partner", "owner"),
    "girlfriend": ("partner", "owner"),
    "boyfriend": ("partner", "owner"),
    "fiancee": ("partner", "owner"),
    "fiance": ("partner", "owner"),
    "daughter": ("parent", "owner"),
    "son": ("parent", "owner"),
    "child": ("parent", "owner"),
    "kid": ("parent", "owner"),
    "stepdaughter": ("parent", "owner"),
    "stepson": ("parent", "owner"),
    "mother": ("parent", "person"),
    "mom": ("parent", "person"),
    "mum": ("parent", "person"),
    "father": ("parent", "person"),
    "dad": ("parent", "person"),
    "stepmother": ("parent", "person"),
    "stepfather": ("parent", "person"),
    "brother": ("sibling", "owner"),
    "sister": ("sibling", "owner"),
    "sibling": ("sibling", "owner"),
}
_PLURAL = {
    "wives": "wife",
    "husbands": "husband",
    "partners": "partner",
    "daughters": "daughter",
    "sons": "son",
    "children": "child",
    "kids": "kid",
    "parents": "father",
    "brothers": "brother",
    "sisters": "sister",
    "siblings": "sibling",
}
_KIN_WORD = "|".join(sorted({*_KIN, *_PLURAL}, key=len, reverse=True))
# Up to two describing words ("second", "older", "step-") between "my" and the
# tie; never one that ends or denies it.
_DESCRIBED = r"(?:(?!(?:not|no|ex|former|late|future|estranged)\b)[a-z]+[ -]){0,2}"
_NAME_FIRST = re.compile(rf"^(?P<names>.+?) (?:is|are) my {_DESCRIBED}(?P<kin>{_KIN_WORD})$")
_KIN_FIRST = re.compile(rf"(?:^|\W)my {_DESCRIBED}(?P<kin>{_KIN_WORD}),? (?:is )?(?P<name>.+)$")
# A statement that denies or ends a tie does not state it ("Erin is not Andy's
# wife", "his ex-wife"), though another tie beside it still may ("his wife, not
# his sister").
_ENDED = re.compile(
    rf"\b(?:not|no|never|isn't|aren't|ex|former|late|estranged)[ -]"
    rf"(?:[a-z]+(?:'s)? ){{0,2}}(?P<kin>{_KIN_WORD})\b"
)
# A clause that ends a tie ("we divorced last year", "my wife Erin passed
# away") withdraws it when it names the person or speaks of them by pronoun.
_ENDING = re.compile(
    r"\b(?:divorc[a-z]*|separated|split up|broke up|passed away|passed on|died|dead|deceased"
    r"|widowed)\b"
)
_PRONOUN = re.compile(r"\b(?:we|us|he|she|they|him|her|them)\b")


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("\u2019", "'")
    return " ".join(text.replace("\u00e9", "e").split())


def _kinship(word: str) -> OwnerKinship:
    return _KIN[_PLURAL.get(word, word)]


def _clauses(text: str) -> list[str]:
    return [part.strip(" ,") for part in re.split(r"[.!?;:\n]+", _normal(text)) if part.strip()]


def _names(listed: str) -> list[str]:
    return [part.strip() for part in re.split(r",\s*(?:and\s+)?|\s+and\s+", listed) if part.strip()]


def owner_kinship(text: str, name: str) -> OwnerKinship | None:
    """The one tie the owner's text states to `name`, or None.

    Two different ties to the same name ("Erin is my wife ... Erin is my
    sister") state none reliably, so neither is returned, and nor is a tie
    the message says ended.
    """

    wanted = _normal(name)
    named = re.compile(rf"(?:^|\W){re.escape(wanted)}(?:\W|$)")
    found: set[OwnerKinship] = set()
    for clause in _clauses(text):
        if _ENDING.search(clause) and (named.search(clause) or _PRONOUN.search(clause)):
            return None
        # "<name> is my wife", "Kyrri and Riv are my daughters".
        if (match := _NAME_FIRST.match(clause)) is not None and wanted in _names(
            match.group("names")
        ):
            found.add(_kinship(match.group("kin")))
        # "my wife Erin", "my mother, Cheryl, lives ...", "my partner is Jules".
        if (match := _KIN_FIRST.search(clause)) is not None:
            rest = match.group("name")
            if rest == wanted or rest.startswith(f"{wanted} ") or rest.startswith(f"{wanted},"):
                found.add(_kinship(match.group("kin")))
    return found.pop() if len(found) == 1 else None


def statement_states_kinship(statement: str, kinship: OwnerKinship) -> bool:
    """Whether the saved statement names the same tie the owner stated."""

    statement = _normal(statement)
    if any(_kinship(match.group("kin")) == kinship for match in _ENDED.finditer(statement)):
        return False
    words = set(re.findall(r"[a-z]+", statement))
    return any(_kinship(word) == kinship for word in words & {*_KIN, *_PLURAL})


def kinship_endpoints(
    kinship: OwnerKinship, person_id: UUID
) -> tuple[PeopleEndpoint, PeopleEndpoint]:
    """Directed (subject, object) for the owner's tie to one person."""

    owner = PeopleEndpoint(kind="owner")
    person = PeopleEndpoint(kind="person", id=person_id)
    return (owner, person) if kinship[1] == "owner" else (person, owner)
