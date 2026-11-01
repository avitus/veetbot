"""Open approved code proposals from the outbox (ADR-0281).

The tool records the owner's approved proposal in PostgreSQL. This worker runs
in the maintenance role, the only one holding the GitHub key, and publishes each
due draft outside any transaction. Publishing is idempotent, so a draft whose
outcome could not be saved is simply published again and finds its pull request.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from agent_core.domain.code import CodeDraft, CodeDraftState, CodeProposal, CodeReceipt
from agent_core.domain.errors import ConflictError
from agent_core.ports.cockpit import WidgetRepositoryUnavailableError
from agent_core.ports.code import CodeProposalPublisher
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

logger = logging.getLogger(__name__)

_BATCH = 20
_RETRY_BASE_SECONDS = 15
_RETRY_MAX_SECONDS = 900
_PUBLICATION_ERROR = "publication_error"
_REFUSED = "proposal_refused"
# About four hours at the backoff's cap; then the owner's chat decides what to do.
_MAXIMUM_ATTEMPTS = 20


def _failure_category(error: Exception) -> str:
    """A bounded name for the failure, never its text, which may quote GitHub.

    The same rule as the Cockpit's `_retry_reason`, plus the adapter's refusal.
    """

    if isinstance(error, ConflictError):
        return _REFUSED
    if not isinstance(error, WidgetRepositoryUnavailableError):
        return _PUBLICATION_ERROR
    text = str(error)
    status = re.match(r"GitHub answered ([1-5][0-9]{2}) to ", text)
    if status:
        return f"github_http_{status[1]}"
    if "token" in text.lower():
        return "github_authentication"
    if text.startswith("GitHub did not answer"):
        return "github_unreachable"
    return _PUBLICATION_ERROR


class CodeProposalWorker:
    """Publish due drafts one at a time; each settles or retries on its own."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        publisher: CodeProposalPublisher,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._publisher = publisher

    async def run_once(self) -> int:
        """Attempt every due draft once; return how many became proposed."""

        async with self._uow_factory() as uow:
            due = await uow.code_drafts.due_proposals(self._clock.now(), limit=_BATCH)
        proposed = 0
        for draft in due:
            try:
                proposed += await self._attempt(draft)
            except ConflictError:
                # The draft moved while it was publishing. If it is still due, the
                # next pass finds the same branch and pull request.
                logger.info("code_proposal_version_moved", extra={"draft_id": str(draft.id)})
            except Exception:
                logger.exception("code_proposal_settle_failed", extra={"draft_id": str(draft.id)})
        return proposed

    async def _attempt(self, draft: CodeDraft) -> int:
        proposal = draft.proposal
        if proposal is None:
            logger.warning("code_proposal_missing", extra={"draft_id": str(draft.id)})
            return 0
        try:
            receipt = await self._publisher.publish(draft)
        except Exception as error:
            category = _failure_category(error)
            logger.warning(
                "code_proposal_publish_failed",
                extra={"draft_id": str(draft.id), "category": category},
                exc_info=category == _PUBLICATION_ERROR,
            )
            retried = self._retried(proposal, category)
            # A refusal cannot succeed later, and an outage must not retry forever. The
            # draft goes back to the chat with its reason, and nothing was merged.
            gave_up = category == _REFUSED or retried.attempts >= _MAXIMUM_ATTEMPTS
            state = CodeDraftState.OPEN if gave_up else CodeDraftState.PROPOSING
            await self._save(draft, state, retried)
            return 0
        await self._save(draft, CodeDraftState.PROPOSED, self._opened(proposal, receipt))
        return 1

    @staticmethod
    def _opened(proposal: CodeProposal, receipt: CodeReceipt) -> CodeProposal:
        return proposal.model_copy(
            update={
                "last_error": None,
                "commit_sha": receipt.commit_sha,
                "pull_number": receipt.pull_number,
                "pull_url": receipt.pull_url,
            }
        )

    def _retried(self, proposal: CodeProposal, category: str) -> CodeProposal:
        delay = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * 2 ** min(proposal.attempts, 16))
        return proposal.model_copy(
            update={
                "attempts": proposal.attempts + 1,
                "next_attempt_at": self._clock.now() + timedelta(seconds=delay),
                "last_error": category,
            }
        )

    async def _save(self, draft: CodeDraft, state: CodeDraftState, proposal: CodeProposal) -> None:
        async with self._uow_factory() as uow:
            await uow.code_drafts.put(
                draft.model_copy(
                    update={
                        "state": state,
                        "proposal": proposal,
                        "version": draft.version + 1,
                        "updated_at": self._clock.now(),
                    }
                ),
                expected_version=draft.version,
            )
