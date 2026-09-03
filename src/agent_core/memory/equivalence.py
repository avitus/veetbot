"""Content-aware statement equivalence shared by the evaluator and the distiller.

The comparative evaluation decides whether a formed belief matches a gold
claim, and the distiller decides whether a live memory already represents a
clause. Both need the same answer to the same question, and both must say no
when a candidate negates, recounts, or elaborates beyond the claim it is
compared with. An overlap ratio over the smaller term set cannot say no to any
of those, so this module compares symmetric content, negation, and quantity.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

DISTILLATION_SCORER_VERSION: Final[Literal["distillation-scorer@2"]] = "distillation-scorer@2"

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "also",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "current",
        "currently",
        "did",
        "do",
        "does",
        "each",
        "enough",
        "every",
        "for",
        "from",
        "goal",
        "goals",
        "had",
        "has",
        "have",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "least",
        "me",
        "my",
        "now",
        "of",
        "on",
        "onto",
        "or",
        "our",
        "own",
        "per",
        "really",
        "s",
        "so",
        "still",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "they",
        "this",
        "to",
        "upcoming",
        "user",
        "users",
        "very",
        "want",
        "wants",
        "was",
        "were",
        "with",
        "your",
    }
)
_NEGATIONS = frozenset(
    {
        "aren't",
        "can't",
        "cannot",
        "couldn't",
        "didn't",
        "doesn't",
        "don't",
        "isn't",
        "longer",
        "neither",
        "never",
        "no",
        "nor",
        "not",
        "shouldn't",
        "unable",
        "wasn't",
        "weren't",
        "without",
        "won't",
        "wouldn't",
    }
)
_NUMBER_WORDS = {
    "zero": "0",
    "once": "1",
    "one": "1",
    "single": "1",
    "twice": "2",
    "two": "2",
    "both": "2",
    "couple": "2",
    "pair": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "dozen": "12",
}
_GENERIC_SUBJECTS = frozenset({"user", "the user", "users", "me", "i", "myself", "user's"})


def normalized_statement(value: str) -> str:
    """Casefold, trim, and drop terminal punctuation."""

    return " ".join(value.casefold().strip().rstrip(".!?").split())


def _tokens(value: str) -> list[str]:
    lowered = value.casefold().replace("-", " ").replace("\u2019", "'")
    lowered = re.sub(r"'s\b", "", lowered)
    return _TOKEN.findall(lowered)


def _stem(term: str) -> str:
    if len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def content_terms(value: str) -> set[str]:
    """The stemmed content-bearing terms, without negation or quantity markers."""

    return {
        _stem(term)
        for term in _tokens(value)
        if term not in _STOPWORDS
        and term not in _NEGATIONS
        and term not in _NUMBER_WORDS
        and not term.isdecimal()
    }


def negation_terms(value: str) -> frozenset[str]:
    return frozenset(term for term in _tokens(value) if term in _NEGATIONS)


def quantity_terms(value: str) -> frozenset[str]:
    """Counts asserted by a statement.

    Numbers of one hundred or more are years, versions, and identifiers rather
    than counts, and a lone "one" says no more than "a", so neither takes part
    in the comparison.
    """

    quantities: set[str] = set()
    for term in _tokens(value):
        if term.isdecimal():
            if int(term) < 100:
                quantities.add(str(int(term)))
        elif term in _NUMBER_WORDS:
            quantities.add(_NUMBER_WORDS[term])
    if quantities == {"1"}:
        return frozenset()
    return frozenset(quantities)


def statements_equivalent(candidate: str, reference: str) -> bool:
    """Whether a candidate statement asserts the same claim as a reference.

    Equal normalized text always matches. Otherwise both statements must carry
    the same negations and the same quantities, share at least three quarters of
    their combined content terms, and the candidate may introduce at most one
    content term the reference lacks, so a paraphrased verb passes while an
    elaboration, a negation, a different count, or a sibling activity does not.
    """

    if normalized_statement(candidate) == normalized_statement(reference):
        return True
    candidate_terms = content_terms(candidate)
    reference_terms = content_terms(reference)
    if not candidate_terms or not reference_terms:
        return False
    if negation_terms(candidate) != negation_terms(reference):
        return False
    if quantity_terms(candidate) != quantity_terms(reference):
        return False
    union = candidate_terms | reference_terms
    shared = candidate_terms & reference_terms
    if len(shared) / len(union) < 0.75:
        return False
    return len(candidate_terms - reference_terms) <= 1


def subject_matches(
    subject: str,
    expected_subjects: Iterable[str],
    expected_statements: Iterable[str] = (),
) -> bool:
    """Whether a belief subject names the thing an expected claim is about.

    A generic bucket such as "User" never matches: subjects are conflict keys,
    and a belief filed under the user rather than the thing it is about cannot
    be corrected or superseded in isolation. Naming conventions differ, so a
    subject also matches when it shares a content term with the gold statement
    itself.
    """

    normalized = normalized_statement(subject)
    if normalized in _GENERIC_SUBJECTS:
        return False
    terms = content_terms(subject)
    if not terms:
        return False
    for expected in expected_subjects:
        if normalized == normalized_statement(expected):
            return True
        if terms & content_terms(expected):
            return True
    return any(terms & content_terms(statement) for statement in expected_statements)


def statement_supports_clause(statement: str, clause: str) -> bool:
    """Whether a memory statement plausibly represents a source clause.

    Used to verify that a clause a provider marks as already represented is in
    fact about the memory it cites: at least a third of the memory's content
    terms must appear in the clause.
    """

    if normalized_statement(statement) == normalized_statement(clause):
        return True
    statement_terms = content_terms(statement)
    if not statement_terms:
        return False
    clause_terms = content_terms(clause)
    return len(statement_terms & clause_terms) / len(statement_terms) >= 0.34
