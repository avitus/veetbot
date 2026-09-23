"""Sending a message validates and claims its attachments (ADR-0120).

Step 5 of the submit handler checks every image or file block against the
caller's uploads in the same session, rebuilds the stored parts from the
artifact row, and — once the run is enqueued — claims each upload for that
run, ending its expiry. Owner-sent documents are marked for knowledge.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from agent_core.domain.policies import TrustLevel
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.trajectory import ArtifactRef
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import AGENT_ID, NOW
from tests.gates.test_attachment_upload_adr0120 import (
    JPEG,
    PNG,
    _client,
    _composition,
    _pdf,
    _session,
    _upload,
)


async def _uploaded(
    client: httpx.AsyncClient,
    session_id: UUID,
    content: bytes,
    name: str,
    media_type: str,
    key: str,
) -> dict[str, Any]:
    response = await _upload(
        client, session_id, content, filename=name, media_type=media_type, key=key
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _send(
    client: httpx.AsyncClient,
    session_id: UUID,
    content: list[dict[str, Any]],
    *,
    key: str | None = None,
) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return await client.post(
        f"/v1/sessions/{session_id}/messages", json={"content": content}, headers=headers
    )


async def _artifact(composition: Any, artifact_id: str) -> ArtifactRef:
    async with composition.uow_factory() as uow:
        artifact: ArtifactRef = await uow.artifacts.get(UUID(artifact_id), composition.principal)
    return artifact


async def test_sending_claims_the_upload_and_records_server_facts(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        image = await _uploaded(client, session_id, PNG, "cat.png", "image/png", "u1")
        notes = await _uploaded(client, session_id, b"# Notes", "notes.md", "text/markdown", "u2")
        sent = await _send(
            client,
            session_id,
            [
                {"type": "text", "text": "what do you see?"},
                {"type": "image", "artifact_id": image["id"], "media_type": "image/gif"},
                # A client-declared type never survives; the upload's is recorded.
                {"type": "file", "artifact_id": notes["id"], "media_type": "text/plain"},
            ],
        )
        assert sent.status_code == 202, sent.text
        run_id = sent.json()["run_id"]

        claimed_image = await _artifact(composition, image["id"])
        assert str(claimed_image.run_id) == run_id
        assert claimed_image.expires_at is None
        assert "auto_ingest" not in claimed_image.metadata
        claimed_notes = await _artifact(composition, notes["id"])
        assert str(claimed_notes.run_id) == run_id
        assert claimed_notes.metadata["auto_ingest"] == "pending"

        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        message = next(event for event in events if event.event_type == "user.message.created")
        parts = message.payload["content"]
        assert parts[1] == {
            "kind": "image",
            "artifact_id": image["id"],
            "media_type": "image/png",
            "detail": "auto",
            "filename": "cat.png",
            "size_bytes": len(PNG),
            "page_count": None,
        }
        assert parts[2]["media_type"] == "text/markdown"
        assert parts[2]["filename"] == "notes.md"

        transcript = await client.get(f"/v1/sessions/{session_id}/messages")
        assert transcript.status_code == 200
        blocks = transcript.json()["items"][0]["content"]
        assert blocks[1]["type"] == "image"
        assert blocks[2] == {
            "type": "file",
            "artifact_id": notes["id"],
            "media_type": "text/markdown",
            "filename": "notes.md",
        }


async def test_a_message_may_be_attachments_alone_and_names_the_chat(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        report = await _uploaded(
            client, session_id, _pdf(3), "Q3 report.pdf", "application/pdf", "u1"
        )
        sent = await _send(
            client,
            session_id,
            [{"type": "file", "artifact_id": report["id"], "media_type": "application/pdf"}],
        )
        assert sent.status_code == 202, sent.text
        session = await client.get(f"/v1/sessions/{session_id}")
        assert session.json()["title"] == "Q3 report.pdf"
        assert (await _artifact(composition, report["id"])).metadata["auto_ingest"] == "pending"


async def test_only_the_callers_live_uploads_in_this_session_are_accepted(
    tmp_path: Path,
) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        other_session = await _session(client)
        elsewhere = await _uploaded(client, other_session, PNG, "a.png", "image/png", "u1")
        foreign_id = UUID(int=7001)
        tool_output_id = UUID(int=7002)
        expired_id = UUID(int=7003)
        async with composition.uow_factory() as uow:
            await uow.sessions.create(
                Session(
                    id=UUID(int=7000),
                    tenant_id=composition.principal.tenant_id,
                    principal_id="someone-else",
                    agent_id=AGENT_ID,
                    agent_version="1.0.0",
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

            def ref(artifact_id: UUID, **changes: Any) -> ArtifactRef:
                values: dict[str, Any] = {
                    "id": artifact_id,
                    "tenant_id": composition.principal.tenant_id,
                    "principal_id": composition.principal.principal_id,
                    "session_id": session_id,
                    "run_id": None,
                    "name": "x.png",
                    "media_type": "image/png",
                    "storage_uri": "",
                    "sha256": "3" * 64,
                    "size_bytes": 10,
                    "origin": "upload",
                    "trust": TrustLevel.EXTERNAL_UNTRUSTED,
                    "expires_at": composition.clock.now() + timedelta(hours=1),
                    "created_at": composition.clock.now(),
                    "metadata": {"attachment": {"kind": "image"}},
                }
                values.update(changes)
                return ArtifactRef.model_validate(values)

            await uow.artifacts.create(
                ref(foreign_id, principal_id="someone-else", session_id=UUID(int=7000))
            )
            await uow.artifacts.create(ref(expired_id, expires_at=composition.clock.now()))
        tool_run = await _send(client, session_id, [{"type": "text", "text": "hello"}])
        assert tool_run.status_code == 202
        async with composition.uow_factory() as uow:
            await uow.artifacts.create(
                ref(tool_output_id, origin="tool_output", run_id=UUID(tool_run.json()["run_id"]))
            )
        await _finish(composition)

        for artifact_id in [elsewhere["id"], foreign_id, tool_output_id, expired_id, UUID(int=1)]:
            refused = await _send(
                client,
                session_id,
                [{"type": "image", "artifact_id": str(artifact_id), "media_type": "image/png"}],
            )
            assert refused.status_code == 404, (artifact_id, refused.text)
        assert (await _artifact(composition, elsewhere["id"])).run_id is None


async def test_blocks_must_match_what_was_uploaded(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        pdf = await _uploaded(client, session_id, _pdf(1), "a.pdf", "application/pdf", "u1")
        image = await _uploaded(client, session_id, JPEG, "b.jpg", "image/jpeg", "u2")
        wrong_kind = await _send(
            client,
            session_id,
            [{"type": "image", "artifact_id": pdf["id"], "media_type": "image/png"}],
        )
        assert wrong_kind.status_code == 400
        assert wrong_kind.json()["error"]["code"] == "malformed_request"
        bad_detail = await _send(
            client,
            session_id,
            [
                {
                    "type": "image",
                    "artifact_id": image["id"],
                    "media_type": "image/jpeg",
                    "detail": "ultra",
                }
            ],
        )
        assert bad_detail.status_code == 400
        assert bad_detail.json()["error"]["code"] == "malformed_request"
        eleven = [{"type": "file", "artifact_id": pdf["id"], "media_type": "application/pdf"}] * 11
        assert (await _send(client, session_id, eleven)).status_code == 400
        assert (await _artifact(composition, pdf["id"])).run_id is None


async def test_documents_are_marked_for_knowledge_only_for_an_owner_with_the_scope(
    tmp_path: Path,
) -> None:
    async with _composition(tmp_path) as composition:
        without_knowledge = composition.principal.model_copy(
            update={"scopes": set(PLATFORM_SCOPES) - {"knowledge.write"}}
        )
        async with _client(composition, principal=without_knowledge) as client:
            session_id = await _session(client)
            notes = await _uploaded(client, session_id, b"plain", "a.txt", "text/plain", "u1")
            sent = await _send(
                client,
                session_id,
                [{"type": "file", "artifact_id": notes["id"], "media_type": "text/plain"}],
            )
            assert sent.status_code == 202, sent.text
        claimed = await _artifact(composition, notes["id"])
        assert claimed.run_id is not None
        assert "auto_ingest" not in claimed.metadata


async def test_a_replayed_submission_claims_nothing_twice(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        image = await _uploaded(client, session_id, PNG, "cat.png", "image/png", "u1")
        content = [{"type": "image", "artifact_id": image["id"], "media_type": "image/png"}]
        first = await _send(client, session_id, content, key="send-1")
        second = await _send(client, session_id, content, key="send-1")
        assert first.status_code == 202
        assert second.json()["run_id"] == first.json()["run_id"]
        assert str((await _artifact(composition, image["id"])).run_id) == first.json()["run_id"]


async def _finish(composition: Any) -> None:
    """Let the fake worker finish the queued run so the session accepts a new one."""

    drain = getattr(composition, "drain", None)
    if drain is not None:
        await drain()


async def test_a_sent_attachment_reaches_the_model_through_the_composed_resolver(
    tmp_path: Path,
) -> None:
    """Upload and send over HTTP; the composed resolver releases it to an adapter."""

    import base64

    from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
    from agent_core.domain.messages import ModelAttempt, ModelCapabilities, ResolvedModel
    from agent_core.domain.messages import UserMessage as NeutralUserMessage
    from agent_core.model.streaming import collect_turn
    from tests.contract.model_fixtures import ScriptedRawSource, openai_text_events
    from tests.contract.test_model_gateway_contract import request

    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _session(client)
        image = await _uploaded(client, session_id, PNG, "cat.png", "image/png", "u1")
        pdf_bytes = _pdf(2)
        pdf = await _uploaded(client, session_id, pdf_bytes, "a.pdf", "application/pdf", "u2")
        sent = await _send(
            client,
            session_id,
            [
                {"type": "text", "text": "look"},
                {"type": "image", "artifact_id": image["id"], "media_type": "image/png"},
                {"type": "file", "artifact_id": pdf["id"], "media_type": "application/pdf"},
            ],
        )
        assert sent.status_code == 202, sent.text
        run_id = UUID(sent.json()["run_id"])
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
        message = next(event for event in events if event.event_type == "user.message.created")
        conversation = [NeutralUserMessage.model_validate({"content": message.payload["content"]})]

        source = ScriptedRawSource([openai_text_events()])
        provider = OpenAIResponsesProvider(
            event_source=source, attachment_resolver=composition.attachment_resolver
        )
        attempt = ModelAttempt(
            attempt_id=UUID(int=1), run_id=run_id, step_number=1, attempt_number=1, started_at=NOW
        )
        resolved = ResolvedModel(
            provider="openai",
            model="contract-model",
            capabilities=ModelCapabilities(images=True, files=True),
            resolved_at=NOW,
        )
        try:
            await collect_turn(provider.stream(request(conversation), resolved, attempt))
        finally:
            await provider.close()
        content = source.requests[0]["input"][0]["content"]
        assert content[2]["image_url"].endswith(base64.b64encode(PNG).decode())
        assert content[4]["file_data"].endswith(base64.b64encode(pdf_bytes).decode())
        assert content[4]["filename"] == "a.pdf"
