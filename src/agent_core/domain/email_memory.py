"""How long an email memory is kept, and whether it is first-class (ADR-0340)."""

from __future__ import annotations

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

    `rated` is false when nothing says how important the thread is: its stored
    rating was reset by a new message and no earlier assessment or owner
    feedback survives. `complete` is false when the stored thread holds only
    part of its messages, so an owner reply may sit in the part not held.
    `sender_seen` is false when this message is not among those held.
    """

    importance: float
    owner_marked_important: bool
    owner_replied: bool
    known_sender: bool
    rated: bool = True
    complete: bool = True
    sender_seen: bool = True

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
    def first_class(self) -> bool:
        """A known sender, in a thread kept for a year or more; both are required."""
        days = self.retention_days
        return self.known_sender and (days is None or days >= _ONE_YEAR_DAYS)

    @property
    def may_lower(self) -> bool:
        """Whether this reading is good enough to shorten or demote a memory.

        Raising needs only what is visible. Lowering needs a rating that is
        known and a thread held whole, so that "no owner reply" is a fact and
        not a gap.
        """
        return self.rated and self.complete

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


def last_rating(assessment: Mapping[str, object] | None) -> float | None:
    """The rating a thread's last saved assessment gave it, if one survives.

    The assessment record keeps the rating itself. A record saved before that
    holds only the model's features; without the reply history of its day the
    relationship share cannot be rebuilt, so the content share of the same
    formula stands in as a floor.
    """
    if assessment is None:
        return None
    stored = assessment.get("priority")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool) and 0 <= stored <= 1:
        return float(stored)
    content, urgency = assessment.get("content_importance"), assessment.get("urgency")
    if not (
        isinstance(content, (int, float))
        and isinstance(urgency, (int, float))
        and 0 <= content <= 1
        and 0 <= urgency <= 1
    ):
        return None
    return assessment_priority(
        float(content), 0.0, float(urgency), bulk=assessment.get("bulk") is True
    )


def thread_importance(
    thread: EmailThread,
    feedback: Sequence[EmailFeedback],
    assessment: Mapping[str, object] | None = None,
) -> tuple[float | None, bool]:
    """The thread's rating for memory, and whether the owner marked it Important.

    This is the saved Inbox rating after the owner's surviving importance
    feedback, the last judgment winning. The dated-attention rule that takes a
    finished event out of the Inbox is not applied: a past event's memory keeps
    the importance its email had.

    A new message resets the stored rating to zero until the thread is
    assessed again. That zero is not a rating: the last saved assessment
    answers instead, and with none the importance is unknown (None).
    """
    rating: float | None = thread.priority
    if not thread.assessment_version and thread.priority == 0:
        rating = last_rating(assessment)
    marked = False
    matching = [item for item in feedback if feedback_matches(item, thread)]
    for item in sorted(matching, key=lambda value: (value.created_at, str(value.id))):
        if item.judgment == "important":
            rating, marked = 1.0, True
        elif item.judgment == "less_important":
            rating, marked = 0.0, False
    return rating, marked


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
    compared the same way. A statement that parses as neither form, or names
    another message, is left alone.
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
    else:
        plain = re.fullmatch(
            rf"(?P<sender>.+?) said by email on (?P<date>{_DATE}) that (?P<claim>.+)", statement
        )
        if plain is None or statement.startswith("Email from "):
            return None
        sender, sent_on, claim = plain["sender"], plain["date"], plain["claim"]
    if first_class:
        return first_class_statement(sender, sent_on, claim)
    return " ".join(
        guess_statement(sender, sent_on, account_id, thread_id, message_id, claim).split()
    )


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
