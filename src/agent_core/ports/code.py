"""The source of the running release, as Veetbot reads it (ADR-0281)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.code import (
    CodeCheck,
    CodeCheckResult,
    CodeDraft,
    CodeFinding,
    CodeReceipt,
)


class SourcePathRefusedError(ValueError):
    """The path is outside tracked source, or names something private or generated."""


class SourceNotTextError(ValueError):
    """The file is binary, too large, or not valid text."""


class SourcePatternInvalidError(ValueError):
    """The search pattern did not compile."""


class CodeCheckUnavailableError(RuntimeError):
    """The check did not finish within its limits, or its report cannot be believed."""


class CodeScreenUnavailableError(RuntimeError):
    """The screen gave no usable verdict, so nothing may be published."""


@dataclass(frozen=True, slots=True)
class SourceEntry:
    name: str
    is_directory: bool
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SourceSearch:
    lines: tuple[str, ...]
    truncated: bool


class SourceTree(Protocol):
    @property
    def release_id(self) -> str | None: ...

    def allows(self, path: str) -> bool:
        """Whether the path, which need not exist yet, is one tracked source may hold."""
        ...

    async def list_directory(self, path: str) -> list[SourceEntry]: ...

    async def read_text(self, path: str) -> str: ...

    async def search(
        self, pattern: str, *, path: str = "", fixed: bool = False, maximum_bytes: int
    ) -> SourceSearch: ...


class CodeDraftRepository(Protocol):
    """Owner-scoped drafts, one per slug, with optimistic versions; also the proposal outbox."""

    async def get(self, principal: Principal, slug: str) -> CodeDraft:
        """Raise NotFoundError for a missing or foreign draft."""
        ...

    async def list_open(self, principal: Principal, *, limit: int) -> list[CodeDraft]: ...

    async def put(self, draft: CodeDraft, *, expected_version: int | None) -> CodeDraft:
        """Insert when expected_version is None; raise ConflictError on a stale version."""
        ...

    async def due_proposals(self, now: datetime, *, limit: int) -> list[CodeDraft]:
        """Drafts in state PROPOSING whose proposal is due, oldest first, across owners."""
        ...


class CodeChecker(Protocol):
    """Run one closed check over a draft in a fresh offline sandbox."""

    async def run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        files: Mapping[str, str | None],
        check: CodeCheck,
    ) -> CodeCheckResult: ...


class CodeScreen(Protocol):
    """Check text about to reach the public repository, offline."""

    async def screen(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        added_lines: Mapping[str, str],
        commit_message: str,
        names: tuple[str, ...],
        settings_values: tuple[str, ...],
    ) -> tuple[CodeFinding, ...]: ...


class CodeProposalPublisher(Protocol):
    """Open one pull request from a new branch. Holds the only write credential."""

    async def publish(self, draft: CodeDraft) -> CodeReceipt:
        """Idempotent for a draft: a retry finds its branch and pull request, never force-pushes."""
        ...
