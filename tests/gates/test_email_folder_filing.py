"""Manual filing of owner-opened email discussions preserves their source state."""

from uuid import UUID, uuid4

import httpx
import pytest

from agent_core.bootstrap import Composition
from agent_core.domain.events import NewEvent
from tests.gates.test_email_experience_m26 import email_client
from tests.gates.test_email_runtime_m26 import _seed_draft
from tests.gates.test_folder_api_boundary_m29 import (
    _client,
    _create_folder,
    _create_session,
    _enabled,
)


async def assert_email_discussion_can_be_filed(
    composition: Composition,
    client: httpx.AsyncClient,
) -> None:
    service = composition.services.email
    service.account_ids = ("default",)
    service.account_servers = {"default": {"read": "gmail_read", "send": "gmail_send"}}
    thread, draft = await _seed_draft(composition)
    assert draft.session_id is not None
    session_id = str(draft.session_id)
    folder = await _create_folder(client, "Board work")
    route = f"/v1/sessions/{session_id}/folder"

    # Draft creation alone must never make the session fileable.
    refused = await client.put(route, json={"folder_id": folder["id"]})
    assert refused.status_code == 409, refused.text
    selected = await client.post(f"/v1/email/threads/{thread.id}/discussion", json={})
    assert selected.status_code == 200, selected.text
    assert selected.json()["session_id"] == session_id

    for _ in range(2):
        filed = await client.put(route, json={"folder_id": folder["id"]})
        assert filed.status_code == 200, filed.text
        assert filed.json()["folder_id"] == folder["id"]
        assert filed.json()["metadata"]["email_thread_id"] == str(thread.id)
    index = await client.get("/v1/sessions")
    row = next(item for item in index.json()["items"] if item["id"] == session_id)
    assert row["folder_id"] == folder["id"]
    assert (await client.get(f"/v1/folders/{folder['id']}")).json()["thread_count"] == 1

    other = await _create_folder(client, "Follow up")
    assert (await client.put(route, json={"folder_id": other["id"]})).status_code == 200
    assert (await client.get(f"/v1/folders/{folder['id']}")).json()["thread_count"] == 0
    assert (await client.get(f"/v1/folders/{other['id']}")).json()["thread_count"] == 1
    for _ in range(2):
        unfiled = await client.put(route, json={"folder_id": None})
        assert unfiled.status_code == 200, unfiled.text
        assert unfiled.json()["folder_id"] is None
    assert await service.draft(composition.principal, draft.id) == draft
    assert (await service.discussion(composition.principal, thread.id))["session_id"] == session_id
    async with composition.uow_factory() as uow:
        events = await uow.events.list_after(UUID(session_id), 0, composition.principal)
        changes = await uow.process_events.list("session.folder.changed")
    assert sum(event.event_type == "email.discussion.opened" for event in events) == 1
    assert not any(event.event_type == "user.message.created" for event in events)
    assert len([event for event in changes if event.payload["session_id"] == session_id]) == 3


async def test_email_discussion_can_be_filed_without_losing_its_draft() -> None:
    async with email_client(thread_folders_api_enabled=True) as (composition, client):
        await assert_email_discussion_can_be_filed(composition, client)


@pytest.mark.parametrize(
    ("event_type", "actor_type", "foreign_author", "excluded_key", "expected"),
    [
        ("user.message.created", "principal", False, None, 200),
        ("email.discussion.opened", "principal", False, None, 200),
        ("user.message.created", "application", False, None, 409),
        ("email.discussion.opened", "application", False, None, 409),
        ("assistant.message.completed", "principal", False, None, 409),
        ("user.message.created", "principal", True, None, 409),
        ("email.discussion.opened", "principal", True, None, 409),
        ("user.message.created", "principal", False, "email_operational", 409),
        ("email.discussion.opened", "principal", False, "schedule_id", 409),
        ("email.discussion.opened", "principal", False, "run_kind", 409),
    ],
)
async def test_email_filing_requires_owner_interaction_and_keeps_exclusions(
    event_type: str,
    actor_type: str,
    foreign_author: bool,
    excluded_key: str | None,
    expected: int,
) -> None:
    async with _enabled() as composition, _client(composition) as client:
        folder = await _create_folder(client, "Work")
        template_id = await _create_session(client)
        session_id = uuid4()
        metadata: dict[str, object] = {"email_thread_id": str(uuid4())}
        if excluded_key is not None:
            metadata[excluded_key] = False  # Presence still excludes, irrespective of value.
        async with composition.uow_factory() as uow:
            template = await uow.sessions.get(template_id, composition.principal)
            await uow.sessions.create(
                template.model_copy(update={"id": session_id, "metadata": metadata})
            )
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type=event_type,
                    actor_type=actor_type,
                    actor_id="other" if foreign_author else composition.principal.principal_id,
                )
            )
            # A newer application event must not mask earlier owner interaction.
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type=event_type,
                    actor_type="application",
                )
            )
        response = await client.put(
            f"/v1/sessions/{session_id}/folder", json={"folder_id": folder["id"]}
        )
        assert response.status_code == expected, response.text
        if expected == 409:
            assert response.json()["error"]["details"]["reason"] == "session_not_chat"
            assert (
                response.json()["error"]["message"]
                == "Only chat conversations can be filed in a folder."
            )
        else:
            foreign = composition.principal.model_copy(update={"principal_id": "other"})
            async with _client(composition, foreign) as other:
                assert (
                    await other.put(f"/v1/sessions/{session_id}/folder", json={"folder_id": None})
                ).status_code == 404
            read_only = composition.principal.model_copy(update={"scopes": {"session.read"}})
            async with _client(composition, read_only) as limited:
                assert (
                    await limited.put(f"/v1/sessions/{session_id}/folder", json={"folder_id": None})
                ).status_code == 403
