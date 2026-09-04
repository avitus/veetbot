"""Content-aware statement equivalence shared by the evaluator and the distiller.

The comparative evaluation decides whether a formed belief matches a gold
claim, and the distiller decides whether a live memory already represents a
clause. Both need the same answer to the same question, and both must say no
when a candidate negates, recounts, or elaborates beyond the claim it is
compared with. An overlap ratio over the smaller term set cannot say no to any
of those, so this module compares symmetric content, negation, quantity, and
direction: a bag of words cannot tell tea-over-coffee from coffee-over-tea, and
a count check that ignores large numbers cannot tell one hundred miles from two
hundred, so both are compared explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

DISTILLATION_SCORER_VERSION: Final[Literal["distillation-scorer@3"]] = "distillation-scorer@3"

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
# "without" states an absence inside a claim rather than negating the claim:
# "runs without music" is a superset of "runs", not its denial.
_ABSENCE = frozenset({"without"})
# A negation after one of these qualifies a circumstance ("bikes on days when
# not lifting") rather than denying the claim itself.
_SUBORDINATORS = frozenset(
    {
        "although",
        "because",
        "except",
        "if",
        "since",
        "though",
        "unless",
        "when",
        "whenever",
        "where",
        "whereas",
        "while",
    }
)
# A marker whose object gives a statement its direction. Each is normalized to
# a class so that "over", "than", and a preference's "to" compare with one
# another while an unrelated marker in only one statement is not compared.
_DIRECTION_CLASSES = {
    "than": "than",
    "over": "than",
    "versus": "than",
    "vs": "than",
    "above": "than",
    "before": "before",
    "after": "after",
    "from": "from",
    "to": "to",
    "into": "to",
    "onto": "to",
    "toward": "to",
    "towards": "to",
    "instead": "instead",
    "rather": "instead",
    "until": "until",
    "till": "until",
}
_PREFERENCE_VERB_PREFIXES = ("prefer", "favor", "favour")
# "to" after one of these introduces an infinitive, not a destination.
_INFINITIVE_LEADS = frozenset(
    {
        "able",
        "aim",
        "aims",
        "decided",
        "going",
        "hope",
        "hopes",
        "how",
        "intend",
        "intends",
        "learn",
        "learning",
        "like",
        "likes",
        "love",
        "loves",
        "need",
        "needs",
        "plan",
        "plans",
        "prefer",
        "prefers",
        "start",
        "started",
        "starting",
        "tries",
        "try",
        "trying",
        "used",
        "want",
        "wanted",
        "wants",
        "wish",
        "wishes",
    }
)


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


def _is_content(term: str) -> bool:
    return (
        term not in _STOPWORDS
        and term not in _NEGATIONS
        and term not in _NUMBER_WORDS
        and not _is_count(term)
    )


def content_terms(value: str) -> set[str]:
    """The stemmed content-bearing terms, without negation or count markers.

    Small numbers are counts and are compared separately; a number of one
    hundred or more is a year, a version, or an identifier and stays a term so
    that two claims differing only in it never compare equal.
    """

    return {_stem(term) for term in _tokens(value) if _is_content(term)}


def _is_count(term: str) -> bool:
    """A short decimal below one hundred; longer digit runs are never parsed."""

    return term.isdecimal() and len(term) <= 3 and int(term) < 100


def negation_terms(value: str) -> frozenset[str]:
    return frozenset(term for term in _tokens(value) if term in _NEGATIONS)


def negated(value: str) -> bool:
    """Whether a statement denies its claim.

    Polarity is compared as parity rather than by the negation word used, so
    "does not drive" and "doesn't drive" agree while either disagrees with
    "drives". An absence such as "without" is content, not polarity, and a
    negation inside a subordinate circumstance ("on days when not lifting")
    qualifies the claim rather than denying it.
    """

    for term in _tokens(value):
        if term in _SUBORDINATORS:
            return False
        if term in _NEGATIONS and term not in _ABSENCE:
            return True
    return False


def large_numbers(value: str) -> frozenset[str]:
    """Numbers of one hundred or more: years, amounts, distances, identifiers."""

    return frozenset(term for term in _tokens(value) if term.isdecimal() and not _is_count(term))


def numbers_agree(left: str, right: str) -> bool:
    """Whether two statements assert the same counts and the same numbers.

    Counts must match exactly, so "two sisters" never equals "sisters". A large
    number is compared only when both statements carry one: an elaboration
    that adds a year passes, but one hundred miles never equals two hundred.
    """

    if quantity_terms(left) != quantity_terms(right):
        return False
    left_numbers = large_numbers(left)
    right_numbers = large_numbers(right)
    return not left_numbers or not right_numbers or left_numbers == right_numbers


def directional_terms(value: str) -> dict[str, tuple[str, ...]]:
    """The object following each directional marker, keyed by marker class.

    "prefers tea to coffee" yields ``{"than": ("coffee",)}`` and "moved from
    Paris to Rome" yields ``{"from": ("pari",), "to": ("rome",)}``. An
    infinitive "to" has no object and is skipped.
    """

    tokens = _tokens(value)
    objects: dict[str, list[str]] = {}
    preferring = False
    for index, token in enumerate(tokens):
        if token.startswith(_PREFERENCE_VERB_PREFIXES):
            preferring = True
        marker = _DIRECTION_CLASSES.get(token)
        if marker is None:
            continue
        if token == "to":
            if index and tokens[index - 1] in _INFINITIVE_LEADS:
                continue
            if preferring:
                marker = "than"
        following = next(
            (
                _stem(later)
                for later in tokens[index + 1 :]
                if _is_content(later) and later not in _DIRECTION_CLASSES
            ),
            None,
        )
        if following is None:
            continue
        objects.setdefault(marker, []).append(following)
    return {marker: tuple(terms) for marker, terms in objects.items()}


def directions_agree(left: str, right: str) -> bool:
    """Whether every directional marker both statements share points the same way.

    Only shared marker classes are compared, so a paraphrase that drops or
    changes a preposition is not penalized, while a reversed comparison,
    origin and destination, or ordering never agrees.
    """

    left_directions = directional_terms(left)
    right_directions = directional_terms(right)
    return all(
        left_directions[marker] == right_directions[marker]
        for marker in left_directions.keys() & right_directions.keys()
    )


def quantity_terms(value: str) -> frozenset[str]:
    """Counts asserted by a statement.

    Numbers of one hundred or more are years, versions, and identifiers rather
    than counts, and a lone "one" says no more than "a", so neither takes part
    in the comparison.
    """

    quantities: set[str] = set()
    for term in _tokens(value):
        if _is_count(term):
            quantities.add(str(int(term)))
        elif term in _NUMBER_WORDS:
            quantities.add(_NUMBER_WORDS[term])
    if quantities == {"1"}:
        return frozenset()
    return frozenset(quantities)


def statements_equivalent(candidate: str, reference: str) -> bool:
    """Whether a candidate statement asserts the same claim as a reference.

    Equal normalized text always matches. Otherwise both statements must carry
    the same polarity, the same counts and numbers, and the same direction on
    every marker they share, share at least three quarters of their combined
    content terms, and the candidate may introduce at most one content term the
    reference lacks, so a paraphrased verb passes while an elaboration, a
    negation, a different count or distance, a reversed comparison, or a
    sibling activity does not.
    """

    if normalized_statement(candidate) == normalized_statement(reference):
        return True
    candidate_terms = content_terms(candidate)
    reference_terms = content_terms(reference)
    if not candidate_terms or not reference_terms:
        return False
    if not statements_compatible(candidate, reference):
        return False
    union = candidate_terms | reference_terms
    shared = candidate_terms & reference_terms
    if len(shared) / len(union) < 0.75:
        return False
    return len(candidate_terms - reference_terms) <= 1


def is_generic_subject(subject: str) -> bool:
    """Whether a subject names the user bucket rather than a conflict key."""

    return normalized_statement(subject.replace("\u2019", "'")) in _GENERIC_SUBJECTS


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
    if is_generic_subject(subject):
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


def statements_compatible(left: str, right: str) -> bool:
    """Whether two statements could assert one claim: no contradiction in kind.

    Polarity, counts and numbers, and shared directions must agree. This is the
    floor under every equivalence, duplicate, and representation check: a
    comparison that passes it may still be about different things, but one
    that fails it is a correction, a recount, or a reversal and must never be
    merged away.
    """

    return (
        negated(left) == negated(right)
        and numbers_agree(left, right)
        and directions_agree(left, right)
    )


def statement_supports_clause(statement: str, clause: str) -> bool:
    """Whether a memory statement represents a source clause.

    Used to verify that a clause a provider marks as already represented is in
    fact asserted by the memory it cites. A memory whose polarity, count,
    number, or direction differs from the clause is being corrected by it, not
    represented in it, so those must agree; then at least half of the memory's
    content terms must appear in the clause.
    """

    if normalized_statement(statement) == normalized_statement(clause):
        return True
    statement_terms = content_terms(statement)
    if not statement_terms:
        return False
    if not statements_compatible(statement, clause):
        return False
    clause_terms = content_terms(clause)
    return len(statement_terms & clause_terms) / len(statement_terms) >= 0.5
