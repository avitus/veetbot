"""The maintenance pass that answers title requests (ADR-0155).

Each request is handled on its own: read the owner's words in one unit of
work, call the titler outside any transaction, then write, clear and audit in
another. Every path clears the request it read, so a title that could not be
produced is retried by the next reply rather than by a loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, ProcessEvent
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.sessions import SessionTitleSource, TitleRequest
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.titles.generator import TitleDecision, TitleInput, TitleOutcome
from agent_core.titles.profiles import TitleGenerationProfile

logger = logging.getLogger(__name__)

TITLE_CHECKED_EVENT = "session.title.checked"
_USER_MESSAGE = "user.message.created"
# Owner messages sit among assistant and tool events; the first is near the start.
_FIRST_MESSAGE_WINDOW = 16
_END_OF_LOG = 2**62


class ConversationTitler(Protocol):
    async def title(self, title_input: TitleInput, *, principal: Principal) -> TitleOutcome: ...


async def request_title_after_run(
    uow_factory: UnitOfWorkFactory, principal: Principal, run: Run, requested_at: datetime
) -> bool:
    """Mark a completed top-level reply's session; the repository checks eligibility."""

    if run.status is not RunStatus.COMPLETED or run.parent_run_id is not None:
        return False
    async with uow_factory() as uow:
        return await uow.sessions.request_title(run.session_id, principal, requested_at)


def _message_text(event: EventEnvelope) -> str:
    """The text and attachment names of one message, collapsed to one line."""

    content: Any = event.payload.get("content")
    if isinstance(content, str):
        pieces = [content]
    elif isinstance(content, list):
        pieces = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("kind") == "text" and isinstance(part.get("text"), str):
                pieces.append(part["text"])
            elif part.get("kind") in {"image", "file"} and isinstance(part.get("filename"), str):
                pieces.append(part["filename"])
    else:
        pieces = []
    return " ".join(" ".join(pieces).split())


class ConversationTitlePass:
    """One principal's title round; `run_once` returns the titles it replaced."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        profile: TitleGenerationProfile,
        titler: ConversationTitler,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._profile = profile
        self._titler = titler

    async def run_once(self) -> int:
        if not self._profile.enabled:
            return 0
        async with self._uow_factory() as uow:
            requests = await uow.sessions.pending_title_requests(
                self._principal, limit=self._profile.batch_size
            )
        replaced = 0
        for request in requests:
            try:
                replaced += await self._answer(request)
            except Exception:
                logger.exception(
                    "conversation title request failed",
                    extra={"session_id": str(request.session_id)},
                )
                await self._abandon(request)
        return replaced

    async def _abandon(self, request: TitleRequest) -> None:
        """Clear a request whose write failed, so it never repeats the model call."""

        try:
            async with self._uow_factory() as uow:
                await uow.sessions.clear_title_request(
                    request.session_id, self._principal, requested_at=request.requested_at
                )
        except Exception:
            logger.exception(
                "conversation title request could not be cleared",
                extra={"session_id": str(request.session_id)},
            )

    async def _answer(self, request: TitleRequest) -> int:
        attempt_id = self._ids.new_id()
        first = request.title_source is SessionTitleSource.FIRST_MESSAGE
        try:
            async with self._uow_factory() as uow:
                title_input = await self._input(uow, request)
            outcome = await self._titler.title(title_input, principal=self._principal)
        except Exception as exc:
            outcome = TitleOutcome(decision=TitleDecision.FAILED, error_class=type(exc).__name__)

        async with self._uow_factory() as uow:
            sessions = uow.sessions
            decision = outcome.decision
            if decision is TitleDecision.REPLACED and outcome.title is not None:
                written = await sessions.write_generated_title(
                    request.session_id,
                    self._principal,
                    expected_title=request.title,
                    title=outcome.title,
                )
                # Another writer changed the title first; theirs stands.
                decision = TitleDecision.REPLACED if written else TitleDecision.KEPT
            elif decision is TitleDecision.KEPT and first:
                # Keeping a placeholder adopts it, so later requests ask about drift.
                await sessions.write_generated_title(
                    request.session_id,
                    self._principal,
                    expected_title=request.title,
                    title=request.title,
                )
            await sessions.clear_title_request(
                request.session_id, self._principal, requested_at=request.requested_at
            )
            await self._audit(uow, request, attempt_id, decision, outcome, first=first)
        return 1 if decision is TitleDecision.REPLACED else 0

    async def _input(self, uow: RepositoryUnitOfWork, request: TitleRequest) -> TitleInput:
        principal = self._principal
        limit = self._profile.message_chars
        opening = await uow.events.list_after(
            request.session_id, 0, principal, limit=_FIRST_MESSAGE_WINDOW
        )
        first_event = next((event for event in opening if self._owned_message(event)), None)
        latest: list[EventEnvelope] = []
        cursor = _END_OF_LOG
        # Bounded: other actors' messages are skipped but still cost a read.
        for _ in range(self._profile.recent_messages * 4):
            event = await uow.events.latest_before(
                request.session_id, cursor, _USER_MESSAGE, principal
            )
            if event is None or (
                first_event is not None and event.sequence <= first_event.sequence
            ):
                break
            cursor = event.sequence
            if self._owned_message(event):
                latest.append(event)
                if len(latest) == self._profile.recent_messages:
                    break
        return TitleInput(
            current_title=request.title,
            placeholder=request.title_source is SessionTitleSource.FIRST_MESSAGE,
            first_message="" if first_event is None else _message_text(first_event)[:limit],
            latest_messages=tuple(_message_text(event)[:limit] for event in reversed(latest)),
        )

    def _owned_message(self, event: EventEnvelope) -> bool:
        return event.event_type == _USER_MESSAGE and event.actor_id == self._principal.principal_id

    async def _audit(
        self,
        uow: RepositoryUnitOfWork,
        request: TitleRequest,
        attempt_id: Any,
        decision: TitleDecision,
        outcome: TitleOutcome,
        *,
        first: bool,
    ) -> None:
        principal = self._principal
        await uow.process_events.append(
            ProcessEvent(
                id=self._ids.new_id(),
                event_type=TITLE_CHECKED_EVENT,
                actor_type="system",
                actor_id=None,
                # Identifiers and counts only: never a title or a message.
                payload={
                    "tenant_id": principal.tenant_id,
                    "principal_id": principal.principal_id,
                    "session_id": str(request.session_id),
                    "attempt_id": str(attempt_id),
                    "outcome": decision.value,
                    "first": first,
                    "provider": outcome.provider,
                    "model": outcome.model,
                    "input_tokens": outcome.usage.input_tokens,
                    "output_tokens": outcome.usage.output_tokens,
                    "cost": str(outcome.usage.cost),
                    "error_class": outcome.error_class,
                },
                derivation_key=(
                    f"{TITLE_CHECKED_EVENT}:{principal.tenant_id}:"
                    f"{principal.principal_id}:{attempt_id}"
                ),
                created_at=self._clock.now(),
            )
        )
