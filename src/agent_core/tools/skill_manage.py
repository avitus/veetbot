"""Governed foreground and background skill authoring."""

from __future__ import annotations

import difflib
import hashlib
from typing import Any, cast

from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    SkillRevisionConflict,
    SkillValidationError,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.skills import (
    SKILL_NAME_PATTERN,
    AuthoringContext,
    SkillPackage,
    SkillPackageMember,
    SkillRef,
    SkillRevision,
    SkillSource,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.skills import SkillPackageStore
from agent_core.skills.package import read_archive_members

SKILL_MANAGE_TOOL_NAME = "skill.manage"
SKILL_WRITE_SCOPE = "skill.write"
MAX_APPROVAL_DIFF_BYTES = 32_768

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["create", "edit", "patch", "archive"]},
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": SKILL_NAME_PATTERN.pattern,
        },
        "expected_revision": {"type": "integer", "minimum": 1},
        "skill_markdown": {"type": "string", "minLength": 1, "maxLength": 262144},
        "files": {
            "type": "object",
            "additionalProperties": {"type": ["string", "null"]},
            "maxProperties": 63,
        },
    },
    "required": ["operation", "name"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string"},
        "name": {"type": "string"},
        "revision": {"type": "integer"},
        "version": {"type": "string"},
        "sha256": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": ["operation", "name", "revision", "status"],
    "additionalProperties": False,
}


def _failure(kind: ToolFailureKind, reason: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[TextPart(text="The skill change was not applied.")],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason,
            detail=detail,
            retryable=reason == "skill_revision_conflict",
        ),
    )


def _result(operation: str, revision: SkillRevision) -> ToolResult:
    structured = {
        "operation": operation,
        "name": revision.manifest.name,
        "revision": revision.revision,
        "version": revision.manifest.version,
        "sha256": revision.content_sha256,
        "status": revision.status.value,
    }
    verb = {
        "create": "created",
        "edit": "edited",
        "patch": "patched",
        "archive": "archived",
    }[operation]
    return ToolResult(
        ok=True,
        content=[
            TextPart(text=(f"Skill {revision.manifest.name}@{revision.revision} was {verb}."))
        ],
        structured=structured,
    )


def _display_path(path: str) -> str:
    return path.replace("\r", r"\r").replace("\n", r"\n")


def _bounded_diff(
    name: str,
    expected: object,
    before_members: dict[str, bytes],
    after_members: dict[str, bytes],
) -> tuple[list[str], bool]:
    diff: list[str] = []
    rendered_bytes = 0
    for path in sorted(set(before_members) | set(after_members)):
        before = before_members.get(path, b"")
        after = after_members.get(path, b"")
        try:
            before_lines = before.decode("utf-8").splitlines()
            after_lines = after.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            before_lines = [f"[binary sha256={hashlib.sha256(before).hexdigest()}]"]
            after_lines = [f"[binary sha256={hashlib.sha256(after).hexdigest()}]"]
        display_path = _display_path(path)
        lines = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{name}@{expected or 0}/{display_path}",
            tofile=f"{name}@proposed/{display_path}",
            lineterm="",
        )
        for line in lines:
            line_bytes = len((line + "\n").encode("utf-8"))
            if rendered_bytes + line_bytes > MAX_APPROVAL_DIFF_BYTES:
                diff.append(f"[diff truncated at {MAX_APPROVAL_DIFF_BYTES} bytes]")
                return diff, True
            diff.append(line)
            rendered_bytes += line_bytes
    return diff, False


class SkillManageTool:
    """Create and revise only agent-owned, tenant-scoped skill packages."""

    spec = ToolSpec(
        name=SKILL_MANAGE_TOOL_NAME,
        version="1.0.0",
        description="Create, revise, patch, or archive an agent-authored skill package.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        required_scopes={SKILL_WRITE_SCOPE},
        timeout_seconds=15,
        maximum_output_bytes=16_384,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, uow_factory: UnitOfWorkFactory, store: SkillPackageStore) -> None:
        self._uow_factory = uow_factory
        self._store = store

    async def approval_view(
        self, arguments: dict[str, Any], *, tenant_id: str
    ) -> tuple[str, dict[str, Any]]:
        """Present the concrete bounded package change instead of opaque raw arguments."""

        operation = str(arguments["operation"])
        name = str(arguments["name"])
        expected = arguments.get("expected_revision")
        current_revision = 0
        before_members: dict[str, bytes] = {}
        if operation != "create":
            try:
                async with self._uow_factory() as uow:
                    current = await uow.skills.resolve(tenant_id, SkillRef(name=name))
                current_revision = current.revision
                if current.source is SkillSource.AGENT:
                    archive = await self._store.archive_bytes(current.package_key)
                    before_members = {
                        member.path: member.data for member in read_archive_members(archive)
                    }
            except (NotFoundError, SkillValidationError):
                before_members = {}
        files = arguments.get("files", {})
        after_members = dict(before_members) if operation in {"patch", "archive"} else {}
        if operation != "archive" and arguments.get("skill_markdown") is not None:
            after_members["SKILL.md"] = str(arguments["skill_markdown"]).encode("utf-8")
        if isinstance(files, dict):
            for raw_path, raw_data in files.items():
                path = str(raw_path)
                if raw_data is None:
                    after_members.pop(path, None)
                elif isinstance(raw_data, str):
                    after_members[path] = raw_data.encode("utf-8")
        diff, diff_truncated = _bounded_diff(name, expected, before_members, after_members)
        proposed_revision = current_revision if operation == "archive" else current_revision + 1
        return (
            f"{operation.capitalize()} agent-authored skill {name}"
            + ("" if expected is None else f" from revision {expected}"),
            {
                "operation": operation,
                "name": name,
                "expected_revision": expected,
                "current_revision": current_revision,
                "proposed_revision": proposed_revision,
                "canonical_diff": diff,
                "diff_truncated": diff_truncated,
                "file_changes": [
                    path
                    for path in sorted(set(before_members) | set(after_members))
                    if before_members.get(path) != after_members.get(path)
                ],
            },
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if SKILL_WRITE_SCOPE not in context.principal.scopes:
            return _failure(
                ToolFailureKind.PERMISSION,
                "policy.scope.missing",
                "skill.write scope is required",
            )
        if context.origin_trust is not TrustLevel.USER:
            return _failure(
                ToolFailureKind.PERMISSION,
                "policy.skill.origin_untrusted",
                "skill authoring requires a user-trusted turn",
            )

        operation = str(arguments.get("operation", ""))
        name = str(arguments.get("name", ""))
        if operation not in {"create", "edit", "patch", "archive"}:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "operation must be create, edit, patch, or archive",
            )
        if SKILL_NAME_PATTERN.fullmatch(name) is None:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "skill name does not match the required grammar",
            )
        expected = arguments.get("expected_revision")
        files = arguments.get("files", {})
        if operation == "create" and expected is not None:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "create must not include expected_revision",
            )
        if operation == "patch" and files:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "patch changes SKILL.md only",
            )
        if operation == "archive" and (arguments.get("skill_markdown") is not None or files):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "archive accepts only name and expected_revision",
            )
        if operation != "create" and (not isinstance(expected, int) or expected < 1):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "expected_revision is required for edits, patches, and archives",
            )
        expected_revision = None if operation == "create" else cast(int, expected)
        if context.run_kind == "skill_review":
            if operation == "archive":
                return _failure(
                    ToolFailureKind.PERMISSION,
                    "policy.skill.review_archive_denied",
                    "background reviews cannot archive skills",
                )
            if operation in {"edit", "patch"} and not any(
                str(item.get("name")) == name and int(item.get("revision", 0)) == expected_revision
                for item in context.loaded_skills
            ):
                return _failure(
                    ToolFailureKind.PERMISSION,
                    "policy.skill.review_read_required",
                    "background reviews must load the current skill revision before writing",
                )

        try:
            if operation == "archive":
                return await self._archive(name, cast(int, expected_revision), context)
            package = await self._package(operation, name, arguments, context)
            await context.mark_effect_sent()
            async with self._uow_factory() as uow:
                revision = await uow.skills.install(
                    context.tenant_id,
                    package,
                    SkillSource.AGENT,
                    0 if expected_revision is None else expected_revision,
                    AuthoringContext(
                        run_id=context.run_id,
                        principal_id=context.principal.principal_id,
                        invocation_id=context.invocation_id,
                        idempotency_key=context.idempotency_key,
                    ),
                )
            return _result(operation, revision)
        except SkillRevisionConflict as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "skill_revision_conflict",
                f"current revision is {exc.current_revision}",
            )
        except NotFoundError as exc:
            return _failure(ToolFailureKind.NOT_FOUND, "skill.not_found", str(exc))
        except SkillValidationError as exc:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                f"skill.validation.{exc.rule}",
                str(exc),
            )
        except (ConflictError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, ConflictError) else None
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                reason or "skill.change_conflict",
                str(exc),
            )

    async def _package(
        self,
        operation: str,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> SkillPackage:
        current_members: dict[str, bytes] = {}
        if operation in {"edit", "patch"}:
            async with self._uow_factory() as uow:
                current = await uow.skills.resolve(context.tenant_id, SkillRef(name=name))
            if current.source is not SkillSource.AGENT:
                raise ConflictError(
                    "only agent-authored skills may be changed by skill.manage",
                    reason="skill.source_immutable",
                )
            expected = int(arguments["expected_revision"])
            if current.revision != expected:
                raise SkillRevisionConflict(current.revision)
            if operation == "patch":
                archive = await self._store.archive_bytes(current.package_key)
                current_members = {item.path: item.data for item in read_archive_members(archive)}

        skill_markdown = arguments.get("skill_markdown")
        if skill_markdown is not None:
            current_members["SKILL.md"] = str(skill_markdown).encode("utf-8")
        files = arguments.get("files", {})
        if not isinstance(files, dict):
            raise ValueError("files must be an object")
        for raw_path, raw_data in files.items():
            path = str(raw_path)
            if path == "SKILL.md":
                raise ValueError("files must not replace SKILL.md; use skill_markdown")
            if raw_data is None:
                current_members.pop(path, None)
            elif isinstance(raw_data, str):
                current_members[path] = raw_data.encode("utf-8")
            else:
                raise ValueError("file values must be strings or null")
        if "SKILL.md" not in current_members:
            raise ValueError("skill_markdown is required")
        return SkillPackage(
            directory_name=name,
            members=tuple(
                SkillPackageMember(path=path, data=data)
                for path, data in sorted(current_members.items())
            ),
        )

    async def _archive(
        self,
        name: str,
        expected_revision: int,
        context: ToolExecutionContext,
    ) -> ToolResult:
        async with self._uow_factory() as uow:
            current = await uow.skills.resolve(
                context.tenant_id, SkillRef(name=name, revision=expected_revision)
            )
        if current.source is not SkillSource.AGENT:
            raise ConflictError(
                "only agent-authored skills may be archived by skill.manage",
                reason="skill.source_immutable",
            )
        if current.revision != expected_revision:
            raise SkillRevisionConflict(current.revision)
        await context.mark_effect_sent()
        async with self._uow_factory() as uow:
            archived = await uow.skills.archive(
                context.tenant_id,
                name,
                expected_revision,
                AuthoringContext(
                    run_id=context.run_id,
                    principal_id=context.principal.principal_id,
                    invocation_id=context.invocation_id,
                    idempotency_key=context.idempotency_key,
                ),
            )
        return _result("archive", archived)
