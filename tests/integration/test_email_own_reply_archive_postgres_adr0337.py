"""Archiving straight after a reply, over HTTP and PostgreSQL (ADR-0337).

Guards ADR-0337 and docs/plan/email-experience.md's Inbox gesture rule where
the gate suite stops at an in-memory service call: the floor a conversation
records is stored state, so it has to survive the database and a restart
between the refresh that saved the owner's reply and the archive request.

- Decisions 1 and 2: a revision that only adds the owner's sent message keeps
  a gesture naming the earlier revision current; mail from anyone else, at
  any point, clears the floor and only the current revision qualifies.
- Decision 3: the same rule admits the request, revalidates the consent
  before the one Gmail write, and projects the confirmed result on the newer
  revision.
- Decision 5: no client projection carries the floor.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_archive_m26 import _archive_factory, _page_with_incoming
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _page, _seed_draft
from tests.integration.journey_support import api_client
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

ARCHIVE_WRITE = {"thread_ids": ["thread-1"], "add_label_ids": None, "remove_label_ids": ["INBOX"]}


def _settings() -> Settings:
    return replace(
        _email_settings(), database_url=database_settings().database_url, email_mode_enabled=True
    )


def _page_with_reply_then_incoming() -> dict[str, Any]:
    """The owner's reply, then a correspondent's answer the owner has not seen."""
    value = _page(changed=True)
    reply = value["messages"][1]
    value["messages"].append(
        {
            **reply,
            "id": "m3",
            "from": "Colleague <colleague@example.test>",
            "direction": "received",
            "label_ids": ["INBOX"],
            "internal_date": reply["internal_date"] + 1000,
            "body": "Thanks. One more thing before you file this.",
        }
    )
    value["history_id"] = "102"
    value["total_messages"] = len(value["messages"])
    return value


async def _sync(app: Composition, thread: Any, page: dict[str, Any]) -> None:
    """Save the conversation as a mailbox refresh read it."""
    await app.services.email.import_thread(
        app.principal, "default", page, thread.source_session_ids[0]
    )


async def _archive(
    client: httpx.AsyncClient, thread_id: UUID, revision: int, key: str
) -> httpx.Response:
    return await client.post(
        f"/v1/email/threads/{thread_id}/archive",
        json={"expected_revision": revision, "archived": True, "idempotency_key": key},
        headers={"Idempotency-Key": key},
    )


async def test_archive_naming_the_revision_before_the_owners_reply_succeeds_after_a_restart() -> (
    None
):
    """The incident: reply, refresh saves it, archive on the view the owner had."""
    writes: list[dict[str, Any]] = []
    factory = await _archive_factory(writes)
    async with (
        build(settings=_settings(), storage="postgres", mcp_client_factory=factory) as app,
        api_client(app) as client,
    ):
        thread, _ = await _seed_draft(app)
        seen = await client.get(f"/v1/email/threads/{thread.id}")
        assert seen.status_code == 200, seen.text
        assert seen.json()["revision"] == 1 and seen.json()["in_inbox"] is True
        await _sync(app, thread, _page(changed=True))

    async with (
        build(settings=_settings(), storage="postgres", mcp_client_factory=factory) as app,
        api_client(app) as client,
    ):
        current = await client.get(f"/v1/email/threads/{thread.id}")
        assert current.json()["revision"] == 2 and len(current.json()["messages"]) == 2
        # The floor is internal state: neither projection carries it.
        listed = await client.get("/v1/email/threads", params={"view": "all"})
        assert "own_reply_floor" not in current.text and "own_reply_floor" not in listed.text

        accepted = await _archive(client, thread.id, 1, "after-own-reply")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == RunStatus.QUEUED.value
        assert writes == []
        worker = cast(DurableWorker, app.worker_factory("archive-after-own-reply"))
        assert await worker.run_once()
        run = await app.runs.get(UUID(accepted.json()["run_id"]))
        assert run.status is RunStatus.COMPLETED, run.failure

        archived = await client.get(f"/v1/email/threads/{thread.id}")
        # The confirmed result lands on the revision that holds the reply.
        assert archived.json()["revision"] == 2 and archived.json()["in_inbox"] is False
        assert archived.json()["archive_operation"]["status"] == "completed"
        assert writes == [ARCHIVE_WRITE]

        replay = await _archive(client, thread.id, 1, "after-own-reply")
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["run_id"] == accepted.json()["run_id"]
    assert writes == [ARCHIVE_WRITE]


@pytest.mark.parametrize("history", ["incoming", "reply_then_incoming"])
async def test_mail_from_anyone_else_stales_the_gesture_and_changes_nothing(history: str) -> None:
    """Unseen mail refuses the archive, with or without the owner's reply before it."""
    writes: list[dict[str, Any]] = []
    factory = await _archive_factory(writes)
    async with (
        build(settings=_settings(), storage="postgres", mcp_client_factory=factory) as app,
        api_client(app) as client,
    ):
        thread, _ = await _seed_draft(app)
        if history == "incoming":
            await _sync(app, thread, _page_with_incoming())
            stale_revisions, current_revision = [1], 2
        else:
            await _sync(app, thread, _page(changed=True))
            await _sync(app, thread, _page_with_reply_then_incoming())
            stale_revisions, current_revision = [1, 2], 3

        for revision in stale_revisions:
            refused = await _archive(client, thread.id, revision, f"stale-{revision}")
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "conflict"
        latest = await client.get(f"/v1/email/threads/{thread.id}")
        assert latest.json()["revision"] == current_revision
        assert latest.json()["in_inbox"] is True and latest.json()["archive_operation"] is None
        assert writes == []
        worker = cast(DurableWorker, app.worker_factory("nothing-queued"))
        assert not await worker.run_once()

        # The view that shows the new mail is still a current gesture.
        accepted = await _archive(client, thread.id, current_revision, "seen-everything")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == RunStatus.QUEUED.value
