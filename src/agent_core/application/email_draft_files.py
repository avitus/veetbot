"""Private staging and approved byte transport for email drafts (ADR-0317)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from agent_core.application.email_attachments import _chunks
from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import (
    ATTACHMENT_METADATA_KEY,
    EMAIL_SOURCE_METADATA_KEY,
    StoredArtifactRef,
)
from agent_core.domain.attachments import stored_email_attachment_name
from agent_core.domain.email import EmailDraft, EmailTask, EmailThread, send_message_arguments
from agent_core.domain.email_drafting import (
    DRAFT_ATTACHMENT_BYTES,
    DRAFT_ATTACHMENT_ORIGIN,
    DRAFT_TARGET_KEY,
    DraftAttachment,
)
from agent_core.domain.errors import (
    ArtifactStorageError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from agent_core.domain.mcp import MCPCallResult
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolExecutionContext
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.artifacts import ArtifactStore, ArtifactWriter, AttachmentInspector
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class EmailDraftFiles:
    """Keep bytes off durable tool paths and freeze them under the owner's consent."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: ArtifactStore,
        inspector: AttachmentInspector,
        clock: Clock,
    ) -> None:
        self.uow_factory, self.store, self.inspector, self.clock = (
            uow_factory,
            store,
            inspector,
            clock,
        )

    async def validate(
        self, uow: RepositoryUnitOfWork, principal: Principal, draft: EmailDraft
    ) -> list[ArtifactRef]:
        """Validate immutable metadata, source authority and retention; caller owns email lock."""
        if (
            len(draft.attachments) > 10
            or sum(a.size_bytes for a in draft.attachments) > DRAFT_ATTACHMENT_BYTES
        ):
            raise ConflictError("The reply attachments exceed the email size limit.")
        rows = []
        for item in draft.attachments:
            row = await uow.artifacts.get(item.artifact_id, principal)
            source = row.metadata.get(EMAIL_SOURCE_METADATA_KEY, {})
            server = source.get("server_id")
            if not isinstance(server, str) or f"mcp.{server}.use" not in principal.scopes:
                raise AuthorizationError("The source mailbox is no longer accessible.")
            if (
                row.origin != DRAFT_ATTACHMENT_ORIGIN
                or row.session_id != draft.session_id
                or row.metadata.get(DRAFT_TARGET_KEY) != str(draft.thread_id)
                or (row.expires_at is not None and row.expires_at <= self.clock.now())
                or row.sha256 != item.sha256
                or row.size_bytes != item.size_bytes
                or row.name != item.filename
                or row.media_type != item.media_type
                or source.get("account_id") != item.account_id
                or source.get("thread_id") != item.source_thread_id
                or source.get("message_id") != item.message_id
                or source.get("attachment_index") != item.attachment_index
            ):
                raise ConflictError(
                    "An attachment changed or is no longer available. Regenerate the reply."
                )
            source_key = hashlib.sha256(
                f"{item.account_id}:{item.source_thread_id}".encode()
            ).hexdigest()
            if await uow.email.get(principal, "excluded_source", source_key) is not None:
                raise ConflictError("An attachment source was erased. Regenerate the reply.")
            rows.append(row)
        return rows

    async def read(self, row: ArtifactRef) -> bytes:
        """Release only bytes verified against the immutable stored size and digest."""
        ref = StoredArtifactRef(
            artifact_id=row.id,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
        )
        stream = await self.store.open_verified(ref, tenant_id=row.tenant_id)
        parts = []
        size = 0
        async for chunk in stream:
            size += len(chunk)
            if size > DRAFT_ATTACHMENT_BYTES:
                raise ConflictError("The attachment exceeds the email size limit.")
            parts.append(chunk)
        content = b"".join(parts)
        if len(content) != row.size_bytes or hashlib.sha256(content).hexdigest() != row.sha256:
            raise ConflictError("The attachment content changed. Regenerate the reply.")
        return content

    async def invoke(
        self,
        context: ToolExecutionContext,
        server_id: str,
        remote: str,
        arguments: dict[str, Any],
        call: Callable[[dict[str, Any]], Awaitable[MCPCallResult]],
    ) -> MCPCallResult:
        """Only a typed task can reach staging or the private send transport."""
        try:
            if remote == "stage_attachment":
                return await self._stage(context, server_id, arguments, call)
            return await self._send(context, server_id, arguments, call)
        except (
            ValueError,
            IndexError,
            ArtifactStorageError,
            KeyError,
            TypeError,
            binascii.Error,
            ConflictError,
            NotFoundError,
            AuthorizationError,
        ):
            return MCPCallResult(
                content=("gmail.arguments_invalid",),
                is_error=True,
                structured={"effect_status": "not_applied"},
            )

    async def _stage(
        self,
        context: ToolExecutionContext,
        server: str,
        args: dict[str, Any],
        call: Callable[[dict[str, Any]], Awaitable[MCPCallResult]],
    ) -> MCPCallResult:
        # Read only after validating application intent; revalidate under the same
        # owner lock after downloading to fence source exclusion and cancellation.
        async def validate(uow: RepositoryUnitOfWork) -> tuple[EmailThread, EmailThread]:
            task_row = await uow.email.get(context.principal, "task", str(context.run_id))
            if task_row is None:
                raise ConflictError("Draft preparation task required")
            task = EmailTask.model_validate(task_row.payload)
            if task.kind not in {"draft", "refresh"} or task.session_id != context.session_id:
                raise ConflictError("Draft preparation task required")
            target_row = await uow.email.get(
                context.principal, "thread", str(UUID(args["draft_thread_id"]))
            )
            source_row = await uow.email.get(
                context.principal, "thread", str(UUID(args["source_thread_id"]))
            )
            if target_row is None or source_row is None:
                raise NotFoundError("Draft source missing")
            target, source = (
                EmailThread.model_validate(target_row.payload),
                EmailThread.model_validate(source_row.payload),
            )
            session = await uow.sessions.get(context.session_id, context.principal)
            servers = session.metadata.get("email_account_servers", {})
            if (
                target.account_id not in task.account_ids
                or (task.kind == "draft" and task.thread_id != target.id)
                or servers.get(source.account_id, {}).get("read") != server
                or f"mcp.{server}.use" not in context.principal.scopes
                or not source.complete
            ):
                raise AuthorizationError("Draft source is not authorized")
            message = next((m for m in source.messages if m.id == args["message_id"]), None)
            if message is None or args["attachment_index"] < 0:
                raise ConflictError("Draft source changed")
            selected = message.attachments[args["attachment_index"]]
            if (
                selected.filename != args["filename"]
                or (selected.size or 0) > DRAFT_ATTACHMENT_BYTES
            ):
                raise ConflictError("Draft source changed")
            return target, source

        async with self.uow_factory() as uow, uow.email.lock(context.principal):
            _target, before = await validate(uow)
        result = await call(args)
        if result.is_error:
            return MCPCallResult(
                content=("gmail.attachment_unavailable",),
                is_error=True,
                structured={"effect_status": "not_applied"},
            )
        value = result.structured or {}
        if (
            value.get("message_id") != args["message_id"]
            or value.get("thread_id") != before.provider_thread_id
            or value.get("filename") != args["filename"]
        ):
            raise ConflictError("Attachment identity changed")
        encoded = value["data_base64"]
        if not isinstance(encoded, str) or len(encoded) > 20 * 1024 * 1024:
            raise ValueError("Attachment exceeds limit")
        content = base64.b64decode(encoded, validate=True)
        if not content or len(content) > DRAFT_ATTACHMENT_BYTES:
            raise ValueError("Attachment exceeds limit")
        original = next(message for message in before.messages if message.id == args["message_id"])
        expected_size = original.attachments[args["attachment_index"]].size
        if expected_size is not None and len(content) != expected_size:
            raise ConflictError("Attachment size changed since the source read")
        name = stored_email_attachment_name(args["filename"])
        facts = await self.inspector.inspect(
            content,
            filename=name,
            declared_media_type=value.get("mime_type") or "application/octet-stream",
        )
        async with self.uow_factory() as uow, uow.email.lock(context.principal):
            target, source = await validate(uow)
            if source.revision != before.revision:
                raise ConflictError("Attachment source changed")
            run = await uow.runs.get(context.run_id, context.principal)
            if (
                run.status.value != "RUNNING"
                or run.cancel_requested_at is not None
                or run.lease_epoch != context.lease_epoch
                or (run.lease_expires_at is not None and run.lease_expires_at <= self.clock.now())
            ):
                raise ConflictError("Draft preparation stopped")
            existing = [
                a
                for a in await uow.artifacts.list_for_run(context.run_id, context.principal)
                if a.origin == DRAFT_ATTACHMENT_ORIGIN
                and a.metadata.get(DRAFT_TARGET_KEY) == str(target.id)
            ]
            digest = hashlib.sha256(content).hexdigest()
            if not any(a.sha256 == digest for a in existing) and (
                len(existing) >= 10
                or sum(a.size_bytes for a in existing) + len(content) > DRAFT_ATTACHMENT_BYTES
            ):
                raise ConflictError("Reply attachments exceed limit")
            stored = await cast(ArtifactWriter, context.artifacts).create(
                _chunks(content),
                name,
                facts.media_type,
                TrustLevel.EXTERNAL_UNTRUSTED,
                metadata={
                    DRAFT_TARGET_KEY: str(target.id),
                    ATTACHMENT_METADATA_KEY: facts.metadata(),
                    EMAIL_SOURCE_METADATA_KEY: {
                        "account_id": source.account_id,
                        "server_id": server,
                        "thread_id": source.provider_thread_id,
                        "message_id": args["message_id"],
                        "attachment_index": args["attachment_index"],
                    },
                },
            )
        receipt = DraftAttachment(
            artifact_id=stored.artifact_id,
            account_id=source.account_id,
            source_thread_id=source.provider_thread_id,
            message_id=args["message_id"],
            attachment_index=args["attachment_index"],
            filename=name,
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        ).model_dump(mode="json")
        return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)

    async def _send(
        self,
        context: ToolExecutionContext,
        server: str,
        args: dict[str, Any],
        call: Callable[[dict[str, Any]], Awaitable[MCPCallResult]],
    ) -> MCPCallResult:
        async with self.uow_factory() as uow, uow.email.lock(context.principal):
            task_row = await uow.email.get(context.principal, "task", str(context.run_id))
            if task_row is None:
                raise ConflictError("Typed send required")
            task = EmailTask.model_validate(task_row.payload)
            draft_row = await uow.email.get(context.principal, "draft", str(task.draft_id))
            if task.kind != "send" or draft_row is None or task.session_id != context.session_id:
                raise ConflictError("Typed send required")
            draft = EmailDraft.model_validate(draft_row.payload)
            if (
                draft.status.value != "sending"
                or draft.send_claim_id != task.id
                or draft.revision != task.expected_revision
                or not draft.attachments
                or draft.send_tool_name != f"mcp.{server}.send_attached_message"
                or args != send_message_arguments(draft, draft.provider_thread_id or "")
            ):
                raise ConflictError("The reviewed reply changed")
            run = await uow.runs.get(context.run_id, context.principal)
            if (
                run.status.value != "RUNNING"
                or run.cancel_requested_at is not None
                or run.lease_epoch != context.lease_epoch
                or (run.lease_expires_at is not None and run.lease_expires_at <= self.clock.now())
            ):
                raise ConflictError("The send was cancelled or its lease changed")
            rows = await self.validate(uow, context.principal, draft)
            payloads = []
            for item, row in zip(draft.attachments, rows, strict=True):
                payloads.append(
                    {
                        **item.model_dump(mode="json"),
                        "data_base64": base64.b64encode(await self.read(row)).decode(),
                    }
                )
            # The email lock serializes source erasure with this exact dispatch.
            return await call({**args, "attachments": payloads})
