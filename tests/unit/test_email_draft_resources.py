"""Draft persistence must not retain a second roster of Gmail transports."""

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.application.email import read_value
from agent_core.bootstrap import Composition, build
from agent_core.domain.email import EmailDraft, EmailThread
from tests.gates.test_email_learning_m26 import observation, prepare
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _mailbox_factory


@pytest.fixture
async def draft_resources() -> AsyncIterator[tuple[Composition, ScriptedMCPClientFactory]]:
    """Use the generated first-party Gmail roster without network or provider calls."""
    factory = await _mailbox_factory([])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        await prepare(app)
        yield app, factory


async def test_generated_draft_releases_new_session_transports_and_preserves_pins(
    draft_resources: tuple[Composition, ScriptedMCPClientFactory],
) -> None:
    """Internal drafts retain conversation and discovery evidence, not idle clients."""
    app, factory = draft_resources
    service = app.services.email
    thread = await service.import_thread(app.principal, "work", observation(), uuid4())
    draft = await service.save_generated_draft(
        app.principal, thread.id, thread.revision, "Thanks.", run_id=uuid4()
    )
    assert len(factory.created) == 3
    assert not any(client.entered for client in factory.created)
    assert draft.session_id in app.mcp._prepared
    assert app.mcp._registry.get("mcp.gmail_read.get_thread", tenant_id="local")
    assert (await app.sessions.get(draft.session_id)).status.value == "ACTIVE"
    async with app.uow_factory() as uow:
        events = await uow.events.list_after(draft.session_id, 0, app.principal)
    assert any(event.event_type == "session.created" for event in events)
    assert any(event.event_type == "mcp.server.connected" for event in events)


async def test_draft_generation_keeps_an_existing_session_transport_alive(
    draft_resources: tuple[Composition, ScriptedMCPClientFactory],
) -> None:
    """A reused discussion session may still be running work that owns its clients."""
    app, factory = draft_resources
    service = app.services.email
    thread = await service.import_thread(app.principal, "work", observation(), uuid4())
    discussion = await service.discussion(app.principal, thread.id)
    assert len(factory.created) == 3
    assert all(client.entered for client in factory.created)
    draft = await service.save_generated_draft(
        app.principal, thread.id, thread.revision, "Thanks.", run_id=uuid4()
    )
    assert str(draft.session_id) == discussion["session_id"]
    assert len(factory.created) == 3
    assert all(client.entered for client in factory.created)


async def test_draft_activation_failure_still_releases_new_session_transports(
    draft_resources: tuple[Composition, ScriptedMCPClientFactory],
) -> None:
    """An event-publication failure cannot strand the just-persisted draft roster."""
    app, factory = draft_resources
    service = app.services.email
    thread = await service.import_thread(app.principal, "work", observation(), uuid4())

    async def fail_activation(session_id: UUID) -> None:
        """Fail after the draft transaction commits and before activation completes."""
        assert (await app.sessions.get(session_id)).status.value == "ACTIVE"
        raise RuntimeError("injected draft activation failure")

    service.activate_session = fail_activation
    with pytest.raises(RuntimeError, match="injected draft activation failure"):
        await service.save_generated_draft(
            app.principal, thread.id, thread.revision, "Thanks.", run_id=uuid4()
        )
    assert len(factory.created) == 3
    assert not any(client.entered for client in factory.created)
    async with app.uow_factory() as uow:
        stored_thread = await read_value(
            uow.email, app.principal, "thread", str(thread.id), EmailThread
        )
        assert stored_thread is not None and stored_thread.draft_id is not None
        draft = await read_value(
            uow.email, app.principal, "draft", str(stored_thread.draft_id), EmailDraft
        )
    assert draft is not None and draft.body == "Thanks."
