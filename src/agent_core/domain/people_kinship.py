"""Owner kinship stated in the owner's own words (ADR-0259).

"Erin is my wife" names a person and the owner's tie to them in one clause.
The tie is read from the owner's message, never from a model's paraphrase,
and only from the plain present-tense forms: a negation, "ex-", "late" or a
past tense states no current tie, and a possessive chain ("my wife's friend")
ties the owner to someone else. An ending said of the person anywhere in the
message ("we divorced", "my wife died") withdraws the tie; one said of someone
else ("Erin's father died") or negated ("we never divorced") does not.
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
# An ending is said of the person when their name, a pronoun or the tie itself
# ("my wife") stands right before it, past linking words ("Erin and I have
# separated", "my wife, who died"), or right after it ("I divorced her").
_ENDING = re.compile(
    r"\b(?:divorc[a-z]*|separated|split up|broke up|passed away|passed on|died|dead|deceased"
    r"|widowed)\b"
)
_WORD = re.compile(r"[a-z0-9']+")
_NEGATION = frozenset({"not", "never", "no"})
_LINKING = frozenset(
    """am is are was were be been being has have had got get getting just recently
    sadly finally both later then eventually officially legally suddenly who that
    which""".split()  # noqa: SIM905 - a word list reads as prose
)
_PRONOUNS = frozenset(
    "we we're we've us he he's she she's they they're they've him her them".split()  # noqa: SIM905
)


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("\u2019", "'")
    return " ".join(text.replace("\u00e9", "e").split())


def _kinship(word: str) -> OwnerKinship:
    return _KIN[_PLURAL.get(word, word)]


def _clauses(text: str) -> list[str]:
    return [part.strip(" ,") for part in re.split(r"[.!?;:\n]+", _normal(text)) if part.strip()]


def _names(listed: str) -> list[str]:
    return [part.strip() for part in re.split(r",\s*(?:and\s+)?|\s+and\s+", listed) if part.strip()]


def _is_tie(word: str, kinship: OwnerKinship) -> bool:
    return (word in _KIN or word in _PLURAL) and _kinship(word) == kinship


def _ends(clause: str, name: list[str], kinship: OwnerKinship) -> bool:
    """Whether the clause says the tie to `name` ended."""

    for match in _ENDING.finditer(clause):
        before = _WORD.findall(clause[: match.start()])
        if any(word in _NEGATION or word.endswith("n't") for word in before[-3:]):
            continue
        while before and before[-1] in _LINKING:
            before.pop()
        if before[-2:] in (["and", "i"], ["and", "me"]):
            del before[-2:]
        after = _WORD.findall(clause[match.end() :])
        if after[:1] in (["from"], ["with"]):
            del after[:1]
        last = before[-1] if before else ""
        first = after[0] if after else ""
        if (
            last in _PRONOUNS
            or first in _PRONOUNS
            or (bool(name) and name in (before[-len(name) :], after[: len(name)]))
            or (_is_tie(last, kinship) and "my" in before[-4:-1])
            or (first == "my" and any(_is_tie(word, kinship) for word in after[1:4]))
        ):
            return True
    return False


def owner_kinship(text: str, name: str) -> OwnerKinship | None:
    """The one tie the owner's text states to `name`, or None.

    Two different ties to the same name ("Erin is my wife ... Erin is my
    sister") state none reliably, so neither is returned, and nor is a tie
    the message says ended.
    """

    wanted = _normal(name)
    clauses = _clauses(text)
    found: set[OwnerKinship] = set()
    for clause in clauses:
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
    if len(found) != 1:
        return None
    (kinship,) = found
    name_words = _WORD.findall(wanted)
    return None if any(_ends(clause, name_words, kinship) for clause in clauses) else kinship


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
