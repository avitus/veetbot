"""The typed, model-free task that executes one consented subscription gesture.

Every effect goes through the ordinary tool pipeline and keeps its approval.
The owner's gesture resolves each pending approval only on an exact match, and
nothing here chooses a destination, a message, a thread, or a label.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

from agent_core.domain.email import EmailTask
from agent_core.domain.email_subscriptions import (
    LABEL_THREADS_PER_CALL,
    LABEL_THREADS_PER_GESTURE,
    UNSUBSCRIBE_TOOL_NAME,
    EmailSubscriptionConsent,
    EmailSubscriptionTarget,
    identity_query,
    summary_identity,
)
from agent_core.domain.errors import (
    ApprovalRequiredError,
    AuthorizationError,
    BudgetExceededError,
    ConflictError,
)
from agent_core.domain.runs import RunOutcome, Step
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus

if TYPE_CHECKING:
    from agent_core.runtime.email_tasks import _TaskIO

_TERMINAL = frozenset(
    {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.UNCERTAIN,
        ToolInvocationStatus.FAILED,
        ToolInvocationStatus.DENIED,
    }
)
Status = Literal["completed", "failed", "uncertain"]


def subscription_modes(task: EmailTask, account_id: str) -> tuple[str, ...]:
    """Only the servers this gesture's mechanisms call for this account (ADR-0104)."""
    consent = task.subscription_consent
    if consent is None:
        return ()
    modes: list[str] = []
    if consent.action == "report_spam" or consent.archive_existing:
        modes.append("read")
    if consent.action != "unsubscribe" or consent.archive_existing:
        modes.append("write")
    if any(
        target.mechanism == "mailto" and target.account_id == account_id
        for target in consent.targets
    ):
        modes.append("send")
    return tuple(modes)


async def _invoke(
    io: _TaskIO, operation: str, call: Callable[[], Awaitable[dict[str, Any]]]
) -> ToolInvocation | None:
    """Dispatch one frozen action; a settled invocation is classified, never repeated."""
    from agent_core.runtime.email_tasks import EmailToolError

    c = io.context
    saved = c.checkpoint.working_state.get("email_calls", {}).get(operation)
    prior = None if saved is None else await io._invocation(saved["call"]["call_id"])
    if prior is not None and saved is not None and prior.status in _TERMINAL:
        await io._account_call(
            saved, Step(run_id=c.run.id, step_number=saved["step"], started_at=c.clock.now())
        )
        return prior
    subscriptions = io.service.subscriptions
    with suppress(EmailToolError):
        try:
            await call()
        except ApprovalRequiredError as exc:
            await subscriptions.approve(c.principal, c.run, c.lease, exc.approval_id)
            # Resolution is never a standing grant: time, scope and consent checks
            # apply again to the invocation the owner's gesture just approved.
            await subscriptions.validate(c.principal, c.run, c.lease)
            await call()
    saved = c.checkpoint.working_state.get("email_calls", {}).get(operation)
    return None if saved is None else await io._invocation(saved["call"]["call_id"])


def _status(invocation: ToolInvocation | None, *, matches: bool = True) -> Status:
    """An ambiguous dispatch is uncertain; it never becomes success or a retry."""
    if invocation is not None and invocation.status is ToolInvocationStatus.SUCCEEDED and matches:
        return "completed"
    if invocation is not None and (
        invocation.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.UNCERTAIN}
        or (
            invocation.status is ToolInvocationStatus.RUNNING
            and invocation.effect_sent_at is not None
        )
    ):
        return "uncertain"
    return "failed"


async def _one_click(io: _TaskIO, consent: EmailSubscriptionConsent) -> set[str]:
    arguments = consent.unsubscribe_arguments
    if arguments is None:
        return set()
    invocation = await _invoke(
        io,
        "unsubscribe",
        lambda: io.call_builtin(UNSUBSCRIBE_TOOL_NAME, arguments, operation="unsubscribe"),
    )
    result = None if invocation is None else invocation.structured_result
    if invocation is None or invocation.status is not ToolInvocationStatus.SUCCEEDED:
        return set()
    # The tool persisted each target's outcome as it completed.
    return {
        str(item.get("subscription_id"))
        for item in (result or {}).get("results", [])
        if isinstance(item, dict) and item.get("code") == "unsubscribe.accepted"
    }


async def _mailto(io: _TaskIO, consent: EmailSubscriptionConsent) -> set[str]:
    c = io.context
    sent: set[str] = set()
    for target in consent.mailto_targets:
        arguments = consent.send_arguments(target)
        operation = f"mailto:{target.subscription_id}"
        invocation = await _invoke(
            io,
            operation,
            lambda target=target, arguments=arguments, operation=operation: io.call(  # type: ignore[misc]
                target.account_id, "send_message", arguments, operation=operation
            ),
        )
        status = _status(invocation)
        await io.service.subscriptions.settle(
            c.principal,
            c.run,
            c.lease,
            target.subscription_id,
            action="unsubscribe",
            status=status,
            code={
                "completed": "unsubscribe.sent",
                "uncertain": "unsubscribe.send_uncertain",
                "failed": "unsubscribe.send_failed",
            }[status],
        )
        if status == "completed":
            sent.add(target.subscription_id)
    return sent


async def _sender_threads(io: _TaskIO, target: EmailSubscriptionTarget, *, limit: int) -> list[str]:
    """The sender's Inbox threads, from a server-composed query and a post-filter."""
    query = identity_query(target.identity_kind, target.identity)
    if query is None or limit <= 0:
        return []
    found: list[str] = []
    page_token: str | None = None
    while len(found) < limit:
        page = await io.call(
            target.account_id,
            "search_threads",
            {
                "query": f"{query} -in:spam -in:trash",
                "max_results": 25,
                **({"page_token": page_token} if page_token else {}),
            },
        )
        for summary in page.get("threads", []) or []:
            # A crafted identity cannot widen the set: every result proves itself.
            if isinstance(summary, dict) and summary_identity(summary) == (
                target.identity_kind,
                target.identity,
            ):
                thread_id = str(summary.get("thread_id", ""))
                if thread_id and thread_id not in found:
                    found.append(thread_id)
        page_token = page.get("next_page_token")
        if not page_token:
            break
    return found[:limit]


async def _labels(
    io: _TaskIO, consent: EmailSubscriptionConsent, account_id: str, thread_ids: list[str]
) -> tuple[Status, list[str]]:
    """Apply the consent's one fixed delta, at most twenty-five threads per call."""
    moved: list[str] = []
    for index in range(0, len(thread_ids), LABEL_THREADS_PER_CALL):
        chunk = thread_ids[index : index + LABEL_THREADS_PER_CALL]
        arguments = consent.label_arguments(chunk)
        operation = f"labels:{account_id}:{index // LABEL_THREADS_PER_CALL}"
        invocation = await _invoke(
            io,
            operation,
            lambda arguments=arguments, operation=operation: io.call(  # type: ignore[misc]
                account_id, "modify_labels", arguments, operation=operation
            ),
        )
        add, remove = consent.label_delta
        receipt = None if invocation is None else invocation.structured_result
        status = _status(
            invocation,
            matches=isinstance(receipt, dict)
            and receipt.get("thread_ids") == chunk
            and receipt.get("add_label_ids") == (add or [])
            and receipt.get("remove_label_ids") == (remove or []),
        )
        if status != "completed":
            return status, moved
        moved.extend(chunk)
    return "completed", moved


async def _report(io: _TaskIO, consent: EmailSubscriptionConsent) -> None:
    c = io.context
    [target] = consent.targets
    if consent.action == "not_spam":
        thread_ids = list(consent.restore_thread_ids)
    else:
        thread_ids = await _sender_threads(io, target, limit=LABEL_THREADS_PER_GESTURE)
        if target.evidence_thread_id and target.evidence_thread_id not in thread_ids:
            thread_ids = [target.evidence_thread_id, *thread_ids][:LABEL_THREADS_PER_GESTURE]
    status, moved = (
        await _labels(io, consent, target.account_id, thread_ids)
        if thread_ids
        else ("completed", [])
    )
    await io.service.subscriptions.settle(
        c.principal,
        c.run,
        c.lease,
        target.subscription_id,
        action=consent.action,
        status=status,
        code=f"labels.{status}",
        thread_ids=moved,
    )


async def _cleanup(io: _TaskIO, consent: EmailSubscriptionConsent, done: set[str]) -> None:
    """Archive existing Inbox mail only for senders whose unsubscribe was accepted or sent."""
    remaining = LABEL_THREADS_PER_GESTURE
    by_account: dict[str, list[str]] = {}
    for target in consent.targets:
        if target.subscription_id not in done or remaining <= 0:
            continue
        threads = by_account.setdefault(target.account_id, [])
        for thread_id in await _sender_threads(io, target, limit=remaining):
            if thread_id not in threads:
                threads.append(thread_id)
                remaining -= 1
    for account_id, thread_ids in by_account.items():
        if thread_ids:
            await _labels(io, consent, account_id, thread_ids)


async def run_subscription(io: _TaskIO) -> RunOutcome:
    """Execute one frozen owner gesture through ordinary approval and effect recovery."""
    from agent_core.runtime.email_tasks import EmailToolError, _finished

    c = io.context
    consent = io.task.subscription_consent
    if consent is None:
        raise ConflictError("subscription task has no owner consent")
    subscriptions = io.service.subscriptions
    try:
        await subscriptions.validate(c.principal, c.run, c.lease)
        if consent.action == "unsubscribe":
            done = await _one_click(io, consent) | await _mailto(io, consent)
            if consent.archive_existing and done:
                await _cleanup(io, consent, done)
        else:
            await _report(io, consent)
    except (EmailToolError, ConflictError, AuthorizationError, BudgetExceededError):
        # Whatever was not attempted returns to where it was; nothing is retried here.
        pass
    await subscriptions.finish(c.principal, c.run, c.lease)
    c.checkpoint.pending_tool_calls = []
    c.checkpoint.pending_approval_ids = []
    return _finished("The subscription request finished. Each sender shows its own outcome.")
