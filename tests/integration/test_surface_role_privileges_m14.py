"""The surface role works under exactly its documented database grants (Milestone 14).

`deploy/app/release.sh` refuses a surface login holding anything beyond the
allowlist in `scripts/check_surface_database_permissions.py`, which
`docs/deployment.md` prints as SQL. This drives the real `build_surface_worker`
composition, whose Telegram transport a stand-in Bot API answers, through every
path the role takes against PostgreSQL while it connects as a NOINHERIT,
NOBYPASSRLS login holding only those grants: startup registration and the
schema-head check, the poll lock, offset and `surface.poll.resumed`, unreadable
receipts, pairing with its failures and lockout, submission, input to a waiting
run, `/approve`, `/deny`, every `/stop` state, `/new`, `/status`, `/help`, idle
rotation, both outbound drains with their question and approval details, and the
optional WhatsApp channel's registration and signed webhook. The application
login plays the API and the run worker around it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from agent_core import bootstrap
from agent_core.adapters.determinism import FixedClock, SystemClock
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.telegram import TelegramBotTransport
from agent_core.adapters.whatsapp import WhatsAppCloudTransport
from agent_core.api import create_app
from agent_core.application.surfaces import surface_notice_text
from agent_core.bootstrap import (
    Composition,
    SurfaceWorkerComposition,
    build,
    build_surface_worker,
)
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.delegations import DelegationChild, DelegationStatus
from agent_core.domain.devices import PushProvider
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import RiskLevel, SideEffectClass
from agent_core.domain.runs import RunKind, RunStatus
from agent_core.domain.surfaces import InboundDisposition, InboundReceipt
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import DurableWorker
from scripts.check_surface_database_permissions import (
    REQUIRED_TABLE_PRIVILEGES,
    permission_failures,
)
from scripts.database_role_permissions import inspect_database_role
from tests.contract.support import run as contract_run
from tests.contract.support import session as contract_session
from tests.contract.test_delegation_repository_contract import delegation
from tests.integration.m2_support import database_settings

# Before the host's real clock, so the run worker's system-clock claims see
# every row the fixed clock stamps as due.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
OWNER = 424242
STRANGER = 515151
GROUP = -1_000_777
SURFACE_SCOPES = frozenset(
    {
        "approval.read",
        "approval.resolve",
        "demo.write",
        "run.cancel",
        "run.read",
        "run.write",
        "surface.read",
        "surface.write",
    }
)
PAIRING_SCOPES = sorted(SURFACE_SCOPES - {"surface.read", "surface.write"})
SURFACE_ID = uuid5(NAMESPACE_URL, "veetbot:surface:local:telegram:configured")
WHATSAPP_PHONE_NUMBER_ID = "100200300400500"
WHATSAPP_SURFACE_ID = uuid5(
    NAMESPACE_URL, f"veetbot:surface:local:whatsapp:{WHATSAPP_PHONE_NUMBER_ID}"
)
WHATSAPP_SENDER = "15550002222"
WHATSAPP_SIGNATURE_KEY = "whatsapp-" + "app-secret-value"


@asynccontextmanager
async def release_surface_role(
    privileges: Mapping[str, frozenset[str]] = REQUIRED_TABLE_PRIVILEGES,
) -> AsyncIterator[str]:
    """Yield a login URL holding exactly ``privileges``, as docs/deployment.md grants."""

    admin_url = make_url(database_settings().database_url)
    role = f"veetbot_surface_probe_{uuid4().hex[:12]}"
    password = uuid4().hex
    engine = create_engine(admin_url.render_as_string(hide_password=False))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            for table, table_privileges in privileges.items():
                if table_privileges:
                    await connection.execute(
                        text(f"GRANT {', '.join(sorted(table_privileges))} ON {table} TO {role}")
                    )
        try:
            yield admin_url.set(username=role, password=password).render_as_string(
                hide_password=False
            )
        finally:
            async with engine.begin() as connection:
                # The role's poller may still hold a pooled session in a failed case.
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE usename = :role"
                    ),
                    {"role": role},
                )
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await engine.dispose()


@dataclass
class _BotApi:
    """Answers the Bot API methods the transport calls; keeps what the bot sent."""

    clock: FixedClock
    updates: list[dict[str, Any]] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    sent: list[tuple[int, str]] = field(default_factory=list)
    whatsapp_sent: list[tuple[str, str]] = field(default_factory=list)
    webhook_deletions: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.facebook.com":
            document = json.loads(request.content)
            self.whatsapp_sent.append((str(document["to"]), str(document["text"]["body"])))
            return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "deleteWebhook":
            self.webhook_deletions += 1
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getUpdates":
            offset = int(request.url.params["offset"])
            self.offsets.append(offset)
            due = [update for update in self.updates if update["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": due})
        if method == "sendMessage":
            document = json.loads(request.content)
            self.sent.append((int(document["chat_id"]), str(document["text"])))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        raise AssertionError(f"unexpected Telegram method {method}")

    def message(
        self,
        text_value: str | None,
        *,
        chat: int = OWNER,
        chat_type: str = "private",
    ) -> int:
        update_id = len(self.updates) + 1
        message: dict[str, Any] = {
            "message_id": update_id,
            "date": int(self.clock.now().timestamp()),
            "chat": {"id": chat, "type": chat_type},
            "from": {"id": abs(chat), "first_name": "Owner"},
        }
        if text_value is None:
            message["photo"] = [{"file_id": "photo", "width": 1, "height": 1}]
        else:
            message["text"] = text_value
        self.updates.append({"update_id": update_id, "message": message})
        return update_id

    def unreadable(self) -> int:
        update_id = len(self.updates) + 1
        self.updates.append({"update_id": update_id, "edited_message": {"message_id": 1}})
        return update_id

    def take(self) -> list[tuple[int, str]]:
        sent, self.sent = self.sent, []
        return sent

    def whatsapp_message(self, message_id: str) -> tuple[bytes, str]:
        """A signed Cloud API webhook body carrying one text message, and its signature."""

        body = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "waba-1",
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messaging_product": "whatsapp",
                                    "metadata": {"phone_number_id": WHATSAPP_PHONE_NUMBER_ID},
                                    "contacts": [
                                        {"profile": {"name": "Owner"}, "wa_id": WHATSAPP_SENDER}
                                    ],
                                    "messages": [
                                        {
                                            "from": WHATSAPP_SENDER,
                                            "id": message_id,
                                            "timestamp": str(int(self.clock.now().timestamp())),
                                            "text": {"body": "Hello from WhatsApp"},
                                            "type": "text",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        digest = hmac.new(WHATSAPP_SIGNATURE_KEY.encode(), body, hashlib.sha256).hexdigest()
        return body, f"sha256={digest}"


def _surface_settings(database_url: str, *, whatsapp: bool) -> Settings:
    settings = replace(
        database_settings(),
        database_url=database_url,
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=None,
        sandbox=SandboxMechanism.GVISOR,
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"surface"}),
        auth_scopes=SURFACE_SCOPES,
        surface_api_enabled=True,
        surface_worker_enabled=True,
        surface_telegram_token=SecretStr("123456789:" + "telegram-test-token-value"),
    )
    if not whatsapp:
        return settings
    return replace(
        settings,
        surface_whatsapp_enabled=True,
        surface_whatsapp_token=SecretStr("whatsapp-" + "access-token-value"),
        surface_whatsapp_app_secret=SecretStr(WHATSAPP_SIGNATURE_KEY),
        surface_whatsapp_verify_token=SecretStr("whatsapp-" + "verify-token-value"),
        surface_whatsapp_phone_number_id=WHATSAPP_PHONE_NUMBER_ID,
        surface_whatsapp_graph_api_version="v21.0",
    )


def _application_settings() -> Settings:
    return replace(
        database_settings(),
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        surface_api_enabled=True,
        surface_worker_enabled=True,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )


@asynccontextmanager
async def _surface_role(
    role_url: str, clock: FixedClock, bot: _BotApi, *, whatsapp: bool = False
) -> AsyncIterator[SurfaceWorkerComposition]:
    """One surface process: the production builder with only the provider APIs replaced."""

    client = httpx.AsyncClient(transport=httpx.MockTransport(bot.handle))

    def telegram(*, token: SecretStr) -> TelegramBotTransport:
        return TelegramBotTransport(token=token, client=client)

    def cloud_api(**options: Any) -> WhatsAppCloudTransport:
        return WhatsAppCloudTransport(**options, client=client)

    try:
        with (
            patch.object(bootstrap, "TelegramBotTransport", telegram),
            patch.object(bootstrap, "WhatsAppCloudTransport", cloud_api),
        ):
            async with build_surface_worker(
                settings=_surface_settings(role_url, whatsapp=whatsapp), clock=clock
            ) as composition:
                yield composition
    finally:
        await client.aclose()


@asynccontextmanager
async def _api(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app, client=("127.0.0.1", 43108), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


def _script() -> FakeModelScript:
    def ask(question: str, call_id: str) -> ScriptedTurn:
        return ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="conversation.ask_user",
                    arguments={"question": question},
                    call_id=call_id,
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        )

    def write(content: str, call_id: str) -> ScriptedTurn:
        return ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="demo.external_write",
                    arguments={"destination": "demo", "content": content},
                    call_id=call_id,
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        )

    return FakeModelScript(
        turns=[
            ask("Which colour should the note use?", "surface-role-question"),
            write("blue", "surface-role-approved-write"),
            ScriptedTurn(text="Noted in blue."),
            write("second", "surface-role-denied-write"),
            ScriptedTurn(text="Left it unwritten."),
            ask("Stop me while I wait?", "surface-role-stopped-question"),
            write("third", "surface-role-stopped-write"),
        ]
    )


async def _park_with_delegated_child(composition: Composition, parent_id: UUID) -> UUID:
    """Leave ``parent_id`` waiting for approval with one delegated child still queued,
    as the run worker leaves a parent whose batch both delegated and needs approval."""

    owner = composition.principal
    now = composition.clock.now()
    delegation_id, invocation_id, child_run_id = uuid4(), uuid4(), uuid4()
    async with composition.uow_factory() as uow:
        assert uow.queue is not None
        claimed = await uow.queue.claim("surface-role-delegator", (0, 10))
        assert claimed is not None and claimed.run.id == parent_id
        parent = claimed.run
        child_session = contract_session().model_copy(
            update={
                "id": uuid4(),
                "tenant_id": owner.tenant_id,
                "principal_id": owner.principal_id,
                "agent_id": parent.agent_id,
                "agent_version": parent.agent_version,
                "metadata": {
                    "run_kind": "delegated",
                    "parent_run_id": str(parent_id),
                    "parent_session_id": str(parent.session_id),
                    "delegation_id": str(delegation_id),
                },
            }
        )
        await uow.sessions.create(child_session)
        await uow.runs.create(
            contract_run().model_copy(
                update={
                    "id": child_run_id,
                    "session_id": child_session.id,
                    "tenant_id": owner.tenant_id,
                    "agent_id": parent.agent_id,
                    "agent_version": parent.agent_version,
                    "parent_run_id": parent_id,
                    "kind": RunKind.DELEGATED,
                }
            )
        )
        await uow.invocations.create(
            ToolInvocation(
                id=invocation_id,
                run_id=parent_id,
                session_id=parent.session_id,
                step_number=1,
                call_id="surface-role-delegate",
                tool_name="delegate.run",
                tool_version="1.0.0",
                side_effect=SideEffectClass.NONE,
                risk=RiskLevel.LOW,
                status=ToolInvocationStatus.RUNNING,
                raw_arguments="{}",
                idempotency_key=f"surface-role-delegate-{invocation_id}",
                suspended_kind="child_run",
                suspended_ref=str(delegation_id),
                created_at=now,
                updated_at=now,
            )
        )
        await uow.delegations.create(
            delegation(
                id=delegation_id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                parent_run_id=parent_id,
                parent_session_id=parent.session_id,
                invocation_id=invocation_id,
                children=[
                    DelegationChild(
                        index=0,
                        child_run_id=child_run_id,
                        child_session_id=child_session.id,
                    )
                ],
            )
        )
        await uow.runs.transition(
            parent_id,
            RunStatus.RUNNING,
            RunStatus.WAITING_FOR_APPROVAL,
            lease=claimed.lease,
        )
    return child_run_id


async def exercise_surface_role(role_url: str) -> None:
    """Drive every surface-role database path with the role connecting as ``role_url``."""

    clock = FixedClock(NOW)
    bot = _BotApi(clock)
    async with (
        build(
            settings=_application_settings(),
            storage="postgres",
            script=_script(),
            clock=clock,
        ) as composition,
        _api(composition) as api,
    ):
        owner = composition.principal

        async def receipt(update_id: int) -> InboundReceipt:
            async with composition.uow_factory() as uow:
                stored = await uow.surfaces.receipts.get(SURFACE_ID, str(update_id))
            assert stored is not None, update_id
            return stored

        async def reason(update_id: int) -> str | None:
            return (await receipt(update_id)).reason_code

        async def work() -> None:
            worker = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=SystemClock(),
                worker_id="surface-role-run-worker",
            )
            assert await worker.run_once()

        async def status(run_id: UUID) -> RunStatus:
            async with composition.uow_factory() as uow:
                return (await uow.runs.get(run_id, owner)).status

        async def poll(surface: SurfaceWorkerComposition) -> list[tuple[int, str]]:
            # A minute per poll keeps the owner under the per-sender rate window.
            clock.advance(timedelta(minutes=1))
            await surface.worker.run_once()
            return bot.take()

        # First process: registration, schema head, lock, offset, pairing.
        async with _surface_role(role_url, clock, bot) as surface:
            async with composition.uow_factory() as uow:
                registered = await uow.devices.get(SURFACE_ID, owner)
            assert registered.push_provider is None

            unreadable = bot.unreadable()
            unpaired = bot.message("hello")
            assert await poll(surface) == [(OWNER, surface_notice_text("surface.unpaired"))]
            assert bot.webhook_deletions == 1
            assert bot.offsets == [0]
            assert (await receipt(unreadable)).disposition is InboundDisposition.IGNORED_UNREADABLE
            assert await reason(unpaired) == "surface.unpaired"

            issued = await api.post(
                f"/v1/surfaces/{SURFACE_ID}/pairing-codes",
                headers={"Idempotency-Key": "surface-role-pairing"},
                json={"granted_scopes": PAIRING_SCOPES, "label": "Owner phone"},
            )
            assert issued.status_code == 201, issued.text
            wrong = bot.message("/pair NOT-THE-CODE")
            paired = bot.message(f"/pair {issued.json()['code']}")
            group = bot.message("hello, group", chat=GROUP, chat_type="group")
            media = bot.message(None)
            status_command = bot.message("/status")
            help_command = bot.message("/help")
            sent = await poll(surface)
            assert bot.offsets[-1] == unpaired + 1
            assert [await reason(update) for update in (wrong, paired, group, media)] == [
                "surface.pairing_invalid",
                "surface.paired",
                "surface.chat_kind_unsupported",
                "surface.media_unsupported",
            ]
            assert await reason(status_command) == "surface.status"
            assert await reason(help_command) == "surface.help"
            assert sent == [
                (OWNER, surface_notice_text("surface.pairing_invalid")),
                (OWNER, surface_notice_text("surface.paired")),
                (GROUP, surface_notice_text("surface.chat_kind_unsupported")),
                (OWNER, surface_notice_text("surface.media_unsupported")),
                (OWNER, surface_notice_text("surface.status")),
                (OWNER, surface_notice_text("surface.help")),
            ]

            # A second process waits on the poll lock without polling.
            polls = len(bot.offsets)
            async with _surface_role(role_url, clock, bot) as standby:
                assert await standby.worker.run_once() == 0
            assert len(bot.offsets) == polls

        # A restarted process resumes from the committed receipts, now with WhatsApp.
        async with _surface_role(role_url, clock, bot, whatsapp=True) as surface:
            async with composition.uow_factory() as uow:
                whatsapp_surface = await uow.devices.get(WHATSAPP_SURFACE_ID, owner)
            assert whatsapp_surface.platform == PushProvider.WHATSAPP.value
            assert surface.webhook_app is not None
            body, signature = bot.whatsapp_message("wamid.surface-role")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=surface.webhook_app),
                base_url="http://surface.test",
            ) as webhook:
                delivered = await webhook.post(
                    "/webhooks/whatsapp",
                    content=body,
                    headers={"X-Hub-Signature-256": signature},
                )
            assert delivered.status_code == 200
            async with composition.uow_factory() as uow:
                whatsapp_receipt = await uow.surfaces.receipts.get(
                    WHATSAPP_SURFACE_ID, "wamid.surface-role"
                )
            assert whatsapp_receipt is not None
            assert whatsapp_receipt.reason_code == "surface.unpaired"
            assert bot.whatsapp_sent == [(WHATSAPP_SENDER, surface_notice_text("surface.unpaired"))]

            # Five wrong codes lock a stranger out; the lock is read, then cleared.
            attempts = [bot.message("/pair 000000", chat=STRANGER) for _ in range(5)]
            locked = bot.message("/pair 000000", chat=STRANGER)
            await poll(surface)
            assert bot.offsets[-1] == help_command + 1
            assert await reason(attempts[-1]) == "surface.pairing_locked"
            assert await reason(locked) == "surface.pairing_locked"
            clock.advance(timedelta(hours=1))
            released = bot.message("hello again", chat=STRANGER)
            await poll(surface)
            assert await reason(released) == "surface.unpaired"

            # A question, the answer as input, an approval, and the reply.
            asked = bot.message("Please note my colour.")
            await poll(surface)
            first = await receipt(asked)
            assert first.disposition is InboundDisposition.SUBMITTED, first
            assert first.run_id is not None
            await work()
            assert await status(first.run_id) is RunStatus.WAITING_FOR_USER
            [(chat, question)] = await poll(surface)
            assert chat == OWNER
            assert question.startswith("Which colour should the note use?\nID: "), question
            answer = bot.message("Blue.")
            await poll(surface)
            assert (await receipt(answer)).disposition is InboundDisposition.INPUT_DELIVERED
            await work()
            assert await status(first.run_id) is RunStatus.WAITING_FOR_APPROVAL
            busy = bot.message("Anything else?")
            # The drain runs before the poll: the prompt, then the refusal.
            [(_, prompt), still_working] = await poll(surface)
            assert prompt.startswith("Approval needed\n"), prompt
            assert still_working == (OWNER, surface_notice_text("surface.active_run"))
            assert await reason(busy) == "surface.active_run"
            [offer] = [line for line in prompt.splitlines() if line.startswith("/approve ")]
            approve = bot.message(" ".join(offer.split()[:2]))
            await poll(surface)
            assert await reason(approve) == "surface.approval_resolved"
            await work()
            assert await status(first.run_id) is RunStatus.COMPLETED
            assert await poll(surface) == [(OWNER, "Noted in blue.")]

            # A new conversation, and a denial by the approval's full identifier.
            rotated = bot.message("/new")
            second_ask = bot.message("Write a second note.")
            await poll(surface)
            assert await reason(rotated) == "surface.session_rotated"
            second = await receipt(second_ask)
            assert second.run_id is not None and second.session_id != first.session_id
            await work()
            [(_, prompt)] = await poll(surface)
            assert prompt.startswith("Approval needed\n"), prompt
            async with composition.uow_factory() as uow:
                [pending] = await uow.approvals.list_pending(owner, limit=10)
            deny = bot.message(f"/deny {pending.id}")
            await poll(surface)
            assert await reason(deny) == "surface.approval_resolved"
            await work()
            assert await status(second.run_id) is RunStatus.COMPLETED
            assert await poll(surface) == [(OWNER, "Left it unwritten.")]

            # /stop in each state a run can be in when the owner sends it. A fresh
            # conversation first: a session holding a denied call cannot yet take
            # another message (its history projection rejects the denial event).
            bot.message("/new")
            queued_ask = bot.message("Queue this.")
            stop_queued = bot.message("/stop")
            nothing = bot.message("/stop")
            await poll(surface)
            queued = await receipt(queued_ask)
            assert queued.run_id is not None
            assert await reason(stop_queued) == "surface.run_stopped"
            assert await reason(nothing) == "surface.no_active_run"
            assert await status(queued.run_id) is RunStatus.CANCELLED

            waiting_ask = bot.message("Ask me something.")
            await poll(surface)
            waiting = await receipt(waiting_ask)
            assert waiting.run_id is not None
            await work()
            assert await status(waiting.run_id) is RunStatus.WAITING_FOR_USER
            await poll(surface)
            stop_waiting = bot.message("/stop")
            await poll(surface)
            assert await reason(stop_waiting) == "surface.run_stopped"
            assert await status(waiting.run_id) is RunStatus.CANCELLED

            parked_ask = bot.message("Write a third note.")
            await poll(surface)
            parked = await receipt(parked_ask)
            assert parked.run_id is not None
            await work()
            assert await status(parked.run_id) is RunStatus.WAITING_FOR_APPROVAL
            await poll(surface)
            stop_parked = bot.message("/stop")
            await poll(surface)
            assert await reason(stop_parked) == "surface.run_stopped"
            assert await status(parked.run_id) is RunStatus.CANCELLED

            # Stopping a parent parked for approval also ends its delegated child.
            delegating_ask = bot.message("Split this up.")
            await poll(surface)
            delegating = await receipt(delegating_ask)
            assert delegating.run_id is not None
            child_run_id = await _park_with_delegated_child(composition, delegating.run_id)
            stop_delegating = bot.message("/stop")
            await poll(surface)
            assert await reason(stop_delegating) == "surface.run_stopped"
            assert await status(delegating.run_id) is RunStatus.CANCELLED
            assert await status(child_run_id) is RunStatus.CANCELLED
            async with composition.uow_factory() as uow:
                [ledger] = await uow.delegations.get_for_parent_run(delegating.run_id)
            assert ledger.status is DelegationStatus.CANCELLED

            running_ask = bot.message("Take your time.")
            await poll(surface)
            running = await receipt(running_ask)
            assert running.run_id is not None
            async with composition.uow_factory() as uow:
                assert uow.queue is not None
                claimed = await uow.queue.claim("surface-role-claimant", (0, 10))
            assert claimed is not None and claimed.run.id == running.run_id
            stop_running = bot.message("/stop")
            await poll(surface)
            assert await reason(stop_running) == "surface.run_stopped"
            async with composition.uow_factory() as uow:
                stopping = await uow.runs.get(running.run_id, owner)
            assert stopping.cancel_requested_at is not None

            # An idle conversation rotates to a new session on the next message.
            clock.advance(timedelta(days=2))
            idle_ask = bot.message("Good morning.")
            await poll(surface)
            idle = await receipt(idle_ask)
            assert idle.disposition is InboundDisposition.SUBMITTED, idle
            assert idle.session_id not in {first.session_id, second.session_id}

        async with composition.uow_factory() as uow:
            route = await uow.devices.get(SURFACE_ID, owner)
        assert route.push_provider is PushProvider.TELEGRAM


async def test_the_surface_role_runs_every_path_under_its_documented_grants() -> None:
    async with release_surface_role() as role_url:
        await exercise_surface_role(role_url)


async def test_the_documented_grants_satisfy_the_release_check() -> None:
    async with release_surface_role() as role_url:
        role = await inspect_database_role(role_url)

    assert permission_failures(role) == []


async def test_the_surface_role_cannot_read_what_it_does_not_need() -> None:
    async with release_surface_role() as role_url:
        engine = create_engine(role_url)
        try:
            for table in ("memories", "email_records", "browser_profiles"):
                with pytest.raises(DBAPIError, match=f"permission denied for table {table}"):
                    async with engine.connect() as connection:
                        await connection.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        finally:
            await engine.dispose()
