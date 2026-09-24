"""A file the agent exports reaches the owner on its reply (ADR-0122)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_core.bootstrap import build
from agent_core.config import Settings, load_settings
from agent_core.domain.artifacts import (
    REPLY_ATTACHMENT_ORIGINS,
    ArtifactOrigin,
    reply_attachments,
    reply_file_reference,
)
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import (
    AssistantMessage,
    FakeModelScript,
    FileReferencePart,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.trajectory import ArtifactRef
from agent_core.domain.views import FileContentBlock, TextContentBlock
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FakeWebProvider

REPORT = "Jev market update\n\nDevelopers are evaluating it seriously.\n"


def _settings(tmp_path: Path) -> Settings:
    return replace(
        load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        artifact_root=tmp_path / "artifacts",
    )


def _export(content: str, *, call_id: str, filename: str = "Jev-Market-Update.txt") -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="artifact.export",
                arguments={"content": content, "filename": filename, "media_type": "text/plain"},
                call_id=call_id,
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _payload(events: list[EventEnvelope], event_type: str) -> dict[str, Any]:
    [event] = [event for event in events if event.event_type == event_type]
    return event.payload


def _files(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [part for part in message["content"] if part.get("kind") == "file"]


async def test_text_the_agent_writes_is_attached_to_its_reply_without_approval(
    tmp_path: Path,
) -> None:
    provider = FakeWebProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="web.fetch", arguments={"url": "https://example.org/jev"})
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _export(REPORT, call_id="export-report"),
            ScriptedTurn(text="The update is attached.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=_settings(tmp_path),
        script=script,
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Research Jev and give me the update as a file.")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(run_id, composition.principal)
        transcript = await composition.services.sessions.messages(
            composition.principal, run.session_id, 100, None
        )

    assert run.status is RunStatus.COMPLETED, run.failure
    assert "approval.requested" not in {event.event_type for event in events}
    [artifact] = artifacts
    assert artifact.origin == ArtifactOrigin.MODEL_OUTPUT.value
    assert artifact.expires_at is None
    reply = _payload(events, "assistant.message.completed")["message"]
    assert _files(reply) == [
        {
            "kind": "file",
            "artifact_id": str(artifact.id),
            "media_type": "text/plain",
            "filename": "Jev-Market-Update.txt",
            "size_bytes": len(REPORT.encode("utf-8")),
            "page_count": None,
        }
    ]
    # run.completed re-sends the reply; a different copy would draw a second bubble.
    assert _payload(events, "run.completed")["final_message"] == reply
    assert run.final_message == "The update is attached."
    assistant = transcript.items[-1]
    assert assistant.role == "assistant"
    assert assistant.content == [
        TextContentBlock(text="The update is attached."),
        FileContentBlock(
            artifact_id=artifact.id,
            media_type="text/plain",
            filename="Jev-Market-Update.txt",
        ),
    ]
    completed = [
        event.payload
        for event in events
        if event.event_type == "tool.call.completed" and event.payload["name"] == "artifact.export"
    ]
    [result_text] = [part["text"] for part in completed[0]["result_item"]["content"]]
    assert json.loads(result_text)["attached_to_reply"] is True


async def test_a_file_exported_from_the_workspace_is_attached_to_the_reply(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="workspace.write_text",
                        arguments={"path": "output/report.md", "content": REPORT},
                        call_id="write-report",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="artifact.export",
                        arguments={
                            "path": "output/report.md",
                            "filename": "report.md",
                            "media_type": "text/markdown",
                        },
                        call_id="export-report",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Here is the report.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=_settings(tmp_path), script=script) as composition:
        # The owner supplies the path and text verbatim, so the write needs no
        # approval; the in-memory composition releases a paused run's sandbox for
        # good, and the PostgreSQL golden journey covers the approved write.
        run_id = await composition.runs.submit(
            f"Write output/report.md containing exactly:\n{REPORT}\nthen give me the file."
        )
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(run_id, composition.principal)

    assert run.status is RunStatus.COMPLETED, run.failure
    [artifact] = artifacts
    assert artifact.origin == ArtifactOrigin.SANDBOX_EXPORT.value
    assert artifact.expires_at is None
    reply = _payload(events, "assistant.message.completed")["message"]
    assert [part["artifact_id"] for part in _files(reply)] == [str(artifact.id)]
    assert _payload(events, "run.completed")["final_message"] == reply


async def test_the_same_file_exported_twice_is_attached_once(tmp_path: Path) -> None:
    script = FakeModelScript(
        turns=[
            _export(REPORT, call_id="export-first"),
            _export(REPORT, call_id="export-again"),
            ScriptedTurn(text="Attached.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=_settings(tmp_path), script=script) as composition:
        run_id = await composition.runs.submit("Give me the report as a file.")
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(run_id, composition.principal)

    assert run.status is RunStatus.COMPLETED, run.failure
    # The repeated export is the same artifact, so the reply names it once.
    [artifact] = artifacts
    reply = _payload(events, "assistant.message.completed")["message"]
    assert [part["artifact_id"] for part in _files(reply)] == [str(artifact.id)]


async def test_a_run_that_exports_nothing_keeps_a_text_only_reply(tmp_path: Path) -> None:
    script = FakeModelScript(
        turns=[ScriptedTurn(text="No file needed.", stop_reason=StopReason.END_TURN)]
    )
    async with build(settings=_settings(tmp_path), script=script) as composition:
        run_id = await composition.runs.submit("Just answer.")
        await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)

    reply = _payload(events, "assistant.message.completed")["message"]
    assert reply["content"] == [{"kind": "text", "text": "No file needed."}]


def _ref(origin: ArtifactOrigin, *, name: str, sha: str, second: int) -> ArtifactRef:
    return ArtifactRef(
        id=UUID(int=second),
        tenant_id="tenant",
        principal_id="owner",
        session_id=UUID(int=500),
        run_id=UUID(int=600),
        name=name,
        media_type="text/plain",
        storage_uri="",
        sha256=sha * 64,
        size_bytes=second,
        origin=origin.value,
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=None,
        created_at=datetime(2026, 9, 23, 12, 0, second, tzinfo=UTC),
    )


def test_only_exports_and_model_files_are_attached_once_each_in_creation_order() -> None:
    first = _ref(ArtifactOrigin.SANDBOX_EXPORT, name="chart.png", sha="a", second=3)
    written = _ref(ArtifactOrigin.MODEL_OUTPUT, name="notes.md", sha="b", second=1)
    repeat = _ref(ArtifactOrigin.MODEL_OUTPUT, name="notes.md", sha="b", second=5)
    renamed = _ref(ArtifactOrigin.MODEL_OUTPUT, name="notes-2.md", sha="b", second=6)
    upload = _ref(ArtifactOrigin.UPLOAD, name="owner.pdf", sha="c", second=2)
    captured = _ref(ArtifactOrigin.TOOL_OUTPUT, name="web-fetch-output.json", sha="d", second=4)

    chosen = reply_attachments([first, written, repeat, renamed, upload, captured])

    assert {ArtifactOrigin.SANDBOX_EXPORT.value, ArtifactOrigin.MODEL_OUTPUT.value} == set(
        REPLY_ATTACHMENT_ORIGINS
    )
    assert [artifact.id for artifact in chosen] == [written.id, first.id, renamed.id]
    assert reply_file_reference(written) == FileReferencePart(
        artifact_id=written.id,
        media_type="text/plain",
        filename="notes.md",
        size_bytes=1,
    )


def test_attaching_files_keeps_the_reply_text_first() -> None:
    message = AssistantMessage(content=[TextPart(text="Attached.")])
    written = _ref(ArtifactOrigin.MODEL_OUTPUT, name="notes.md", sha="b", second=1)

    updated = message.model_copy(
        update={"content": [*message.content, reply_file_reference(written)]}
    )

    assert updated.content[0] == TextPart(text="Attached.")
    assert isinstance(updated.content[1], FileReferencePart)
