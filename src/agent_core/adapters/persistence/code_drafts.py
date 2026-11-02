"""Owner-scoped code drafts, which double as the proposal outbox (ADR-0281)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import CodeDraftRow
from agent_core.domain.agents import Principal
from agent_core.domain.code import CodeDraft, CodeDraftState, CodeProposal
from agent_core.domain.errors import ConflictError, NotFoundError


def _conflict() -> ConflictError:
    return ConflictError(
        "The code draft changed; read it again before editing.", reason="code_draft_changed"
    )


def _next(draft: CodeDraft, expected_version: int | None) -> None:
    """Reject a write that does not advance the expected version by exactly one."""
    if draft.version != (expected_version or 0) + 1:
        raise _conflict()


class InMemoryCodeDraftRepository:
    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str, str], CodeDraft] = {}
        self._lock = asyncio.Lock()

    async def get(self, principal: Principal, slug: str) -> CodeDraft:
        """Read an owned draft or report that it does not exist."""
        async with self._lock:
            value = self._drafts.get((principal.tenant_id, principal.principal_id, slug))
            if value is None:
                raise NotFoundError("Code draft not found.")
            return value.model_copy(deep=True)

    async def list_open(self, principal: Principal, *, limit: int) -> list[CodeDraft]:
        """Return this owner's open drafts, most recently updated first."""
        async with self._lock:
            drafts = [
                value
                for key, value in self._drafts.items()
                if key[:2] == (principal.tenant_id, principal.principal_id)
                and value.state is CodeDraftState.OPEN
            ]
        drafts.sort(key=lambda draft: draft.slug)
        drafts.sort(key=lambda draft: draft.updated_at, reverse=True)
        return [draft.model_copy(deep=True) for draft in drafts[:limit]]

    async def put(self, draft: CodeDraft, *, expected_version: int | None) -> CodeDraft:
        """Insert or replace a draft using the expected persisted version."""
        _next(draft, expected_version)
        key = (draft.tenant_id, draft.principal_id, draft.slug)
        async with self._lock:
            previous = self._drafts.get(key)
            if (None if previous is None else previous.version) != expected_version:
                raise _conflict()
            self._drafts[key] = draft.model_copy(deep=True)
            return draft.model_copy(deep=True)

    async def due_proposals(self, now: datetime, *, limit: int) -> list[CodeDraft]:
        """Return proposing drafts that are due, oldest first, across owners."""
        async with self._lock:
            due = [
                (value.proposal.next_attempt_at, value.created_at, value.id, value)
                for value in self._drafts.values()
                if value.state is CodeDraftState.PROPOSING
                and value.proposal is not None
                and value.proposal.next_attempt_at <= now
            ]
        due.sort(key=lambda entry: entry[:3])
        return [entry[3].model_copy(deep=True) for entry in due[:limit]]


def _values(draft: CodeDraft) -> dict[str, Any]:
    """The draft's mutable columns, in row form."""
    return {
        "id": draft.id,
        "base_release": draft.base_release,
        "base_revision": draft.base_revision,
        "files": draft.files,
        "state": draft.state.value,
        "version": draft.version,
        "proposal": None if draft.proposal is None else draft.proposal.model_dump(mode="json"),
        "next_attempt_at": None if draft.proposal is None else draft.proposal.next_attempt_at,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


def _to_domain(row: CodeDraftRow) -> CodeDraft:
    """Rebuild the domain draft from its row."""
    return CodeDraft(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        slug=row.slug,
        base_release=row.base_release,
        base_revision=row.base_revision,
        files=row.files,
        state=CodeDraftState(row.state),
        version=row.version,
        proposal=None if row.proposal is None else CodeProposal.model_validate(row.proposal),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresCodeDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, principal: Principal, slug: str) -> CodeDraft:
        """Read an owned draft or report that it does not exist."""
        row = (
            await self._session.execute(
                select(CodeDraftRow)
                .where(
                    CodeDraftRow.tenant_id == principal.tenant_id,
                    CodeDraftRow.principal_id == principal.principal_id,
                    CodeDraftRow.slug == slug,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Code draft not found.")
        return _to_domain(row)

    async def list_open(self, principal: Principal, *, limit: int) -> list[CodeDraft]:
        """Return this owner's open drafts, most recently updated first."""
        rows = (
            await self._session.execute(
                select(CodeDraftRow)
                .where(
                    CodeDraftRow.tenant_id == principal.tenant_id,
                    CodeDraftRow.principal_id == principal.principal_id,
                    CodeDraftRow.state == CodeDraftState.OPEN.value,
                )
                .order_by(CodeDraftRow.updated_at.desc(), CodeDraftRow.slug)
                .limit(limit)
                .execution_options(populate_existing=True)
            )
        ).scalars()
        return [_to_domain(row) for row in rows]

    async def put(self, draft: CodeDraft, *, expected_version: int | None) -> CodeDraft:
        """Insert or replace a draft using the expected persisted version."""
        _next(draft, expected_version)
        if expected_version is None:
            try:
                async with self._session.begin_nested():
                    await self._session.execute(
                        insert(CodeDraftRow).values(
                            tenant_id=draft.tenant_id,
                            principal_id=draft.principal_id,
                            slug=draft.slug,
                            **_values(draft),
                        )
                    )
            except IntegrityError as exc:
                raise _conflict() from exc
        else:
            changed = (
                await self._session.execute(
                    update(CodeDraftRow)
                    .where(
                        CodeDraftRow.tenant_id == draft.tenant_id,
                        CodeDraftRow.principal_id == draft.principal_id,
                        CodeDraftRow.slug == draft.slug,
                        CodeDraftRow.version == expected_version,
                    )
                    .values(**_values(draft))
                    .returning(CodeDraftRow.slug)
                )
            ).scalar_one_or_none()
            if changed is None:
                raise _conflict()
        return draft.model_copy(deep=True)

    async def due_proposals(self, now: datetime, *, limit: int) -> list[CodeDraft]:
        """Return proposing drafts that are due, oldest first, across owners."""
        rows = (
            await self._session.execute(
                select(CodeDraftRow)
                .where(
                    CodeDraftRow.state == CodeDraftState.PROPOSING.value,
                    CodeDraftRow.next_attempt_at <= now,
                )
                .order_by(
                    CodeDraftRow.next_attempt_at,
                    CodeDraftRow.created_at,
                    CodeDraftRow.id,
                )
                .limit(limit)
                .execution_options(populate_existing=True)
            )
        ).scalars()
        return [_to_domain(row) for row in rows]
