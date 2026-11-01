"""ADR-0277: Chat deletes a file it generated and the chat stops showing it."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.bootstrap import build
from agent_core.domain.artifacts import reply_file_reference
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import ContentBlock, FileContentBlock, TextContentBlock
from tests.unit.test_reply_attachments import _export, _settings


async def test_a_deleted_file_leaves_the_chat_and_the_kept_file_stays(tmp_path: Path) -> None:
    delete = ScriptedToolCall(name="tool.call", arguments={}, call_id="delete-draft")
    script = FakeModelScript(
        turns=[
            _export("draft", call_id="export-draft", filename="draft.txt"),
            _export("final", call_id="export-final", filename="final.txt"),
            ScriptedTurn(text="Both files are attached.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(tool_calls=[delete], stop_reason=StopReason.TOOL_USE),
            ScriptedTurn(text="Deleted the draft.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=_settings(tmp_path), script=script) as composition:
        first = await composition.runs.wait_terminal(
            await composition.runs.submit("Give me a draft and a final file.")
        )
        async with composition.uow_factory() as uow:
            draft, final = await uow.artifacts.list_for_run(first.id, composition.principal)
        # The model reads ids from the reply's file markers; the script stands in for that.
        delete.arguments = {
            "name": "artifact.delete",
            "arguments": {"artifact_ids": [f"artifact:{draft.id}"]},
        }
        second_id = await composition.runs.submit(
            "Delete the draft and keep the final file.", first.session_id
        )
        second = await composition.runs.wait_terminal(second_id)
        events = await composition.runs.events(second_id)
        transcript = await composition.services.sessions.messages(
            composition.principal, first.session_id, 100, None
        )
        with pytest.raises(NotFoundError):
            await composition.services.artifacts.get(composition.principal, draft.id)
        assert await composition.services.artifacts.get(composition.principal, final.id)

    assert first.status is RunStatus.COMPLETED, first.failure
    assert second.status is RunStatus.COMPLETED, second.failure
    assert "approval.requested" not in {event.event_type for event in events}
    assert [event.payload["name"] for event in events if event.event_type == "tool.call.completed"]
    reply = next(
        item
        for item in transcript.items
        if TextContentBlock(text="Both files are attached.") in item.content
    )
    assert reply.content == [
        TextContentBlock(text="Both files are attached."),
        FileContentBlock(artifact_id=final.id, media_type="text/plain", filename="final.txt"),
    ]


async def test_a_reply_left_empty_by_a_deletion_does_not_end_paging(tmp_path: Path) -> None:
    # A reply that carried only the deleted file has nothing left to show, but
    # it still counts toward the page so the cursor moves past it.
    script = FakeModelScript(
        turns=[
            _export("draft", call_id="export-draft", filename="draft.txt"),
            ScriptedTurn(text="Draft attached.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="Later answer.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=_settings(tmp_path), script=script) as composition:
        first = await composition.runs.wait_terminal(
            await composition.runs.submit("Give me a draft file.")
        )
        async with composition.uow_factory() as uow:
            [draft] = await uow.artifacts.list_for_run(first.id, composition.principal)
            file_only = reply_file_reference(draft).model_dump(mode="json")
            await uow.events.append(
                NewEvent(
                    session_id=first.session_id,
                    run_id=None,
                    event_type="assistant.message.completed",
                    actor_type="runtime",
                    payload={"message": {"kind": "assistant", "content": [file_only]}},
                )
            )
            await uow.artifacts.expire(draft.id, composition.principal, draft.created_at)
        await composition.runs.wait_terminal(
            await composition.runs.submit("And a later question.", first.session_id)
        )
        seen: list[tuple[str, list[ContentBlock]]] = []
        cursor: str | None = None
        for _page in range(10):
            page = await composition.services.sessions.messages(
                composition.principal, first.session_id, 1, cursor
            )
            seen.extend((item.role, item.content) for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

    assert seen == [
        ("user", [TextContentBlock(text="Give me a draft file.")]),
        ("assistant", [TextContentBlock(text="Draft attached.")]),
        ("user", [TextContentBlock(text="And a later question.")]),
        ("assistant", [TextContentBlock(text="Later answer.")]),
    ]
