"""Read, change, test and propose Veetbot's own source (ADR-0281)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_core.application.cockpit import screen_names, screen_settings_values
from agent_core.application.code_drafts import CodeDraftRefusedError, CodeDrafts
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.code import (
    CHECKED_ROOTS,
    CODE_DRAFT_TOOL_NAME,
    CODE_PROPOSE_TOOL_NAME,
    CODE_READ_TOOL_NAME,
    CODE_TEST_TOOL_NAME,
    CodeCheck,
    CodeCheckKind,
    CodeDraft,
    CodeDraftState,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.code import (
    CodeChecker,
    CodeScreen,
    SourceNotTextError,
    SourcePathRefusedError,
    SourcePatternInvalidError,
    SourceTree,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.tools.cockpit import offers_widget_writes

# Tool results reach the model whole only up to the 4 KiB inline budget.
_PAGE_BYTES = 3400
_MAXIMUM_LINE_CHARACTERS = 1000
_MAXIMUM_ENTRIES = 150


def _failure(reason: str, detail: str, kind: ToolFailureKind) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(kind=kind, reason_code=reason, detail=detail, retryable=False),
    )


def _read_result(text: str, body: dict[str, Any]) -> ToolResult:
    return ToolResult(
        ok=True,
        content=[TextPart(text=text)],
        structured=body,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )


class CodeReadTool:
    spec = ToolSpec(
        name=CODE_READ_TOOL_NAME,
        version="1.0.0",
        description=(
            "Read and search the source and design documents of the release you are running. "
            "status: release and revision. list: a directory, or the root with path ''. "
            "read: a file from start_line, in pages. search: an extended regular expression, "
            "or literal text with fixed, optionally under path. AGENTS.md routes each subject "
            "to its design under docs/plan."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["status", "list", "read", "search"]},
                "path": {"type": "string", "maxLength": 512},
                "start_line": {"type": "integer", "minimum": 1},
                "pattern": {"type": "string", "minLength": 1, "maxLength": 300},
                "fixed": {"type": "boolean"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes={"code.read"},
        timeout_seconds=20,
        maximum_output_bytes=16 * 1024,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, tree: SourceTree) -> None:
        self._tree = tree

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del context
        operation = arguments["operation"]
        path = arguments.get("path", "")
        try:
            if operation == "status":
                return self._status()
            if operation == "list":
                return await self._list(path)
            if operation == "read":
                return await self._read(path, arguments.get("start_line", 1))
            pattern = arguments.get("pattern")
            if not pattern:
                return _failure(
                    "code.pattern_required",
                    "search needs a pattern.",
                    ToolFailureKind.INVALID_ARGUMENTS,
                )
            return await self._search(pattern, path, fixed=arguments.get("fixed", False))
        except SourcePathRefusedError:
            return _failure(
                "code.path_refused",
                "That path is not tracked source, or it is private or generated.",
                ToolFailureKind.INVALID_ARGUMENTS,
            )
        except SourceNotTextError:
            return _failure(
                "code.not_text",
                "That file is binary, over 2 MiB, or not valid UTF-8.",
                ToolFailureKind.INVALID_ARGUMENTS,
            )
        except SourcePatternInvalidError:
            return _failure(
                "code.pattern_invalid",
                "That pattern is not a valid extended regular expression; try fixed.",
                ToolFailureKind.INVALID_ARGUMENTS,
            )

    def _status(self) -> ToolResult:
        release = self._tree.release_id
        body = {
            "release_id": release,
            "revision": release.rsplit("-", 1)[-1] if release else None,
        }
        return _read_result(f"Running release {release or 'unknown'}.", body)

    async def _list(self, path: str) -> ToolResult:
        entries = await self._tree.list_directory(path)
        shown = entries[:_MAXIMUM_ENTRIES]
        body = {
            "entries": [
                {"name": entry.name, "directory": entry.is_directory, "bytes": entry.size_bytes}
                for entry in shown
            ],
            "truncated": len(entries) > len(shown),
        }
        text = "\n".join(
            f"{entry.name}/" if entry.is_directory else f"{entry.name} {entry.size_bytes}"
            for entry in shown
        )
        return _read_result(text or "(empty)", body)

    async def _read(self, path: str, start_line: int) -> ToolResult:
        lines = (await self._tree.read_text(path)).splitlines()
        shown: list[str] = []
        used = 0
        next_line: int | None = None
        for number in range(start_line, len(lines) + 1):
            line = f"{number}: {lines[number - 1][:_MAXIMUM_LINE_CHARACTERS]}"
            used += len(line.encode()) + 1
            if used > _PAGE_BYTES and shown:
                next_line = number
                break
            shown.append(line)
        body = {"path": path, "total_lines": len(lines), "next_line": next_line}
        return _read_result("\n".join(shown) or "(no lines at or after start_line)", body)

    async def _search(self, pattern: str, path: str, *, fixed: bool) -> ToolResult:
        found = await self._tree.search(pattern, path=path, fixed=fixed, maximum_bytes=_PAGE_BYTES)
        body = {"matches": len(found.lines), "truncated": found.truncated}
        return _read_result("\n".join(found.lines) or "(no matches)", body)


_DRAFT_SLUG = {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,47}$"}
_INTERACTIVE = "interactive"
_DIFF_MAX_LINES = 400
_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class CodeToolset:
    drafts: CodeDrafts
    uow_factory: UnitOfWorkFactory
    clock: Clock
    checker: CodeChecker | None = None
    screen: CodeScreen | None = None
    owner_names: tuple[str, ...] = ()
    proposal_wait_seconds: float = 30.0


async def _owner_chat(toolset: CodeToolset, context: ToolExecutionContext) -> ToolResult | None:
    """A change starts only in the owner's own chat, even when a tool is called by name."""
    async with toolset.uow_factory() as uow:
        session = await uow.sessions.get(context.session_id, context.principal)
    if context.run_kind != _INTERACTIVE or not offers_widget_writes(session.metadata):
        return _failure(
            "code.interactive_only",
            "Changes to Veetbot's code start in the owner's interactive chat.",
            ToolFailureKind.PERMISSION,
        )
    return None


def _refused(exc: CodeDraftRefusedError) -> ToolResult:
    return _failure(exc.reason, str(exc), ToolFailureKind.INVALID_ARGUMENTS)


def _draft_body(draft: CodeDraft) -> dict[str, Any]:
    return {
        "slug": draft.slug,
        "state": draft.state.value,
        "base_release": draft.base_release,
        "files": sorted(draft.files),
        "version": draft.version,
        "pull_url": draft.proposal.pull_url if draft.proposal else None,
        "proposal_error": draft.proposal.last_error if draft.proposal else None,
    }


class CodeDraftTool:
    spec = ToolSpec(
        name=CODE_DRAFT_TOOL_NAME,
        version="1.0.0",
        description=(
            "Change a private copy of the release you are running. open: start a draft by slug. "
            "edit: one exact replacement (old, new), a whole file (content), or delete. show: "
            "its diff, in pages from start_line. list and discard. CI, deployment and policy "
            "files are refused. Nothing leaves this server until code.propose."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["open", "edit", "show", "list", "discard"],
                },
                "slug": _DRAFT_SLUG,
                "path": {"type": "string", "maxLength": 512},
                "old": {"type": "string", "maxLength": 65536},
                "new": {"type": "string", "maxLength": 65536},
                "content": {"type": "string", "maxLength": 262144},
                "delete": {"type": "boolean"},
                "start_line": {"type": "integer", "minimum": 1},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"code.write"},
        timeout_seconds=20,
        maximum_output_bytes=16 * 1024,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, toolset: CodeToolset) -> None:
        self._toolset = toolset

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        refusal = await _owner_chat(self._toolset, context)
        if refusal is not None:
            return refusal
        operation = arguments["operation"]
        drafts = self._toolset.drafts
        principal = context.principal
        if operation == "list":
            async with self._toolset.uow_factory() as uow:
                found = await uow.code_drafts.list_open(principal, limit=20)
            body = {"drafts": [_draft_body(draft) for draft in found]}
            return _read_result(json.dumps(body, separators=(",", ":")), body)
        slug = arguments.get("slug")
        if not slug:
            return _failure(
                "code.slug_required",
                f"{operation} needs a slug.",
                ToolFailureKind.INVALID_ARGUMENTS,
            )
        try:
            if operation == "open":
                draft = await drafts.open(principal, slug)
            elif operation == "discard":
                draft = await drafts.discard(principal, slug)
            elif operation == "show":
                return await self._show(slug, arguments.get("start_line", 1), context)
            else:
                path = arguments.get("path")
                if not path:
                    return _failure(
                        "code.path_required",
                        "edit needs a path.",
                        ToolFailureKind.INVALID_ARGUMENTS,
                    )
                draft = await drafts.edit(
                    principal,
                    slug,
                    path,
                    old=arguments.get("old"),
                    new=arguments.get("new"),
                    content=arguments.get("content"),
                    delete=arguments.get("delete", False),
                )
        except CodeDraftRefusedError as exc:
            return _refused(exc)
        except NotFoundError:
            return _failure(
                "code.draft_not_found", "No such draft; open it.", ToolFailureKind.NOT_FOUND
            )
        body = _draft_body(draft)
        return _read_result(json.dumps(body, separators=(",", ":")), body)

    async def _show(self, slug: str, start_line: int, context: ToolExecutionContext) -> ToolResult:
        async with self._toolset.uow_factory() as uow:
            draft = await uow.code_drafts.get(context.principal, slug)
        lines = await self._toolset.drafts.diff(draft)
        shown: list[str] = []
        used = 0
        next_line: int | None = None
        for number in range(start_line, len(lines) + 1):
            used += len(lines[number - 1].encode()) + 1
            if used > _PAGE_BYTES and shown:
                next_line = number
                break
            shown.append(lines[number - 1])
        body = {**_draft_body(draft), "total_lines": len(lines), "next_line": next_line}
        return _read_result("\n".join(shown) or "(no changes)", body)


class CodeTestTool:
    spec = ToolSpec(
        name=CODE_TEST_TOOL_NAME,
        version="1.0.0",
        description=(
            "Run one check over a draft in a fresh offline sandbox built from the running "
            "release. kind: pytest, ruff, mypy or cockpit; targets: repository paths; keyword: "
            "a pytest filter. Tests needing PostgreSQL, Docker or a browser cannot run here, so "
            "a pass is partial evidence."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": _DRAFT_SLUG,
                "kind": {"type": "string", "enum": [kind.value for kind in CodeCheckKind]},
                "targets": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 512},
                    "maxItems": 20,
                },
                "keyword": {"type": "string", "pattern": "^[A-Za-z0-9_]{1,80}$"},
            },
            "required": ["slug", "kind", "targets"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.CODE_EXECUTION,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"code.write"},
        timeout_seconds=330,
        maximum_output_bytes=16 * 1024,
        allow_parallel=False,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target_kind="sandbox",
    )

    def __init__(self, toolset: CodeToolset) -> None:
        self._toolset = toolset

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        refusal = await _owner_chat(self._toolset, context)
        if refusal is not None:
            return refusal
        checker = self._toolset.checker
        if checker is None:
            return _failure(
                "code.checks_unavailable",
                "This server has no code sandbox.",
                ToolFailureKind.UPSTREAM_ERROR,
            )
        try:
            draft = await self._toolset.drafts.current(context.principal, arguments["slug"])
        except CodeDraftRefusedError as exc:
            return _refused(exc)
        except NotFoundError:
            return _failure(
                "code.draft_not_found", "No such draft; open it.", ToolFailureKind.NOT_FOUND
            )
        targets = tuple(arguments.get("targets", ()))
        if not targets:
            return _failure(
                "code.targets_required",
                "Name the test files or directories; the whole suite outlasts the sandbox.",
                ToolFailureKind.INVALID_ARGUMENTS,
            )
        # A change outside what the checks read, such as a document, cannot affect them.
        applied = {
            path: content
            for path, content in draft.files.items()
            if path.split("/", 1)[0] in CHECKED_ROOTS
        }
        try:
            result = await checker.run(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                lease_epoch=context.lease_epoch,
                files=applied,
                check=CodeCheck(
                    kind=CodeCheckKind(arguments["kind"]),
                    targets=targets,
                    keyword=arguments.get("keyword"),
                ),
            )
        except (ValueError, RuntimeError, OSError, TimeoutError):
            return _failure(
                "code.check_failed",
                "The sandbox reached no verdict. Nothing is known about this draft yet.",
                ToolFailureKind.UPSTREAM_ERROR,
            )
        body = {
            "passed": result.passed,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "seconds": result.seconds,
        }
        verdict = "passed" if result.passed else "did not pass"
        skipped = sorted(set(draft.files) - set(applied))
        note = f" Not applied, outside what checks read: {', '.join(skipped)}." if skipped else ""
        text = f"{arguments['kind']} {verdict}.{note}\n{result.output[-_PAGE_BYTES:]}"
        return ToolResult(
            ok=True,
            content=[TextPart(text=text)],
            structured=body,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )


class CodeProposeTool:
    spec = ToolSpec(
        name=CODE_PROPOSE_TOOL_NAME,
        version="1.0.0",
        description=(
            "Ask the owner to open a draft as a pull request to dev from a new branch. It "
            "publishes the change to a public repository, so run code.test first and say what "
            "you could not verify in summary. You cannot merge or deploy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": _DRAFT_SLUG,
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "summary": {"type": "string", "minLength": 1, "maxLength": 6000},
            },
            "required": ["slug", "title", "summary"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.PUBLICATION,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        required_scopes={"code.write"},
        timeout_seconds=120,
        maximum_output_bytes=16 * 1024,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, toolset: CodeToolset) -> None:
        self._toolset = toolset

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        refusal = await _owner_chat(self._toolset, context)
        if refusal is not None:
            return refusal
        toolset = self._toolset
        slug, title = arguments["slug"], arguments["title"]
        principal = context.principal
        try:
            async with toolset.uow_factory() as uow:
                existing = await uow.code_drafts.get(principal, slug)
        except NotFoundError:
            return _failure(
                "code.draft_not_found", "No such draft; open it.", ToolFailureKind.NOT_FOUND
            )
        # A replay finds the proposal it already recorded, and opens nothing new.
        if (
            existing.state in {CodeDraftState.PROPOSING, CodeDraftState.PROPOSED}
            and existing.proposal is not None
            and existing.proposal.title == title
        ):
            return await self._await_pull(existing, context)
        if toolset.screen is None:
            return _failure(
                "code.proposals_unavailable",
                "This server cannot screen a proposal.",
                ToolFailureKind.UPSTREAM_ERROR,
            )
        try:
            draft = await toolset.drafts.current(principal, slug)
        except CodeDraftRefusedError as exc:
            return _refused(exc)
        if not draft.files:
            return _failure(
                "code.draft_empty", "This draft changes nothing.", ToolFailureKind.INVALID_ARGUMENTS
            )
        body = (
            f"{arguments['summary']}\n\n---\nProposed by Veetbot from release "
            f"{draft.base_release} after the owner approved opening it. Hosted CI has not run."
        )
        async with toolset.uow_factory() as uow:
            names = await screen_names(uow, principal, toolset.owner_names)
            settings_values = await screen_settings_values(uow, principal)
        try:
            findings = await toolset.screen.screen(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                lease_epoch=context.lease_epoch,
                added_lines=await toolset.drafts.added_lines(draft),
                commit_message=f"{title}\n\n{body}",
                names=names,
                settings_values=settings_values,
            )
        except (ValueError, RuntimeError, OSError, TimeoutError):
            return _failure(
                "code.screen_unavailable",
                "The privacy screen reached no verdict, so nothing was published.",
                ToolFailureKind.UPSTREAM_ERROR,
            )
        if findings:
            detail = "; ".join(f"{item.file}: {item.message}" for item in findings[:10])
            return _failure(
                "code.screen_refused",
                f"The change would publish private data or a secret. {detail}"[:2000],
                ToolFailureKind.INVALID_ARGUMENTS,
            )
        proposing = await toolset.drafts.mark_proposing(
            draft, title=title, body=body, now=toolset.clock.now()
        )
        return await self._await_pull(proposing, context)

    async def _await_pull(self, draft: CodeDraft, context: ToolExecutionContext) -> ToolResult:
        toolset = self._toolset
        waited = 0.0
        while draft.state is CodeDraftState.PROPOSING and waited < toolset.proposal_wait_seconds:
            await toolset.clock.sleep(_POLL_SECONDS)
            waited += _POLL_SECONDS
            async with toolset.uow_factory() as uow:
                draft = await uow.code_drafts.get(context.principal, draft.slug)
        proposal = draft.proposal
        body = {
            **_draft_body(draft),
            "branch": proposal.branch if proposal else None,
            "pull_number": proposal.pull_number if proposal else None,
        }
        if draft.state is CodeDraftState.PROPOSED:
            text = f"Opened pull request {body['pull_url']} for the owner's review."
        elif draft.state is CodeDraftState.PROPOSING:
            text = "The pull request is queued; code.draft show will carry its link."
        else:
            text = f"The pull request was not opened: {body['proposal_error']}. Nothing changed."
        return _read_result(text, body)

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        *,
        run: object,
        principal: Principal,
        turn: object,
        not_covered: object,
    ) -> ApprovalPresentation:
        """The change as a bounded diff, in server-authored words."""
        del run, turn, not_covered
        slug = str(arguments.get("slug", ""))
        view: dict[str, Any] = {
            "slug": slug,
            "title": arguments.get("title"),
            "summary": arguments.get("summary"),
        }
        try:
            async with self._toolset.uow_factory() as uow:
                draft = await uow.code_drafts.get(principal, slug)
            lines = await self._toolset.drafts.diff(draft)
            view.update(
                file_changes=sorted(draft.files),
                diff=lines[:_DIFF_MAX_LINES],
                diff_truncated=len(lines) > _DIFF_MAX_LINES,
            )
        except (NotFoundError, CodeDraftRefusedError):
            view["diff_status"] = "unknown"
        return ApprovalPresentation(
            summary=(
                "Publish this change to the public repository as a new branch and a pull request "
                "to dev, for your review. Veetbot cannot merge or deploy it."
            ),
            arguments=view,
        )
