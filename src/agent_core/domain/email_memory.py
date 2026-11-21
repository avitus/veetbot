"""How long an email memory is kept, and whether it is first-class (ADR-0340)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.email import (
    EmailFeedback,
    EmailThread,
    assessment_priority,
    feedback_matches,
)
from agent_core.domain.memory import EMAIL_RETENTION_POLICY as EMAIL_RETENTION_POLICY

# The policy whose memories the table governs; the evaluated @1 policy is unchanged.
EMAIL_STANDING_POLICY = "email-semantic@2"
# A guess keeps the hypothesis ceiling; a first-class memory takes the ceiling
# every automatically captured chat memory has.
GUESS_CONFIDENCE_CEILING = 0.4
FIRST_CLASS_CONFIDENCE_CEILING = 0.65
_ONE_YEAR_DAYS = 365
_RETENTION_LABELS = {None: "no_expiry", 365: "1_year", 90: "90_days", 30: "30_days"}
_DATE = r"\d{4}-\d{2}-\d{2}"


@dataclass(frozen=True, slots=True)
class EmailMemoryStanding:
    """What stored state says about one message's thread and sender.

    `rated` is false when the thread's importance is not a known rating. Either
    nothing says how important it is: its stored rating was reset by a new
    message and no earlier assessment or owner feedback survives. Or
    `importance` is only a floor: an older assessment record's content share,
    or the zero an abstaining assessment saved. A floor lengthens and never
    shortens. `complete` is false when the stored thread holds only part of its
    messages, so an owner reply may sit in the part not held. `sender_seen` is
    false when this message is not among those held. `sender_unverified` is
    true when the address is one the owner knows but Gmail gave no verdict for
    the message: the sender is not known, and nothing shows it is anyone else.
    """

    importance: float
    owner_marked_important: bool
    owner_replied: bool
    known_sender: bool
    rated: bool = True
    complete: bool = True
    sender_seen: bool = True
    sender_unverified: bool = False

    @property
    def retention_days(self) -> int | None:
        """Days kept after the last evidence date; None is no expiry."""
        if self.owner_marked_important or self.owner_replied or self.importance >= 0.85:
            return None
        if self.importance >= 0.6:
            return _ONE_YEAR_DAYS
        if self.importance >= 0.3:
            return 90
        return 30

    @property
    def retention(self) -> str:
        return retention_label(self.retention_days)

    @property
    def kept_a_year(self) -> bool:
        days = self.retention_days
        return days is None or days >= _ONE_YEAR_DAYS

    @property
    def first_class(self) -> bool:
        """A known sender, in a thread kept for a year or more; both are required."""
        return self.known_sender and self.kept_a_year

    @property
    def may_lower(self) -> bool:
        """Whether this reading is good enough to shorten or demote a memory.

        Raising needs only what is visible. Lowering needs a rating that is
        known and a thread held whole, so that "no owner reply" is a fact and
        not a gap.
        """
        return self.rated and self.complete

    @property
    def may_demote(self) -> bool:
        """Whether this reading takes first-class standing away from a memory.

        Either the sender is seen and is not someone the owner knows, or a
        known, complete reading keeps the thread under a year. A sender Gmail
        gave no verdict for takes nothing away: a stored message may have been
        cached before the check existed, and that says nothing about who wrote.
        """
        unknown = self.sender_seen and not self.known_sender and not self.sender_unverified
        return unknown or (self.may_lower and not self.kept_a_year)

    def expires_at(self, evidence_at: datetime) -> datetime | None:
        days = self.retention_days
        return None if days is None else evidence_at + timedelta(days=days)


UNRATED = EmailMemoryStanding(
    importance=0.0,
    owner_marked_important=False,
    owner_replied=False,
    known_sender=False,
    rated=False,
    complete=False,
    sender_seen=False,
)


def retention_label(days: int | None) -> str:
    return _RETENTION_LABELS.get(days, "other")


def kept_label(expires_at: datetime | None, evidence_at: datetime) -> str:
    """The tier a stored expiry corresponds to, for a report."""
    return retention_label(None if expires_at is None else (expires_at - evidence_at).days)


def last_rating(assessment: Mapping[str, object] | None) -> tuple[float, bool] | None:
    """The rating a thread's last saved assessment gave it, and whether it is exact.

    The assessment record keeps the rating itself. A record saved before that
    holds only the model's features; without the reply history of its day the
    relationship share cannot be rebuilt, so the content share of the same
    formula stands in. That figure is a floor, not the rating: it may lengthen
    a memory's retention and never shortens it.
    """
    if assessment is None:
        return None
    stored = assessment.get("priority")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool) and 0 <= stored <= 1:
        return float(stored), True
    content, urgency = assessment.get("content_importance"), assessment.get("urgency")
    if not (
        isinstance(content, (int, float))
        and isinstance(urgency, (int, float))
        and 0 <= content <= 1
        and 0 <= urgency <= 1
    ):
        return None
    floor = assessment_priority(
        float(content), 0.0, float(urgency), bulk=assessment.get("bulk") is True
    )
    return floor, False


def thread_importance(
    thread: EmailThread,
    feedback: Sequence[EmailFeedback],
    assessment: Mapping[str, object] | None = None,
) -> tuple[float | None, bool, bool]:
    """The thread's rating for memory, whether the owner marked it Important, and
    whether the rating is known.

    This is the saved Inbox rating after the owner's surviving importance
    feedback, the last judgment winning. The dated-attention rule that takes a
    finished event out of the Inbox is not applied: a past event's memory keeps
    the importance its email had.

    A new message resets the stored rating to zero until the thread is
    assessed again. That zero is not a rating: the last saved assessment
    answers instead, and with none the importance is unknown (None).

    A rating is known when a grounded assessment or the owner's own feedback
    gave it. Two figures are only floors, good for lengthening a memory and
    never for shortening it: the content share of an assessment record saved
    before ratings were kept, and the rating an assessment saved without a
    grounded reading. A rejected or ungrounded result, and a junk first pass,
    save zero features and with them a zero that reads no part of the thread.
    """
    rating: float | None = thread.priority
    known = True
    if not thread.assessment_version and thread.priority == 0:
        last = last_rating(assessment)
        rating, known = (None, False) if last is None else last
    if assessment is not None and assessment.get("grounded") is False:
        known = False
    marked = False
    matching = [item for item in feedback if feedback_matches(item, thread)]
    for item in sorted(matching, key=lambda value: (value.created_at, str(value.id))):
        if item.judgment == "important":
            rating, marked, known = 1.0, True, True
        elif item.judgment == "less_important":
            rating, marked, known = 0.0, False, True
    return rating, marked, known


def claim_text(subject: str, predicate: str, value: str) -> str:
    return f'{subject} {predicate}: "{value}".'


def guess_statement(
    sender: str, sent_on: str, account_id: str, thread_id: str, message_id: str, claim: str
) -> str:
    """Today's record line: a short-lived attributed guess."""
    return (
        f"Email from {sender} on {sent_on} "
        f"(mailbox {account_id}, thread {thread_id}, message {message_id}) reports {claim}"
    )


def first_class_statement(sender: str, sent_on: str, claim: str) -> str:
    """A plain statement that still says who said it, by what channel, and when."""
    return f"{sender} said by email on {sent_on} that {claim}"


# Where each wording's attribution ends. Both are searched for, not matched from
# the start, so reading a statement stays linear in its length.
_GUESS_HEAD = re.compile(rf" on ({_DATE}) \(mailbox ")
_PLAIN_HEAD = re.compile(rf" said by email on ({_DATE}) that ")


def attributed_email_statement(statement: str) -> bool:
    """Whether a stored statement is still one of the two email wordings.

    An owner statement can take an email guess over in place. The row keeps its
    email policy label and holds the owner's words, and from then on it is the
    owner's memory: neither the retention table nor a later email touches it.
    """
    return (
        re.fullmatch(rf"Email from .+ on {_DATE} \(mailbox .+\) reports .+", statement) is not None
        or re.fullmatch(rf".+ said by email on {_DATE} that .+", statement) is not None
    )


def restated(
    statement: str,
    *,
    account_id: str,
    thread_id: str,
    message_id: str,
    first_class: bool,
) -> str | None:
    """The same claim in the other standing's words, or None when it is not this source's.

    A stored statement has its whitespace collapsed, so the identifiers are
    compared the same way. The standing asked for says which wording the
    statement is in now, so the text never has to be guessed at:

    - A guess becomes first-class only from this message's own record line. A
      record line that names another message is left alone.
    - A first-class statement returns to a guess from its plain wording. Its
      sender is whatever stands before the first "said by email on <date>
      that", and may itself begin "Email from".

    A statement in neither wording is left alone.
    """
    marker = " ".join(
        f" (mailbox {account_id}, thread {thread_id}, message {message_id}) reports ".split()
    )
    head, found, claim = statement.partition(f" {marker} ")
    guess = (
        re.fullmatch(rf"Email from (?P<sender>.+) on (?P<date>{_DATE})", head) if found else None
    )
    if guess is not None:
        sender, sent_on = guess["sender"], guess["date"]
    elif first_class:
        return None
    else:
        plain = _PLAIN_HEAD.search(statement)
        if plain is None or plain.start() == 0 or plain.end() == len(statement):
            return None
        sender, sent_on, claim = statement[: plain.start()], plain[1], statement[plain.end() :]
    if first_class:
        return first_class_statement(sender, sent_on, claim)
    return " ".join(
        guess_statement(sender, sent_on, account_id, thread_id, message_id, claim).split()
    )


# A quote this long that also stands in an earlier message of the thread is that
# message's sentence. A shorter one, such as "Approved", two people can each write.
QUOTED_SENTENCE_WORDS = 6


def _claim(statement: str) -> str | None:
    """The claim an email statement makes, past its attribution, in either wording.

    Whichever attribution ends first is the statement's own; the other, if it
    appears at all, is text inside the claim.
    """
    guess = _GUESS_HEAD.search(statement) if statement.startswith("Email from ") else None
    plain = _PLAIN_HEAD.search(statement)
    if guess is not None and (plain is None or guess.start() < plain.start()):
        _, found, claim = statement[guess.end() :].partition(") reports ")
        return claim if found else None
    return None if plain is None else statement[plain.end() :]


def stated_value(statement: str, subject: str | None = None) -> str | None:
    """The value an email statement quotes at its end, in either wording.

    The claim reads ``<subject> <predicate>: "<value>".`` and the value opens at
    the first ``: "`` after the subject, so a quotation inside the subject or
    inside the value does not move it. `subject` is the memory's stored subject.
    A memory linked to a person is stored under that person and not under the
    words its statement uses; then the first ``: "`` of the claim opens the value.
    """
    if not attributed_email_statement(statement):
        return None
    claim = _claim(statement)
    if claim is None or not claim.endswith('".'):
        return None
    named = " ".join((subject or "").split())
    opened = claim.find(': "', len(named) if named and claim.startswith(f"{named} ") else 0)
    return None if opened < 0 or opened + 3 > len(claim) - 2 else claim[opened + 3 : -2]


# Opening quotation marks and the mark that closes each: straight, curly and angle.
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "\N{LEFT DOUBLE QUOTATION MARK}": "\N{RIGHT DOUBLE QUOTATION MARK}",
    "\N{LEFT SINGLE QUOTATION MARK}": "\N{RIGHT SINGLE QUOTATION MARK}",
    "\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}": (
        "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"
    ),
}
_CLOSING_MARKS = ".,;:!?"


def value_key(value: str) -> str:
    """A value compared without case, spacing, wrapping quotes or a closing mark (ADR-0341).

    These are wording: a model that reads one message twice may take the full
    stop or the quotation marks with the value, or leave them. Every other mark
    is part of the value: ``-5 °C`` is not ``5 °C``, and ``3.5%`` is not ``3-5%``.
    """
    folded = " ".join(value.casefold().split())
    bare = folded
    while True:
        trimmed = bare.rstrip(_CLOSING_MARKS).rstrip()
        inner = trimmed[1:-1]
        if (
            len(trimmed) > 1
            and _QUOTE_PAIRS.get(trimmed[0]) == trimmed[-1]
            and trimmed[0] not in inner
            and trimmed[-1] not in inner
        ):
            trimmed = inner.strip()
        if trimmed == bare:
            break
        bare = trimmed
    return bare or folded


def claim_key(belief_type: str, subject: str, value: str) -> str:
    """One fact per message: its subject, belief type and value, whatever the wording."""
    identity = json.dumps([belief_type, " ".join(subject.casefold().split()), value_key(value)])
    return "claim:" + hashlib.sha256(identity.encode()).hexdigest()


def _unmarked(text: str) -> str:
    """Text with quote marks at line starts dropped and its wrapping collapsed."""
    return " ".join(re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", text).split())


def quoted_text(quote: str, body: str, earlier_bodies: Sequence[str]) -> bool:
    """Whether a fact's quote is quoted text in the message it names (ADR-0341).

    It is when it appears only on lines marked ``>``, or when it is a sentence of
    at least six words that an earlier message of the thread already holds,
    whatever the reply's layout. Marks and line wrapping are ignored.
    """
    needle = _unmarked(quote)
    if not needle:
        return False
    own = _unmarked(
        "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    )
    if needle not in own:
        return needle in _unmarked(body)
    if len(needle.split()) < QUOTED_SENTENCE_WORDS:
        return False
    return any(needle in _unmarked(earlier) for earlier in earlier_bodies)


class EmailMergeReport(BaseModel):
    """Counts only: what a merge of repeated email facts found and changed."""

    model_config = ConfigDict(extra="forbid")

    applied: bool = False
    examined: int = 0
    facts: int = 0
    superseded: int = 0
    other_subjects: int = 0


class EmailRerateReport(BaseModel):
    """Counts only: what a re-rating found and changed."""

    model_config = ConfigDict(extra="forbid")

    applied: bool = False
    examined: int = 0
    changed: int = 0
    promoted: int = 0
    demoted: int = 0
    first_class: int = 0
    guesses: int = 0
    by_retention: dict[str, int] = Field(default_factory=dict)
