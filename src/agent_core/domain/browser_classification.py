"""The shared, deny-biased browser action classifier (ADR-0129).

One module decides an action's consequence and whether a grant covers it, in
the worker against the observation that named the element and again in the
isolated runtime against the live page. Its functions are pure and read only
the standard library and the domain, so both processes import it.

Every text is classified separately: each label source, the enclosing dialog's
name, a chosen option, and the first path segment of a link or form target. A
hit in any one of them names the consequence. Over-matching only adds an
approval, so word forms are generated generously, and a text is also read as
it is displayed: in the order a right-to-left override shows it, and with
letters that look Latin read as the Latin letters.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime
from urllib.parse import unquote, urlsplit

from agent_core.domain.argument_views import CREDENTIAL_SHAPE
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionKind,
    BrowserCoverage,
    BrowserDispatchConstraint,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserKey,
    BrowserTargetFacts,
    browser_origin,
    normalize_browser_origin,
)

C = BrowserActionConsequence

# Categories in the order they are tried; the first hit wins.
_VOCABULARY: tuple[tuple[BrowserActionConsequence, str], ...] = (
    (
        C.PAYMENT,
        "pay pays paying payment payments paypal billing bill bills invoice invoices "
        "checkout card cards credit debit wallet refund refunds donate donation tip tips "
        "cvv cvc iban usd eur gbp jpy cny inr cad aud chf gem gems lingot lingots coin "
        "coins spend refill transfer transfers wire withdraw withdrawal deposit payee "
        "remit | check out",
    ),
    (
        C.PURCHASE,
        "buy buying purchase purchases order orders ordering cart basket shop store "
        "subscribe subscribed subscription subscriptions trial trials free upgrade "
        "upgrades premium renew renewal gift gifts redeem coupon promo membership plans "
        "sell trade invest bid booking reservation | choose a plan | choose plan | "
        "select plan | change plan | add to cart",
    ),
    (
        C.AUTHENTICATION_CHANGE,
        "password passwords passcode passkey passkeys otp 2fa mfa authenticator verify "
        "verification login logout logon logoff signin signout signup register "
        "registration reauthenticate | log in | log out | log off | sign in | sign out | "
        "sign off | sign up | two factor | one time code | switch account | "
        "switch accounts",
    ),
    (C.ACCOUNT_RECOVERY, "recover recovery forgot forgotten reset"),
    (
        C.SECURITY_CHANGE,
        "account accounts settings setting preferences preference privacy security "
        "profile username email emails phone address notifications notification edit "
        "change update manage",
    ),
    (
        C.PERMISSION_CHANGE,
        "permission permissions allow block unblock connect disconnect link unlink "
        "authorize authorise grant revoke unsubscribe",
    ),
    (C.LEGAL_ACCEPTANCE, "accept accepted agree agreed consent terms tos eula"),
    (
        C.PUBLICATION,
        "send sent post posts publish share reply comment comments message messages chat "
        "invite tweet submit report follow unfollow friend friends discuss discussion "
        "forum feedback contact dm nudge repost retweet reblog upvote",
    ),
    (
        C.DESTRUCTIVE,
        "delete deleting deletion remove removing erase discard trash destroy deactivate "
        "cancel cancellation wipe purge unenroll archive",
    ),
    (
        C.FILE_TRANSFER,
        "upload uploads download downloads attach attachment attachments import export",
    ),
)
_IRREGULAR_FORMS: dict[str, tuple[str, ...]] = {
    "pay": ("paid",),
    "buy": ("bought",),
    "sell": ("sold",),
    "send": ("sent",),
    "spend": ("spent",),
    "choose": ("chose", "chosen"),
    "withdraw": ("withdrew", "withdrawn"),
}
_ROUTINE_NAMES = frozenset(
    {"continue", "done", "finish", "got it", "next", "practice", "review", "skip", "start"}
    | {"try again"}
)
_SENSITIVE_SEGMENTS = frozenset(
    {"admin", "api", "auth", "oauth", "sso", "pricing", "plus", "inbox", "compose"}
)
_FIELD_CONSEQUENCES: dict[BrowserFieldKind, BrowserActionConsequence] = {
    BrowserFieldKind.PASSWORD: C.AUTHENTICATION_CHANGE,
    BrowserFieldKind.ONE_TIME_CODE: C.AUTHENTICATION_CHANGE,
    BrowserFieldKind.PAYMENT: C.PAYMENT,
    BrowserFieldKind.IDENTITY: C.SECURITY_CHANGE,
    BrowserFieldKind.EMAIL: C.SECURITY_CHANGE,
    BrowserFieldKind.TELEPHONE: C.SECURITY_CHANGE,
    BrowserFieldKind.FILE: C.FILE_TRANSFER,
}
_TYPED_FIELDS = frozenset(
    {BrowserFieldKind.TEXT, BrowserFieldKind.SEARCH, BrowserFieldKind.MULTILINE}
)
# Never CHOICE: an arrow key checks another radio in the group, whose labels
# nobody classified. Select and check cover choice controls instead.
_PRESSED_FIELDS = _TYPED_FIELDS | {BrowserFieldKind.NONE}
_UNCLICKABLE_FIELDS = frozenset(
    {
        BrowserFieldKind.PASSWORD,
        BrowserFieldKind.ONE_TIME_CODE,
        BrowserFieldKind.PAYMENT,
        BrowserFieldKind.IDENTITY,
        BrowserFieldKind.EMAIL,
        BrowserFieldKind.TELEPHONE,
        BrowserFieldKind.FILE,
        BrowserFieldKind.URL,
        BrowserFieldKind.OTHER,
    }
)
_NAVIGATING_KINDS = frozenset({BrowserActionKind.CLICK, BrowserActionKind.PRESS})
# Enter submits a form from a field or a button; Space activates a focused button.
_SUBMITTING_KEYS = frozenset({BrowserKey.ENTER, BrowserKey.SPACE})
# Coverage rule 8's per-action cap; a task grant's constraint carries the same.
MAXIMUM_COVERED_TEXT_CHARACTERS = 256
_CONSONANTS = frozenset("bcdfghjklmnpqrstvwxyz")


def _word_forms(word: str) -> frozenset[str]:
    """``word`` and its generated inflections (security review G4)."""

    forms = {word, *_IRREGULAR_FORMS.get(word, ())}
    if not word.isascii() or not word.isalpha():
        return frozenset(forms)
    forms.update(word + suffix for suffix in ("s", "es", "d", "ed", "ing", "er", "ers"))
    stem, last = word[:-1], word[-1]
    if last == "e":
        forms.update(stem + suffix for suffix in ("ing", "ed", "er"))
    if last == "y":
        forms.update(stem + suffix for suffix in ("ied", "ies"))
    if last in _CONSONANTS:
        forms.update(word + last + suffix for suffix in ("ed", "ing", "er"))
    return frozenset(forms)


_Phrase = tuple[frozenset[str], tuple[str, ...]]
_Category = tuple[BrowserActionConsequence, frozenset[str], tuple[_Phrase, ...]]


def _compile() -> tuple[_Category, ...]:
    categories = []
    for consequence, spec in _VOCABULARY:
        words: set[str] = set()
        phrases: list[_Phrase] = []
        entries = [entry.split() for entry in spec.split("|")]
        for tokens in entries[0:1]:
            for word in tokens:
                words.update(_word_forms(word))
        for tokens in entries[1:]:
            phrases.append((_word_forms(tokens[0]), tuple(tokens[1:])))
        categories.append((consequence, frozenset(words), tuple(phrases)))
    return tuple(categories)


# Generated once, at import, into a frozen lookup.
_CATEGORIES = _compile()
_CATEGORY_ORDER = tuple(consequence for consequence, _words, _phrases in _CATEGORIES)


# Letters that display like a Latin letter once case-folded and stripped of
# marks: Cyrillic, Greek and Armenian lookalikes, and Latin small capitals and
# IPA letters NFKC leaves alone, abridged from Unicode TR39's confusables, with
# the digits it confuses with letters. A capital that looks Latin can case-fold
# to a letter that looks like another one, so an ambiguous letter has a second
# reading.
_LOOKALIKES = str.maketrans(
    {
        # Cyrillic.
        "\u0430": "a",
        "\u0432": "b",
        "\u0441": "c",
        "\u0501": "d",
        "\u0435": "e",
        "\u04bb": "h",
        "\u043d": "h",
        "\u0456": "i",
        "\u0458": "j",
        "\u043a": "k",
        "\u04cf": "l",
        "\u043c": "m",
        "\u043f": "n",
        "\u043e": "o",
        "\u0440": "p",
        "\u051b": "q",
        "\u0455": "s",
        "\u0442": "t",
        "\u051d": "w",
        "\u0445": "x",
        "\u0443": "y",
        "\u04af": "y",
        "\u044c": "b",
        # Greek.
        "\u03b1": "a",
        "\u03b2": "b",
        "\u03b5": "e",
        "\u03b6": "z",
        "\u03b7": "h",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bc": "m",
        "\u03bd": "n",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u03c5": "y",
        "\u03c7": "x",
        "\u03b3": "y",
        "\u03c9": "w",
        "\u03f2": "c",
        "\u03f3": "j",
        # Armenian.
        "\u0585": "o",
        "\u057d": "u",
        "\u0578": "n",
        "\u0570": "h",
        "\u0581": "g",
        "\u0566": "q",
        # Latin small capitals and IPA letters.
        "\u1d00": "a",
        "\u0299": "b",
        "\u1d04": "c",
        "\u1d05": "d",
        "\u1d07": "e",
        "\ua730": "f",
        "\u0262": "g",
        "\u029c": "h",
        "\u026a": "i",
        "\u1d0a": "j",
        "\u1d0b": "k",
        "\u029f": "l",
        "\u1d0d": "m",
        "\u0274": "n",
        "\u1d0f": "o",
        "\u1d18": "p",
        "\u0280": "r",
        "\ua731": "s",
        "\u1d1b": "t",
        "\u1d1c": "u",
        "\u1d20": "v",
        "\u1d21": "w",
        "\u028f": "y",
        "\u1d22": "z",
        "\u0251": "a",
        "\u0261": "g",
        "\u0131": "i",
        "\u0237": "j",
        "\u0269": "i",
        "\u01c0": "l",
        # Digits.
        "0": "o",
        "1": "l",
    }
)
_LOOKALIKES_SECOND = str.maketrans(
    {"\u03b7": "n", "\u03bd": "v", "\u03bc": "u", "\u03c5": "u", "\u043f": "n"}
)
# The right-to-left override, and the controls that open and close an
# embedding or override, which it nests with.
_RIGHT_TO_LEFT_OVERRIDE = "\u202e"
_EMBEDDING_OPENERS = frozenset("\u202a\u202b\u202d\u202e")
_EMBEDDING_CLOSER = "\u202c"
_PARAGRAPH_ENDS = frozenset("\u000a\u000d\u2029\u0085")


def normalize_text(value: str) -> str:
    """The design's ``N(s)``: fold compatibility, case, format and marks away.

    NFKC; drop format characters (zero-width, soft hyphen, bidi marks) and
    turn controls into spaces; casefold; NFKD and drop combining marks; turn
    everything that is not a letter or a number into a space; collapse spaces.
    """

    text = unicodedata.normalize("NFKC", value)
    kept = []
    for character in text:
        category = unicodedata.category(character)
        if category == "Cf":
            continue
        kept.append(" " if category == "Cc" else character)
    text = unicodedata.normalize("NFKD", "".join(kept).casefold())
    text = "".join(character for character in text if unicodedata.category(character) != "Mn")
    text = "".join(
        character if unicodedata.category(character)[0] in "LN" else " " for character in text
    )
    return " ".join(text.split())


def _displayed_order(value: str) -> str:
    """``value`` with each right-to-left override shown as it displays.

    An override reverses everything after it, nested controls included, up
    to its closing control or the end of the paragraph; bidirectional controls
    themselves are dropped later by normalization.
    """

    shown: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != _RIGHT_TO_LEFT_OVERRIDE:
            shown.append(value[index])
            index += 1
            continue
        depth, end = 1, index + 1
        while end < len(value) and value[end] not in _PARAGRAPH_ENDS:
            if value[end] in _EMBEDDING_OPENERS:
                depth += 1
            elif value[end] == _EMBEDDING_CLOSER:
                depth -= 1
                if depth == 0:
                    break
            end += 1
        shown.extend(reversed(value[index + 1 : end]))
        index = end + 1 if end < len(value) and depth == 0 else end
    return "".join(shown)


def _readings(value: str) -> frozenset[str]:
    """Every normalized way ``value`` can read: as written, as a right-to-left
    override displays it, and with lookalike letters read as Latin ones."""

    orders = {value}
    if _RIGHT_TO_LEFT_OVERRIDE in value:
        orders.add(_displayed_order(value))
    readings = set()
    for order in orders:
        text = normalize_text(order)
        readings.update(
            {
                text,
                text.translate(_LOOKALIKES),
                text.translate(_LOOKALIKES_SECOND).translate(_LOOKALIKES),
            }
        )
    return frozenset(readings - {""})


def _has_currency_symbol(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Sc"
        for character in unicodedata.normalize("NFKC", value)
    )


def _named_consequence(text: str) -> BrowserActionConsequence | None:
    """The first category whose vocabulary ``text`` hits, or a currency symbol."""

    if _has_currency_symbol(text):
        return C.PAYMENT
    readings = [reading.split() for reading in _readings(text)]
    for consequence, words, phrases in _CATEGORIES:
        for tokens in readings:
            if any(token in words for token in tokens):
                return consequence
            for first_forms, rest in phrases:
                width = len(rest)
                for index, token in enumerate(tokens):
                    if (
                        token in first_forms
                        and tuple(tokens[index + 1 : index + 1 + width]) == rest
                    ):
                        return consequence
    return None


def path_is_within_prefix(path: str, path_prefix: str) -> bool:
    """Whether ``path`` is ``path_prefix`` or below it, on segment boundaries.

    The comparison is case-sensitive, and a path with a dot segment, written
    plainly or percent-encoded, is never within any prefix.
    """

    if any(unquote(segment) in {".", ".."} for segment in path.split("/")):
        return False
    return path == path_prefix or path.startswith(path_prefix + "/")


def segment_is_sensitive(segment: str) -> bool:
    """A path segment is sensitive when it hits the exclusion vocabulary, holds
    a currency symbol, or names an administrative, authentication, pricing or
    messaging area."""

    decoded = unquote(segment, errors="replace")
    if not _SENSITIVE_SEGMENTS.isdisjoint({decoded.casefold(), *_readings(decoded)}):
        return True
    return _named_consequence(decoded) is not None


def path_is_sensitive(path: str) -> bool:
    """Whether any non-empty segment of ``path`` is sensitive."""

    return any(segment_is_sensitive(segment) for segment in path.split("/") if segment)


def typed_text_allowed(value: str) -> bool:
    """Coverage rule 9: no address, URL, web host, four-digit run or credential shape."""

    text = unicodedata.normalize("NFKC", value)
    if "@" in text or "://" in text or "www." in text.casefold():
        return False
    run = 0
    for character in text:
        run = run + 1 if unicodedata.category(character) == "Nd" else 0
        if run >= 4:
            return False
    return CREDENTIAL_SHAPE.search(text) is None


def _label_texts(labels: Sequence[str], facts: BrowserElementFacts | None) -> list[str]:
    texts = list(labels)
    if facts is not None:
        texts.extend(facts.labels.values())
    return texts


def _targets(
    kind: BrowserActionKind, facts: BrowserElementFacts | None
) -> list[BrowserTargetFacts]:
    if facts is None or kind not in _NAVIGATING_KINDS:
        return []
    return [target for target in (facts.link_target, facts.form_target) if target is not None]


def classify_browser_action(
    *,
    kind: BrowserActionKind,
    role: str,
    labels: Sequence[str],
    facts: BrowserElementFacts | None,
    option_texts: Sequence[str],
) -> BrowserActionConsequence:
    """One action's consequence; the first matching rule wins.

    ``labels`` is every label text the caller has, and ``facts.labels`` joins
    it. The texts classified are those labels, the dialog name, every option
    text and, for a click or key press, each target's first path segment.
    """

    label_texts = _label_texts(labels, facts)
    texts: list[str] = [*label_texts, *option_texts]
    targets = _targets(kind, facts)
    if facts is not None:
        texts.append(facts.context_name)
        texts.extend(target.first_segment for target in targets if target.first_segment)
        field_consequence = _FIELD_CONSEQUENCES.get(facts.field_kind)
        if field_consequence is not None:
            return field_consequence
        if facts.download:
            return C.FILE_TRANSFER
    if role.strip().casefold() == "switch":
        return C.SECURITY_CHANGE
    if any(_has_currency_symbol(text) for text in texts):
        return C.PAYMENT
    named = [consequence for text in texts if (consequence := _named_consequence(text))]
    if named:
        return min(named, key=_CATEGORY_ORDER.index)
    if kind is BrowserActionKind.SCROLL:
        return C.ROUTINE
    if kind is BrowserActionKind.CLICK and role in {"button", "link"}:
        normalized = {normalize_text(text) for text in label_texts} - {""}
        if (
            len(normalized) == 1
            and normalized <= _ROUTINE_NAMES
            and not any(target.sensitive_path for target in targets)
        ):
            return C.ROUTINE
    return C.UNKNOWN


def _refused(consequence: BrowserActionConsequence, reason: str) -> BrowserCoverage:
    return BrowserCoverage(covered=False, consequence=consequence, reason=reason)


def _origin_of(url: str) -> str | None:
    try:
        return browser_origin(url)
    except ValueError:
        return None


def _target_inside(target: BrowserTargetFacts, path_prefix: str) -> bool:
    return (
        target.same_origin and target.first_segment == path_prefix[1:] and not target.sensitive_path
    )


def task_grant_coverage(
    *,
    action: BrowserAction,
    page_url: str,
    role: str,
    labels: Sequence[str],
    facts: BrowserElementFacts | None,
    option_texts: Sequence[str],
    origin: str,
    path_prefix: str,
) -> BrowserCoverage:
    """Whether a task grant for ``origin`` and ``path_prefix`` covers ``action``.

    Rules run in the design's order and the first that fails names the reason.
    The worker and the isolated runtime run the same rules.
    """

    consequence = classify_browser_action(
        kind=action.kind, role=role, labels=labels, facts=facts, option_texts=option_texts
    )
    if facts is None:
        return _refused(consequence, "facts_unavailable")
    if _origin_of(page_url) != origin:
        return _refused(consequence, "outside_origin")
    path = urlsplit(page_url).path
    if not path_is_within_prefix(path, path_prefix):
        return _refused(consequence, "outside_prefix")
    if path_is_sensitive(path):
        return _refused(consequence, "sensitive_path")
    kind = action.kind
    if kind is not BrowserActionKind.SCROLL and not any(
        normalize_text(text) for text in _label_texts(labels, facts)
    ):
        return _refused(consequence, "unnamed_element")
    if consequence not in {C.ROUTINE, C.UNKNOWN}:
        return _refused(consequence, f"excluded.{consequence.value}")
    field = facts.field_kind
    field_covered = (
        (kind is BrowserActionKind.TYPE and field in _TYPED_FIELDS)
        or (
            kind in {BrowserActionKind.SELECT, BrowserActionKind.CHECK}
            and field is BrowserFieldKind.CHOICE
        )
        or (kind is BrowserActionKind.PRESS and field in _PRESSED_FIELDS)
        or (kind is BrowserActionKind.CLICK and field not in _UNCLICKABLE_FIELDS)
        or kind is BrowserActionKind.SCROLL
    )
    if not field_covered:
        return _refused(consequence, "field_not_covered")
    if kind is BrowserActionKind.TYPE:
        value = action.value or ""
        if len(value) > MAXIMUM_COVERED_TEXT_CHARACTERS:
            return _refused(consequence, "text_too_long")
        if not typed_text_allowed(value):
            return _refused(consequence, "text_not_covered")
    link = facts.link_target
    if kind in _NAVIGATING_KINDS and link is not None and not _target_inside(link, path_prefix):
        return _refused(consequence, "link_outside_prefix")
    # Any click may land on a submit button, whatever its role or its field
    # kind, so every click on an element with a form counts as a submission.
    form = facts.form_target
    submits = kind is BrowserActionKind.CLICK or (
        kind is BrowserActionKind.PRESS and action.key in _SUBMITTING_KEYS
    )
    if submits and form is not None and not _target_inside(form, path_prefix):
        return _refused(consequence, "form_outside_prefix")
    return BrowserCoverage(covered=True, consequence=consequence)


def standing_ceiling_coverage(
    *,
    action: BrowserAction,
    page_url: str,
    role: str,
    labels: Sequence[str],
    facts: BrowserElementFacts | None,
    option_texts: Sequence[str],
    origins: Sequence[str],
) -> BrowserCoverage:
    """A standing grant's ceiling: one of its origins, and a routine action.

    ``facts`` may be absent, because standing grants predate facts; every
    label the caller has must still read as the same routine word.
    """

    consequence = classify_browser_action(
        kind=action.kind, role=role, labels=labels, facts=facts, option_texts=option_texts
    )
    if _origin_of(page_url) not in set(origins):
        return _refused(consequence, "outside_origin")
    if consequence is not C.ROUTINE:
        return _refused(consequence, f"excluded.{consequence.value}")
    return BrowserCoverage(covered=True, consequence=consequence)


def _normalized_origins(origins: Iterable[str]) -> set[str]:
    normalized = set()
    for origin in origins:
        try:
            normalized.add(normalize_browser_origin(origin))
        except ValueError:
            continue
    return normalized


def dispatch_constraint_coverage(
    constraint: BrowserDispatchConstraint,
    *,
    action: BrowserAction,
    page_url: str,
    role: str,
    labels: Sequence[str],
    facts: BrowserElementFacts | None,
    option_texts: Sequence[str],
    runtime_origins: Sequence[str],
    now: datetime,
) -> BrowserCoverage:
    """The isolated runtime's check of a grant-authorized act against the live page.

    The constraint only narrows: it must not have expired, its origins must be
    ones the runtime already allows, and the live page and element must pass
    the grant kind's coverage rules.
    """

    if now >= constraint.not_after:
        consequence = classify_browser_action(
            kind=action.kind, role=role, labels=labels, facts=facts, option_texts=option_texts
        )
        return _refused(consequence, "expired")
    if not set(constraint.origins) <= _normalized_origins(runtime_origins):
        consequence = classify_browser_action(
            kind=action.kind, role=role, labels=labels, facts=facts, option_texts=option_texts
        )
        return _refused(consequence, "outside_origin")
    if constraint.grant_kind == "task" and constraint.path_prefix is not None:
        return task_grant_coverage(
            action=action,
            page_url=page_url,
            role=role,
            labels=labels,
            facts=facts,
            option_texts=option_texts,
            origin=constraint.origins[0],
            path_prefix=constraint.path_prefix,
        )
    if constraint.grant_kind == "standing":
        return standing_ceiling_coverage(
            action=action,
            page_url=page_url,
            role=role,
            labels=labels,
            facts=facts,
            option_texts=option_texts,
            origins=constraint.origins,
        )
    consequence = classify_browser_action(
        kind=action.kind, role=role, labels=labels, facts=facts, option_texts=option_texts
    )
    return _refused(consequence, "outside_prefix")
