"""Explicit historical discovery through the existing governed Gmail read path."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from agent_core.domain.email import EmailAccount, EmailTask
from agent_core.domain.email_semantics import EmailSemanticSource
from agent_core.domain.errors import ConflictError, ToolTrustRejectedError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart, ToolResultItem
from agent_core.domain.people import PeopleImportJob, PeopleQuery, PersonIdentifier
from agent_core.domain.people_imports import PeopleMailboxMessage, PeopleMailboxProgress
from agent_core.domain.policies import TrustLevel
from agent_core.ports.email import EmailContextRenderer, EmailRuntimeServices
from agent_core.ports.people_runtime import PeopleEmailImportSemantics
from agent_core.ports.tools import ToolRegistry
from agent_core.runtime.email_tasks import _TaskIO
from agent_core.runtime.people_imports import ImportSlice, ImportStoppedError


class _DiscoveryIO(_TaskIO):
    worker: ImportSlice

    async def call(
        self,
        account_id: str,
        remote: str,
        arguments: dict[str, Any],
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        async with self.context.uow_factory() as uow:
            await self.worker.guard(uow)
        return await super().call(account_id, remote, arguments, operation=operation)

    async def _register_sources(self, account_id: str, value: dict[str, Any]) -> None:
        # Discovery validates and retains only selected sources below. Projection
        # waits until all discovered evidence can be replayed chronologically.
        return None


class PeopleMailboxImporter:
    def __init__(
        self,
        worker: ImportSlice,
        job: PeopleImportJob,
        *,
        registry: ToolRegistry,
        service: EmailRuntimeServices,
        semantics: PeopleEmailImportSemantics,
        render_context: EmailContextRenderer,
        selected: Callable[[EmailSemanticSource, PeopleImportJob], Awaitable[bool]],
    ) -> None:
        self.worker = worker
        self.context = worker.context
        self.job = job
        self.semantics = semantics
        self.selected = selected
        self.io = _DiscoveryIO(
            self.context,
            EmailTask(
                id=job.id,
                run_id=self.context.run.id,
                session_id=self.context.run.session_id,
                kind="refresh",
                account_ids=job.scope.account_ids,
                created_at=self.context.clock.now(),
            ),
            registry,
            service,
            semantics,
            render_context,
        )
        self.io.worker = worker

    async def save(self, **updates: object) -> None:
        c = self.context
        async with c.uow_factory() as uow, uow.people.lock(c.principal):
            current = await self.worker.guard(uow)
            progress = PeopleMailboxProgress.model_validate(
                {**current.mailbox.model_dump(), **updates}
            )
            self.job = await self.worker.save(uow, current, mailbox=progress)

    async def call(self, account: str, remote: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self.context.uow_factory() as uow:
            self.job = await self.worker.guard(uow)
        key = hashlib.sha256(
            json.dumps(
                [self.job.mailbox.model_dump(mode="json"), remote, arguments], sort_keys=True
            ).encode()
        ).hexdigest()
        await self.save(read_calls=self.job.mailbox.read_calls + 1)
        return await self.io.call(account, remote, arguments, operation=f"people-read:{key}")

    async def query(self) -> str:
        scope = self.job.scope
        query = (
            f"after:{math.floor(scope.since.timestamp()) - 1} "
            f"before:{math.ceil(scope.until.timestamp())}"
        )
        if not scope.person_ids:
            return query
        values: set[str] = set()
        async with self.context.uow_factory() as uow:
            for person_id in scope.person_ids:
                aliases = await uow.people.query(
                    PeopleQuery(
                        tenant_id=self.context.principal.tenant_id,
                        principal_id=self.context.principal.principal_id,
                        person_id=person_id,
                        kinds=["identifier"],
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                        limit=100,
                    )
                )
                if len(aliases) > 100:
                    raise ImportStoppedError("person filter exceeds its bounded identifier window")
                values.update(
                    alias.value
                    for alias in aliases
                    if isinstance(alias, PersonIdentifier)
                    and alias.identifier_kind == "email"
                    and alias.verification == "owner_confirmed"
                    and alias.valid_from < scope.until
                    and (alias.valid_to is None or alias.valid_to > scope.since)
                )
        if not values:
            raise ImportStoppedError("person filter has no confirmed email address")
        return (
            query
            + " {"
            + " ".join(
                f"{field}:{json.dumps(value)}"
                for value in sorted(values)
                for field in ("from", "to", "cc", "bcc")
            )
            + "}"
        )

    async def header(self, account: str, pending: PeopleMailboxMessage) -> dict[str, Any]:
        c = self.context
        async with c.uow_factory() as uow:
            events = await uow.events.list_after(
                pending.header_session_id, pending.header_sequence - 1, c.principal, limit=1
            )
        if not events or events[0].sequence != pending.header_sequence:
            raise ImportStoppedError("original import headers are unavailable")
        event = events[0]
        if (
            event.event_type != "tool.call.completed"
            or event.actor_type != "runtime"
            or event.payload.get("name")
            != f"mcp.{self.job.account_servers[account]['read']}.get_thread_page"
        ):
            raise ImportStoppedError("original import headers have invalid provenance")
        result = ToolResultItem.model_validate(event.payload.get("result_item"))
        if result.is_error or result.trust != TrustLevel.EXTERNAL_UNTRUSTED:
            raise ImportStoppedError("original import headers have invalid trust")
        value = json.loads(
            "\n".join(part.text for part in result.content if isinstance(part, TextPart))
        )
        matches = [
            message
            for message in value.get("messages", [])
            if message.get("id") == pending.message_id
        ]
        if len(matches) != 1:
            raise ImportStoppedError("original import headers are ambiguous")
        return dict(matches[0])

    async def retain(
        self,
        account: str,
        thread: str,
        header: dict[str, Any],
        body: dict[str, Any],
        *,
        pending: PeopleMailboxMessage | None = None,
    ) -> None:
        if not body.get("body"):
            return
        source = EmailSemanticSource(
            account_id=account,
            provider_thread_id=thread,
            message_id=header["id"],
            session_id=self.context.run.session_id,
            source_event_sequence=body["source_event_sequence"],
            tool_name=body["source_tool_name"],
            header_session_id=pending.header_session_id if pending else None,
            header_event_sequence=pending.header_sequence if pending else None,
            body_offset=pending.offset if pending else 0,
            sender=header["from"],
            body=body["body"],
            sent_at=datetime.fromtimestamp(int(header["internal_date"]) / 1000, tz=UTC),
        )
        if not await self.selected(source, self.job):
            return
        # Exclusions and erasures are checked before retaining a source and again
        # by the semantics transaction. A concurrent exclusion stops this slice.
        async with self.context.uow_factory() as uow:
            from agent_core.domain.people_sources import email_source_id

            key = hashlib.sha256(f"{account}:{thread}".encode()).hexdigest()
            if await uow.email.get(
                self.context.principal, "excluded_source", key
            ) is not None or await uow.people.source_suppressed(
                self.context.principal, email_source_id(self.context.principal, source)
            ):
                return
        await self.semantics.retain_source(source, run=self.context.run, lease=self.context.lease)

    async def discover(self) -> bool:
        c = self.context
        async with c.uow_factory() as uow:
            self.job = await self.worker.guard(uow)
        # Pending calls remain under the same job guard as fresh reads.
        await self.io.recover_reads()
        verified: set[str] = set()
        while c.run.tool_call_count < 8:
            progress = self.job.mailbox
            if progress.complete:
                return True
            if progress.read_calls >= self.job.scope.max_records * 20 + 20:
                await self.save(complete=True, partial=True)
                return True
            if progress.account_index == len(self.job.scope.account_ids):
                await self.save(complete=True)
                return True
            account = self.job.scope.account_ids[progress.account_index]
            if account not in verified:
                profile = await self.call(account, "get_profile", {})
                async with c.uow_factory() as uow:
                    record = await uow.email.get(c.principal, "account", account)
                if record is None:
                    raise ImportStoppedError("selected account is unavailable")
                expected = EmailAccount.model_validate(record.payload).email_address
                if (
                    not expected
                    or profile.get("email_address", "").casefold() != expected.casefold()
                ):
                    raise ImportStoppedError("mailbox identity changed")
                verified.add(account)
                continue
            if progress.pending is not None:
                pending: PeopleMailboxMessage | None = progress.pending
                assert pending is not None
                thread = progress.thread_ids[progress.thread_index]
                header = await self.header(account, pending)
                body = await self.call(
                    account,
                    "get_message_body",
                    {
                        "message_id": pending.message_id,
                        "offset": pending.offset,
                        "max_bytes": 65536,
                        "expected_history_id": header["history_id"],
                    },
                )
                if body.get("source_changed") or not body.get("body_available"):
                    raise ConflictError("mailbox message changed while importing")
                await self.retain(account, thread, header, body, pending=pending)
                offset = body.get("next_offset")
                if offset is not None and (not isinstance(offset, int) or offset <= pending.offset):
                    raise ToolTrustRejectedError("mailbox body cursor did not advance")
                partial = offset is not None and pending.passages >= 16
                next_pending = (
                    pending.model_copy(update={"offset": offset, "passages": pending.passages + 1})
                    if offset is not None and not partial
                    else None
                )
                await self.save(
                    pending=next_pending,
                    partial=progress.partial or partial,
                    thread_index=progress.thread_index
                    + int(next_pending is None and progress.thread_page is None),
                )
                continue
            if progress.records_read >= self.job.scope.max_records:
                await self.save(complete=True, partial=True)
                return True
            if not progress.thread_ids:
                arguments: dict[str, Any] = {"query": await self.query(), "max_results": 25}
                if progress.search_page:
                    arguments["page_token"] = progress.search_page
                page = await self.call(account, "search_threads", arguments)
                threads = page.get("threads")
                if not isinstance(threads, list) or len(threads) > 25:
                    raise ToolTrustRejectedError("mailbox search exceeded its page bound")
                if any(not isinstance(item, dict) for item in threads):
                    raise ToolTrustRejectedError("mailbox search returned invalid thread entries")
                ids = [item.get("thread_id") for item in threads]
                if any(
                    not isinstance(value, str) or not value or len(value) > 1024 for value in ids
                ) or len(set(ids)) != len(ids):
                    raise ToolTrustRejectedError(
                        "mailbox search returned invalid thread identities"
                    )
                cursor = page.get("next_page_token")
                if cursor is not None and cursor == progress.search_page:
                    raise ToolTrustRejectedError("mailbox search cursor did not advance")
                await self.save(
                    thread_ids=ids,
                    search_page=cursor,
                    thread_index=0,
                    account_index=progress.account_index + int(not ids and cursor is None),
                )
                continue
            if progress.thread_index == len(progress.thread_ids):
                await self.save(
                    thread_ids=[],
                    thread_index=0,
                    thread_page=None,
                    account_index=progress.account_index + int(progress.search_page is None),
                )
                continue
            thread = progress.thread_ids[progress.thread_index]
            arguments = {"thread_id": thread, "max_messages": 1}
            if progress.thread_page:
                arguments["page_token"] = progress.thread_page
            page = await self.call(account, "get_thread_page", arguments)
            messages = page.get("messages")
            if (
                page.get("thread_id") != thread
                or not isinstance(messages, list)
                or len(messages) > 1
            ):
                raise ToolTrustRejectedError("mailbox thread exceeded its source bound")
            cursor = page.get("next_page_token")
            if cursor is not None and cursor == progress.thread_page:
                raise ToolTrustRejectedError("mailbox thread cursor did not advance")
            pending = None
            partial = progress.partial
            for header in messages:
                sent_at = datetime.fromtimestamp(int(header["internal_date"]) / 1000, tz=UTC)
                if not self.job.scope.since <= sent_at < self.job.scope.until:
                    continue
                if not header.get("headers_complete") or not header.get("body_available", True):
                    partial = True
                    continue
                if header.get("body"):
                    await self.retain(account, thread, header, {**page, "body": header["body"]})
                offset = header.get("next_body_offset")
                if offset is not None:
                    pending = PeopleMailboxMessage(
                        message_id=header["id"],
                        header_session_id=c.run.session_id,
                        header_sequence=page["source_event_sequence"],
                        offset=offset,
                    )
                elif not header.get("body_complete"):
                    partial = True
            await self.save(
                thread_page=cursor,
                pending=pending,
                partial=partial,
                records_read=progress.records_read + len(messages),
                thread_index=progress.thread_index + int(cursor is None and pending is None),
            )
        return False
