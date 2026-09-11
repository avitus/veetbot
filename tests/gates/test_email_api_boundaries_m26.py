"""HTTP command matrix for Email, including denied and lost-response retries."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest

from agent_core.api import create_app
from agent_core.bootstrap import Composition
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_experience_m26 import email_client, seed_mail


@dataclass(frozen=True)
class Command:
    name: str
    method: str
    path: str
    scope: str
    body: dict[str, object] | None = None
    account_bound: bool = True


COMMANDS = (
    Command("accounts", "GET", "/v1/email/accounts", "email.read", account_bound=False),
    Command("inbox", "GET", "/v1/email/threads", "email.read", account_bound=False),
    Command("thread", "GET", "/v1/email/threads/{t}", "email.read"),
    Command(
        "feedback",
        "POST",
        "/v1/email/feedback",
        "email.write",
        {"thread_id": "{t}", "target": "thread", "judgment": "important"},
    ),
    Command("undo", "DELETE", "/v1/email/feedback/{f}", "email.write"),
    Command("draft", "GET", "/v1/email/drafts/{d}", "email.read"),
    Command(
        "edit",
        "PUT",
        "/v1/email/drafts/{d}",
        "email.write",
        {
            "expected_revision": 1,
            "to": ["ceo@example.com"],
            "subject": "Re: Board materials",
            "body": "I will review the materials today.",
        },
    ),
    Command("revisions", "GET", "/v1/email/drafts/{d}/revisions", "email.read"),
    Command("refresh", "POST", "/v1/email/refresh", "email.write", account_bound=False),
    Command("operation", "GET", "/v1/email/operations/{o}", "email.read", account_bound=False),
    Command("learning", "GET", "/v1/email/learning", "email.read", account_bound=False),
    Command("pause", "PUT", "/v1/email/learning", "email.write", {"paused": True}, False),
    Command("reset", "POST", "/v1/email/learning/reset", "email.write", {"scope": "all"}, False),
    Command("generate", "POST", "/v1/email/threads/{t}/drafts", "email.write", {}),
    Command(
        "send",
        "POST",
        "/v1/email/drafts/{d}/send-proposal",
        "email.write",
        {"expected_revision": 1},
    ),
    Command("discuss", "POST", "/v1/email/threads/{t}/discussion", "email.write"),
    Command(
        "dismiss", "POST", "/v1/email/threads/{t}/dismiss", "email.write", {"expected_revision": 1}
    ),
    Command("discard", "DELETE", "/v1/email/drafts/{d}?expected_revision=1", "email.write"),
    Command(
        "exclude", "POST", "/v1/email/threads/{t}/exclude", "email.write", {"expected_revision": 1}
    ),
    Command(
        "endorse",
        "POST",
        "/v1/email/drafts/{d}/style-example",
        "email.write",
        {"expected_revision": 1},
    ),
)


async def _request(
    app: Composition, client: httpx.AsyncClient, command: Command
) -> tuple[str, dict[str, object] | None]:
    thread, draft = await seed_mail(app)

    async def dispatch(run_id: UUID) -> None:
        return None

    app.services.email.dispatch = dispatch
    identities = {"t": thread.id, "d": draft.id, "f": uuid4(), "o": uuid4()}
    if command.name == "undo":
        response = await client.post(
            "/v1/email/feedback",
            json={"thread_id": str(thread.id), "target": "thread", "judgment": "important"},
        )
        assert response.status_code == 200
        identities["f"] = UUID(response.json()["feedback_id"])
    if command.name == "operation":
        response = await client.post("/v1/email/refresh")
        assert response.status_code == 200
        identities["o"] = UUID(response.json()["operation_id"])
    body = (
        None
        if command.body is None
        else {
            key: value.format(**identities) if isinstance(value, str) else value
            for key, value in command.body.items()
        }
    )
    return command.path.format(**identities), body


async def test_every_email_route_has_a_scoped_http_command_matrix() -> None:
    async with email_client() as (app, _):
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        schema = api.openapi()
        actual = {
            (method.upper(), path, route["required_scope"])
            for path, methods in schema["paths"].items()
            if path.startswith("/v1/email/")
            for method, route in methods.items()
        }
        expanded = {
            "{t}": "{thread_id}",
            "{d}": "{draft_id}",
            "{f}": "{feedback_id}",
            "{o}": "{operation_id}",
        }
        expected = set()
        for command in COMMANDS:
            path = command.path.split("?")[0]
            for short, full in expanded.items():
                path = path.replace(short, full)
            expected.add((command.method, path, command.scope))
        assert actual == expected


async def _deny_missing_scope_before_mutation(command: Command) -> None:
    async with email_client() as (app, client):
        path, body = await _request(app, client, command)
        async with app.uow_factory() as uow:
            before = await uow.email.list(app.principal, "thread")
            tasks = await uow.email.list(app.principal, "task")
        app.principal.scopes.remove(command.scope)
        response = await client.request(command.method, path, json=body)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "private, no-store"
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "thread") == before
            assert await uow.email.list(app.principal, "task") == tasks


async def _succeed_and_retry_without_duplicate_actions(command: Command) -> None:
    async with email_client() as (app, client):
        path, body = await _request(app, client, command)
        headers = {"Idempotency-Key": f"retry-{command.name}"}
        first = await client.request(command.method, path, json=body, headers=headers)
        assert first.status_code == 200, first.text
        second = await client.request(command.method, path, json=body, headers=headers)
        assert second.status_code == 200, second.text
        for response in (first, second):
            assert response.headers["cache-control"] == "private, no-store"
        if command.name in {"refresh", "generate", "send"}:
            assert first.json()["run_id"] == second.json()["run_id"]
        if command.name in {"feedback", "discuss", "edit", "endorse", "undo", "discard"}:
            assert first.json() == second.json()
        async with app.uow_factory() as uow:
            assert len(await uow.email.list(app.principal, "task")) <= 1


async def _recheck_revoked_gmail_authority(command: Command) -> None:
    async with email_client() as (app, client):
        path, body = await _request(app, client, command)
        app.principal.scopes.remove("mcp.gmail_read.use")
        response = await client.request(command.method, path, json=body)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "private, no-store"


async def _reject_extra_fields(command: Command) -> None:
    async with email_client() as (app, client):
        path, body = await _request(app, client, command)
        response = await client.request(
            command.method, path, json={**(body or {}), "unexpected": True}
        )
        assert response.status_code == 400
        assert response.headers["cache-control"] == "private, no-store"


async def _isolate_principal_identifiers(command: Command) -> None:
    async with email_client() as (app, owner):
        path, body = await _request(app, owner, command)
        foreign = app.principal.model_copy(update={"principal_id": "other-owner"})
        api = create_app(
            app.services, app.settings, foreign, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
        ) as client:
            response = await client.request(command.method, path, json=body)
        assert response.status_code == 404
        assert "Board materials" not in response.text
        assert response.headers["cache-control"] == "private, no-store"


async def _validate_path_identifiers(command: Command) -> None:
    async with email_client() as (app, client):
        _, body = await _request(app, client, command)
        path = command.path.format(t="invalid", d="invalid", f="invalid", o="invalid")
        response = await client.request(command.method, path, json=body)
        assert response.status_code == 400
        assert response.headers["cache-control"] == "private, no-store"


async def _preserve_current_mail_on_revision_conflict(command: Command) -> None:
    async with email_client() as (app, client):
        path, body = await _request(app, client, command)
        if command.name == "discard":
            path = path.replace("expected_revision=1", "expected_revision=2")
        else:
            body = {**(body or {}), "expected_revision": 2}
        response = await client.request(command.method, path, json=body)
        assert response.status_code == 409
        assert response.headers["cache-control"] == "private, no-store"
        async with app.uow_factory() as uow:
            assert await uow.email.list(app.principal, "task") == []
            [thread] = await uow.email.list(app.principal, "thread")
            [draft] = await uow.email.list(app.principal, "draft")
            assert thread.payload["revision"] == draft.payload["revision"] == 1
            assert draft.payload["status"] == "ready"


async def _reject_reused_idempotency_key(name: str) -> None:
    command = next(item for item in COMMANDS if item.name == name)
    async with email_client() as (app, client):
        path, body = await _request(app, client, command)
        headers = {"Idempotency-Key": "same-request"}
        first = await client.request(command.method, path, json=body, headers=headers)
        assert first.status_code == 200
        changes: dict[str, dict[str, object]] = {
            "feedback": {"judgment": "less_important"},
            "edit": {"body": "A different response."},
            "generate": {"instruction": "Make this shorter."},
            "send": {"expected_revision": 2},
        }
        updated = {**(body or {}), **changes[name]}
        response = await client.request(command.method, path, json=updated, headers=headers)
        assert response.status_code == 409


async def _retry_durable_operation_after_dispatch_failure(name: str) -> None:
    command = next(item for item in COMMANDS if item.name == name)
    async with email_client() as (app, owner):
        path, body = await _request(app, owner, command)
        calls: list[UUID] = []

        async def unavailable(run_id: UUID) -> None:
            calls.append(run_id)
            raise RuntimeError("dispatcher temporarily unavailable")

        app.services.email.dispatch = unavailable
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
            base_url="http://agent.test",
        ) as client:
            headers = {"Idempotency-Key": "lost-dispatch-response"}
            first = await client.request(command.method, path, json=body, headers=headers)
            assert first.status_code == 500
            assert first.headers["cache-control"] == "private, no-store"
            assert "dispatcher temporarily unavailable" not in first.text
            retry = await client.request(command.method, path, json=body, headers=headers)
            assert retry.status_code == 200
            assert retry.json()["run_id"] == str(calls[0])
            assert retry.json()["status"] == RunStatus.QUEUED.value
        async with app.uow_factory() as uow:
            assert len(await uow.email.list(app.principal, "task")) == 1
        assert len(calls) == 1


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command.name)
async def test_email_application_http_boundaries(command: Command) -> None:
    """The registered gate runs all applicable boundaries for every command."""
    await _deny_missing_scope_before_mutation(command)
    await _succeed_and_retry_without_duplicate_actions(command)
    if command.account_bound:
        await _recheck_revoked_gmail_authority(command)
    if command.body is not None:
        await _reject_extra_fields(command)
    if command.account_bound or command.name == "operation":
        await _isolate_principal_identifiers(command)
    if "{" in command.path:
        await _validate_path_identifiers(command)
    if command.name in {"feedback", "edit", "send", "dismiss", "discard", "exclude", "endorse"}:
        await _preserve_current_mail_on_revision_conflict(command)
    if command.name in {"feedback", "edit", "generate", "send"}:
        await _reject_reused_idempotency_key(command.name)
    if command.name in {"refresh", "generate", "send"}:
        await _retry_durable_operation_after_dispatch_failure(command.name)


@pytest.mark.parametrize("command", ["draft", "send", "pause", "reset"])
async def test_write_commands_never_mutate_then_fail_response_authorization(command: str) -> None:
    async with email_client() as (app, client):
        thread, draft = await seed_mail(app)
        dispatched: list[UUID] = []

        async def dispatch(run_id: UUID) -> None:
            dispatched.append(run_id)

        app.services.email.dispatch = dispatch
        app.principal.scopes.remove("email.read")
        if command in {"draft", "send"}:
            path = (
                f"/v1/email/threads/{thread.id}/drafts"
                if command == "draft"
                else f"/v1/email/drafts/{draft.id}/send-proposal"
            )
            response = await client.post(
                path, json={} if command == "draft" else {"expected_revision": 1}
            )
            assert response.status_code == 403
            assert dispatched == []
            async with app.uow_factory() as uow:
                assert await uow.email.list(app.principal, "task") == []
        else:
            response = (
                await client.put("/v1/email/learning", json={"paused": True})
                if command == "pause"
                else await client.post("/v1/email/learning/reset", json={"scope": "all"})
            )
            assert response.status_code == 200
            assert response.json()["paused"] is (command == "pause")
        assert response.headers["cache-control"] == "private, no-store"
