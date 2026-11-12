"""Research a missing fact before drafting; files remain internal until Send."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_learning_m26 import observation, prepare
from tests.gates.test_email_m18 import _email_settings, _generated_gmail_discovery
from tests.gates.test_email_runtime_m26 import _mailbox_factory


def test_text_only_consent_keeps_legacy_gesture_digest() -> None:
    import hashlib
    from datetime import UTC, datetime

    from agent_core.domain.email import EmailSendConsent

    consent = EmailSendConsent(
        tenant_id="t",
        principal_id="p",
        account_id="work",
        provider_thread_id="thread",
        read_server_id="read",
        send_server_id="send",
        draft_id=uuid4(),
        draft_revision=1,
        source_revision=1,
        arguments_digest="a" * 64,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        undo_until=datetime(2026, 1, 1, tzinfo=UTC),
    )
    legacy = consent.model_dump_json(exclude={"with_attachments"})
    assert consent.digest == hashlib.sha256(legacy.encode()).hexdigest()
    assert consent.model_copy(update={"with_attachments": True}).digest != consent.digest


async def test_generated_proposal_replay_recognizes_own_commit_but_not_owner_edit() -> None:
    from agent_core.domain.email import EmailDraftEdit
    from agent_core.domain.errors import ConflictError

    async with build(settings=replace(_email_settings(), email_mode_enabled=True)) as app:
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        run_id = uuid4()
        arguments: dict[str, Any] = {
            "run_id": run_id,
            "instruction": "",
            "expected_draft_revision": 0,
        }
        first = await service.save_generated_draft(
            app.principal, thread.id, thread.revision, "Prepared reply", **arguments
        )
        replay = await service.save_generated_draft(
            app.principal, thread.id, thread.revision, "Prepared reply", **arguments
        )
        assert replay == first
        await service.edit_draft(
            app.principal,
            first.id,
            EmailDraftEdit(
                expected_revision=first.revision,
                to=first.to,
                subject=first.subject,
                body="Owner edit",
            ),
        )
        with pytest.raises(ConflictError, match="changed during preparation"):
            await service.save_generated_draft(
                app.principal, thread.id, thread.revision, "Prepared reply", **arguments
            )


async def test_draft_searches_other_mail_before_asking_for_known_context() -> None:
    """A named meeting's date is in another email, not the selected request."""
    source = observation(body="The Cedar board meeting is January 4. No files are needed.")
    source["thread_id"] = "meeting-source"
    source["messages"][0].update(id="meeting-message", thread_id="meeting-source")
    factory = await _mailbox_factory(
        [
            ("get_profile", {"email_address": "owner@example.com"}),
            ("search_threads", {"threads": [{"thread_id": "meeting-source"}]}),
            ("get_thread_page", source),
        ]
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                text=json.dumps(
                    {
                        "body": "",
                        "mail_queries": [
                            {
                                "account_id": "work",
                                "query": "Cedar board",
                                "reason": "Find the meeting date",
                            }
                        ],
                    }
                )
            ),
            ScriptedTurn(text='{"body":"The Cedar board meeting is January 4."}'),
        ]
    )
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=factory,
    ) as app:
        service = await prepare(app)
        vars(service)["owner_timezone"] = "America/New_York"
        thread = await service.import_thread(
            app.principal, "work", observation(body="When is the Cedar board meeting?"), uuid4()
        )
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(operation.run_id, app.principal)
        reads = [invocation.tool_name.rsplit(".", 1)[-1] for invocation in invocations]
        assert "search_threads" in reads, (
            "Drafting must research missing context through governed reads"
        )
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        detail = cast(dict[str, Any], await service.thread(app.principal, thread.id))
        assert detail["draft"]["body"] == "The Cedar board meeting is January 4."
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert "America/New_York" in provider.requests[0].model_dump_json()
        assert not {"send_message", "send_attached_message", "create_draft"}.intersection(reads)


async def test_attachment_capabilities_are_application_only() -> None:
    """Neither Chat nor a job can directly stage a file or inject outbound bytes."""
    read = await _generated_gmail_discovery("read")
    send = await _generated_gmail_discovery("send")
    tools = {tool.name: tool for tool in (*read.tools, *send.tools)}
    assert "stage_attachment" in tools, "The typed drafter needs private attachment staging"
    assert "send_attached_message" in tools, "Sending needs a protected byte transport"
    assert tools["stage_attachment"].application_only
    assert tools["send_attached_message"].application_only
    assert "data_base64" not in json.dumps(tools["send_attached_message"].input_schema)


@pytest.mark.parametrize(
    "after",
    [
        "none",
        "discard",
        "revoke_preview",
        "exclude_source",
        "send",
        "progress",
        "expire_send",
        "remove",
        "concurrent_edit",
        "early_edit",
        "refine",
        "manual_switch",
        "legacy_send",
        "expired_dispatch",
        "expired_during_read",
        "closed_session",
        "invalid_claim",
        "stale_attachment_edit",
    ],
)
async def test_drafter_stages_verified_file_without_sending_or_persisting_bytes(after: str) -> None:
    import base64
    import hashlib

    from tests.gates.test_email_attachment_save_adr0296 import CONTENT

    source = observation(body="The requested board deck is attached.")
    source["messages"][0]["attachments"] = [
        {"filename": "board.pdf", "mime_type": "application/pdf", "size": len(CONTENT)}
    ]
    factory = await _mailbox_factory(
        [
            ("get_profile", {"email_address": "owner@example.com"}),
            ("search_threads", {"threads": [{"thread_id": source["thread_id"]}]}),
            ("get_thread_page", source),
            (
                "stage_attachment",
                {
                    "thread_id": source["thread_id"],
                    "message_id": source["messages"][0]["id"],
                    "filename": "board.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(CONTENT),
                    "data_base64": base64.b64encode(CONTENT).decode(),
                },
            ),
            ("get_profile", {"email_address": "owner@example.com"}),
        ]
    )
    sent_payloads: list[dict[str, object]] = []
    observed_stages: list[object] = []
    base_factory = factory

    def client_factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:

        client = base_factory(config, credential, environment)
        original_call = client.call_tool

        async def call(name: str, arguments: dict[str, object]) -> MCPCallResult:
            if name == "stage_attachment" or (
                after == "early_edit"
                and name == "get_thread_page"
                and arguments.get("thread_id") == "request-thread"
            ):
                if (after == "concurrent_edit" and name == "stage_attachment") or (
                    after == "early_edit" and name == "get_thread_page"
                ):
                    from agent_core.domain.email import EmailDraftEdit

                    await service.edit_draft(
                        app.principal,
                        original.id,
                        EmailDraftEdit(
                            expected_revision=original.revision,
                            to=original.to,
                            subject=original.subject,
                            body="Owner edit during preparation",
                        ),
                    )
                visible = await service.thread(app.principal, thread.id)
                observed_stages.append(visible.get("preparation_stage"))
            if name == "send_attached_message":
                sent_payloads.append(arguments)
                return MCPCallResult(
                    structured={"message_id": "sent", "thread_id": "request-thread"}
                )
            if name == "get_thread_page" and arguments.get("thread_id") == "request-thread":
                return MCPCallResult(structured=request)
            return await original_call(name, arguments)

        vars(client)["call_tool"] = call
        return client

    script = FakeModelScript(turns=[])
    async with build(
        settings=replace(
            _email_settings(),
            email_mode_enabled=True,
            email_send_undo_seconds=0,
        ),
        script=script,
        mcp_client_factory=client_factory,
    ) as app:
        service = await prepare(app)
        document_thread = await service.import_thread(app.principal, "work", source, uuid4())
        request = observation(body="Please send the board materials.")
        request["thread_id"] = "request-thread"
        request["messages"][0].update(id="request-message", thread_id="request-thread")
        thread = await service.import_thread(app.principal, "work", request, uuid4())
        if after in {"concurrent_edit", "early_edit"}:
            original = await service.save_generated_draft(
                app.principal,
                thread.id,
                thread.revision,
                "Before regeneration",
                run_id=uuid4(),
                instruction="",
            )
        if after == "early_edit":
            from agent_core.runtime.email_state import save_value

            async with app.uow_factory() as uow:
                await save_value(
                    uow.email,
                    app.principal,
                    "thread",
                    str(thread.id),
                    thread.model_copy(update={"complete": False}),
                    app.clock.now(),
                )
        identifier = hashlib.sha256(
            f"work:{document_thread.id}:{document_thread.messages[0].id}:0".encode()
        ).hexdigest()[:20]
        script.turns.append(
            ScriptedTurn(
                text=json.dumps(
                    {
                        "mail_queries": [
                            {
                                "account_id": "work",
                                "query": "board",
                                "reason": "Find requested documents",
                            }
                        ]
                    }
                )
            )
        )
        script.turns.append(
            ScriptedTurn(
                text=json.dumps(
                    {
                        "body": "The board deck is attached.",
                        "attachment_ids": [identifier],
                        "requires_attachments": True,
                    }
                )
            )
        )
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        run = await app.runs.get(operation.run_id)
        if after in {"concurrent_edit", "early_edit"}:
            assert run.status is RunStatus.FAILED
            current = await service.draft(app.principal, original.id)
            assert current.body == "Owner edit during preparation"
            assert current.revision == original.revision + 1
            return
        assert run.status is RunStatus.COMPLETED, run.failure
        detail = cast(dict[str, Any], await service.thread(app.principal, thread.id))
        [file] = detail["draft"]["attachments"]
        assert file["sha256"] == hashlib.sha256(CONTENT).hexdigest()
        async with app.uow_factory() as uow:
            [artifact] = await uow.artifacts.list_for_run(run.id, app.principal)
            events = await uow.events.list_after(run.session_id, 0, app.principal)
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
        assert artifact.origin == "email_draft_attachment"
        assert artifact.expires_at is None
        assert not any("send" in call.tool_name for call in invocations)
        assert "data_base64" not in json.dumps([event.model_dump(mode="json") for event in events])
        assert service.draft_files is not None
        assert await service.draft_files.read(artifact) == CONTENT

        if after == "discard":
            from datetime import timedelta
            from uuid import UUID

            before_discard = app.clock.now()
            await service.discard_draft(
                app.principal, UUID(detail["draft"]["id"]), detail["draft"]["revision"]
            )
            async with app.uow_factory() as uow:
                settled = await uow.artifacts.get(artifact.id, app.principal)
            # Production clock advances between calls; bound the exact thirty-day TTL.
            assert settled.expires_at is not None
            assert before_discard + timedelta(days=30) <= settled.expires_at
            assert settled.expires_at <= app.clock.now() + timedelta(days=30)
        elif after == "revoke_preview":
            from agent_core.domain.errors import AuthorizationError

            source_server = service.account_servers["work"]["read"]
            limited = app.principal.model_copy(
                update={
                    "scopes": {
                        scope
                        for scope in app.principal.scopes
                        if scope != f"mcp.{source_server}.use"
                    }
                }
            )
            with pytest.raises(AuthorizationError):
                await app.services.artifacts.get(limited, artifact.id)
        elif after == "exclude_source":
            await service.exclude_source(
                app.principal, document_thread.id, document_thread.revision
            )
            detail = cast(dict[str, Any], await service.thread(app.principal, thread.id))
            assert detail["draft"]["attachments"] == []
            assert detail["draft"]["body"] == ""
            assert detail["draft"]["stale"] is True
        elif after == "progress":
            assert observed_stages == ["staging_files"]
        elif after == "expire_send":
            from uuid import UUID

            from agent_core.domain.errors import ConflictError

            async with app.uow_factory() as uow:
                await uow.artifacts.expire(artifact.id, app.principal, app.clock.now())
            with pytest.raises(ConflictError):
                await service.send(
                    app.principal,
                    UUID(detail["draft"]["id"]),
                    expected_revision=detail["draft"]["revision"],
                    source_revision=thread.revision,
                    idempotency_key="missing-file",
                )
            assert sent_payloads == []
        elif after in {"remove", "stale_attachment_edit"}:
            from uuid import UUID

            from agent_core.domain.email import EmailDraftEdit, send_message_arguments

            draft = await service.draft(app.principal, UUID(detail["draft"]["id"]))
            edited = await service.edit_draft(
                app.principal,
                draft.id,
                EmailDraftEdit(
                    expected_revision=draft.revision,
                    to=draft.to,
                    cc=draft.cc,
                    subject=draft.subject,
                    body="Reply without the file",
                    attachment_ids=[],
                ),
            )
            assert edited.revision == draft.revision + 1 and edited.attachments == []
            assert "attachments" not in send_message_arguments(edited, thread.provider_thread_id)
            if after == "stale_attachment_edit":
                from agent_core.domain.errors import ConflictError

                with pytest.raises(ConflictError, match="both edits were preserved"):
                    await service.edit_draft(
                        app.principal,
                        draft.id,
                        EmailDraftEdit(
                            expected_revision=draft.revision,
                            to=draft.to,
                            subject=draft.subject,
                            body="Stale device's useful edit",
                            attachment_ids=[artifact.id],
                        ),
                    )
                versions = await service.draft_revisions(app.principal, draft.id)
                assert any(
                    v["body"] == "Stale device's useful edit"
                    for v in cast(list[dict[str, Any]], versions["items"])
                )
                assert (await service.draft(app.principal, draft.id)).attachments == []
                return
            await service.discard_draft(app.principal, edited.id, edited.revision)
            async with app.uow_factory() as uow:
                prior_file = await uow.artifacts.get(artifact.id, app.principal)
            assert prior_file.expires_at is not None
        elif after == "refine":
            from uuid import UUID

            draft = await service.draft(app.principal, UUID(detail["draft"]["id"]))
            script.turns.append(
                ScriptedTurn(text='{"body":"Attached.","requires_attachments":true}')
            )
            refinement = await service.submit_task(
                app.principal, kind="draft", thread_id=thread.id, instruction="Shorten this reply"
            )
            refined_run = await app.runs.get(refinement.run_id)
            assert refined_run.status is RunStatus.COMPLETED, refined_run.failure
            refined = await service.draft(app.principal, draft.id)
            provider = app.executor._model_provider
            assert isinstance(provider, FakeModelProvider)
            assert "The board deck is attached." in provider.requests[-1].model_dump_json()
            assert refined.body == "Attached."
            assert refined.attachments == draft.attachments
            assert refined.revision == draft.revision + 1
            async with app.uow_factory() as uow:
                await service.validate_draft_attachments(uow, app.principal, refined)
            assert sent_payloads == []
        elif after == "manual_switch":
            from uuid import UUID

            draft = await service.draft(app.principal, UUID(detail["draft"]["id"]))
            manual = await service.select_reply(
                app.principal,
                thread.id,
                kind="manual",
                source_revision=thread.revision,
                expected_revision=draft.revision,
            )
            assert manual.body == "" and manual.attachments == []
            restored = await service.select_reply(
                app.principal,
                thread.id,
                kind="veetbot",
                source_revision=thread.revision,
                expected_revision=manual.revision,
            )
            assert restored.attachments == draft.attachments
            assert restored.body == draft.body
            async with app.uow_factory() as uow:
                await service.validate_draft_attachments(uow, app.principal, restored)
        elif after == "legacy_send":
            import httpx

            from agent_core.api import create_app

            api = create_app(
                app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
            ) as client:
                response = await client.post(
                    f"/v1/email/drafts/{detail['draft']['id']}/send",
                    json={
                        "expected_revision": detail["draft"]["revision"],
                        "source_revision": thread.revision,
                        "idempotency_key": "legacy",
                    },
                )
            assert response.status_code == 409
            assert sent_payloads == []
        elif after in {
            "send",
            "expired_dispatch",
            "expired_during_read",
            "closed_session",
            "invalid_claim",
        }:
            from uuid import UUID

            import httpx

            from agent_core.api import create_app
            from tests.gates.test_email_runtime_m26 import _drain

            if after == "closed_session":
                async with app.uow_factory() as uow:
                    await uow.sessions.close(artifact.session_id, app.principal, app.clock.now())
            if after == "invalid_claim":
                from agent_core.domain.errors import ConflictError

                validate_files = service.validate_draft_attachments

                async def unavailable_on_claim(uow: Any, principal: Any, draft: Any) -> None:
                    if draft.status.value == "sending":
                        raise ConflictError("File is no longer readable")
                    await validate_files(uow, principal, draft)

                vars(service)["validate_draft_attachments"] = unavailable_on_claim
            if after in {"expired_dispatch", "expired_during_read"}:
                from collections.abc import AsyncIterator
                from contextlib import asynccontextmanager
                from datetime import timedelta

                from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

                original_factory = service.draft_files.uow_factory
                authority_reads = 0

                @asynccontextmanager
                async def expired_factory() -> AsyncIterator[RepositoryUnitOfWork]:
                    async with original_factory() as uow:
                        original_get = uow.runs.get

                        async def expired_get(*args: Any, **kwargs: Any) -> Any:
                            nonlocal authority_reads
                            authority_reads += 1
                            value = await original_get(*args, **kwargs)
                            if after == "expired_during_read" and authority_reads == 1:
                                return value
                            return value.model_copy(
                                update={"lease_expires_at": app.clock.now() - timedelta(seconds=1)}
                            )

                        vars(uow.runs)["get"] = expired_get
                        try:
                            yield uow
                        finally:
                            vars(uow.runs)["get"] = original_get

                service.draft_files.uow_factory = cast(UnitOfWorkFactory, expired_factory)

            api = create_app(
                app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
            ) as client:
                body = {
                    "expected_revision": detail["draft"]["revision"],
                    "source_revision": thread.revision,
                    "idempotency_key": "reviewed-files",
                    "reviewed_attachment_ids": [file["artifact_id"]],
                }
                response = await client.post(
                    f"/v1/email/drafts/{detail['draft']['id']}/send", json=body
                )
                assert response.status_code == 200, response.text
                replay = await client.post(
                    f"/v1/email/drafts/{detail['draft']['id']}/send", json=body
                )
                assert (
                    replay.status_code == 200
                    and replay.json()["run_id"] == response.json()["run_id"]
                )
            await _drain(app)
            run = await app.runs.get(UUID(response.json()["run_id"]))
            if after in {"expired_dispatch", "expired_during_read"}:
                assert sent_payloads == [], "An expired worker must not dispatch reviewed bytes"
                return
            if after == "invalid_claim":
                assert sent_payloads == []
                kept = await service.draft(app.principal, UUID(detail["draft"]["id"]))
                assert kept.status.value in {"ready", "failed"}
                return
            assert run.status is RunStatus.COMPLETED, run.failure
            current_detail = cast(dict[str, Any], await service.thread(app.principal, thread.id))
            assert current_detail["draft"]["status"] == "sent"
            [payload] = sent_payloads
            [outbound] = cast(list[dict[str, Any]], payload["attachments"])
            assert base64.b64decode(outbound["data_base64"]) == CONTENT
            async with app.uow_factory() as uow:
                invocations = await uow.invocations.list_for_run(run.id, app.principal)
                [send] = [
                    call for call in invocations if call.tool_name.endswith("send_attached_message")
                ]
                approval = await uow.approvals.get_by_action(send.id)
                events = await uow.events.list_after(run.session_id, 0, app.principal)
            assert approval is not None and approval.status.value == "approved"
            assert "data_base64" not in json.dumps(
                [event.model_dump(mode="json") for event in events]
            )
            assert send.normalized_arguments is not None
            assert send.normalized_arguments["attachments"][0]["sha256"] == file["sha256"]


def test_multipart_reply_keeps_headers_and_exact_file_bytes() -> None:
    import base64
    import hashlib
    from email import policy
    from email.parser import BytesParser

    from gmail_mcp.client import GmailClient
    from gmail_mcp.errors import GmailError

    content = b"board evidence\x00\xff"
    item = {
        "filename": "Board_十一月.pdf",
        "media_type": "application/pdf",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "data_base64": base64.b64encode(content).decode(),
    }
    arguments: dict[str, Any] = dict(  # noqa: C408
        to="recipient@example.com",
        cc=None,
        bcc=None,
        subject="Re: Board",
        body="Attached.",
        in_reply_to="<request@example.com>",
        references="<earlier@example.com> <request@example.com>",
        attachments=[item],
    )
    raw = GmailClient._raw_message(**arguments)
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    [file] = message.iter_attachments()
    assert file.get_payload(decode=True) == content
    assert file.get_filename() == item["filename"]
    assert message["In-Reply-To"] == "<request@example.com>"
    assert str(message["References"]) == "<earlier@example.com> <request@example.com>"
    item["sha256"] = "0" * 64
    with pytest.raises(GmailError, match=r"gmail\.arguments_invalid"):
        GmailClient._raw_message(**arguments)


async def test_each_automatic_reply_owns_its_copy_of_the_same_source_file(tmp_path: Path) -> None:
    from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
    from agent_core.application.artifact_writer import ArtifactWriterFactory
    from agent_core.domain.artifacts import EMAIL_SOURCE_METADATA_KEY, ArtifactOrigin
    from agent_core.domain.policies import TrustLevel
    from tests.gates.test_artifact_m6 import _chunks

    async with build(settings=replace(_email_settings(), email_mode_enabled=True)) as app:
        writers = ArtifactWriterFactory(
            uow_factory=app.uow_factory,
            store=FilesystemArtifactStore(tmp_path / "files"),
            clock=app.clock,
            ids=app.ids,
        )
        writer = writers.for_run(
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            session_id=uuid4(),
            run_id=uuid4(),
            origin=ArtifactOrigin.EMAIL_DRAFT_ATTACHMENT,
        )
        metadata = {
            EMAIL_SOURCE_METADATA_KEY: {"message_id": "source-message"},
            "email_draft_thread": str(uuid4()),
        }
        first = await writer.create(
            _chunks(b"document"),
            "board.txt",
            "text/plain",
            TrustLevel.EXTERNAL_UNTRUSTED,
            metadata=metadata,
        )
        retry = await writer.create(
            _chunks(b"document"),
            "board.txt",
            "text/plain",
            TrustLevel.EXTERNAL_UNTRUSTED,
            metadata=metadata,
        )
        second = await writer.create(
            _chunks(b"document"),
            "board.txt",
            "text/plain",
            TrustLevel.EXTERNAL_UNTRUSTED,
            metadata={**metadata, "email_draft_thread": str(uuid4())},
        )
        assert retry.artifact_id == first.artifact_id
        assert second.artifact_id != first.artifact_id


@pytest.mark.parametrize(
    "plan",
    [
        {"body": "Attached.", "attachment_ids": ["invented-file"], "requires_attachments": True},
        {"body": "Attached.", "requires_attachments": True},
        {"preparation_issue": "Two versions conflict."},
    ],
)
async def test_missing_or_ambiguous_materials_preserve_existing_owner_draft(
    plan: dict[str, Any],
) -> None:
    from agent_core.domain.email import EmailDraftEdit

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[ScriptedTurn(text=json.dumps(plan))]),
        mcp_client_factory=await _mailbox_factory([]),
    ) as app:
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        original = await service.save_generated_draft(
            app.principal,
            thread.id,
            thread.revision,
            "Initial reply",
            run_id=uuid4(),
            instruction="",
        )
        edited = await service.edit_draft(
            app.principal,
            original.id,
            EmailDraftEdit(
                expected_revision=original.revision,
                to=original.to,
                subject=original.subject,
                body="My carefully edited reply",
            ),
        )
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        assert (await app.runs.get(operation.run_id)).status is RunStatus.FAILED
        current = await service.draft(app.principal, edited.id)
        assert current.body == edited.body and current.revision == edited.revision
        assert (await service.thread(app.principal, thread.id))["preparation_issue"]
        async with app.uow_factory() as uow:
            assert await uow.invocations.list_for_run(operation.run_id, app.principal) == []


async def test_research_stops_at_three_mail_queries_without_saving_partial_reply() -> None:
    queries = [
        {"account_id": "work", "query": f"board version {n}", "reason": "Find the current deck"}
        for n in range(4)
    ]
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text=json.dumps({"mail_queries": queries[:3]})),
            ScriptedTurn(text=json.dumps({"mail_queries": queries[3:]})),
        ]
    )
    responses: list[tuple[str, dict[str, Any]]] = [
        ("get_profile", {"email_address": "owner@example.com"})
    ] + [("search_threads", {"threads": []}) for _ in range(3)]
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=await _mailbox_factory(responses),
    ) as app:
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        assert (await app.runs.get(operation.run_id)).status is RunStatus.FAILED
        assert (await service.thread(app.principal, thread.id))["draft"] is None
        async with app.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(operation.run_id, app.principal)
        assert sum(call.tool_name.endswith(".search_threads") for call in calls) == 3


@pytest.mark.parametrize("calendar_unavailable", [False, True])
@pytest.mark.parametrize("shared_calendar", [False, True])
async def test_draft_reads_selected_calendar_and_exposes_timezone_to_synthesis(
    calendar_unavailable: bool,
    shared_calendar: bool,
) -> None:
    from pydantic import SecretStr

    from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
    from agent_core.domain.mcp import (
        MCPDiscovery,
        MCPRemoteTool,
        ScriptedMCPResponse,
        ScriptedMCPServer,
    )
    from calendar_mcp.client import CalendarClient
    from calendar_mcp.server import create_server
    from tests.contract.test_calendar_mcp_contract import credential

    calendar_client = CalendarClient(json.dumps(credential()))
    try:
        definitions = await create_server(calendar_client).list_tools()
    finally:
        await calendar_client.close()
    calendar_factory = ScriptedMCPClientFactory(
        {
            "calendar_read": ScriptedMCPServer(
                name="calendar_read",
                discovery=MCPDiscovery(
                    tools=tuple(
                        MCPRemoteTool(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.input_schema,
                        )
                        for t in definitions
                    )
                ),
                responses=(
                    ScriptedMCPResponse(
                        name="list_calendars",
                        result=MCPCallResult(
                            structured={
                                "calendars": [
                                    {
                                        "id": "shared-calendar"
                                        if shared_calendar
                                        else "owner@example.com",
                                        "timeZone": "America/Los_Angeles",
                                        "primary": not shared_calendar,
                                    }
                                ]
                            }
                        ),
                    ),
                    ScriptedMCPResponse(
                        name="list_events",
                        result=MCPCallResult(is_error=True, content=("calendar.unavailable",))
                        if calendar_unavailable
                        else MCPCallResult(
                            structured={
                                "events": [
                                    {
                                        "summary": "Cedar board",
                                        "start": {"dateTime": "2026-11-11T09:00:00-08:00"},
                                    }
                                ],
                                "next_page_token": None,
                            }
                        ),
                    ),
                ),
            )
        }
    )
    mail_factory = await _mailbox_factory([])

    def client_factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        return (calendar_factory if config.server_id == "calendar_read" else mail_factory)(
            config, credential, environment
        )

    settings = _email_settings()
    settings = replace(
        settings,
        email_mode_enabled=True,
        calendar_enabled=True,
        credentials={**settings.credentials, "calendar_read": SecretStr(json.dumps(credential()))},
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                text=json.dumps(
                    {
                        "calendar_queries": [
                            {
                                "query": "Cedar",
                                "time_min": "2026-11-11T00:00:00-08:00",
                                "time_max": "2026-11-12T00:00:00-08:00",
                            }
                        ]
                    }
                )
            ),
            ScriptedTurn(
                text=json.dumps(
                    {"body": "The Cedar board meeting is November 11 at 9 a.m. Pacific."}
                )
            ),
        ]
    )
    async with build(settings=settings, script=script, mcp_client_factory=client_factory) as app:
        service = await prepare(app)
        thread = await service.import_thread(
            app.principal,
            "work",
            observation(body="Cedar board meeting: November 11, 9 a.m. Pacific. Can you confirm?"),
            uuid4(),
        )
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        contexts = [request.model_dump_json() for request in provider.requests]
        assert "America/Los_Angeles" in contexts[0]
        assert "America/Los_Angeles" in contexts[-1]
        if calendar_unavailable:
            assert '"available":false' in contexts[-1].replace('\\"', '"').replace(" ", "")
        else:
            assert "2026-11-11T09:00:00-08:00" in contexts[-1]
        async with app.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(run.id, app.principal)
        assert [call.tool_name for call in calls] == [
            "mcp.calendar_read.list_calendars",
            "mcp.calendar_read.list_events",
        ]


async def test_private_mcp_send_accepts_verified_transport_bytes_and_encodes_multiple_files() -> (
    None
):
    """The real MCP handler accepts private bytes that its public schema never advertises."""
    import base64
    import hashlib
    from email import policy
    from email.parser import BytesParser

    from tests.gates.test_email_m26_gmail import Mailbox, invoke

    mailbox = Mailbox()
    files = [("board.pdf", b"%PDF-1.4\nsynthetic"), ("résolution.bin", bytes(range(256)))]
    manifest = [
        {
            "artifact_id": str(uuid4()),
            "account_id": "work",
            "source_thread_id": "thread-1",
            "message_id": "message-1",
            "attachment_index": index,
            "filename": name,
            "media_type": "application/octet-stream",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "data_base64": base64.b64encode(content).decode(),
        }
        for index, (name, content) in enumerate(files)
    ]
    result = await invoke(
        mailbox,
        "send_attached_message",
        {
            "to": "recipient@example.test",
            "subject": "Board materials",
            "body": "Attached.",
            "attachments": manifest,
            "in_reply_to": "<request@example.test>",
        },
        mode="send",
    )
    assert result.is_error is False
    raw = json.loads(mailbox.requests[-1].content)["raw"]
    parsed = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    assert parsed["In-Reply-To"] == "<request@example.test>"
    assert [
        (p.get_filename(), p.get_payload(decode=True)) for p in parsed.iter_attachments()
    ] == files
