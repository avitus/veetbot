"""The proposal outbox: publish outside the transaction, settle with a version (ADR-0281)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.code.github import CodeProposalRefusedError
from agent_core.adapters.determinism import FixedClock
from agent_core.application.code_proposals import CodeProposalWorker
from agent_core.domain.code import CodeDraft, CodeDraftState, CodeProposal, CodeReceipt
from agent_core.domain.errors import ConflictError
from agent_core.ports.cockpit import WidgetRepositoryUnavailableError

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
RECEIPT = CodeReceipt(
    commit_sha="a" * 40, pull_number=7, pull_url="https://github.com/avitus/veetbot/pull/7"
)
UPSTREAM_BODY = "upstream said sensitive-body-text"


def _draft(
    *,
    slug: str = "fix-widget",
    attempts: int = 0,
    next_attempt_at: datetime = NOW,
    state: CodeDraftState = CodeDraftState.PROPOSING,
) -> CodeDraft:
    return CodeDraft(
        id=uuid4(),
        tenant_id="tenant",
        principal_id="owner",
        slug=slug,
        base_release="20261101-abcdef0",
        base_revision="abcdef0",
        files={"src/a.py": "new\n"},
        state=state,
        version=3,
        proposal=CodeProposal(
            title="Fix",
            body="Why",
            branch=f"veetbot/{slug}",
            attempts=attempts,
            next_attempt_at=next_attempt_at,
        ),
        created_at=NOW,
        updated_at=NOW,
    )


class _Store:
    """The port's outbox behaviour, with the versions it promises."""

    def __init__(self, *drafts: CodeDraft) -> None:
        self.drafts = {draft.id: draft for draft in drafts}
        self.limits: list[int] = []
        self.puts: list[tuple[CodeDraft, int | None]] = []
        self.open_transactions = 0
        self.before_put: Callable[[CodeDraft], None] | None = None

    async def due_proposals(self, now: datetime, *, limit: int) -> list[CodeDraft]:
        self.limits.append(limit)
        due = [
            draft
            for draft in self.drafts.values()
            if draft.state is CodeDraftState.PROPOSING
            and draft.proposal is not None
            and draft.proposal.next_attempt_at <= now
        ]
        return due[:limit]

    async def put(self, draft: CodeDraft, *, expected_version: int | None) -> CodeDraft:
        if self.before_put is not None:
            self.before_put(draft)
        if expected_version != self.drafts[draft.id].version:
            raise ConflictError("stale")
        self.puts.append((draft, expected_version))
        self.drafts[draft.id] = draft
        return draft


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.code_drafts = store
        self._store = store

    async def __aenter__(self) -> _Uow:
        self._store.open_transactions += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        self._store.open_transactions -= 1


class _Publisher:
    def __init__(
        self,
        store: _Store,
        outcome: Callable[[CodeDraft], Awaitable[CodeReceipt]] | None = None,
    ) -> None:
        self._store = store
        self._outcome = outcome
        self.published: list[UUID] = []
        self.open_during_publish: list[int] = []

    async def publish(self, draft: CodeDraft) -> CodeReceipt:
        self.published.append(draft.id)
        self.open_during_publish.append(self._store.open_transactions)
        if self._outcome is not None:
            return await self._outcome(draft)
        return RECEIPT


def _worker(store: _Store, publisher: _Publisher, clock: FixedClock | None = None) -> Any:
    return CodeProposalWorker(
        uow_factory=lambda: _Uow(store),  # type: ignore[arg-type]
        clock=clock or FixedClock(NOW),
        publisher=publisher,
    )


def _failing(error: Exception) -> Callable[[CodeDraft], Awaitable[CodeReceipt]]:
    async def outcome(_: CodeDraft) -> CodeReceipt:
        raise error

    return outcome


async def test_a_published_draft_becomes_proposed_with_its_receipt() -> None:
    draft = _draft(attempts=2).model_copy(deep=True)
    store = _Store(draft)
    clock = FixedClock(NOW + timedelta(seconds=5))
    assert await _worker(store, _Publisher(store), clock).run_once() == 1

    settled = store.drafts[draft.id]
    assert settled.state is CodeDraftState.PROPOSED
    assert settled.version == draft.version + 1
    assert settled.updated_at == clock.now()
    assert settled.proposal is not None
    assert settled.proposal.commit_sha == RECEIPT.commit_sha
    assert settled.proposal.pull_number == RECEIPT.pull_number
    assert settled.proposal.pull_url == RECEIPT.pull_url
    assert settled.proposal.last_error is None
    assert store.puts == [(settled, draft.version)]


async def test_publishing_holds_no_transaction_open() -> None:
    store = _Store(_draft())
    publisher = _Publisher(store)
    await _worker(store, publisher).run_once()
    assert publisher.open_during_publish == [0]


async def test_a_batch_is_bounded_and_a_draft_not_yet_due_is_left_alone() -> None:
    later = _draft(next_attempt_at=NOW + timedelta(seconds=1))
    store = _Store(later)
    publisher = _Publisher(store)
    assert await _worker(store, publisher).run_once() == 0
    assert publisher.published == []
    assert store.puts == []
    assert len(store.limits) == 1 and 0 < store.limits[0] <= 100


async def test_a_failure_counts_the_attempt_and_backs_off() -> None:
    draft = _draft(attempts=1)
    store = _Store(draft)
    publisher = _Publisher(
        store,
        _failing(WidgetRepositoryUnavailableError("GitHub answered 503 to POST /git/blobs")),
    )
    assert await _worker(store, publisher).run_once() == 0

    retried = store.drafts[draft.id]
    assert retried.state is CodeDraftState.PROPOSING
    assert retried.version == draft.version + 1
    assert retried.proposal is not None
    assert retried.proposal.attempts == 2
    assert retried.proposal.next_attempt_at == NOW + timedelta(seconds=30)
    assert retried.proposal.last_error == "github_http_503"
    assert retried.proposal.pull_number is None


@pytest.mark.parametrize(
    ("attempts", "delay"), [(0, 15), (1, 30), (5, 480), (6, 900), (7, 900), (500, 900)]
)
async def test_the_backoff_doubles_up_to_its_cap(attempts: int, delay: int) -> None:
    draft = _draft(attempts=attempts)
    store = _Store(draft)
    await _worker(store, _Publisher(store, _failing(RuntimeError("no")))).run_once()
    proposal = store.drafts[draft.id].proposal
    assert proposal is not None
    assert proposal.next_attempt_at == NOW + timedelta(seconds=delay)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (WidgetRepositoryUnavailableError("GitHub answered 422 to POST /pulls"), "github_http_422"),
        (
            WidgetRepositoryUnavailableError("GitHub did not answer GET /pulls"),
            "github_unreachable",
        ),
        (WidgetRepositoryUnavailableError("GitHub did not issue a token"), "github_authentication"),
        (
            WidgetRepositoryUnavailableError("GitHub refused an installation token (401)"),
            "github_authentication",
        ),
        (CodeProposalRefusedError("the branch holds someone's commit"), "proposal_refused"),
        (KeyError("html_url"), "publication_error"),
        (RuntimeError(UPSTREAM_BODY), "publication_error"),
    ],
)
async def test_last_error_is_a_bounded_category(error: Exception, category: str) -> None:
    draft = _draft()
    store = _Store(draft)
    await _worker(store, _Publisher(store, _failing(error))).run_once()
    proposal = store.drafts[draft.id].proposal
    assert proposal is not None
    assert proposal.last_error == category


@pytest.mark.parametrize(
    "error",
    [
        WidgetRepositoryUnavailableError(f"GitHub answered 500 to {UPSTREAM_BODY}"),
        WidgetRepositoryUnavailableError(f"GitHub did not answer {UPSTREAM_BODY}"),
        CodeProposalRefusedError(UPSTREAM_BODY),
        RuntimeError(UPSTREAM_BODY),
        RuntimeError(f"{UPSTREAM_BODY} token"),
    ],
)
async def test_no_upstream_text_reaches_the_draft(error: Exception) -> None:
    draft = _draft()
    store = _Store(draft)
    await _worker(store, _Publisher(store, _failing(error))).run_once()
    stored = store.drafts[draft.id].model_dump_json()
    assert "sensitive-body-text" not in stored
    assert "upstream" not in stored


async def test_one_failing_draft_does_not_stop_the_others() -> None:
    first, second, third = _draft(slug="first"), _draft(slug="second"), _draft(slug="third")
    store = _Store(first, second, third)

    async def outcome(draft: CodeDraft) -> CodeReceipt:
        if draft.id == second.id:
            raise RuntimeError("no")
        return RECEIPT

    publisher = _Publisher(store, outcome)
    assert await _worker(store, publisher).run_once() == 2
    assert publisher.published == [first.id, second.id, third.id]
    assert store.drafts[first.id].state is CodeDraftState.PROPOSED
    assert store.drafts[second.id].state is CodeDraftState.PROPOSING
    assert store.drafts[third.id].state is CodeDraftState.PROPOSED


async def test_a_stale_version_keeps_the_newer_draft_and_the_others_still_settle() -> None:
    raced, other = _draft(slug="raced"), _draft(slug="other")
    store = _Store(raced, other)
    newer = raced.model_copy(update={"version": raced.version + 1})

    def race(draft: CodeDraft) -> None:
        if draft.id == raced.id:
            store.drafts[raced.id] = newer

    store.before_put = race
    assert await _worker(store, _Publisher(store)).run_once() == 1
    assert store.drafts[raced.id] == newer
    assert store.drafts[other.id].state is CodeDraftState.PROPOSED


async def test_a_store_failure_while_settling_does_not_stop_the_others() -> None:
    broken, other = _draft(slug="broken"), _draft(slug="other")
    store = _Store(broken, other)

    def fail(draft: CodeDraft) -> None:
        if draft.id == broken.id:
            raise OSError("the database went away")

    store.before_put = fail
    assert await _worker(store, _Publisher(store)).run_once() == 1
    assert store.drafts[other.id].state is CodeDraftState.PROPOSED


async def test_a_retried_draft_waits_for_its_backoff_then_settles() -> None:
    draft = _draft()
    store = _Store(draft)
    clock = FixedClock(NOW)
    attempts = 0

    async def outcome(_: CodeDraft) -> CodeReceipt:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WidgetRepositoryUnavailableError("GitHub did not answer POST /pulls")
        return RECEIPT

    worker = _worker(store, _Publisher(store, outcome), clock)
    assert await worker.run_once() == 0
    assert await worker.run_once() == 0
    assert attempts == 1
    clock.advance(timedelta(seconds=15))
    assert await worker.run_once() == 1
    assert store.drafts[draft.id].state is CodeDraftState.PROPOSED


async def test_a_refusal_hands_the_draft_back_instead_of_retrying_forever() -> None:
    draft = _draft()
    store = _Store(draft)
    error = CodeProposalRefusedError("the branch holds someone's commit")
    await _worker(store, _Publisher(store, _failing(error))).run_once()
    settled = store.drafts[draft.id]
    assert settled.state is CodeDraftState.OPEN
    assert settled.proposal is not None
    assert settled.proposal.last_error == "proposal_refused"


async def test_an_outage_gives_up_after_its_last_attempt_and_says_why() -> None:
    draft = _draft()
    assert draft.proposal is not None
    worn = draft.model_copy(update={"proposal": draft.proposal.model_copy(update={"attempts": 19})})
    store = _Store(worn)
    error = WidgetRepositoryUnavailableError("GitHub answered 500 to POST /git/refs")
    await _worker(store, _Publisher(store, _failing(error))).run_once()
    settled = store.drafts[draft.id]
    assert settled.state is CodeDraftState.OPEN
    assert settled.proposal is not None
    assert settled.proposal.last_error == "github_http_500"
