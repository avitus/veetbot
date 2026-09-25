"""People history names each email and reads its retained original (ADR-0126)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import jsonschema
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.people import PublicPeopleService
from agent_core.domain.email import EmailMessage, EmailThread
from agent_core.domain.email_semantics import semantic_source_key, thread_source_key
from agent_core.domain.errors import AuthorizationError, NotFoundError
from agent_core.domain.people import Person
from agent_core.domain.tools import ToolExecutionContext
from agent_core.memory.email_people import EmailPeopleFormationService
from agent_core.runtime.email_state import save_value
from agent_core.tools.people import LegacyPeopleHistoryTool, PeopleHistoryTool
from tests.contract.people_mail_fixtures import (
    ALEX,
    OWNER,
    all_rows,
    correspondence_stack,
    mail,
    seed_account,
)
from tests.contract.support import NOW, ids, principal, run, tool_context

BODY = "Can you send me the board deck before Thursday's call?"


async def _stack() -> tuple[Any, PublicPeopleService, Person]:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    await service.register_source(
        await mail(factory, message_id="m-sent", sender=OWNER, to=ALEX, labels=["SENT"], body=BODY)
    )
    clock = FixedClock(NOW)
    people = PublicPeopleService(
        factory,
        clock,
        email_sources=lambda owner: EmailPeopleFormationService(
            factory, clock, ids(), owner, provider="people-source", model="none"
        ),
    )
    async with factory() as uow:
        await uow.runs.create(run())
        [alex] = [
            row for row in await uow.people.query(all_rows(["person"])) if isinstance(row, Person)
        ]
    return factory, people, alex


def _context(*scopes: str) -> ToolExecutionContext:
    return replace(tool_context(), principal=principal().model_copy(update={"scopes": set(scopes)}))


async def test_an_email_item_names_its_message_and_the_cached_thread() -> None:
    factory, people, alex = await _stack()
    tool = PeopleHistoryTool(people)
    context = _context("people.read", "email.read")

    result = await tool.execute({"person_id": str(alex.id)}, context)
    assert result.structured is not None
    [item] = result.structured["items"]
    assert item["email"] == {"account_id": "work", "message_id": "m-sent", "thread_id": None}
    assert tool.spec.output_schema is not None
    jsonschema.validate(result.structured, tool.spec.output_schema)

    thread = EmailThread(
        id=uuid4(),
        account_id="work",
        provider_thread_id="t-mail",
        subject="Board deck",
        updated_at=NOW,
        messages=[EmailMessage(id="m-sent", sender=OWNER, body=BODY, sent_at=NOW)],
        last_accessed_at=NOW,
    )
    async with factory() as uow:
        await save_value(uow.email, principal(), "thread", str(thread.id), thread, NOW)
        await uow.email.put(
            _index(thread_source_key("work", "t-mail"), str(thread.id)), expected_revision=0
        )
    result = await tool.execute({"person_id": str(alex.id)}, context)
    assert result.structured is not None
    assert result.structured["items"][0]["email"]["thread_id"] == str(thread.id)


def _index(key: str, thread_id: str) -> Any:
    from agent_core.domain.email import EmailRecord

    return EmailRecord(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        kind="thread_source",
        key=key,
        revision=1,
        payload={"thread_id": thread_id},
        created_at=NOW,
        updated_at=NOW,
    )


async def test_a_source_read_returns_the_retained_original_email() -> None:
    _factory, people, alex = await _stack()
    tool = PeopleHistoryTool(people)
    context = _context("people.read", "email.read")
    listed = await tool.execute({"person_id": str(alex.id)}, context)
    assert listed.structured is not None
    source_id = listed.structured["items"][0]["source_ids"][0]

    result = await tool.execute({"person_id": str(alex.id), "source_id": source_id}, context)

    assert result.structured is not None
    assert result.structured["items"] == []
    source = result.structured["source"]
    assert source["source_id"] == source_id
    assert source["text"] == BODY
    assert source["sender"] == OWNER and source["to"] == ALEX
    assert source["account_id"] == "work" and source["message_id"] == "m-sent"
    assert source["offset"] == 0 and source["next_offset"] is None and source["complete"] is True
    assert tool.spec.output_schema is not None
    jsonschema.validate(result.structured, tool.spec.output_schema)


async def test_a_source_read_needs_email_access_and_a_source_of_this_person() -> None:
    _factory, people, alex = await _stack()
    tool = PeopleHistoryTool(people)
    listed = await tool.execute({"person_id": str(alex.id)}, _context("people.read", "email.read"))
    assert listed.structured is not None
    source_id = listed.structured["items"][0]["source_ids"][0]

    with pytest.raises(AuthorizationError):
        await tool.execute(
            {"person_id": str(alex.id), "source_id": source_id}, _context("people.read")
        )
    with pytest.raises(NotFoundError):
        await tool.execute(
            {"person_id": str(alex.id), "source_id": str(uuid4())},
            _context("people.read", "email.read"),
        )


async def test_an_excluded_source_no_longer_returns_its_text() -> None:
    factory, people, alex = await _stack()
    tool = PeopleHistoryTool(people)
    context = _context("people.read", "email.read")
    listed = await tool.execute({"person_id": str(alex.id)}, context)
    assert listed.structured is not None
    source_id = listed.structured["items"][0]["source_ids"][0]
    async with factory() as uow:
        key = semantic_source_key("work", "t-mail", "m-sent")
        record = await uow.email.get(principal(), "semantic_source", key)
        assert record is not None
        await uow.email.put(
            record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "payload": {**record.payload, "excluded": True},
                }
            ),
            expected_revision=record.revision,
        )

    with pytest.raises(NotFoundError):
        await tool.execute({"person_id": str(alex.id), "source_id": source_id}, context)


async def test_chats_pinned_to_the_first_version_keep_its_published_shape() -> None:
    _factory, people, alex = await _stack()
    legacy = LegacyPeopleHistoryTool(people)
    assert legacy.spec.version == "1.0.0" and PeopleHistoryTool.spec.version == "1.1.0"
    assert "source_id" not in legacy.spec.input_schema["properties"]

    result = await legacy.execute(
        {"person_id": str(alex.id)}, _context("people.read", "email.read")
    )

    assert result.structured is not None and "source" not in result.structured
    [item] = result.structured["items"]
    assert "email" not in item
    assert legacy.spec.output_schema is not None
    jsonschema.validate(result.structured, legacy.spec.output_schema)


def test_the_first_version_keeps_its_exact_pinned_spec() -> None:
    import hashlib
    import json

    # A chat pins the whole spec; a restored run fails on any difference.
    spec = json.dumps(LegacyPeopleHistoryTool.spec.model_dump(mode="json"), sort_keys=True)
    assert (
        hashlib.sha256(spec.encode()).hexdigest()
        == "a150f49efb3d706a088b3dd9745523c2327958e4b82271835c2049dbbeebb951"
    )
