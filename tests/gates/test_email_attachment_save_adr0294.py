"""An email attachment reaches the email's chat without entering history (ADR-0294)."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from io import BytesIO
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from pypdf import PdfWriter

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.api import create_app
from agent_core.application.email_attachments import EmailAttachmentSaver
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.mcp.configuration import is_email_attachment_tool
from agent_core.mcp.job_reads import JobMCPReads
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory, _page


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    target = BytesIO()
    writer.write(target)
    return target.getvalue()


CONTENT = _pdf()
ENCODED = base64.b64encode(CONTENT).decode("ascii")
ATTACHMENT = {"filename": "invoice.pdf", "mime_type": "application/pdf", "size": len(CONTENT)}
RECEIPT = {"filename": "receipt.pdf", "mime_type": "application/pdf", "size": 10}
SELECTION: dict[str, Any] = {"message_id": "m1", "attachment_index": 0, "filename": "invoice.pdf"}


def _attached_page(**overrides: Any) -> dict[str, Any]:
    page = _page(**overrides)
    page["messages"][0]["attachments"] = [ATTACHMENT, RECEIPT]
    return page


async def _factory(*, content: bytes = CONTENT, calls: list[str] | None = None) -> Any:
    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name in {"fetch_attachment", "save_attachment"}:
                if calls is not None:
                    calls.append(name)
                assert arguments == SELECTION
                return MCPCallResult(
                    structured={
                        "thread_id": "thread-1",
                        "message_id": "m1",
                        "filename": "invoice.pdf",
                        "mime_type": "application/octet-stream",
                        "size_bytes": len(content),
                        "data_base64": base64.b64encode(content).decode("ascii"),
                    }
                )
            if name == "get_thread_page":
                page = _attached_page()
                return MCPCallResult(content=(json.dumps(page),), structured=page)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def _seed_thread(app: Composition) -> Any:
    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailAccount

    principal = app.principal
    async with app.uow_factory() as uow, uow.email.lock(principal):
        await save_value(
            uow.email,
            principal,
            "account",
            "default",
            EmailAccount(
                id="default",
                label="Personal",
                email_address="owner@example.test",
                verified_addresses=["owner@example.test"],
            ),
            app.clock.now(),
        )
    session = await app.sessions.create()
    return await app.services.email.import_thread(principal, "default", _attached_page(), session)


def _settings() -> Any:
    return replace(_email_settings(), email_mode_enabled=True)


def _client(app: Composition) -> httpx.AsyncClient:
    api = create_app(
        app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://agent.test")


def _granted(principal: Principal, *scopes: str) -> Principal:
    return principal.model_copy(update={"scopes": {*principal.scopes, *scopes}})


async def _press(app: Composition, thread_id: UUID, *scopes: str) -> Any:
    principal = _granted(app.principal, "artifact.write", *scopes)
    return await app.services.email.save_attachment(
        principal,
        thread_id,
        **SELECTION,
        idempotency_key="press-1",
    )


def _no_bytes(value: object) -> None:
    rendered = json.dumps(value, default=str)
    assert ENCODED not in rendered
    assert "data_base64" not in rendered


async def test_a_press_files_the_attachment_in_the_emails_chat_without_a_model() -> None:
    calls: list[str] = []
    async with build(settings=_settings(), mcp_client_factory=await _factory(calls=calls)) as app:
        thread = await _seed_thread(app)
        operation = await _press(app, thread.id)
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.model_call_count == 0
        assert calls == ["fetch_attachment"]
        async with app.uow_factory() as uow:
            [artifact] = await uow.artifacts.list_for_run(run.id, app.principal)
            events = await uow.events.list_after(run.session_id, 0, app.principal)
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
            session = await uow.sessions.get(run.session_id, app.principal)
        latest = await app.services.email.thread(app.principal, thread.id)
        content = await app.services.artifacts.open_content(app.principal, artifact.id)
        body = b"".join([chunk async for chunk in await content.open()])

    assert str(run.session_id) == latest["session_id"] == str(session.id)
    assert artifact.origin == ArtifactOrigin.EMAIL_ATTACHMENT.value
    assert artifact.trust == TrustLevel.EXTERNAL_UNTRUSTED.value
    assert artifact.expires_at is None
    assert artifact.name == "invoice.pdf"
    # The sender's declared type is not believed over the file's own bytes.
    assert artifact.media_type == "application/pdf"
    assert body == CONTENT

    assert "email.discussion.opened" in {event.event_type for event in events}
    [reply] = [e for e in events if e.event_type == "assistant.message.completed"]
    [text, file_part] = reply.payload["message"]["content"]
    assert "invoice" not in text["text"]
    assert file_part["kind"] == "file"
    assert file_part["artifact_id"] == str(artifact.id)
    assert file_part["filename"] == "invoice.pdf"

    for event in events:
        _no_bytes(event.payload)
    for invocation in invocations:
        _no_bytes(invocation.structured_result)


@pytest.mark.parametrize(
    ("content", "granted", "expected"),
    [
        (CONTENT, ("knowledge.write",), "pending"),
        (CONTENT, (), None),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 32, ("knowledge.write",), None),
    ],
    ids=["document", "no-knowledge-scope", "not-a-document"],
)
async def test_a_saved_document_is_offered_to_knowledge(
    content: bytes, granted: tuple[str, ...], expected: str | None
) -> None:
    factory = await _factory(content=content)
    async with build(settings=_settings(), mcp_client_factory=factory) as app:
        scopes = {*app.principal.scopes, *granted, "artifact.write"}
        if not granted:
            scopes.discard("knowledge.write")
        principal = app.principal.model_copy(update={"scopes": scopes})
        thread = await _seed_thread(app)
        operation = await app.services.email.save_attachment(
            principal, thread.id, **SELECTION, idempotency_key="press-1"
        )
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        async with app.uow_factory() as uow:
            [artifact] = await uow.artifacts.list_for_run(run.id, app.principal)
            pending = await uow.artifacts.pending_auto_ingest(
                app.principal, now=app.clock.now(), limit=10
            )
    assert artifact.metadata.get("auto_ingest") == expected
    assert [item.id for item in pending] == ([artifact.id] if expected else [])


async def test_the_route_requires_artifact_write_and_replays_its_key() -> None:
    async with build(settings=_settings(), mcp_client_factory=await _factory()) as app:
        thread = await _seed_thread(app)
        async with _client(app) as client:
            body = {**SELECTION, "idempotency_key": "press-1"}
            path = f"/v1/email/threads/{thread.id}/attachments/save"
            first = await client.post(path, json=body)
            assert first.status_code == 200, first.text
            assert first.headers["cache-control"] == "private, no-store"
            again = await client.post(path, json=body)
            assert again.status_code == 200, again.text
            assert again.json()["run_id"] == first.json()["run_id"]
            assert again.json()["replayed"] is True

            unknown = await client.post(
                path, json={**body, "filename": "other.pdf", "idempotency_key": "press-2"}
            )
            assert unknown.status_code == 404
            extra = await client.post(path, json={**body, "surprise": True})
            assert extra.status_code == 400

        without = app.principal.model_copy(
            update={"scopes": set(app.principal.scopes) - {"artifact.write"}}
        )
        from agent_core.domain.errors import AuthorizationError

        with pytest.raises(AuthorizationError):
            await app.services.email.save_attachment(
                without, thread.id, **SELECTION, idempotency_key="press-3"
            )


@pytest.mark.parametrize(
    "selection",
    [
        {"message_id": "m404", "attachment_index": 0, "filename": "invoice.pdf"},
        {"message_id": "m1", "attachment_index": 1, "filename": "invoice.pdf"},
        {"message_id": "m1", "attachment_index": 0, "filename": "other.pdf"},
    ],
    ids=["message", "position", "name"],
)
async def test_a_selection_the_thread_does_not_list_starts_no_run(
    selection: dict[str, Any],
) -> None:
    calls: list[str] = []
    async with build(settings=_settings(), mcp_client_factory=await _factory(calls=calls)) as app:
        thread = await _seed_thread(app)
        principal = _granted(app.principal, "artifact.write")
        with pytest.raises(NotFoundError):
            await app.services.email.save_attachment(
                principal, thread.id, **selection, idempotency_key="press-1"
            )
    assert calls == []


async def test_a_provider_refusal_fails_the_run_and_stores_nothing() -> None:
    base = await _factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "fetch_attachment":
                return MCPCallResult(
                    content=("gmail.attachment_too_large",),
                    structured={"effect_status": "not_applied"},
                    is_error=True,
                )
            return cast(MCPCallResult, await original(name, arguments))

        vars(client)["call_tool"] = call_tool
        return cast(ScriptedMCPClient, client)

    async with build(settings=_settings(), mcp_client_factory=factory) as app:
        thread = await _seed_thread(app)
        operation = await _press(app, thread.id)
        run = await app.runs.get(operation.run_id)
        async with app.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(run.id, app.principal)
    assert run.status is RunStatus.FAILED
    assert artifacts == []


async def test_a_busy_chat_is_a_conflict_not_a_second_run() -> None:
    async with build(settings=_settings(), mcp_client_factory=await _factory()) as app:
        thread = await _seed_thread(app)
        real = app.services.email.dispatch

        async def parked(_run_id: UUID) -> None:
            return None

        vars(app.services.email)["dispatch"] = parked
        await _press(app, thread.id)
        vars(app.services.email)["dispatch"] = real
        principal = _granted(app.principal, "artifact.write")
        same = await app.services.email.save_attachment(
            principal, thread.id, **SELECTION, idempotency_key="press-2"
        )
        assert same.replayed is True
        with pytest.raises(ConflictError):
            await app.services.email.save_attachment(
                principal,
                thread.id,
                message_id="m1",
                attachment_index=1,
                filename="receipt.pdf",
                idempotency_key="press-3",
            )


async def test_chat_saves_only_after_the_owner_approves_and_the_reply_carries_the_file() -> None:
    calls: list[str] = []
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="tool.call",
                        arguments={
                            "name": "mcp.gmail_read.save_attachment",
                            "arguments": SELECTION,
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I saved it.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=_settings(), script=script, mcp_client_factory=await _factory(calls=calls)
    ) as app:
        run_id = await app.runs.submit("Save the invoice from that email.")
        assert calls == []
        [approval] = await app.approvals.list_pending(run_id=run_id)
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await app.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        events = await app.runs.events(run_id)
        async with app.uow_factory() as uow:
            [artifact] = await uow.artifacts.list_for_run(run_id, app.principal)
    assert calls == ["save_attachment"]
    assert artifact.origin == ArtifactOrigin.EMAIL_ATTACHMENT.value
    assert artifact.session_id == run.session_id
    assert artifact.expires_at is None
    reply = next(e for e in events if e.event_type == "assistant.message.completed")
    assert [p["artifact_id"] for p in reply.payload["message"]["content"] if p["kind"] == "file"]
    for event in events:
        _no_bytes(event.payload)


def test_only_the_gmail_read_servers_two_tools_are_recognized() -> None:
    assert is_email_attachment_tool("gmail_read", "save_attachment")
    assert is_email_attachment_tool("gmail_work_read", "fetch_attachment")
    assert not is_email_attachment_tool("gmail_read", "get_thread")
    assert not is_email_attachment_tool("gmail_write", "save_attachment")
    assert not is_email_attachment_tool("someone_else", "save_attachment")
    assert not is_email_attachment_tool(None, "save_attachment")


async def test_the_knowledge_sweep_admits_a_saved_document_as_it_does_an_owner_file() -> None:
    from agent_core.domain.artifacts import AUTO_INGEST_INGESTED
    from agent_core.knowledge.uploads import UploadAutoIngest

    async with build(settings=_settings(), mcp_client_factory=await _factory()) as app:
        thread = await _seed_thread(app)
        operation = await _press(app, thread.id, "knowledge.write")
        async with app.uow_factory() as uow:
            [artifact] = await uow.artifacts.list_for_run(operation.run_id, app.principal)
        ingested: list[Any] = []

        class Knowledge:
            async def ingest(self, request: Any, *, origin_trust: TrustLevel) -> None:
                ingested.append((request.source.id, origin_trust))

        sweep = UploadAutoIngest(
            uow_factory=app.uow_factory,
            knowledge=cast(Any, Knowledge()),
            principal=app.principal,
            clock=app.clock,
        )
        assert await sweep.sweep_once() == 1
        async with app.uow_factory() as uow:
            recorded = await uow.artifacts.get(artifact.id, app.principal)
    assert ingested == [(artifact.id, TrustLevel.USER)]
    assert recorded.metadata["auto_ingest"] == AUTO_INGEST_INGESTED


async def test_source_erasure_expires_the_file_and_its_receipt_but_cache_expiry_does_not() -> None:
    async with build(settings=_settings(), mcp_client_factory=await _factory()) as app:
        thread = await _seed_thread(app)
        operation = await _press(app, thread.id)
        await app.services.email.expire_cache(app.principal)
        async with app.uow_factory() as uow:
            [artifact] = await uow.artifacts.list_for_run(operation.run_id, app.principal)
            assert artifact.expires_at is None
            counts = await uow.session_deletions.erase_email_source(
                app.principal, "default", "thread-1", frozenset({"m1"}), app.clock.now()
            )
        assert counts["pending_artifacts"] >= 1
        async with app.uow_factory() as uow:
            expired = await uow.artifacts.list_expired(app.clock.now(), limit=100)
            [invocation] = [
                item
                for item in await uow.invocations.list_for_run(operation.run_id, app.principal)
                if item.tool_name.endswith("fetch_attachment")
            ]
            events = await uow.events.list_after(artifact.session_id, 0, app.principal)
    assert artifact.id in {item.id for item in expired}
    assert "invoice.pdf" not in json.dumps(invocation.structured_result)
    completed = [e for e in events if e.event_type == "tool.call.completed"]
    assert completed
    assert not any("invoice.pdf" in json.dumps(e.payload, default=str) for e in completed)


async def test_a_job_cannot_read_a_file_through_its_mcp_grant() -> None:
    calls: list[str] = []
    script = FakeModelScript(turns=[ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)])
    factory = await _factory(calls=calls)
    async with build(settings=_settings(), script=script, mcp_client_factory=factory) as app:
        run_id = await app.runs.submit("Ready?")
        await app.runs.wait_terminal(run_id)
        pipeline = app.tool_pipeline
        reader = JobMCPReads(
            runtime=cast(Any, app.mcp),
            registry=pipeline._registry,
            policy=pipeline._policy,
            uow_factory=app.uow_factory,
            clock=app.clock,
            ids=app.ids,
        )
        with pytest.raises(ValueError, match=r"job\.mcp_refused"):
            await reader.read(
                principal=app.principal,
                run_id=run_id,
                tool="mcp.gmail_read.save_attachment",
                arguments=SELECTION,
                maximum_response_bytes=1024 * 1024,
                call_id="job-call",
            )
    assert calls == []


async def test_without_a_saver_a_press_is_refused_and_no_bytes_are_returned() -> None:
    calls: list[str] = []
    async with build(settings=_settings(), mcp_client_factory=await _factory(calls=calls)) as app:
        thread = await _seed_thread(app)
        vars(app.mcp)["_email_attachment_saver"] = None
        operation = await _press(app, thread.id)
        run = await app.runs.get(operation.run_id)
        async with app.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(run.id, app.principal)
    assert run.status is RunStatus.FAILED
    assert artifacts == []
    assert calls == []


async def test_a_refused_fetch_reaches_the_caller_without_inspecting_anything() -> None:
    async def call(_values: dict[str, Any]) -> MCPCallResult:
        return MCPCallResult(content=("gmail.attachment_too_large",), is_error=True)

    class Inspector:
        async def inspect(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("a refused file is never inspected")

    result = await EmailAttachmentSaver(cast(Any, Inspector()))(cast(Any, None), SELECTION, call)
    assert result.is_error
    assert result.content == ("gmail.attachment_too_large",)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ('in"voice/2026\\final.pdf', "in_voice_2026_final.pdf"),
        ("  plain.txt ", "plain.txt"),
        ("‮", "_"),
        ("\n", "_"),
    ],
)
def test_a_senders_file_name_is_repaired_to_a_safe_one(given: str, stored: str) -> None:
    from agent_core.domain.attachments import stored_email_attachment_name, upload_name

    assert stored_email_attachment_name(given) == stored
    assert upload_name(stored) == stored


def test_a_long_name_keeps_its_extension() -> None:
    from agent_core.domain.attachments import UPLOAD_NAME_MAX_BYTES, stored_email_attachment_name

    name = stored_email_attachment_name("é" * 400 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode()) <= UPLOAD_NAME_MAX_BYTES


def test_a_failed_save_can_be_retried_into_knowledge() -> None:
    from agent_core.domain.artifacts import AUTO_INGEST_FAILED, retry_refusal
    from agent_core.domain.trajectory import ArtifactRef

    assert (
        retry_refusal(
            ArtifactRef.model_construct(
                origin=ArtifactOrigin.EMAIL_ATTACHMENT.value,
                metadata={"auto_ingest": AUTO_INGEST_FAILED},
            )
        )
        is None
    )


def test_the_default_instructions_advertise_saving_an_attachment() -> None:
    from agent_core.bootstrap import DEFAULT_AGENT_INSTRUCTIONS

    assert "mcp.gmail_read.save_attachment" in DEFAULT_AGENT_INSTRUCTIONS
