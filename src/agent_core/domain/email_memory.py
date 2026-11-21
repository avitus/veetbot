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
# The mark a saved assessment carries until its thread's memories have followed
# it. The re-rating clears it in its own transaction, so a refresh that stops
# between the two leaves it for the next one to find.
RERATING_PENDING = "rerating_pending"
# A follow-up that leaves the mark standing is counted and the next one put off,
# so debts that keep failing cannot hold back the ones behind them.
RERATING_ATTEMPTS = "rerating_attempts"
RERATING_RETRY_AT = "rerating_retry_at"
_RERATING_RETRY_CEILING = timedelta(days=1)
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
    `owner_lowered` is true when the owner's Less important is what rates the
    thread.
    """

    importance: float
    owner_marked_important: bool
    owner_replied: bool
    known_sender: bool
    rated: bool = True
    complete: bool = True
    sender_seen: bool = True
    sender_unverified: bool = False
    owner_lowered: bool = False

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


def reset_rating(thread: EmailThread) -> bool:
    """Whether the thread's stored rating is the zero a changed thread is reset to."""
    return not thread.assessment_version and thread.priority == 0


def reads_current_source(thread: EmailThread, assessment: Mapping[str, object] | None) -> bool:
    """Whether a saved assessment read the thread's messages as they are stored now.

    Each assessment records the revision of the messages it read. A thread
    stored before revisions were kept has none, and nothing shows what an
    assessment of it read.
    """
    return (
        assessment is not None
        and bool(thread.source_fingerprint)
        and assessment.get("source_fingerprint") == thread.source_fingerprint
    )


def rerating_attempts(assessment: Mapping[str, object]) -> int:
    """How many follow-ups of this saved assessment left its mark standing."""
    attempts = assessment.get(RERATING_ATTEMPTS)
    return attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0


def rerating_due(assessment: Mapping[str, object], now: datetime) -> bool:
    """Whether a marked assessment's next follow-up may run now."""
    try:
        return datetime.fromisoformat(str(assessment.get(RERATING_RETRY_AT))) <= now
    except (TypeError, ValueError):
        return True


def rerating_put_off(assessment: Mapping[str, object], now: datetime) -> dict[str, object]:
    """The assessment with one more failed follow-up counted and the next one put off.

    The wait doubles with each failure, from two minutes up to a day, so a
    debt that cannot be paid costs one attempt a day and a passing refusal is
    retried by the next refresh or the one after.
    """
    attempts = rerating_attempts(assessment) + 1
    wait = min(_RERATING_RETRY_CEILING, timedelta(minutes=2 ** min(attempts, 16)))
    return {
        **assessment,
        RERATING_ATTEMPTS: attempts,
        RERATING_RETRY_AT: (now + wait).isoformat(),
    }


def rerating_settled(assessment: Mapping[str, object]) -> dict[str, object]:
    """The assessment without its mark, or anything a follow-up noted beside it."""
    owed = {RERATING_PENDING, RERATING_ATTEMPTS, RERATING_RETRY_AT}
    return {key: value for key, value in assessment.items() if key not in owed}


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

    A rating is known when the owner's own feedback gave it, or a grounded
    assessment of the thread as it is stored now. Three figures are only
    floors, good for lengthening a memory and never for shortening it: the
    content share of an assessment record saved before ratings were kept, the
    rating an assessment saved without a grounded reading, and the rating of a
    reset thread's last assessment when that assessment read another revision
    of its messages. A rejected or ungrounded result, and a junk first pass,
    save zero features and with them a zero that reads no part of the thread.
    """
    rating: float | None = thread.priority
    known = True
    if reset_rating(thread):
        last = last_rating(assessment)
        rating, known = (None, False) if last is None else last
        # A message came or went since: what was read then is not the thread now.
        known = known and reads_current_source(thread, assessment)
    if assessment is not None and assessment.get("grounded") is False:
        known = False
    judgment = owner_judgment(thread, feedback)
    if judgment == "important":
        return 1.0, True, True
    if judgment == "less_important":
        return 0.0, False, True
    return rating, False, known


def owner_judgment(thread: EmailThread, feedback: Sequence[EmailFeedback]) -> str | None:
    """The owner's last surviving importance judgment that matches the thread, if any."""
    judgment: str | None = None
    matching = [item for item in feedback if feedback_matches(item, thread)]
    for item in sorted(matching, key=lambda value: (value.created_at, str(value.id))):
        if item.judgment in {"important", "less_important"}:
            judgment = item.judgment
    return judgment


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


@dataclass(frozen=True, slots=True)
class _Attribution:
    """What a statement's own attribution says, and the claim that follows it.

    `source` is the mailbox, thread and message a record line names, as the
    line holds them; a plain statement names none.
    """

    sender: str
    sent_on: str
    source: str
    claim: str


_RECORD_OPENING = "Email from"


def _collapsed(text: str) -> str:
    """Text as a stored statement holds it: every run of whitespace is one space."""
    return " ".join(text.split())


def _record_attribution(statement: str, sender: str | None = None) -> _Attribution | None:
    """A record line's own header, or None when the statement is not a record line.

    The header is the only place a statement is read for a date or for the
    message it came from. It ends at the first dated mailbox clause, so a
    record line the claim quotes is part of the claim. With `sender`, the
    stored message's own, the header must open with exactly that sender, so a
    display name that holds a clause of its own is never read as one.
    """
    if not statement.startswith(f"{_RECORD_OPENING} "):
        return None
    if sender is None:
        head = _GUESS_HEAD.search(statement, len(_RECORD_OPENING))
        plain = _PLAIN_HEAD.search(statement)
        if head is not None and plain is not None and plain.start() < head.start():
            # A plain statement whose sender's name begins "Email from".
            return None
    else:
        named = _collapsed(sender)
        opening = f"{_RECORD_OPENING} {named}" if named else _RECORD_OPENING
        head = _GUESS_HEAD.match(statement, len(opening)) if statement.startswith(opening) else None
    if head is None:
        return None
    source, found, claim = statement[head.end() :].partition(") reports ")
    if not found:
        return None
    return _Attribution(statement[len(_RECORD_OPENING) + 1 : head.start()], head[1], source, claim)


def _plain_attribution(statement: str, sender: str | None = None) -> _Attribution | None:
    """A plain statement's own attribution: its sender, then the first dated clause.

    With `sender`, the stored message's own, the statement must open with
    exactly that sender, so a display name that holds "said by email on
    <date> that" supplies neither the sender nor the date.
    """
    if sender is None:
        head = _PLAIN_HEAD.search(statement)
    else:
        named = _collapsed(sender)
        head = _PLAIN_HEAD.match(statement, len(named)) if statement.startswith(named) else None
    if head is None or head.start() == 0 or head.end() == len(statement):
        return None
    return _Attribution(statement[: head.start()], head[1], "", statement[head.end() :])


def _source_ids(account_id: str, thread_id: str, message_id: str) -> str:
    """How a record line's header names this message, as a stored statement holds it."""
    return _collapsed(f"{account_id}, thread {thread_id}, message {message_id}")


def names_message(
    statement: str, account_id: str, thread_id: str, message_id: str, sender: str | None = None
) -> bool:
    """Whether a statement is the record line of this very message.

    A guess says which message formed it. That identifies the message by
    itself, where a date can be shared by two. Only the line's own header is
    read: an email the claim quotes names nothing. `sender` is the stored
    message's sender when the caller holds it.
    """
    header = _record_attribution(statement, sender)
    return header is not None and header.source == _source_ids(account_id, thread_id, message_id)


def names_thread(statement: str, account_id: str, thread_id: str) -> bool:
    """Whether a statement is the record line of some message of this thread."""
    header = _record_attribution(statement)
    opening = _collapsed(f"{account_id}, thread {thread_id}, message")
    return header is not None and header.source.startswith(f"{opening} ")


def named_message(statement: str) -> tuple[str, str, str] | None:
    """The mailbox, thread and message a record line's own header names, if it names one.

    This says only where to look. Whether that message formed the memory is
    for its retained record to say, or for `names_message` on a record that
    keeps no role.
    """
    header = _record_attribution(statement)
    if header is None:
        return None
    account_id, first, rest = header.source.partition(", thread ")
    thread_id, second, message_id = rest.partition(", message ")
    return (account_id, thread_id, message_id) if first and second and message_id else None


def restated(
    statement: str,
    *,
    account_id: str,
    thread_id: str,
    message_id: str,
    first_class: bool,
    direct: bool | None = None,
    sender: str | None = None,
) -> str | None:
    """The same claim in the other standing's words, or None when it is not this source's.

    A stored statement has its whitespace collapsed, so the identifiers are
    compared the same way. The sender, the date and the message are read from
    the statement's own attribution and nowhere else.

    - A guess becomes first-class only from this message's own record line. A
      record line whose header names another message is left alone, whatever
      its claim holds: an email it quotes is never read as its sender.
    - A first-class statement returns to a guess from its plain wording. Its
      sender may itself begin "Email from", or hold a whole record line.

    `direct` is the row's own standing, which says which wording it is in. A
    sender's display name can hold a whole record line, and a guess's claim can
    quote an email, so the words alone can be read both ways. Without it the
    statement is read as the wording whose attribution comes first.

    `sender` is the stored message's sender. With it the attribution must open
    with exactly that sender: a display name the sender chose then supplies
    neither a date nor a message. A promotion requires that match. A demotion
    takes standing away, so when the stored sender no longer opens the
    statement it falls back to the first attribution the words hold.

    A statement in neither wording is left alone.
    """
    if direct is None:
        wording = _wording(statement)
        if wording is None:
            return None
        direct = wording[0] == "plain"
    if direct:
        if first_class:
            return None
        found = _plain_attribution(statement, sender)
        if found is None and sender is not None:
            found = _plain_attribution(statement)
    else:
        found = _record_attribution(statement, sender)
        if found is not None and found.source != _source_ids(account_id, thread_id, message_id):
            found = None
    if found is None:
        return None
    if first_class:
        return first_class_statement(found.sender, found.sent_on, found.claim)
    return _collapsed(
        guess_statement(found.sender, found.sent_on, account_id, thread_id, message_id, found.claim)
    )


# A quote this long that also stands in an earlier message of the thread is that
# message's sentence. A shorter one, such as "Approved", two people can each write.
QUOTED_SENTENCE_WORDS = 6


def _wording(statement: str) -> tuple[str, str] | None:
    """Which email wording a statement reads as, and the claim past its attribution.

    Whichever attribution ends first is taken as the statement's own; the other,
    if it appears at all, is text inside the claim or the sender's name.
    """
    guess = _GUESS_HEAD.search(statement) if statement.startswith("Email from ") else None
    plain = _PLAIN_HEAD.search(statement)
    if guess is not None and (plain is None or guess.start() < plain.start()):
        _, found, claim = statement[guess.end() :].partition(") reports ")
        return ("guess", claim) if found else None
    return None if plain is None else ("plain", statement[plain.end() :])


def _subject_length(claim: str, subject: str | None) -> int:
    """How much of a claim is its subject, as far as the stored subject can say.

    A claim opens with the subject the email fact named. A memory is stored
    under that subject, or, when it is linked to a person, under
    ``person:<who>:<that subject>``, where ``<who>`` holds identifiers and none
    of the email's words. Zero when the claim opens with neither.
    """
    named = " ".join((subject or "").split())
    if named and claim.startswith(f"{named} "):
        return len(named)
    at = named.find(":") if named.startswith("person:") else -1
    while at >= 0:
        rendered = named[at + 1 :].strip()
        if rendered and claim.startswith(f"{rendered} "):
            return len(rendered)
        at = named.find(":", at + 1)
    return 0


def stated_value(statement: str, subject: str | None = None) -> str | None:
    """The value an email statement quotes at its end, in either wording.

    The claim reads ``<subject> <predicate>: "<value>".`` and the value opens at
    the first ``: "`` after the subject, so a quotation inside the subject or
    inside the value does not move it. `subject` is the memory's stored subject,
    which names the claim's subject directly or after a person's identity. When
    it names neither, the first ``: "`` of the claim opens the value.
    """
    if not attributed_email_statement(statement):
        return None
    wording = _wording(statement)
    if wording is None or not wording[1].endswith('".'):
        return None
    claim = wording[1]
    opened = claim.find(': "', _subject_length(claim, subject))
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
