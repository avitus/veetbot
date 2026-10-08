"""Milestone 31 suspected spam (ADR-0165): a flag never moves mail; the owner's tap does."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.api import create_app
from agent_core.application.email import save_value
from agent_core.bootstrap import Composition, build
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailAccount, EmailTask, EmailThread
from agent_core.domain.errors import ConflictError
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.policies import PolicyDecisionType
from agent_core.domain.runs import RunStatus
from tests.contract.memory_fixtures import browse_query
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import MailboxFactory, _current_mail_factory, _page
from tests.gates.test_email_unsubscribe_gmail_m31 import (
    Header,
    Mailbox,
    _call,
    _output,
    _server,
)
from tests.unit.test_email_refresh_bulk_formation import _mail, _people_assessment

EVIDENCE = "Please approve the board materials."
SENDER_DOMAIN = "example.test"


def _settings(*, enabled: bool = True, people: bool = False) -> Any:
    return replace(
        _email_settings(),
        email_mode_enabled=True,
        email_unsubscribe_enabled=enabled,
        people_enabled=people,
    )


def _assessment(**changes: Any) -> dict[str, Any]:
    """Grounded importance features for the seeded conversation, as refresh saves them."""
    value: dict[str, Any] = {
        "summary": "Board request",
        "reason": "Direct request",
        "topics": ["board"],
        "content_importance": 1,
        "relationship_importance": 0,
        "urgency": 0,
        "needs_reply": True,
        "bulk": False,
        "spam": True,
        "supported_evidence": [EVIDENCE],
        "grounded": True,
        "profile_revision": 0,
    }
    value.update(changes)
    return value


async def _seed(app: Composition, page: dict[str, Any] | None = None) -> EmailThread:
    """Import the scripted conversation into the configured default account."""
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
    return await app.services.email.import_thread(principal, "default", page or _page(), session)


async def _profile_revision(app: Composition) -> int:
    return (await app.services.email.learning(app.principal)).profile_revision


async def _save(app: Composition, thread: EmailThread, **changes: Any) -> dict[str, Any]:
    await app.services.email.save_assessment(
        app.principal,
        thread.id,
        thread.revision,
        _assessment(profile_revision=await _profile_revision(app), **changes),
    )
    return await app.services.email.thread(app.principal, thread.id)


@asynccontextmanager
async def _client(app: Composition) -> AsyncIterator[httpx.AsyncClient]:
    api = create_app(
        app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
    ) as client:
        yield client


async def _ids(client: httpx.AsyncClient, view: str) -> list[str]:
    response = await client.get("/v1/email/threads", params={"view": view, "limit": 100})
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def _no_mailbox_action(app: Composition, writes: list[dict[str, Any]]) -> None:
    """Nothing was written to Gmail, approved, or queued as an owner gesture."""
    assert writes == []
    async with app.uow_factory() as uow:
        tasks = await uow.email.list(app.principal, "task")
    assert [row.payload["kind"] for row in tasks if row.payload["kind"] != "refresh"] == []
    assert not [row for row in tasks if row.payload.get("archive_consent") is not None]


def _recording(base: MailboxFactory, writes: list[dict[str, Any]]) -> MailboxFactory:
    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "modify_labels":
                writes.append(arguments)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def _assert_the_flag_demotes_without_touching_gmail() -> None:
    writes: list[dict[str, Any]] = []
    factory = _recording(await _current_mail_factory(), writes)
    async with build(settings=_settings(), mcp_client_factory=factory) as app:
        thread = await _seed(app)
        detail = await _save(app, thread)
        assert detail["suspected_spam"] is True
        assert detail["spam_cleared"] is False
        assert detail["priority"] == 0.0 and detail["needs_reply"] is False
        async with _client(app) as client:
            assert str(thread.id) not in await _ids(client, "priority")
            other = await client.get("/v1/email/threads", params={"view": "other"})
            [row] = [item for item in other.json()["items"] if item["id"] == str(thread.id)]
            assert row["suspected_spam"] is True and row["in_spam"] is False
            # Explicit owner feedback applies after the flag and still wins.
            feedback = await client.post(
                "/v1/email/feedback",
                json={"thread_id": str(thread.id), "target": "thread", "judgment": "important"},
            )
            assert feedback.status_code == 200, feedback.text
            assert str(thread.id) in await _ids(client, "priority")
            await client.delete(f"/v1/email/feedback/{feedback.json()['feedback_id']}")
            assert str(thread.id) not in await _ids(client, "priority")
        async with app.uow_factory() as uow:
            stored = await uow.email.get(app.principal, "assessment", str(thread.id))
        assert stored is not None and stored.payload["suspected_spam"] is True
        await _no_mailbox_action(app, writes)


async def _assert_only_a_grounded_verdict_on_an_unprotected_sender_flags() -> None:
    async with build(settings=_settings(), mcp_client_factory=await _current_mail_factory()) as app:
        thread = await _seed(app)
        assert (await _save(app, thread, grounded=False))["suspected_spam"] is False
        assert (await _save(app, thread, spam=False))["suspected_spam"] is False
        # The owner wrote to this sender inside the window: content alone never flags.
        reply = _page(changed=True)
        reply["messages"][1]["to"] = "Colleague <colleague@example.test>"
        session = await app.sessions.create()
        thread = await app.services.email.import_thread(app.principal, "default", reply, session)
        assert (await _save(app, thread))["suspected_spam"] is False
        # A failed Gmail sender check on the newest received message removes protection.
        failed = json.loads(json.dumps(reply))
        failed["messages"].append(
            {
                **reply["messages"][0],
                "id": "m3",
                "internal_date": 1789128002000,
                "history_id": "102",
                "sender_check": "fail",
            }
        )
        failed["history_id"] = "102"
        thread = await app.services.email.import_thread(app.principal, "default", failed, session)
        assert (await _save(app, thread))["suspected_spam"] is True
    async with build(
        settings=_settings(enabled=False), mcp_client_factory=await _current_mail_factory()
    ) as app:
        thread = await _seed(app)
        assert (await _save(app, thread))["suspected_spam"] is False


async def _assert_clearing_restores_importance_and_sticks() -> None:
    writes: list[dict[str, Any]] = []
    factory = _recording(await _current_mail_factory(), writes)
    async with build(settings=_settings(), mcp_client_factory=factory) as app:
        thread = await _seed(app)
        await _save(app, thread)
        async with _client(app) as client:
            path = f"/v1/email/threads/{thread.id}/spam-flag"
            assert (await client.post(path, json={"cleared": "yes"})).status_code == 400
            cleared = await client.post(path, json={"cleared": True})
            assert cleared.status_code == 200, cleared.text
            assert cleared.headers["cache-control"] == "private, no-store"
            body = cleared.json()
            assert body["suspected_spam"] is False and body["spam_cleared"] is True
            assert body["revision"] == thread.revision
            assert str(thread.id) in await _ids(client, "priority")
            # Later assessments of a cleared thread never flag it again.
            assert (await _save(app, thread))["suspected_spam"] is False
            assert str(thread.id) in await _ids(client, "priority")
            restored = await client.post(path, json={"cleared": False})
            assert restored.status_code == 200 and restored.json()["spam_cleared"] is False
            assert (await _save(app, thread))["suspected_spam"] is True
            missing = await client.post(
                f"/v1/email/threads/{UUID(int=7)}/spam-flag", json={"cleared": True}
            )
            assert missing.status_code == 404
        await _no_mailbox_action(app, writes)


async def _assert_refresh_flags_drafts_nothing_and_forms_nothing() -> None:
    writes: list[dict[str, Any]] = []
    turn = json.loads(_people_assessment(bulk=False).text or "{}")
    turn.update(spam=True, needs_reply=True, content_importance=1)
    from agent_core.domain.messages import ScriptedTurn

    async with build(
        settings=_settings(people=True),
        # One assessment and nothing else: a draft request would exhaust the script.
        script=FakeModelScript(turns=[ScriptedTurn(text=json.dumps(turn))]),
        mcp_client_factory=_recording(await _mail(census=False), writes),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.model_call_count == 1
        listed = await app.services.email.threads(app.principal, view="all")
        [row] = cast(list[dict[str, Any]], listed["items"])
        assert row["suspected_spam"] is True and row["draft_id"] is None
        async with app.uow_factory() as uow:
            memories = await uow.memories.browse(
                browse_query(
                    tenant_id=app.principal.tenant_id, principal_id=app.principal.principal_id
                )
            )
            drafts = await uow.email.list(app.principal, "draft")
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        assert memories == [] and drafts == []
        skipped = [event for event in events if event.event_type == "email.semantic.skipped"]
        assert [event.payload["reason"] for event in skipped] == ["spam_assessment"]
        assert not any(event.event_type == "approval.requested" for event in events)
        await _no_mailbox_action(app, writes)


async def test_suspected_spam_is_a_flag_never_a_mailbox_action() -> None:
    """gate.email.spam_flag."""
    await _assert_the_flag_demotes_without_touching_gmail()
    await _assert_only_a_grounded_verdict_on_an_unprotected_sender_flags()
    await _assert_clearing_restores_importance_and_sticks()
    await _assert_refresh_flags_drafts_nothing_and_forms_nothing()


_RESULTS = ("pass", "fail", "none", "temperror", "permerror", "bestguesspass", "softfail")


def _gmail_verdict(dmarc: str | None, *, authserv: str = "mx.google.com", fold: bool) -> str:
    """One Authentication-Results value in the shape Gmail prepends to received mail."""
    results = [
        authserv,
        # Sender-influenced text inside comments and quoted strings is never a result.
        f"spf=pass (google.com: domain of x@{SENDER_DOMAIN}; dmarc=pass) "
        f"smtp.mailfrom=x@{SENDER_DOMAIN}",
        f'dkim=pass header.i=@{SENDER_DOMAIN} header.s="s1; dmarc=fail"',
    ]
    if dmarc is not None:
        results.append(f"dmarc={dmarc} (p=NONE sp=NONE dis=NONE) header.from={SENDER_DOMAIN}")
    return (";\r\n       " if fold else "; ").join(results)


@st.composite
def _sender_checks(draw: st.DrawFn) -> tuple[list[Header], str]:
    fold = draw(st.booleans())
    gmail = draw(st.sampled_from(("absent", "no_dmarc", *_RESULTS)))
    verdicts: list[str] = []
    if draw(st.booleans()):
        # Another service's verdict above Gmail's is skipped, never trusted.
        verdicts.append(
            _gmail_verdict(draw(st.sampled_from(_RESULTS)), authserv="inbound.test", fold=fold)
        )
    if gmail != "absent":
        verdicts.append(_gmail_verdict(None if gmail == "no_dmarc" else gmail, fold=fold))
        if draw(st.booleans()):
            # Gmail prepends its verdict, so a sender's forged copy is always lower.
            verdicts.append(_gmail_verdict(draw(st.sampled_from(_RESULTS)), fold=fold))
    if draw(st.booleans()):
        verdicts.append(
            _gmail_verdict(
                draw(st.sampled_from(_RESULTS)), authserv="mx.google.com.example.test", fold=fold
            )
        )
    headers: list[Header] = [
        ("From", f"Sender <sender@{SENDER_DOMAIN}>"),
        ("To", "owner@example.test"),
        ("Subject", "Hello"),
        ("Date", "Mon, 14 Sep 2026 09:00:00 +0000"),
        *(("Authentication-Results", value) for value in verdicts),
    ]
    expected = gmail if gmail in {"pass", "fail"} else "none"
    return headers, expected


@given(case=_sender_checks())
@hypothesis_settings(max_examples=200, deadline=None, derandomize=True)
def test_sender_check_is_gmails_own_verdict(case: tuple[list[Header], str]) -> None:
    """gate.email.spam_sender_check."""
    headers, expected = case

    async def exercise() -> dict[str, Any]:
        mailbox = Mailbox()
        mailbox.add("thread-1", "message-1", headers)
        page = _output(
            await _call(
                _server(mailbox), "get_thread_page", {"thread_id": "thread-1", "max_messages": 5}
            )
        )
        return page

    page = asyncio.run(exercise())
    [message] = page["messages"]
    assert message["sender_check"] == expected


async def test_sender_check_is_metadata_outside_the_content_identity() -> None:
    """gate.email.spam_sender_check: the check renews neither revision nor fingerprint."""
    async with build(settings=_settings(), mcp_client_factory=await _current_mail_factory()) as app:
        legacy = _page()
        assert "sender_check" not in legacy["messages"][0]
        thread = await _seed(app, legacy)
        assert thread.messages[0].sender_check == "none"
        checked = json.loads(json.dumps(legacy))
        checked["messages"][0]["sender_check"] = "fail"
        session = await app.sessions.create()
        again = await app.services.email.import_thread(app.principal, "default", checked, session)
        assert again.revision == thread.revision
        assert again.source_fingerprint == thread.source_fingerprint
        assert again.messages[0].sender_check == "fail"
    async with build(
        settings=_settings(), mcp_client_factory=await _current_mail_factory()
    ) as fresh:
        imported = await _seed(fresh, checked)
        assert imported.source_fingerprint == thread.source_fingerprint


def _spam_factory(
    base: MailboxFactory, writes: list[dict[str, Any]], *, result: str = "success"
) -> MailboxFactory:
    """A provider whose labels follow the confirmed writes, as Gmail's do."""
    from agent_core.domain.errors import MCPTransportError

    labels = ["INBOX"]

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "modify_labels":
                writes.append(arguments)
                if result == "disconnect":
                    raise MCPTransportError()
                for label in arguments.get("remove_label_ids") or []:
                    labels.remove(label) if label in labels else None
                labels.extend(arguments.get("add_label_ids") or [])
                receipt = {key: value or [] for key, value in arguments.items()}
                return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)
            if name == "get_thread_page":
                value = _page()
                value["messages"][0]["label_ids"] = list(labels)
                return MCPCallResult(content=(json.dumps(value),), structured=value)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


async def test_thread_report_spam_is_an_exact_owner_gesture() -> None:
    """gate.email.spam_thread_gesture."""
    writes: list[dict[str, Any]] = []
    factory = _spam_factory(await _current_mail_factory(), writes)
    async with build(settings=_settings(), mcp_client_factory=factory) as app:
        thread = await _seed(app)
        await _save(app, thread)

        async def exhausted(*args: object, **kwargs: object) -> None:
            raise AssertionError("an owner's spam report must not consult automatic dollars")

        vars(app.services.email)["_check_budget"] = exhausted
        async with _client(app) as client:
            [account] = (await client.get("/v1/email/accounts")).json()["items"]
            assert account["spam_supported"] is True
            path = f"/v1/email/threads/{thread.id}/spam"
            not_spam = {"expected_revision": thread.revision, "spam": False, "idempotency_key": "n"}
            assert (await client.post(path, json=not_spam)).status_code == 409
            for invalid in (
                {"expected_revision": thread.revision, "spam": "yes", "idempotency_key": "k"},
                {"expected_revision": thread.revision, "spam": True},
                {
                    "expected_revision": thread.revision,
                    "spam": True,
                    "idempotency_key": "k",
                    "labels": ["TRASH"],
                },
            ):
                assert (await client.post(path, json=invalid)).status_code == 400
            stale = {"expected_revision": thread.revision + 1, "spam": True, "idempotency_key": "s"}
            assert (await client.post(path, json=stale)).status_code == 409
            body = {"expected_revision": thread.revision, "spam": True, "idempotency_key": "r1"}
            result = await client.post(path, json=body)
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "private, no-store"
            replay = await client.post(path, json=body)
            assert replay.status_code == 200 and replay.json()["replayed"]
            assert replay.json()["run_id"] == result.json()["run_id"]
            run = await app.runs.get(UUID(result.json()["run_id"]))
            assert run.status is RunStatus.COMPLETED, run.failure
            assert run.model_call_count == 0
            reported = await app.services.email.thread(app.principal, thread.id)
            assert reported["in_inbox"] is False and reported["in_spam"] is True
            assert reported["revision"] == thread.revision
            operation = reported["archive_operation"]
            assert isinstance(operation, dict)
            assert operation["status"] == "completed"
            assert operation["target_archived"] is True and operation["target_spam"] is True
            assert str(thread.id) in await _ids(client, "other")
            again = {"expected_revision": thread.revision, "spam": True, "idempotency_key": "r2"}
            assert (await client.post(path, json=again)).status_code == 409
            async with app.uow_factory() as uow:
                task = await uow.email.get(app.principal, "task", str(run.id))
                events = await uow.events.list_after(run.session_id, 0, app.principal)
                invocations = await uow.invocations.list_for_run(run.id, app.principal)
                [write] = [i for i in invocations if i.tool_name.endswith("modify_labels")]
                approval = await uow.approvals.get_by_action(write.id)
            assert approval is not None and approval.status is ApprovalStatus.APPROVED
            assert approval.policy_decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
            assert task is not None
            consent = EmailTask.model_validate(task.payload).archive_consent
            assert consent is not None and consent.spam is True
            [gesture] = [e for e in events if e.event_type == "email.archive.requested"]
            assert gesture.actor_type == "principal"
            assert gesture.payload["spam"] is True and gesture.payload["archived"] is True
            requested = [e for e in events if e.event_type == "approval.requested"]
            resolved = [e for e in events if e.event_type == "approval.resolved"]
            assert len(requested) == len(resolved) == 1
            assert requested[0].sequence < resolved[0].sequence
            assert consent.expires_at - run.created_at <= timedelta(seconds=120)
            not_spam = {
                "expected_revision": thread.revision,
                "spam": False,
                "idempotency_key": "n1",
            }
            undone = await client.post(path, json=not_spam)
            assert undone.status_code == 200, undone.text
            restored = await app.services.email.thread(app.principal, thread.id)
            assert restored["in_inbox"] is True and restored["in_spam"] is False
            assert restored["spam_cleared"] is True and restored["suspected_spam"] is False
            foreign = await client.post(f"/v1/email/threads/{UUID(int=9)}/spam", json=body)
            assert foreign.status_code == 404
    assert writes == [
        {"thread_ids": ["thread-1"], "add_label_ids": ["SPAM"], "remove_label_ids": ["INBOX"]},
        {"thread_ids": ["thread-1"], "add_label_ids": ["INBOX"], "remove_label_ids": ["SPAM"]},
    ]


async def test_plain_archive_consent_and_gesture_are_unchanged_by_the_spam_marker() -> None:
    """gate.email.spam_thread_gesture: an archive's consent digest is the pre-spam digest."""
    writes: list[dict[str, Any]] = []
    async with build(
        settings=_settings(),
        mcp_client_factory=_spam_factory(await _current_mail_factory(), writes),
    ) as app:
        thread = await _seed(app)
        operation = await app.services.email.archive(
            app.principal, thread.id, thread.revision, archived=True, idempotency_key="a"
        )
        async with app.uow_factory() as uow:
            task = await uow.email.get(app.principal, "task", str(operation.run_id))
            run = await app.runs.get(operation.run_id)
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        assert task is not None
        consent = EmailTask.model_validate(task.payload).archive_consent
        assert consent is not None and consent.spam is False
        [gesture] = [e for e in events if e.event_type == "email.archive.requested"]
        assert set(gesture.payload) == {"task_id", "thread_id", "archived", "consent_digest"}
        legacy = consent.model_dump_json(exclude={"spam"})
        assert gesture.payload["consent_digest"] == hashlib.sha256(legacy.encode()).hexdigest()
        assert writes == [
            {"thread_ids": ["thread-1"], "add_label_ids": None, "remove_label_ids": ["INBOX"]}
        ]


async def test_thread_spam_conflicts_with_a_pending_archive_and_never_resends() -> None:
    """gate.email.spam_thread_gesture: one Inbox operation at a time; uncertainty is final."""
    writes: list[dict[str, Any]] = []
    async with build(
        settings=_settings(),
        mcp_client_factory=_spam_factory(await _current_mail_factory(), writes),
    ) as app:
        thread = await _seed(app)
        approve = app.services.email.approve_archive
        conflicts: list[str] = []

        async def interleave(owner: Any, run: Any, lease: Any, approval_id: Any) -> None:
            try:
                await app.services.email.report_thread_spam(
                    owner, thread.id, thread.revision, spam=True, idempotency_key="during"
                )
            except ConflictError as error:
                conflicts.append(str(error))
            await approve(owner, run, lease, approval_id)

        vars(app.services.email)["approve_archive"] = interleave
        await app.services.email.archive(
            app.principal, thread.id, thread.revision, archived=True, idempotency_key="first"
        )
        assert len(conflicts) == 1 and "Inbox operation" in conflicts[0]
    writes.clear()
    async with build(
        settings=_settings(),
        mcp_client_factory=_spam_factory(
            await _current_mail_factory(), writes, result="disconnect"
        ),
    ) as app:
        thread = await _seed(app)
        operation = await app.services.email.report_thread_spam(
            app.principal, thread.id, thread.revision, spam=True, idempotency_key="lost"
        )
        latest = await app.services.email.thread(app.principal, thread.id)
        assert isinstance(latest["archive_operation"], dict)
        assert latest["archive_operation"]["status"] == "uncertain"
        assert latest["in_inbox"] is True and latest["in_spam"] is False
        replay = await app.services.email.report_thread_spam(
            app.principal, thread.id, thread.revision, spam=True, idempotency_key="lost"
        )
        assert replay.replayed and replay.run_id == operation.run_id
        with pytest.raises(ConflictError, match="uncertain"):
            await app.services.email.report_thread_spam(
                app.principal, thread.id, thread.revision, spam=True, idempotency_key="new"
            )
        assert len(writes) == 1


async def test_thread_spam_requires_current_write_authority() -> None:
    """gate.email.spam_thread_gesture: support and admission follow the account's authority."""
    writes: list[dict[str, Any]] = []
    async with build(
        settings=_settings(),
        mcp_client_factory=_spam_factory(await _current_mail_factory(), writes),
    ) as app:
        thread = await _seed(app)
        limited = app.principal.model_copy(
            update={"scopes": app.principal.scopes - {"mcp.gmail_write.use"}}
        )
        api = create_app(
            app.services, app.settings, limited, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
        ) as client:
            [account] = (await client.get("/v1/email/accounts")).json()["items"]
            assert account["spam_supported"] is False
            body = {"expected_revision": thread.revision, "spam": True, "idempotency_key": "k"}
            denied = await client.post(f"/v1/email/threads/{thread.id}/spam", json=body)
            assert denied.status_code == 403
    assert writes == []
