"""Bulk-sender census, owner consent, and unsubscribe outcomes (Milestone 31).

The census is a projection of header metadata the refresh task already reads.
Every external effect stays behind ``REQUIRE_APPROVAL``: an owner gesture is an
immutable, expiring consent that derives the only invocations it authorizes.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.email import (
    EMAIL_HISTORY_DAYS,
    EmailAccount,
    EmailFeedback,
    EmailOperation,
    EmailRecord,
    EmailTask,
    EmailThread,
)
from agent_core.domain.email_base import EmailValue
from agent_core.domain.email_subscriptions import (
    ACTIONABLE_STATES,
    LABEL_THREADS_PER_GESTURE,
    SUBSCRIPTION_BATCH_LIMIT,
    SUBSCRIPTION_CONSENT_LIFETIME,
    SUBSCRIPTION_VERIFY_LIMIT,
    UNSUBSCRIBE_TOOL_NAME,
    BulkObservation,
    EmailSubscription,
    EmailSubscriptionConsent,
    EmailSubscriptionOperation,
    EmailSubscriptionTarget,
    SubscriptionAction,
    observe,
    subscription_identity,
    subscription_key,
    summary_identity,
    verify,
    without_evidence,
)
from agent_core.domain.errors import AuthorizationError, ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.domain.unsubscribe import UnsubscribeOutcomeCode
from agent_core.ports.email import EmailStore
from agent_core.ports.persistence import RepositoryUnitOfWork

if TYPE_CHECKING:
    from agent_core.application.email import EmailExperienceService

_GESTURE_SCOPES = ("email.read", "email.write", "run.write", "session.write", "approval.resolve")
_LABEL_OPERATIONS = LABEL_THREADS_PER_GESTURE // 25


@dataclass(frozen=True)
class SubscriptionRequest:
    """One authenticated owner command, before it becomes a consent."""

    action: SubscriptionAction
    targets: tuple[tuple[str, str | None, int], ...]
    archive_existing: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps([self.action, list(self.targets), self.archive_existing]).encode()
        ).hexdigest()


@dataclass(frozen=True)
class DispatchTarget:
    """What the request tool may do for one target: dial, or report a closed code."""

    subscription_id: str
    url: str | None
    code: UnsubscribeOutcomeCode | None


def _thread_key(account_id: str, provider_thread_id: str) -> str:
    """The account-qualified thread key source exclusion tombstones already use."""
    return hashlib.sha256(f"{account_id}:{provider_thread_id}".encode()).hexdigest()


def _call_id(run_id: UUID, operation: str) -> str:
    return "email-" + hashlib.sha256(f"{run_id}:{operation}".encode()).hexdigest()[:32]


def _received_at(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class EmailSubscriptions:
    """Owner-scoped subscription records over the ordinary email store."""

    def __init__(self, service: EmailExperienceService, *, enabled: bool, grace: timedelta) -> None:
        self.service = service
        self.enabled = enabled
        self.grace = grace

    # -- store mechanics ---------------------------------------------------------------------

    async def _rows(self, store: EmailStore, principal: Principal) -> AsyncIterator[EmailRecord]:
        after: str | None = None
        while page := await store.list(principal, "subscription", after=after):
            for row in page:
                yield row
            after = page[-1].key

    async def _save(
        self, store: EmailStore, principal: Principal, kind: str, key: str, value: EmailValue
    ) -> EmailRecord:
        now = self.service.clock.now()
        previous = await store.get(principal, kind, key)
        revision = 0 if previous is None else previous.revision
        return await store.put(
            EmailRecord(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kind=kind,
                key=key,
                revision=revision + 1,
                payload=value.model_dump(mode="json"),
                created_at=now if previous is None else previous.created_at,
                updated_at=now,
            ),
            expected_revision=revision,
        )

    async def _load(
        self, store: EmailStore, principal: Principal, subscription_id: str
    ) -> tuple[EmailSubscription, int]:
        """A foreign, unknown, or unauthorized-account subscription is one 404."""
        row = await store.get(principal, "subscription", subscription_id)
        if row is None:
            raise NotFoundError("email subscription not found")
        value = EmailSubscription.model_validate(row.payload)
        try:
            self.service._authorize_account(principal, value.account_id)
        except AuthorizationError as exc:
            raise NotFoundError("email subscription not found") from exc
        return value, row.revision

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise NotFoundError("email unsubscribe assistance is not enabled")

    def _window_start(self) -> datetime:
        return self.service.clock.now() - timedelta(days=EMAIL_HISTORY_DAYS)

    async def _reconcile_in(
        self, uow: RepositoryUnitOfWork, principal: Principal, value: EmailSubscription
    ) -> EmailSubscription:
        """A terminal run never strands a sender: what it did not settle is settled here.

        The caller holds the owner lock. A possibly dispatched send or label write
        stays uncertain; a constant one-click request that left no outcome is safe
        to offer again.
        """
        operation = value.operation
        if value.state != "pending" or operation is None:
            return value
        try:
            run = await uow.runs.get(operation.run_id, principal)
        except NotFoundError:
            run = None
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            return value
        dispatched = run is not None and any(
            invocation.tool_name.endswith((".send_message", ".modify_labels"))
            and (
                invocation.status
                in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.UNCERTAIN}
                or (
                    invocation.status is ToolInvocationStatus.RUNNING
                    and invocation.effect_sent_at is not None
                )
            )
            for invocation in await uow.invocations.list_for_run(operation.run_id, principal)
        )
        settled = value.model_copy(
            update={
                "state": "failed" if operation.action == "unsubscribe" else operation.prior_state,
                "operation": operation.model_copy(
                    update={
                        "status": "uncertain" if dispatched else "failed",
                        "code": "unsubscribe.outcome_unknown"
                        if dispatched
                        else "unsubscribe.not_attempted",
                    }
                ),
            }
        )
        await self._save(uow.email, principal, "subscription", value.id, settled)
        return settled

    # -- authority ---------------------------------------------------------------------------

    def supported(self, principal: Principal, account_id: str) -> bool:
        """Advertise only an action this principal can explicitly consent to."""
        service = self.service
        return bool(
            self.enabled
            and service.resolve_archive_approval is not None
            and service.archive_self_approval_enabled
            and account_id in service.account_servers
            and {*_GESTURE_SCOPES, f"mcp.{service.account_servers[account_id]['read']}.use"}
            <= principal.scopes
        )

    def _require(
        self,
        principal: Principal,
        account_id: str,
        *,
        send: bool,
        write: bool,
    ) -> None:
        """Preflight current authority before storing a consent or a queue row."""
        self._require_enabled()
        for scope in _GESTURE_SCOPES:
            require_scope(principal, scope)
        service = self.service
        service._authorize_account(principal, account_id)
        servers = service.account_servers[account_id]
        if service.resolve_archive_approval is None:
            raise ConflictError("unsubscribe assistance is unavailable for this account")
        if send:
            require_scope(principal, f"mcp.{servers['send']}.use")
        if write:
            require_scope(principal, f"mcp.{servers['write']}.use")
        if not service.archive_self_approval_enabled:
            raise AuthorizationError("approval requires a distinct resolver")

    def _require_consent(self, principal: Principal, consent: EmailSubscriptionConsent) -> None:
        write = consent.archive_existing or consent.action != "unsubscribe"
        for account_id in {target.account_id for target in consent.targets}:
            self._require(
                principal,
                account_id,
                send=any(
                    target.mechanism == "mailto" and target.account_id == account_id
                    for target in consent.targets
                ),
                write=write,
            )

    # -- the census (refresh task, under its worker lease) -----------------------------------

    async def observe(
        self,
        principal: Principal,
        account_id: str,
        summaries: Iterable[dict[str, Any]],
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> int:
        """Fold thread summaries refresh already read; issues no query and no model call."""
        if not self.enabled:
            return 0
        seen: list[BulkObservation] = []
        for summary in summaries:
            bulk = summary.get("bulk")
            labels = summary.get("label_ids")
            thread_id = summary.get("thread_id")
            if (
                not isinstance(bulk, dict)
                or not isinstance(thread_id, str)
                or not isinstance(bulk.get("message_id"), str)
                or not bulk["message_id"]
                or (isinstance(labels, list) and {"SPAM", "TRASH"} & set(labels))
            ):
                continue
            received_at = _received_at(bulk.get("date"))
            offered = bulk.get("unsubscribe")
            if received_at is None or offered not in {"one_click", "mailto", "link", "none"}:
                continue
            seen.append(
                BulkObservation(
                    account_id=account_id,
                    provider_thread_id=thread_id,
                    message_id=bulk["message_id"],
                    sender=str(bulk.get("from", ""))[:998],
                    received_at=received_at,
                    list_id=str(bulk.get("list_id", ""))[:255],
                    offered=offered,
                )
            )
        if not seen:
            return 0
        changed = 0
        window_start = self._window_start()
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            await self.service._fence(uow, run, lease)
            account = await uow.email.get(principal, "account", account_id)
            owned = (
                set()
                if account is None
                else set(EmailAccount.model_validate(account.payload).verified_addresses)
            )
            for item in seen:
                identity = subscription_identity(item.sender, item.list_id)
                if identity is None or identity[3] in owned:
                    continue
                if await uow.email.get(
                    principal, "excluded_source", _thread_key(account_id, item.provider_thread_id)
                ):
                    # An excluded source never forms or renews a record again.
                    continue
                key = subscription_key(account_id, identity[0], identity[1])
                row = await uow.email.get(principal, "subscription", key)
                before = None if row is None else EmailSubscription.model_validate(row.payload)
                after = observe(before, item, window_start=window_start, grace=self.grace)
                if after is None or after == before:
                    continue
                await self._save(uow.email, principal, "subscription", key, after)
                await self._index_threads(uow.email, principal, before, after)
                changed += 1
        return changed

    async def _index_threads(
        self,
        store: EmailStore,
        principal: Principal,
        before: EmailSubscription | None,
        after: EmailSubscription | None,
    ) -> None:
        """Keep the thread-to-sender index that places the action on a thread."""
        old = set() if before is None else set(before.threads)
        new = set() if after is None else set(after.threads)
        account_id = (after or before).account_id  # type: ignore[union-attr]
        for thread_id in new - old:
            assert after is not None
            key = _thread_key(account_id, thread_id)
            await self._save(
                store,
                principal,
                "subscription_thread",
                key,
                _ThreadIndex(subscription_id=after.id),
            )
        for thread_id in old - new:
            key = _thread_key(account_id, thread_id)
            row = await store.get(principal, "subscription_thread", key)
            if row is not None:
                await store.delete(
                    principal, "subscription_thread", key, expected_revision=row.revision
                )

    async def forget_thread_in(
        self,
        store: EmailStore,
        principal: Principal,
        account_id: str,
        provider_thread_id: str,
        message_ids: frozenset[str],
    ) -> None:
        """Source exclusion removes the thread, its index entry, and evidence drawn from it."""
        index = await store.get(
            principal, "subscription_thread", _thread_key(account_id, provider_thread_id)
        )
        if index is None:
            return
        row = await store.get(principal, "subscription", str(index.payload["subscription_id"]))
        if row is None:
            await store.delete(
                principal, "subscription_thread", index.key, expected_revision=index.revision
            )
            return
        before = EmailSubscription.model_validate(row.payload)
        evidence = before.evidence
        after = before.model_copy(
            update={
                "threads": {
                    key: at for key, at in before.threads.items() if key != provider_thread_id
                },
                "evidence": (
                    None
                    if evidence is not None
                    and (
                        evidence.provider_thread_id == provider_thread_id
                        or evidence.message_id in message_ids
                    )
                    else evidence
                ),
            }
        )
        await self._index_threads(store, principal, before, after)
        if not after.threads and after.state == "active":
            await store.delete(principal, "subscription", row.key, expected_revision=row.revision)
        else:
            await self._save(store, principal, "subscription", row.key, after)

    async def sweep(
        self,
        principal: Principal,
        account_id: str,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None:
        """Drop undecided senders, and evidence, whose mail left the ninety-day window."""
        if not self.enabled:
            return
        window_start = self._window_start()
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            await self.service._fence(uow, run, lease)
            rows = [row async for row in self._rows(uow.email, principal)]
            for row in rows:
                value = EmailSubscription.model_validate(row.payload)
                if value.account_id != account_id:
                    continue
                if value.state == "active" and value.last_received_at < window_start:
                    await self._index_threads(uow.email, principal, value, None)
                    await uow.email.delete(
                        principal, "subscription", row.key, expected_revision=row.revision
                    )
                elif value.evidence is not None and value.evidence.received_at < window_start:
                    await self._save(
                        uow.email, principal, "subscription", row.key, without_evidence(value)
                    )

    async def unverified(self, principal: Principal, account_id: str) -> list[tuple[str, str]]:
        """Highest-volume senders whose newest evidence has not been read yet."""
        if not self.enabled:
            return []
        async with self.service.uow_factory() as uow:
            pending = [
                value
                async for row in self._rows(uow.email, principal)
                if (value := EmailSubscription.model_validate(row.payload)).account_id == account_id
                and value.state in ACTIONABLE_STATES
                and value.evidence is not None
                and not value.evidence.verified
            ]
        pending.sort(key=lambda value: (-len(value.threads), value.id))
        return [
            (value.id, value.evidence.message_id)
            for value in pending[:SUBSCRIPTION_VERIFY_LIMIT]
            if value.evidence is not None
        ]

    async def apply_verification(
        self,
        principal: Principal,
        subscription_id: str,
        block: dict[str, Any],
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None:
        if not self.enabled:
            return
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            await self.service._fence(uow, run, lease)
            row = await uow.email.get(principal, "subscription", subscription_id)
            if row is None:
                return
            before = EmailSubscription.model_validate(row.payload)
            if before.state not in ACTIONABLE_STATES:
                return
            after = verify(before, block)
            if after != before:
                await self._save(uow.email, principal, "subscription", subscription_id, after)

    # -- projections -------------------------------------------------------------------------

    async def _protected(self, store: EmailStore, principal: Principal) -> dict[str, str]:
        """Addresses the owner marked Important or writes to, with the reason."""
        reasons: dict[str, str] = {}
        after: str | None = None
        while page := await store.list(principal, "relationship", after=after):
            for row in page:
                recipients = row.payload.get("recipients")
                for address in recipients if isinstance(recipients, list) else []:
                    if isinstance(address, str):
                        reasons[address.casefold()] = "correspondent"
            after = page[-1].key
        after = None
        while page := await store.list(principal, "feedback", after=after):
            for row in page:
                feedback = EmailFeedback.model_validate(row.payload)
                if (
                    feedback.target == "person"
                    and feedback.judgment == "important"
                    and feedback.undone_at is None
                ):
                    for address in feedback.target_values:
                        reasons[address.casefold()] = "important_feedback"
            after = page[-1].key
        return reasons

    def view(
        self, value: EmailSubscription, revision: int, protected: dict[str, str]
    ) -> dict[str, object]:
        """The owner-facing row. It never carries the unsubscribe address."""
        evidence = value.evidence
        reason = protected.get(value.address) if value.address else None
        operation = value.operation
        return {
            "id": value.id,
            "account_id": value.account_id,
            "display_name": value.display_name,
            "address": value.address,
            "list_id": value.identity if value.identity_kind == "list" else "",
            "thread_count": len(value.threads),
            "thread_count_overflow": value.overflow,
            "first_seen_at": value.first_seen_at.isoformat(),
            "last_received_at": value.last_received_at.isoformat(),
            "mechanism": value.mechanism,
            "verified": evidence is not None and evidence.verified,
            "link_only": value.link_only,
            "destination": ""
            if evidence is None or not evidence.verified
            else evidence.destination,
            "mailto": (
                None
                if evidence is None or not evidence.verified or evidence.mailto is None
                else evidence.mailto.model_dump(mode="json")
            ),
            "evidence_digest": (
                None if evidence is None or value.mechanism == "none" else evidence.digest
            ),
            "state": value.state,
            "protected": reason is not None,
            "protected_reason": reason,
            "requested_at": None if value.requested_at is None else value.requested_at.isoformat(),
            "operation": None if operation is None else operation.model_dump(mode="json"),
            "revision": revision,
        }

    async def browse(
        self,
        principal: Principal,
        *,
        account_id: str | None = None,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """Volume then recency, protected senders last."""
        self._require_enabled()
        require_scope(principal, "email.read")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between one and one hundred")
        try:
            offset = 0 if cursor is None else int(base64.urlsafe_b64decode(cursor.encode()))
        except ValueError as exc:
            raise ValueError("invalid cursor") from exc
        if offset < 0:
            raise ValueError("invalid cursor")
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            protected = await self._protected(uow.email, principal)
            stored = [row async for row in self._rows(uow.email, principal)]
            rows: list[tuple[EmailSubscription, int]] = []
            for row in stored:
                value = EmailSubscription.model_validate(row.payload)
                if (
                    value.account_id not in self.service.account_servers
                    or f"mcp.{self.service.account_servers[value.account_id]['read']}.use"
                    not in principal.scopes
                    or (account_id is not None and value.account_id != account_id)
                ):
                    continue
                revision = row.revision
                if value.state == "pending":
                    settled = await self._reconcile_in(uow, principal, value)
                    revision += settled is not value
                    value = settled
                if state is None or value.state == state:
                    rows.append((value, revision))
        rows.sort(
            key=lambda item: (
                item[0].address in protected,
                -len(item[0].threads),
                -item[0].last_received_at.timestamp(),
                item[0].id,
            )
        )
        items = [self.view(value, revision, protected) for value, revision in rows]
        page = items[offset : offset + limit]
        more = offset + limit < len(items)
        return {
            "items": page,
            "next_cursor": (
                base64.urlsafe_b64encode(str(offset + limit).encode()).decode() if more else None
            ),
        }

    async def describe(
        self, principal: Principal, subscription_ids: Iterable[str]
    ) -> dict[str, dict[str, object]]:
        """Owner-facing rows for exactly these senders; unknown ones are simply absent."""
        self._require_enabled()
        require_scope(principal, "email.read")
        found: dict[str, dict[str, object]] = {}
        async with self.service.uow_factory() as uow:
            protected = await self._protected(uow.email, principal)
            for subscription_id in subscription_ids:
                with suppress(NotFoundError):
                    value, revision = await self._load(uow.email, principal, subscription_id)
                    found[subscription_id] = self.view(value, revision, protected)
        return found

    async def thread_block(
        self, store: EmailStore, principal: Principal, thread: EmailThread
    ) -> dict[str, object] | None:
        """The additive block that places the action on a bulk conversation."""
        if not self.enabled:
            return None
        index = await store.get(
            principal,
            "subscription_thread",
            _thread_key(thread.account_id, thread.provider_thread_id),
        )
        if index is None:
            return None
        row = await store.get(principal, "subscription", str(index.payload["subscription_id"]))
        if row is None:
            return None
        value = EmailSubscription.model_validate(row.payload)
        evidence = value.evidence
        return {
            "id": value.id,
            "state": value.state,
            "mechanism": value.mechanism,
            "destination": ""
            if evidence is None or not evidence.verified
            else evidence.destination,
            "evidence_digest": (
                None if evidence is None or value.mechanism == "none" else evidence.digest
            ),
            "revision": row.revision,
        }

    # -- owner commands ----------------------------------------------------------------------

    async def keep(
        self, principal: Principal, subscription_id: str, expected_revision: int, *, kept: bool
    ) -> dict[str, object]:
        """A durable local decision; it changes no mailbox."""
        self._require_enabled()
        require_scope(principal, "email.write")
        if type(kept) is not bool:
            raise ValueError("kept must be a boolean")
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            value, revision = await self._load(uow.email, principal, subscription_id)
            if (kept and value.state == "kept") or (not kept and value.state in ACTIONABLE_STATES):
                # Repeating a decision already in force is not a conflict.
                return self.view(value, revision, await self._protected(uow.email, principal))
            if revision != expected_revision:
                raise ConflictError("the subscription changed; review it before deciding")
            if kept and value.state not in ACTIONABLE_STATES:
                raise ConflictError("this sender cannot be kept in its current state")
            if not kept and value.state != "kept":
                raise ConflictError("this sender is not kept")
            value = (
                without_evidence(value).model_copy(update={"state": "kept"})
                if kept
                else value.model_copy(update={"state": "active"})
            )
            row = await self._save(uow.email, principal, "subscription", subscription_id, value)
            return self.view(value, row.revision, await self._protected(uow.email, principal))

    async def unsubscribe(
        self,
        principal: Principal,
        targets: list[tuple[str, str, int]],
        *,
        archive_existing: bool,
        idempotency_key: str,
    ) -> EmailOperation:
        """Admit one consented batch; the gesture is consent to exactly these senders."""
        self._require_enabled()
        if (
            not 1 <= len(targets) <= SUBSCRIPTION_BATCH_LIMIT
            or len({item[0] for item in targets}) != len(targets)
            or type(archive_existing) is not bool
            or not idempotency_key
            or len(idempotency_key) > 200
        ):
            raise ValueError("invalid unsubscribe request")
        return await self.service.submit_task(
            principal,
            kind="subscription",
            idempotency_key=idempotency_key,
            subscription=SubscriptionRequest(
                action="unsubscribe",
                targets=tuple(sorted(targets)),
                archive_existing=archive_existing,
            ),
        )

    async def spam(
        self,
        principal: Principal,
        subscription_id: str,
        expected_revision: int,
        *,
        spam: bool,
        idempotency_key: str,
    ) -> EmailOperation:
        self._require_enabled()
        if type(spam) is not bool or not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("invalid spam request")
        return await self.service.submit_task(
            principal,
            kind="subscription",
            idempotency_key=idempotency_key,
            subscription=SubscriptionRequest(
                action="report_spam" if spam else "not_spam",
                targets=((subscription_id, None, expected_revision),),
            ),
        )

    # -- admission (inside submit_task's owner lock) -----------------------------------------

    async def _eligible(
        self, uow: RepositoryUnitOfWork, principal: Principal, request: SubscriptionRequest
    ) -> list[tuple[EmailSubscription, int]]:
        store = uow.email
        loaded: list[tuple[EmailSubscription, int]] = []
        for subscription_id, digest, expected_revision in request.targets:
            value, revision = await self._load(store, principal, subscription_id)
            if value.state == "pending":
                settled = await self._reconcile_in(uow, principal, value)
                revision += settled is not value
                value = settled
            if revision != expected_revision:
                raise ConflictError("the subscription changed; review it before acting")
            if request.action == "unsubscribe":
                evidence = value.evidence
                if (
                    value.state not in ACTIONABLE_STATES
                    or evidence is None
                    or not evidence.verified
                    or evidence.mechanism == "none"
                    or evidence.digest != digest
                ):
                    raise ConflictError("this sender offers no verified unsubscribe right now")
            elif request.action == "report_spam":
                if value.state not in ACTIONABLE_STATES:
                    raise ConflictError("this sender cannot be reported in its current state")
            elif value.state != "reported_spam":
                raise ConflictError("this sender was not reported as spam")
            loaded.append((value, revision))
        return loaded

    async def accounts_in(
        self, uow: RepositoryUnitOfWork, principal: Principal, request: SubscriptionRequest
    ) -> list[str]:
        self._require_enabled()
        loaded = await self._eligible(uow, principal, request)
        write = request.archive_existing or request.action != "unsubscribe"
        accounts = sorted({value.account_id for value, _ in loaded})
        for account_id in accounts:
            self._require(
                principal,
                account_id,
                send=any(
                    value.account_id == account_id and value.mechanism == "mailto"
                    for value, _ in loaded
                )
                and request.action == "unsubscribe",
                write=write,
            )
        return accounts

    async def consent_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        request: SubscriptionRequest,
        task_id: UUID,
        run_id: UUID,
        now: datetime,
    ) -> EmailSubscriptionConsent:
        """Freeze the gesture and mark each sender pending, atomically with the queue row."""
        store = uow.email
        loaded = await self._eligible(uow, principal, request)
        targets: list[EmailSubscriptionTarget] = []
        restore: list[str] = []
        for value, _ in loaded:
            pending = value.model_copy(
                update={
                    "state": "pending",
                    "operation": EmailSubscriptionOperation(
                        operation_id=task_id,
                        run_id=run_id,
                        action=request.action,
                        requested_at=now,
                        prior_state=value.state,
                    ),
                }
            )
            row = await self._save(store, principal, "subscription", value.id, pending)
            evidence = value.evidence
            unsubscribing = request.action == "unsubscribe" and evidence is not None
            targets.append(
                EmailSubscriptionTarget(
                    subscription_id=value.id,
                    account_id=value.account_id,
                    revision=row.revision,
                    mechanism=value.mechanism if unsubscribing else "none",
                    evidence_digest=evidence.digest if unsubscribing and evidence else "",
                    mailto=evidence.mailto if unsubscribing and evidence else None,
                    identity_kind=value.identity_kind,
                    identity=value.identity,
                    evidence_thread_id="" if evidence is None else evidence.provider_thread_id,
                )
            )
            restore.extend(value.spam_thread_ids if request.action == "not_spam" else [])
        return EmailSubscriptionConsent(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            action=request.action,
            targets=targets,
            archive_existing=request.archive_existing,
            servers={
                target.account_id: dict(self.service.account_servers[target.account_id])
                for target in targets
            },
            restore_thread_ids=restore[:LABEL_THREADS_PER_GESTURE],
            expires_at=now + SUBSCRIPTION_CONSENT_LIFETIME,
        )

    def gesture_payload(self, task: EmailTask) -> dict[str, object]:
        consent = task.subscription_consent
        assert consent is not None
        return {
            "task_id": str(task.id),
            "action": consent.action,
            "subscription_ids": [target.subscription_id for target in consent.targets],
            # Canonical, because a stored consent comes back with its keys reordered.
            "consent_digest": hashlib.sha256(
                json.dumps(
                    consent.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }

    # -- consent consumption (typed task, under its worker lease) ----------------------------

    def _read_tool(self, consent: EmailSubscriptionConsent, tool_name: str) -> bool:
        return any(
            tool_name.startswith(f"mcp.{servers['read']}.") for servers in consent.servers.values()
        )

    async def _allowed_threads(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        run: Run,
        consent: EmailSubscriptionConsent,
    ) -> dict[str, set[str]]:
        """Per account, the threads this run's own governed searches proved are the sender's."""
        allowed: dict[str, set[str]] = {account: set() for account in consent.servers}
        if consent.action == "not_spam":
            for target in consent.targets:
                allowed[target.account_id].update(consent.restore_thread_ids)
            return allowed
        identities = {
            (target.account_id, target.identity_kind, target.identity) for target in consent.targets
        }
        for target in consent.targets:
            if consent.action == "report_spam" and target.evidence_thread_id:
                allowed[target.account_id].add(target.evidence_thread_id)
        for invocation in await uow.invocations.list_for_run(run.id, principal):
            result = invocation.structured_result
            if (
                invocation.status is not ToolInvocationStatus.SUCCEEDED
                or not invocation.tool_name.endswith(".search_threads")
                or not isinstance(result, dict)
            ):
                continue
            account_id = next(
                (
                    account
                    for account, servers in consent.servers.items()
                    if invocation.tool_name == f"mcp.{servers['read']}.search_threads"
                ),
                None,
            )
            if account_id is None:
                continue
            for summary in result.get("threads", []) or []:
                found = summary_identity(summary) if isinstance(summary, dict) else None
                if found is not None and (account_id, *found) in identities:
                    allowed[account_id].add(str(summary.get("thread_id")))
        return allowed

    def _derived(
        self,
        consent: EmailSubscriptionConsent,
        invocation: ToolInvocation,
        allowed: dict[str, set[str]],
    ) -> bool:
        """Whether one invocation is exactly an action this consent derives."""
        name, arguments = invocation.tool_name, invocation.normalized_arguments
        if arguments is None:
            return False
        if name == UNSUBSCRIBE_TOOL_NAME:
            return arguments == consent.unsubscribe_arguments
        if name.endswith(".send_message"):
            return any(
                name == consent.send_tool(target) and arguments == consent.send_arguments(target)
                for target in consent.mailto_targets
            )
        if name.endswith(".modify_labels"):
            labelled = consent.action != "unsubscribe" or consent.archive_existing
            thread_ids = arguments.get("thread_ids")
            return labelled and any(
                name == consent.label_tool(account_id)
                and isinstance(thread_ids, list)
                and 0 < len(thread_ids) <= 25
                and set(thread_ids) <= threads
                and arguments == consent.label_arguments(thread_ids)
                for account_id, threads in allowed.items()
            )
        return False

    def operations(self, consent: EmailSubscriptionConsent) -> set[str]:
        """The only checkpoint operation keys a subscription task may dispatch under."""
        return {
            "unsubscribe",
            *(f"mailto:{target.subscription_id}" for target in consent.mailto_targets),
            *(
                f"labels:{account_id}:{index}"
                for account_id in consent.servers
                for index in range(_LABEL_OPERATIONS)
            ),
        }

    async def validate(
        self, principal: Principal, run: Run, lease: WorkerLease | None
    ) -> EmailTask:
        """Revalidate the immutable consent immediately before each undispatched attempt."""
        self._require_enabled()
        principal = self.service._archive_authority(principal)
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            if await uow.email.get(principal, "task", str(run.id)) is None:
                # No task means no consent; there is nothing for this run to fence.
                raise ConflictError("subscription request is unavailable")
            await self.service._fence(uow, run, lease)
            return await self._validate_in(uow, principal, run)

    async def _validate_in(
        self, uow: RepositoryUnitOfWork, principal: Principal, run: Run
    ) -> EmailTask:
        row = await uow.email.get(principal, "task", str(run.id))
        task = None if row is None else EmailTask.model_validate(row.payload)
        consent = None if task is None else task.subscription_consent
        if task is None or task.kind != "subscription" or consent is None:
            raise ConflictError("subscription request is unavailable")
        if (consent.tenant_id, consent.principal_id) != (
            principal.tenant_id,
            principal.principal_id,
        ):
            raise ConflictError("subscription request does not belong to this owner")
        self._require_consent(principal, consent)
        now = self.service.clock.now()
        if (
            task.session_id != run.session_id
            or consent.expires_at <= now
            or sorted(task.account_ids) != sorted(consent.servers)
            or any(
                self.service.account_servers.get(account) != servers
                for account, servers in consent.servers.items()
            )
        ):
            raise ConflictError("subscription request expired or its accounts changed")
        session = await uow.sessions.get(run.session_id, principal)
        if session.metadata.get("email_account_servers") != self.service.account_servers:
            raise ConflictError("subscription account configuration changed")
        event = await uow.events.get_by_derivation(f"email-subscription:{task.id}", principal)
        if (
            event is None
            or event.run_id != run.id
            or event.actor_type != "principal"
            or event.actor_id != principal.principal_id
            or event.payload != self.gesture_payload(task)
        ):
            raise ConflictError("subscription consent has no matching authenticated gesture")
        for target in consent.targets:
            stored = await uow.email.get(principal, "subscription", target.subscription_id)
            value = None if stored is None else EmailSubscription.model_validate(stored.payload)
            if (
                value is None
                or value.operation is None
                or value.operation.run_id != run.id
                or value.account_id != target.account_id
            ):
                raise ConflictError("the subscription changed after the gesture")
        allowed = await self._allowed_threads(uow, principal, run, consent)
        for invocation in await uow.invocations.list_for_run(run.id, principal):
            if self._read_tool(consent, invocation.tool_name):
                continue
            approval = await uow.approvals.get_by_action(invocation.id)
            if not self._derived(consent, invocation, allowed) or (
                approval is not None and (approval.expires_at is None or approval.expires_at <= now)
            ):
                raise ConflictError("the frozen subscription invocation expired or changed")
        return task

    async def approve(
        self, principal: Principal, run: Run, lease: WorkerLease | None, approval_id: UUID
    ) -> None:
        """Consume the authenticated gesture through ordinary one-time approval resolution."""
        task = await self.validate(principal, run, lease)
        consent = task.subscription_consent
        assert consent is not None
        authority = self.service._archive_authority(principal)
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            await self.service._fence(uow, run, lease)
            approval = await uow.approvals.get(approval_id, principal)
            invocations = await uow.invocations.list_for_run(run.id, principal)
            invocation = next((i for i in invocations if i.id == approval.tool_invocation_id), None)
            allowed = await self._allowed_threads(uow, principal, run, consent)
            if (
                approval.run_id != run.id
                or approval.session_id != run.session_id
                or approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                or approval.expires_at is None
                or approval.expires_at <= self.service.clock.now()
                or invocation is None
                or approval.tool_name != invocation.tool_name
                or invocation.call_id
                not in {_call_id(run.id, key) for key in self.operations(consent)}
                or not self._derived(consent, invocation, allowed)
                or approval.normalized_arguments_hash != invocation.normalized_arguments_hash
            ):
                raise ConflictError("approval does not match the consented subscription action")
            await uow.events.append(
                NewEvent(
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type="approval.requested",
                    actor_type="runtime",
                    payload={"approval_id": str(approval.id)},
                    derivation_key=f"email-subscription-approval:{approval.id}",
                ),
                lease=lease,
            )
        assert self.service.resolve_archive_approval is not None
        await self.service.resolve_archive_approval(authority, approval_id, lease)

    async def guard(
        self, owner: Principal, run: Run, invocation: ToolInvocation, lease: WorkerLease | None
    ) -> None:
        """Recheck consent at the effect boundary; a revocation reaches nothing."""
        task = await self.service.get_task(owner, run.id)
        consent = None if task is None else task.subscription_consent
        if consent is None:
            raise ConflictError("subscription task has no owner consent")
        if self._read_tool(consent, invocation.tool_name):
            return
        await self.validate(owner, run, lease)
        async with self.service.uow_factory() as uow:
            allowed = await self._allowed_threads(uow, owner, run, consent)
        if not self._derived(consent, invocation, allowed):
            raise ConflictError("subscription task cannot dispatch another external action")

    # -- outcomes ----------------------------------------------------------------------------

    async def dispatch_targets(
        self, principal: Principal, run_id: UUID, pairs: list[tuple[str, str]]
    ) -> list[DispatchTarget]:
        """Server-derive each destination; a conversation chooses the sender, never where."""
        self._require_enabled()
        resolved: list[DispatchTarget] = []
        async with self.service.uow_factory() as uow:
            for subscription_id, digest in pairs:
                try:
                    value, _ = await self._load(uow.email, principal, subscription_id)
                except NotFoundError:
                    resolved.append(
                        DispatchTarget(subscription_id, None, UnsubscribeOutcomeCode.NOT_ELIGIBLE)
                    )
                    continue
                operation = value.operation
                mine = operation is not None and operation.run_id == run_id
                if (
                    mine
                    and value.state == "unsubscribed"
                    and operation is not None
                    and operation.code == UnsubscribeOutcomeCode.ACCEPTED
                ):
                    # A re-executed invocation never dials an accepted target again.
                    resolved.append(
                        DispatchTarget(subscription_id, None, UnsubscribeOutcomeCode.ACCEPTED)
                    )
                    continue
                evidence = value.evidence
                if not (value.state in ACTIONABLE_STATES or (value.state == "pending" and mine)):
                    code: UnsubscribeOutcomeCode | None = UnsubscribeOutcomeCode.NOT_ELIGIBLE
                elif evidence is None or not evidence.verified or evidence.mechanism == "none":
                    code = UnsubscribeOutcomeCode.NOT_ELIGIBLE
                elif evidence.digest != digest:
                    code = UnsubscribeOutcomeCode.EVIDENCE_CHANGED
                elif evidence.mechanism == "mailto":
                    code = UnsubscribeOutcomeCode.REQUIRES_SEND
                else:
                    code = None
                resolved.append(
                    DispatchTarget(
                        subscription_id,
                        evidence.https_uri if code is None and evidence is not None else None,
                        code,
                    )
                )
        return resolved

    async def record_request(
        self,
        principal: Principal,
        subscription_id: str,
        run_id: UUID,
        code: UnsubscribeOutcomeCode,
    ) -> None:
        """Persist one target's outcome once; only an accepted request unsubscribes."""
        await self._settle(
            principal,
            subscription_id,
            run_id,
            action="unsubscribe",
            status="completed" if code is UnsubscribeOutcomeCode.ACCEPTED else "failed",
            code=code.value,
        )

    async def _settle(
        self,
        principal: Principal,
        subscription_id: str,
        run_id: UUID,
        *,
        action: SubscriptionAction,
        status: Literal["completed", "failed", "uncertain"],
        code: str,
        thread_ids: list[str] | None = None,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None:
        now = self.service.clock.now()
        async with self.service.uow_factory() as uow, uow.email.lock(principal):
            await self.service._fence(uow, run, lease)
            row = await uow.email.get(principal, "subscription", subscription_id)
            if row is None:
                return
            value = EmailSubscription.model_validate(row.payload)
            operation = value.operation
            if operation is not None and operation.run_id == run_id:
                if operation.status != "pending":
                    return
                prior = operation.prior_state
            else:
                if value.state not in ACTIONABLE_STATES:
                    return
                prior = value.state
                operation = EmailSubscriptionOperation(
                    operation_id=self.service.ids.new_id(),
                    run_id=run_id,
                    action=action,
                    requested_at=now,
                    prior_state=prior,
                )
            update: dict[str, object] = {
                "operation": operation.model_copy(update={"status": status, "code": code})
            }
            if status != "completed":
                # A refusal, a rejection, or an unknown send leaves every option open.
                update["state"] = "failed" if action == "unsubscribe" else prior
            elif action == "unsubscribe":
                update.update(state="unsubscribed", requested_at=now, evidence=None)
            elif action == "report_spam":
                update.update(
                    state="reported_spam", evidence=None, spam_thread_ids=thread_ids or []
                )
            else:
                update.update(state="active", spam_thread_ids=[])
            await self._save(
                uow.email,
                principal,
                "subscription",
                subscription_id,
                value.model_copy(update=update),
            )

    async def settle(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
        subscription_id: str,
        *,
        action: SubscriptionAction,
        status: Literal["completed", "failed", "uncertain"],
        code: str,
        thread_ids: list[str] | None = None,
    ) -> None:
        await self._settle(
            principal,
            subscription_id,
            run.id,
            action=action,
            status=status,
            code=code,
            thread_ids=thread_ids,
            run=run,
            lease=lease,
        )

    async def finish(self, principal: Principal, run: Run, lease: WorkerLease | None) -> None:
        """Nothing stays pending: an unattempted sender returns to where it was."""
        task = await self.service.get_task(principal, run.id)
        consent = None if task is None else task.subscription_consent
        if consent is None:
            return
        for target in consent.targets:
            await self._settle(
                principal,
                target.subscription_id,
                run.id,
                action=consent.action,
                status="failed",
                code="unsubscribe.not_attempted",
                run=run,
                lease=lease,
            )


class _ThreadIndex(EmailValue):
    subscription_id: str
