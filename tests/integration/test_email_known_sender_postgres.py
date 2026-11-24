"""Mail from someone the owner writes to, end to end over HTTP and PostgreSQL.

One refresh reads a conversation the owner started and a known correspondent
answered, with Gmail's passed sender check and a signature under the reply.
The unit suites cover each rule against stand-in stores; this journey crosses
the refresh route, the durable worker, the email task, People and memory on
PostgreSQL, and reads the outcome back through the public routes.

- ADR-0336 decisions 1 and 2: the signature's phone number and the line under
  the sender's name become `phone` and `role` details on the sender, as the
  sender's own claim (`contextual`, context "email signature"), and never
  appear in the Email reader's text while the retained body keeps them.
- ADR-0340 decisions 1 to 3: the sender is known (someone the owner has
  written to, on mail that passed the check), the owner's own message in the
  thread removes the expiry, and the memory is first class: a plain statement
  of what the sender said, direct and durable.
- ADR-0340 decision 1 and ADR-0336 decision 2: the same mail without a passed
  check has no known sender, forms a guess and updates nobody.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import Composition, build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import MAIL_SENT_AT, _mailbox_factory, _page, _profile
from tests.integration.journey_support import api_client
from tests.integration.m2_support import database_settings
from tests.unit.test_email_memory_rerating_hooks import NOW, _assessment

pytestmark = pytest.mark.integration

SENDER = "Casey Colleague <colleague@example.test>"
TITLE = "Partner, Example Capital"
PHONE = "650.555.0100"
REQUEST = "Please approve the board materials."
SIGNED_BODY = f"{REQUEST}\n\n-- \nCasey Colleague\n{TITLE}\n{PHONE}\n"


def _conversation(sender_check: str) -> dict[str, Any]:
    """The owner wrote first; the correspondent answered under their signature."""
    page = _page()
    [received] = page["messages"]
    asked_at = MAIL_SENT_AT - timedelta(days=2)
    asked = {
        **received,
        "id": "m0",
        "from": "owner@example.test",
        "to": SENDER,
        "cc": "",
        "reply_to": "",
        "direction": "sent",
        "label_ids": ["SENT"],
        "date": format_datetime(asked_at),
        "internal_date": int(asked_at.timestamp() * 1000),
        "message_id_header": "<m0@example.test>",
        "references": "",
        "body": "Could you send the board materials for approval?",
    }
    received.update({"from": SENDER, "body": SIGNED_BODY, "sender_check": sender_check})
    page["messages"] = [asked, received]
    page["total_messages"] = page["returned_messages"] = 2
    return page


async def _mailbox(sender_check: str) -> Any:
    base = await _mailbox_factory([])

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> Any:
        client = base(config, credential, environment)

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "get_profile":
                value: dict[str, Any] = _profile()
            elif name == "get_thread_page":
                value = _conversation(sender_check)
            elif name == "search_threads":
                summary = {"thread_id": "thread-1", "label_ids": ["INBOX"]}
                value = {"threads": [summary] if "in:inbox" in arguments["query"] else []}
            else:
                assert name == "sync_changes"
                value = {
                    "schema_version": 1,
                    "history_id": "100",
                    "changes": [],
                    "resync_required": False,
                    "next_page_token": None,
                }
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def _refresh(app: Composition, client: httpx.AsyncClient) -> None:
    """Ask for a refresh over the API and let the background lane finish it."""
    asked = await client.post("/v1/email/refresh", headers={"Idempotency-Key": "known-sender"})
    assert asked.status_code == 200, asked.text
    run_id = UUID(asked.json()["run_id"])
    worker = cast(DurableWorker, app.async_worker_factory("known-sender-refresh"))
    while (await app.runs.get(run_id)).status is not RunStatus.COMPLETED:
        assert await worker.run_once(), (await app.runs.get(run_id)).model_dump()


async def _read_back(client: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The correspondent's profile, the one email memory and the reader's text."""
    people = await client.get("/v1/people", params={"ceiling": "sensitive"})
    assert people.status_code == 200, people.text
    [casey] = [item for item in people.json()["items"] if item["display_name"] == "Casey Colleague"]
    profile = await client.get(f"/v1/people/{casey['id']}", params={"ceiling": "sensitive"})
    assert profile.status_code == 200, profile.text
    memories = await client.get("/v1/memories", params={"ceiling": "sensitive"})
    assert memories.status_code == 200, memories.text
    [memory] = memories.json()["items"]
    threads = await client.get("/v1/email/threads", params={"view": "all"})
    [thread] = threads.json()["items"]
    detail = await client.get(f"/v1/email/threads/{thread['id']}")
    assert detail.status_code == 200, detail.text
    [received] = [item for item in detail.json()["messages"] if item["direction"] == "received"]
    # The retained body keeps the signature whatever the reader shows.
    assert TITLE in received["body"] and PHONE in received["body"]
    return profile.json(), memory, str(received["reader_body"])


def _build(sender_check: str) -> Any:
    settings = replace(
        _email_settings(),
        email_mode_enabled=True,
        people_enabled=True,
        memory_api_enabled=True,
        database_url=database_settings().database_url,
    )

    async def composed() -> Any:
        return build(
            settings=settings,
            storage="postgres",
            clock=FixedClock(NOW),
            script=FakeModelScript(turns=[_assessment(0.6)]),
            mcp_client_factory=await _mailbox(sender_check),
        )

    return composed()


async def test_a_known_senders_signed_reply_updates_people_and_forms_a_first_class_memory() -> None:
    async with await _build("pass") as app, api_client(app) as client:
        await _refresh(app, client)
        profile, memory, reader_text = await _read_back(client)

    details = {
        (alias["identifier_kind"], alias["value"]): alias
        for alias in profile["aliases"]
        if alias["identifier_kind"] in {"phone", "role", "email"}
    }
    assert set(details) == {
        ("email", "colleague@example.test"),
        ("phone", PHONE),
        ("role", TITLE),
    }
    for kind in ("phone", "role"):
        [claim] = [alias for key, alias in details.items() if key[0] == kind]
        # The sender's own claim, shown as a claim and never as identity.
        assert (claim["verification"], claim["context"]) == ("contextual", "email signature")
    # The signature is not part of what the owner reads.
    assert REQUEST in reader_text
    assert TITLE not in reader_text and PHONE not in reader_text

    # Known sender and a thread the owner wrote in: first class, with no expiry.
    assert memory["statement"] == (
        f"{SENDER} said by email on {MAIL_SENT_AT.date().isoformat()} that "
        'Board materials requested_action: "approve the board materials".'
    )
    assert (memory["derivation"], memory["longevity"]) == ("direct", "durable")
    assert memory["expires_at"] is None


@pytest.mark.parametrize("sender_check", ["fail", "none"])
async def test_the_same_mail_without_a_passed_check_is_a_guess_and_updates_nobody(
    sender_check: str,
) -> None:
    async with await _build(sender_check) as app, api_client(app) as client:
        await _refresh(app, client)
        profile, memory, _ = await _read_back(client)

    # The owner wrote to the address, so the person exists; the signature under
    # mail Gmail did not authenticate adds nothing to them.
    assert {alias["identifier_kind"] for alias in profile["aliases"]} <= {"name", "email"}
    # A From line alone is not a known sender. The owner's own message still
    # removes the expiry, for every sender.
    assert memory["statement"].startswith("Email from ")
    assert (memory["derivation"], memory["longevity"]) == ("hypothesis", "tentative")
    assert memory["expires_at"] is None
