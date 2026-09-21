"""Owner-scoped bulk-sender subscriptions and unsubscribe consent (Milestone 31)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from agent_core.domain.email_base import EmailValue
from agent_core.domain.web import is_public_https_url

UNSUBSCRIBE_TOOL_NAME: Final = "email.unsubscribe"
SUBSCRIPTIONS_TOOL_NAME: Final = "email.subscriptions"
UNSUBSCRIBE_TARGET_KIND: Final = "unsubscribe_endpoint"
SUBSCRIPTION_BATCH_LIMIT: Final = 25
SUBSCRIPTION_THREAD_CAP: Final = 200
SUBSCRIPTION_VERIFY_LIMIT: Final = 25
SUBSCRIPTION_CONSENT_LIFETIME: Final = timedelta(seconds=120)
LABEL_THREADS_PER_CALL: Final = 25
LABEL_THREADS_PER_GESTURE: Final = 100

Offered = Literal["one_click", "mailto", "link", "none"]
Mechanism = Literal["one_click", "mailto", "none"]
SubscriptionState = Literal[
    "active", "kept", "pending", "unsubscribed", "failed", "still_sending", "reported_spam"
]
SubscriptionAction = Literal["unsubscribe", "report_spam", "not_spam"]

# States in which the sender can still be acted on, so its evidence is retained.
ACTIONABLE_STATES: Final[frozenset[str]] = frozenset({"active", "failed", "still_sending"})

# Identities reach a Gmail query, so only a plain list identifier or address forms one.
_LIST_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,253}[a-z0-9])?$")
_ADDRESS = re.compile(r"^[a-z0-9._%+=-]{1,64}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class EmailMailto(EmailValue):
    """The closed single-recipient message an RFC 2369 mailto header specifies."""

    to: str = Field(min_length=3, max_length=254)
    subject: str = Field(default="", max_length=256)
    body: str = Field(default="", max_length=1024)


class EmailUnsubscribeEvidence(EmailValue):
    """The newest header-bearing message; its address is never projected or logged."""

    message_id: str = Field(min_length=1, max_length=256)
    provider_thread_id: str = Field(min_length=1, max_length=256)
    received_at: datetime
    offered: Offered = "none"
    verified: bool = False
    mechanism: Mechanism = "none"
    https_uri: str = Field(default="", max_length=2048)
    mailto: EmailMailto | None = None
    digest: str = ""

    @property
    def destination(self) -> str:
        """What a consent names to the owner: a host or a recipient, never a path."""
        if self.mechanism == "one_click":
            return urlsplit(self.https_uri).hostname or ""
        return "" if self.mailto is None else self.mailto.to


class EmailSubscriptionOperation(EmailValue):
    """Latest durable action; pending or uncertain never implies success."""

    operation_id: UUID
    run_id: UUID
    action: SubscriptionAction
    status: Literal["pending", "completed", "failed", "uncertain"] = "pending"
    code: str | None = None
    requested_at: datetime
    prior_state: SubscriptionState = "active"


class EmailSubscription(EmailValue):
    """One bulk sender as one account sees it."""

    id: str
    account_id: str
    identity_kind: Literal["list", "sender"]
    identity: str
    display_name: str = ""
    address: str = ""
    threads: dict[str, datetime] = Field(default_factory=dict)
    overflow: bool = False
    first_seen_at: datetime
    last_received_at: datetime
    last_message_id: str = ""
    evidence: EmailUnsubscribeEvidence | None = None
    state: SubscriptionState = "active"
    requested_at: datetime | None = None
    operation: EmailSubscriptionOperation | None = None
    spam_thread_ids: list[str] = Field(default_factory=list, max_length=LABEL_THREADS_PER_GESTURE)

    @property
    def mechanism(self) -> Mechanism:
        evidence = self.evidence
        return "none" if evidence is None or not evidence.verified else evidence.mechanism

    @property
    def link_only(self) -> bool:
        return self.evidence is not None and self.evidence.offered == "link"


class BulkObservation(EmailValue):
    """One thread summary's newest received bulk message, as the read server normalized it."""

    account_id: str
    provider_thread_id: str
    message_id: str
    sender: str
    received_at: datetime
    list_id: str = ""
    offered: Offered = "none"


class EmailSubscriptionTarget(EmailValue):
    subscription_id: str
    account_id: str
    revision: int = Field(ge=1)
    mechanism: Mechanism
    evidence_digest: str = ""
    mailto: EmailMailto | None = None
    identity_kind: Literal["list", "sender"]
    identity: str
    evidence_thread_id: str = ""


class EmailSubscriptionConsent(EmailValue):
    """Finite authenticated owner gesture bound to one bounded batch of senders.

    The consent derives the only invocations it authorizes; callers supply
    neither a destination, a message, a thread, nor a label.
    """

    tenant_id: str
    principal_id: str
    action: SubscriptionAction
    targets: list[EmailSubscriptionTarget] = Field(
        min_length=1, max_length=SUBSCRIPTION_BATCH_LIMIT
    )
    archive_existing: bool = False
    servers: dict[str, dict[str, str]]
    restore_thread_ids: list[str] = Field(
        default_factory=list, max_length=LABEL_THREADS_PER_GESTURE
    )
    expires_at: datetime

    @property
    def one_click_targets(self) -> list[EmailSubscriptionTarget]:
        return [target for target in self.targets if target.mechanism == "one_click"]

    @property
    def mailto_targets(self) -> list[EmailSubscriptionTarget]:
        return [target for target in self.targets if target.mechanism == "mailto"]

    @property
    def unsubscribe_arguments(self) -> dict[str, object] | None:
        """The sole one-click invocation, or none when no target offers one."""
        if self.action != "unsubscribe" or not self.one_click_targets:
            return None
        return unsubscribe_arguments(
            (target.subscription_id, target.evidence_digest) for target in self.one_click_targets
        )

    def send_tool(self, target: EmailSubscriptionTarget) -> str:
        return f"mcp.{self.servers[target.account_id]['send']}.send_message"

    def send_arguments(self, target: EmailSubscriptionTarget) -> dict[str, object]:
        """Exactly the header's message; never a copy recipient, thread or reply header."""
        if self.action != "unsubscribe" or target.mailto is None:
            raise ValueError("target has no mailto message")
        return {
            "to": target.mailto.to,
            "cc": None,
            "bcc": None,
            "subject": target.mailto.subject,
            "body": target.mailto.body,
            "thread_id": None,
            "in_reply_to": None,
            "references": None,
        }

    def label_tool(self, account_id: str) -> str:
        return f"mcp.{self.servers[account_id]['write']}.modify_labels"

    @property
    def label_delta(self) -> tuple[list[str] | None, list[str] | None]:
        """The sole (add, remove) label change this gesture may make."""
        if self.action == "report_spam":
            return ["SPAM"], ["INBOX"]
        if self.action == "not_spam":
            return ["INBOX"], ["SPAM"]
        return None, ["INBOX"]

    def label_arguments(self, thread_ids: list[str]) -> dict[str, object]:
        add, remove = self.label_delta
        return {"thread_ids": thread_ids, "add_label_ids": add, "remove_label_ids": remove}


def unsubscribe_arguments(pairs: Any) -> dict[str, object]:
    """Canonical tool arguments: targets sorted so one batch has one approval hash."""
    return {
        "targets": [
            {"subscription_id": subscription_id, "evidence_digest": digest}
            for subscription_id, digest in sorted(pairs)
        ]
    }


def subscription_identity(sender: str, list_id: str) -> tuple[str, str, str, str] | None:
    """Return (kind, identity, display name, address), or none when no safe identity forms.

    A domain is deliberately not an identity: one hosting domain carries thousands
    of unrelated lists.
    """
    if any(character in sender for character in "\r\n\x00"):
        return None
    name, address = parseaddr(sender)
    address = address.casefold()
    if not _ADDRESS.fullmatch(address):
        address = ""
    normalized = list_id.casefold()
    if _LIST_ID.fullmatch(normalized):
        return "list", normalized, name[:200], address
    if address:
        return "sender", address, name[:200], address
    return None


def subscription_key(account_id: str, kind: str, identity: str) -> str:
    return hashlib.sha256(f"{account_id}:{kind}:{identity}".encode()).hexdigest()


def identity_query(kind: str, identity: str) -> str | None:
    """A server-composed Inbox search, or none when the identity is not plain."""
    if kind == "list" and _LIST_ID.fullmatch(identity):
        return f"list:{identity} in:inbox"
    if kind == "sender" and _ADDRESS.fullmatch(identity):
        return f"from:{identity} in:inbox"
    return None


def summary_identity(summary: dict[str, Any]) -> tuple[str, str] | None:
    """The (kind, identity) a search result itself carries, for the post-filter."""
    bulk = summary.get("bulk")
    if not isinstance(bulk, dict):
        return None
    found = subscription_identity(str(bulk.get("from", "")), str(bulk.get("list_id", "")))
    return None if found is None else (found[0], found[1])


def evidence_digest(
    account_id: str, message_id: str, mechanism: str, https_uri: str, mailto: EmailMailto | None
) -> str:
    canonical = json.dumps(
        {
            "account_id": account_id,
            "message_id": message_id,
            "mechanism": mechanism,
            "https_uri": https_uri,
            "mailto": None if mailto is None else mailto.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _newer(seen: BulkObservation, subscription: EmailSubscription) -> bool:
    return (seen.received_at, seen.message_id) > (
        subscription.last_received_at,
        subscription.last_message_id,
    )


def observe(
    subscription: EmailSubscription | None,
    seen: BulkObservation,
    *,
    window_start: datetime,
    grace: timedelta,
) -> EmailSubscription | None:
    """Fold one observation into its sender's record; a pure, order-independent projection.

    Only mail that offers a way out, or names a list, counts toward a census
    row. Any mail from an unsubscribed sender after its grace period still
    marks that sender as still sending, header or not.
    """
    identity = subscription_identity(seen.sender, seen.list_id)
    if identity is None or seen.received_at < window_start:
        return subscription
    kind, value, name, address = identity
    qualifies = seen.offered != "none" or kind == "list"
    if subscription is None:
        if not qualifies:
            return None
        subscription = EmailSubscription(
            id=subscription_key(seen.account_id, kind, value),
            account_id=seen.account_id,
            identity_kind="list" if kind == "list" else "sender",
            identity=value,
            first_seen_at=seen.received_at,
            last_received_at=seen.received_at,
        )
    update: dict[str, object] = {}
    state = subscription.state
    if (
        state == "unsubscribed"
        and subscription.requested_at is not None
        and seen.received_at > subscription.requested_at + grace
    ):
        # The sender had its grace period; nothing is dispatched, the row only says so.
        state = "still_sending"
        update["state"] = state
    if not qualifies:
        return subscription.model_copy(update=update) if update else subscription
    threads = dict(subscription.threads)
    threads[seen.provider_thread_id] = max(
        threads.get(seen.provider_thread_id, seen.received_at), seen.received_at
    )
    threads = {key: at for key, at in threads.items() if at >= window_start}
    overflow = subscription.overflow
    if len(threads) > SUBSCRIPTION_THREAD_CAP:
        kept = sorted(threads.items(), key=lambda item: (item[1], item[0]), reverse=True)
        threads, overflow = dict(kept[:SUBSCRIPTION_THREAD_CAP]), True
    update.update(
        threads=threads,
        overflow=overflow,
        first_seen_at=min(subscription.first_seen_at, seen.received_at),
    )
    if not subscription.last_message_id or _newer(seen, subscription):
        update.update(
            last_received_at=seen.received_at,
            last_message_id=seen.message_id,
            display_name=name,
            address=address,
        )
    evidence = subscription.evidence
    if (
        state in ACTIONABLE_STATES
        and seen.offered != "none"
        and (
            evidence is None
            or (seen.received_at, seen.message_id) > (evidence.received_at, evidence.message_id)
        )
    ):
        update["evidence"] = EmailUnsubscribeEvidence(
            message_id=seen.message_id,
            provider_thread_id=seen.provider_thread_id,
            received_at=seen.received_at,
            offered=seen.offered,
        )
    return subscription.model_copy(update=update)


def verify(subscription: EmailSubscription, block: dict[str, Any]) -> EmailSubscription:
    """Store one normalized ``get_unsubscribe`` block; the package normalizes, this authorizes."""
    evidence = subscription.evidence
    if evidence is None or block.get("message_id") != evidence.message_id:
        return subscription
    mechanism: Mechanism = "none"
    https_uri = ""
    mailto: EmailMailto | None = None
    offered = block.get("mechanism")
    if offered == "one_click" and block.get("authenticated") is True:
        candidate = block.get("https_uri")
        if isinstance(candidate, str) and len(candidate) <= 2048 and is_public_https_url(candidate):
            mechanism, https_uri = "one_click", candidate
    elif offered == "mailto" and block.get("authenticated") is True:
        raw = block.get("mailto")
        if isinstance(raw, dict):
            try:
                mailto = EmailMailto.model_validate(raw)
            except ValueError:
                mailto = None
        if mailto is not None and _ADDRESS.fullmatch(mailto.to.casefold()):
            mechanism = "mailto"
        else:
            mailto = None
    return subscription.model_copy(
        update={
            "evidence": evidence.model_copy(
                update={
                    "verified": True,
                    "mechanism": mechanism,
                    "https_uri": https_uri,
                    "mailto": mailto,
                    "digest": evidence_digest(
                        subscription.account_id, evidence.message_id, mechanism, https_uri, mailto
                    ),
                }
            )
        }
    )


def without_evidence(subscription: EmailSubscription) -> EmailSubscription:
    """Decision records keep identity, state and times, never an address or a message."""
    return subscription.model_copy(update={"evidence": None})
