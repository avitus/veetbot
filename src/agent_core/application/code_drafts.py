"""One owner-scoped set of edits against the running release (ADR-0281)."""

from __future__ import annotations

import difflib
from datetime import datetime

from agent_core.domain.agents import Principal
from agent_core.domain.code import (
    BRANCH_PREFIX,
    CodeDraft,
    CodeDraftState,
    CodeProposal,
    draft_digest,
    path_is_protected,
)
from agent_core.domain.errors import NotFoundError
from agent_core.ports.code import SourceNotTextError, SourcePathRefusedError, SourceTree
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory

_DIFF_LINE_CHARACTERS = 400


class CodeDraftRefusedError(ValueError):
    """A draft operation the owner's model can repair, named by a stable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _revision(release: str) -> str:
    return release.rsplit("-", 1)[-1]


class CodeDrafts:
    def __init__(
        self,
        *,
        tree: SourceTree,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._tree = tree
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids

    async def open(self, principal: Principal, slug: str) -> CodeDraft:
        """Start a draft, or begin again from a finished one. An open draft is returned as it is."""
        release = self._tree.release_id
        if not release:
            raise CodeDraftRefusedError(
                "code.release_unknown", "This server does not know which release it is running."
            )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            try:
                existing = await uow.code_drafts.get(principal, slug)
            except NotFoundError:
                existing = None
            if existing is not None and existing.state is CodeDraftState.OPEN:
                return existing
            if existing is not None and existing.state is CodeDraftState.PROPOSING:
                raise CodeDraftRefusedError(
                    "code.draft_proposing", "This draft is being opened as a pull request."
                )
            fresh = CodeDraft(
                id=self._ids.new_id(),
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                slug=slug,
                base_release=release,
                base_revision=_revision(release),
                version=1 if existing is None else existing.version + 1,
                created_at=now,
                updated_at=now,
            )
            return await uow.code_drafts.put(
                fresh, expected_version=None if existing is None else existing.version
            )

    async def current(self, principal: Principal, slug: str) -> CodeDraft:
        """The open draft, refused when a newer release means its base is gone."""
        async with self._uow_factory() as uow:
            draft = await uow.code_drafts.get(principal, slug)
        if draft.state is not CodeDraftState.OPEN:
            raise CodeDraftRefusedError(
                "code.draft_closed", f"This draft is {draft.state.value}; open it again to edit."
            )
        if draft.base_release != self._tree.release_id:
            raise CodeDraftRefusedError(
                "code.draft_stale",
                "A newer release is running, so this draft's base is gone. Discard it, open it "
                "again and reapply the edits.",
            )
        return draft

    async def _base(self, path: str) -> str | None:
        """A path's content in the release, refusing private and protected paths."""
        if not self._tree.allows(path):
            raise CodeDraftRefusedError(
                "code.path_refused", "That path is not tracked source, or it is private."
            )
        if path_is_protected(path):
            raise CodeDraftRefusedError(
                "code.path_protected",
                "CI, deployment and policy files stay with people; describe the change instead.",
            )
        try:
            return await self._tree.read_text(path)
        except SourcePathRefusedError:
            return None
        except SourceNotTextError as exc:
            raise CodeDraftRefusedError(
                "code.not_text", "That file is binary, over 2 MiB, or not valid UTF-8."
            ) from exc

    async def edit(
        self,
        principal: Principal,
        slug: str,
        path: str,
        *,
        old: str | None = None,
        new: str | None = None,
        content: str | None = None,
        delete: bool = False,
    ) -> CodeDraft:
        """Apply one exact replacement, whole-file write or deletion."""
        draft = await self.current(principal, slug)
        base = await self._base(path)
        before = draft.files.get(path, base)
        after: str | None
        if delete:
            if before is None:
                raise CodeDraftRefusedError("code.file_missing", "There is no such file to delete.")
            after = None
        elif content is not None:
            after = content
        elif old is not None and new is not None:
            if before is None:
                raise CodeDraftRefusedError(
                    "code.file_missing", "There is no such file; write it with content."
                )
            count = before.count(old)
            if count != 1:
                raise CodeDraftRefusedError(
                    "code.old_not_unique",
                    f"old must match exactly once, and it matched {count} times.",
                )
            after = before.replace(old, new)
        else:
            raise CodeDraftRefusedError(
                "code.edit_incomplete", "Give old and new, or content, or delete."
            )
        if after is not None and "\x00" in after:
            raise CodeDraftRefusedError("code.not_text", "A draft holds text only.")
        files = dict(draft.files)
        if after == base:
            # Back to the release's own content: no longer a change.
            files.pop(path, None)
        else:
            files[path] = after
        try:
            updated = draft.model_copy(
                update={
                    "files": files,
                    "version": draft.version + 1,
                    "updated_at": self._clock.now(),
                }
            )
            updated = CodeDraft.model_validate(updated.model_dump())
        except ValueError as exc:
            raise CodeDraftRefusedError("code.draft_too_large", str(exc)[:300]) from exc
        async with self._uow_factory() as uow:
            return await uow.code_drafts.put(updated, expected_version=draft.version)

    async def discard(self, principal: Principal, slug: str) -> CodeDraft:
        """Discard the owner's draft unless it is already being proposed."""
        async with self._uow_factory() as uow:
            draft = await uow.code_drafts.get(principal, slug)
            if draft.state is CodeDraftState.PROPOSING:
                raise CodeDraftRefusedError(
                    "code.draft_proposing", "This draft is being opened as a pull request."
                )
            return await uow.code_drafts.put(
                draft.model_copy(
                    update={
                        "state": CodeDraftState.DISCARDED,
                        "version": draft.version + 1,
                        "updated_at": self._clock.now(),
                    }
                ),
                expected_version=draft.version,
            )

    async def complete_diff(self, draft: CodeDraft) -> list[str]:
        """A unified diff of the draft against the release, every line whole."""
        lines: list[str] = []
        for path in sorted(draft.files):
            base = await self._base(path)
            after = draft.files[path]
            lines.extend(
                difflib.unified_diff(
                    (base or "").splitlines(),
                    (after or "").splitlines(),
                    fromfile=f"a/{path}" if base is not None else "/dev/null",
                    tofile=f"b/{path}" if after is not None else "/dev/null",
                    lineterm="",
                )
            )
        return lines

    async def diff(self, draft: CodeDraft) -> list[str]:
        """The diff for reading in chat, marking any long line it clips."""
        return [
            line
            if len(line) <= _DIFF_LINE_CHARACTERS
            else f"{line[:_DIFF_LINE_CHARACTERS]} ... [line clipped]"
            for line in await self.complete_diff(draft)
        ]

    async def added_lines(self, draft: CodeDraft) -> dict[str, str]:
        """What a proposal adds to the public repository, file by file."""
        added: dict[str, str] = {}
        for path, after in draft.files.items():
            if after is None:
                continue
            base = await self._base(path)
            matcher = difflib.SequenceMatcher(
                None, (base or "").splitlines(), after.splitlines(), autojunk=False
            )
            lines = after.splitlines()
            new = [
                line
                for tag, _, _, start, end in matcher.get_opcodes()
                if tag in {"insert", "replace"}
                for line in lines[start:end]
            ]
            added[path] = "\n".join(new)
        return added

    async def mark_proposing(
        self, draft: CodeDraft, *, title: str, body: str, now: datetime
    ) -> CodeDraft:
        # A retry keeps its branch's head, so different content must never share the name.
        branch = f"{BRANCH_PREFIX}{draft.slug}-{draft.id.hex[:8]}-{draft_digest(draft)}"
        async with self._uow_factory() as uow:
            return await uow.code_drafts.put(
                draft.model_copy(
                    update={
                        "state": CodeDraftState.PROPOSING,
                        "version": draft.version + 1,
                        "updated_at": now,
                        "proposal": CodeProposal(
                            title=title, body=body, branch=branch, next_attempt_at=now
                        ),
                    }
                ),
                expected_version=draft.version,
            )
