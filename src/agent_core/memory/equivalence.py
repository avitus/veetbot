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

DISTILLATION_SCORER_VERSION: Final[Literal["distillation-scorer@7"]] = "distillation-scorer@7"

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
_ABSENCE = frozenset({"non", "without"})
_ABSENCE_FILLERS = frozenset({"all", "any"})
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
        and (term not in _NEGATIONS or term in _ABSENCE)
        and term not in _NUMBER_WORDS
        and not _is_count(term)
    )


def content_terms(value: str) -> set[str]:
    """The stemmed content-bearing terms, without negation or count markers.

    Small numbers are counts and are compared separately; a number of one
    hundred or more is a year, a version, or an identifier and stays a term so
    that two claims differing only in it never compare equal.
    """

    return {
        "without" if term in _ABSENCE else _stem(term)
        for term in _tokens(value)
        if _is_content(term)
    }


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
    qualifies the claim rather than denying it. Scope is the clause: a fronted
    circumstance ("When travelling, I cannot ...") ends at its comma, or
    without one where the main clause's subject begins, and does not hide the
    main clause's negation.
    """

    clauses = re.split(r"[,;]", value)
    for index, clause in enumerate(clauses):
        terms = _tokens(clause)
        if terms and terms[0] in _SUBORDINATORS:
            if index < len(clauses) - 1:
                # A fronted circumstance ends at its comma or semicolon, even
                # when it carries its own subject: "Although I do not run,
                # User bikes" is not negated.
                continue
            # With no separator it ends where the main clause's subject
            # begins: "Although busy I do not take ..." is negated, "When I
            # am not lifting I run" is not.
            starts = [start for start, term in enumerate(terms) if term in _SUBJECT_TOKENS]
            if not starts:
                continue
            terms = terms[starts[-1] :]
        for term in terms:
            if term in _SUBORDINATORS:
                break
            if term in _NEGATIONS and term not in _ABSENCE:
                return True
    return False


_NAME = re.compile(r"\b[A-Z][a-z]+\b")
_IRREGULAR_LEMMAS = {
    "ate": "eat",
    "attending": "attend",
    "baking": "bake",
    "biking": "bike",
    "boxing": "box",
    "coding": "code",
    "commuting": "commute",
    "cycling": "cycle",
    "dancing": "dance",
    "did": "do",
    "does": "do",
    "doing": "do",
    "driven": "drive",
    "driving": "drive",
    "drove": "drive",
    "eating": "eat",
    "flew": "fly",
    "flying": "fly",
    "gave": "give",
    "given": "give",
    "goes": "go",
    "going": "go",
    "gone": "go",
    "had": "have",
    "has": "have",
    "having": "have",
    "hiking": "hike",
    "jogging": "jog",
    "lifting": "lift",
    "made": "make",
    "making": "make",
    "meditating": "meditate",
    "practicing": "practice",
    "ran": "run",
    "riding": "ride",
    "rode": "ride",
    "rowing": "row",
    "running": "run",
    "sailing": "sail",
    "sat": "sit",
    "sitting": "sit",
    "skating": "skate",
    "skiing": "ski",
    "smoking": "smoke",
    "studies": "study",
    "studying": "study",
    "surfing": "surf",
    "swam": "swim",
    "swimming": "swim",
    "taken": "take",
    "taking": "take",
    "took": "take",
    "tries": "try",
    "volunteering": "volunteer",
    "wearing": "wear",
    "went": "go",
    "wore": "wear",
    "worn": "wear",
    "writing": "write",
    "written": "write",
    "wrote": "write",
}
_SUBJECT_TOKENS = frozenset(
    {"i", "i'd", "i'll", "i'm", "i've", "user", "users", "we", "we're", "we've"}
)
# Verbs and adverbs that carry an activity rather than being one: "goes
# running", "keeps swimming", "usually runs".
_LIGHT_LEADS = frozenset(
    {
        "always",
        "enjoy",
        "enjoys",
        "go",
        "goes",
        "going",
        "keep",
        "keeps",
        "kept",
        "like",
        "likes",
        "love",
        "loves",
        "mostly",
        "normally",
        "often",
        "prefer",
        "prefers",
        "regularly",
        "sometimes",
        "start",
        "started",
        "starting",
        "tend",
        "tends",
        "try",
        "tries",
        "usually",
        "went",
    }
)


def lemma(term: str) -> str:
    """A light lemma: inflection stripped so "running", "ran", and "runs" agree."""

    if term in _IRREGULAR_LEMMAS:
        return _IRREGULAR_LEMMAS[term]
    if term.endswith("ing") and len(term) > 5:
        base = term[:-3]
        if base[-1] == base[-2] and base[-1] not in "aeiou":
            base = base[:-1]
        return base
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith(("ches", "shes", "sses", "xes", "zes")):
        return term[:-2]
    if term.endswith("ed") and len(term) > 4:
        base = term[:-2]
        if base[-1] == base[-2] and base[-1] not in "aeiou":
            base = base[:-1]
        return base
    if term.endswith("oes") and len(term) > 4 and term not in _OE_STEMS:
        return term[:-2]
    if term.endswith("s") and not term.endswith("ss") and len(term) > 3:
        return term[:-1]
    return term


# Plurals in "-oes" whose singular keeps the "e": a shoe, a canoe, a toe.
_OE_STEMS = frozenset(
    {"shoes", "canoes", "toes", "oboes", "aloes", "woes", "foes", "hoes", "roes", "floes", "throes"}
)
# A stem this long names its own inflections and compounds: "weight" names
# "weightlifting" and "prefer" names "preference", while "run" is too short
# to claim "runway".
_COMPOUND_STEM_MIN = 5


def _stems_related(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= _COMPOUND_STEM_MIN and longer.startswith(shorter)


def _terms_related(left: set[str], right: set[str]) -> bool:
    """Whether a lemma on one side names one on the other, or its extension."""

    return any(_stems_related(a, b) for a in left for b in right)


def lemmatized_terms(value: str) -> set[str]:
    """Content terms reduced to lemmas, for matching an activity across wordings."""

    return {lemma(term) for term in _tokens(value) if _is_content(term)}


def main_verb(value: str) -> str | None:
    """The lemma of the first content word after any negation and light lead.

    "User no longer runs outdoors" and "User goes running outdoors most
    mornings" both give "run"; "User tracks runs outdoors in a journal" gives
    "track", which is why a retraction of running does not end it.
    """

    for term in _tokens(value):
        if not _is_content(term) or term in _LIGHT_LEADS:
            continue
        return lemma(term)
    return None


def proper_names(value: str) -> list[str]:
    """Capitalized names in order of first appearance, excluding the subject word."""

    found: list[str] = []
    for match in _NAME.finditer(value):
        if match.start() == 0 or match.group(0) in {"User", "The"}:
            continue
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def absence_terms(value: str) -> frozenset[str]:
    """Normalized objects of absence conditions inside a positive claim."""

    tokens = _tokens(value)
    conditions: set[str] = set()
    for index, term in enumerate(tokens):
        if term not in _ABSENCE:
            continue
        following = next(
            (
                _stem(later)
                for later in tokens[index + 1 :]
                if later not in _ABSENCE_FILLERS
                and later not in _ABSENCE
                and not any(character.isdigit() for character in later)
                and _is_content(later)
            ),
            "without",
        )
        conditions.add(following)
    return frozenset(conditions)


def ordered_terms(value: str) -> list[str]:
    """The stemmed content terms in order of first appearance."""

    ordered: list[str] = []
    for term in _tokens(value):
        if _is_content(term):
            stem = "without" if term in _ABSENCE else _stem(term)
            if stem not in ordered:
                ordered.append(stem)
    return ordered


def _longest_common_subsequence(left: list[str], right: list[str]) -> int:
    lengths = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i, left_term in enumerate(left, start=1):
        for j, right_term in enumerate(right, start=1):
            lengths[i][j] = (
                lengths[i - 1][j - 1] + 1
                if left_term == right_term
                else max(lengths[i - 1][j], lengths[i][j - 1])
            )
    return lengths[len(left)][len(right)]


def shared_terms_in_order(left: str, right: str) -> bool:
    """Whether the content terms both statements share keep their order.

    A bag of words cannot tell "hired Alice and fired Bob" from "hired Bob and
    fired Alice"; the order of the terms they share can. Shared proper names
    must keep their exact order. Among the other shared terms one may move,
    because "modify the routine to improve it" and "improve the routine" are
    one claim, while a swap of two arguments ("the cat chased the dog") moves
    two and never passes. Terms only one side carries are ignored, so an
    elaboration is judged by the overlap rules.
    """

    if not names_in_order(left, right):
        return False
    left_ordered = ordered_terms(left)
    right_ordered = ordered_terms(right)
    shared = set(left_ordered) & set(right_ordered)
    left_shared = [term for term in left_ordered if term in shared]
    right_shared = [term for term in right_ordered if term in shared]
    return _longest_common_subsequence(left_shared, right_shared) >= len(shared) - 1


def names_in_order(left: str, right: str) -> bool:
    """Whether the proper names both statements share appear in the same order."""

    left_names = proper_names(left)
    right_names = proper_names(right)
    shared = set(left_names) & set(right_names)
    return [name for name in left_names if name in shared] == [
        name for name in right_names if name in shared
    ]


def distinguishing_words(statement: str, others: Iterable[str]) -> list[str]:
    """The words of a statement whose content no other statement carries, in order."""

    covered: set[str] = set()
    for other in others:
        covered |= content_terms(other)
    words: list[str] = []
    for term in _tokens(statement):
        if _is_content(term) and _stem(term) not in covered and term not in words:
            words.append(term)
    return words


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
                lemma(later)
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
    every marker they share, keep the content terms they share in the same
    order, share at least three quarters of their combined content terms, and
    the candidate may introduce at most one content term the reference lacks,
    so a paraphrased verb passes while an elaboration, a negation, a different
    count or distance, a reversed comparison, or a sibling activity does not.
    """

    if normalized_statement(candidate) == normalized_statement(reference):
        return True
    candidate_terms = content_terms(candidate)
    reference_terms = content_terms(reference)
    if not candidate_terms or not reference_terms:
        return False
    if not statements_compatible(candidate, reference):
        return False
    if not shared_terms_in_order(candidate, reference):
        return False
    union = candidate_terms | reference_terms
    shared = candidate_terms & reference_terms
    if len(shared) / len(union) < 0.75:
        return False
    return len(candidate_terms - reference_terms) <= 1


# Bare qualifiers that place a claim in time without changing it.
_QUALIFIER_TERMS = frozenset(
    {
        "currently",
        "now",
        "always",
        "lately",
        "recently",
        "still",
        "already",
        "presently",
        "nowadays",
    }
)


def _claim_terms(value: str) -> set[str]:
    return {
        lemma(term) for term in _tokens(value) if _is_content(term) and term not in _QUALIFIER_TERMS
    }


def statement_matches_claim(candidate: str, reference: str) -> bool:
    """Whether a formed belief states a gold claim, possibly with more detail.

    The scorer's rule since distillation-scorer@7. Equal normalized text
    matches. Otherwise the two must be compatible (polarity, absence
    conditions, counts, numbers, directions) and keep their shared terms in
    order; then a belief carrying every content lemma of the gold matches
    however many words it adds, and one that does not must share three
    quarters of the combined lemmas and add at most one. Inflections agree
    and bare qualifiers such as "currently" are not content, so "has written
    firmware for eight years" states "eight years of experience writing
    firmware", while a different count, a reversed comparison, a negation,
    an added absence, a dropped conjunct, or a sibling activity never does.
    The runtime combiner keeps the stricter `statements_equivalent`, because
    merging is irreversible and scoring is not.
    """

    if normalized_statement(candidate) == normalized_statement(reference):
        return True
    candidate_terms = _claim_terms(candidate)
    reference_terms = _claim_terms(reference)
    if not candidate_terms or not reference_terms:
        return False
    if not statements_compatible(candidate, reference):
        return False
    if not shared_terms_in_order(candidate, reference):
        return False
    if reference_terms <= candidate_terms:
        return True
    union = candidate_terms | reference_terms
    if len(candidate_terms & reference_terms) / len(union) < 0.75:
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
    subject also matches when a lemma of it names a lemma of the gold subjects
    or of the gold statement itself, or an inflection or compound of one:
    "weightlifting" names "lifting weights" and "tomato growing" names
    "tomatoes".
    """

    normalized = normalized_statement(subject)
    if is_generic_subject(subject):
        return False
    terms = lemmatized_terms(subject)
    if not terms:
        return False
    for expected in expected_subjects:
        if normalized == normalized_statement(expected):
            return True
        if _terms_related(terms, lemmatized_terms(expected)):
            return True
    return any(
        _terms_related(terms, lemmatized_terms(statement)) for statement in expected_statements
    )


def statements_compatible(left: str, right: str) -> bool:
    """Whether two statements could assert one claim: no contradiction in kind.

    Polarity, absence conditions, counts and numbers, and shared directions
    must agree. This is the floor under every equivalence, duplicate, and representation check: a
    comparison that passes it may still be about different things, but one
    that fails it is a correction, a recount, or a reversal and must never be
    merged away.
    """

    return (
        negated(left) == negated(right)
        and absence_terms(left) == absence_terms(right)
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
    if not shared_terms_in_order(statement, clause):
        return False
    clause_terms = content_terms(clause)
    return len(statement_terms & clause_terms) / len(statement_terms) >= 0.5
