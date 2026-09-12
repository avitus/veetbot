"""Owner gestures archive through the real approval/tool lifecycle, never a model."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.policies import PolicyDecisionType
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import (
    MailboxFactory,
    _current_mail_factory,
    _page,
    _seed_draft,
)


async def test_archive_and_restore_use_one_exact_owner_approval_without_model_or_budget() -> None:
    base = await _current_mail_factory()
    writes: list[tuple[str, dict[str, Any]]] = []
    inbox = True

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            nonlocal inbox
            if name == "modify_labels":
                writes.append((config.server_id, arguments))
                inbox = "INBOX" in (arguments.get("add_label_ids") or [])
                receipt = {key: value or [] for key, value in arguments.items()}
                return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)
            if name == "get_thread_page":
                value = _page()
                value["messages"][0]["label_ids"] = ["INBOX"] if inbox else []
                value["history_id"] = str(100 + len(writes))
                return MCPCallResult(content=(json.dumps(value),), structured=value)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        thread, draft = await _seed_draft(app)

        async def exhausted(*args: object, **kwargs: object) -> None:
            raise AssertionError("explicit archive must not consult automatic learning dollars")

        vars(app.services.email)["_check_budget"] = exhausted
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
        ) as client:
            for archived in (True, False):
                body = {
                    "expected_revision": thread.revision,
                    "archived": archived,
                    "idempotency_key": f"owner-archive-{archived}",
                }
                result = await client.post(f"/v1/email/threads/{thread.id}/archive", json=body)
                assert result.status_code == 200, result.text
                assert result.headers["cache-control"] == "private, no-store"
                replay = await client.post(f"/v1/email/threads/{thread.id}/archive", json=body)
                assert replay.status_code == 200 and replay.json()["replayed"]
                assert replay.json()["run_id"] == result.json()["run_id"]
                from uuid import UUID

                run = await app.runs.get(UUID(result.json()["run_id"]))
                assert run.status is RunStatus.COMPLETED, run.failure
                assert run.model_call_count == 0
                latest = await app.services.email.thread(app.principal, thread.id)
                assert latest["in_inbox"] is not archived
                assert latest["revision"] == thread.revision
                operation = latest["archive_operation"]
                assert isinstance(operation, dict)
                assert operation["status"] == "completed"
                assert operation["target_archived"] is archived
                preserved = await app.services.email.draft(app.principal, draft.id)
                assert preserved.body == draft.body and preserved.revision == draft.revision
                assert not preserved.stale
                async with app.uow_factory() as uow:
                    events = await uow.events.list_after(run.session_id, 0, app.principal)
                    invocations = await uow.invocations.list_for_run(run.id, app.principal)
                    [write] = [i for i in invocations if i.tool_name.endswith("modify_labels")]
                    approval = await uow.approvals.get_by_action(write.id)
                    assert approval is not None and approval.status is ApprovalStatus.APPROVED
                    assert approval.policy_decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
                    assert approval.resolved_by == app.principal.principal_id
                requested = [
                    e for e in events if e.event_type == "approval.requested" and e.run_id == run.id
                ]
                resolved = [
                    e for e in events if e.event_type == "approval.resolved" and e.run_id == run.id
                ]
                assert len(requested) == len(resolved) == 1
                assert requested[0].sequence < resolved[0].sequence
                assert not any(e.event_type == "user.message.created" for e in events)
        assert writes == [
            (
                "gmail_write",
                {"thread_ids": ["thread-1"], "add_label_ids": None, "remove_label_ids": ["INBOX"]},
            ),
            (
                "gmail_write",
                {"thread_ids": ["thread-1"], "add_label_ids": ["INBOX"], "remove_label_ids": None},
            ),
        ]


@pytest.mark.parametrize(
    "scope",
    [
        "email.read",
        "email.write",
        "approval.resolve",
        "run.write",
        "session.write",
        "mcp.gmail_read.use",
        "mcp.gmail_write.use",
    ],
)
@pytest.mark.parametrize("stage", ["before_worker", "before_dispatch"])
async def test_archive_rechecks_current_authority_at_dispatch(scope: str, stage: str) -> None:
    """The admission scope stamp cannot outlive a revoked current owner permission."""
    base = await _current_mail_factory()
    writes: list[str] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "modify_labels":
                writes.append(name)
                receipt = {key: value or [] for key, value in arguments.items()}
                return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        thread, _ = await _seed_draft(app)
        dispatch = app.services.email.dispatch

        async def queued(run_id: Any) -> None:
            if stage == "before_worker":
                app.principal.scopes.remove(scope)
            await dispatch(run_id)

        app.services.email.dispatch = queued
        pipeline = app.executor._dispatch_tools
        attempts = 0

        async def revoke_before_dispatch(**kwargs: Any) -> Any:
            nonlocal attempts
            if kwargs["tool_calls"][0].name.endswith("modify_labels"):
                attempts += 1
                if stage == "before_dispatch" and attempts == 2:
                    app.principal.scopes.discard(scope)
            return await pipeline(**kwargs)

        app.executor._dispatch_tools = revoke_before_dispatch
        operation = await app.services.email.archive(
            app.principal,
            thread.id,
            thread.revision,
            archived=True,
            idempotency_key=f"revoke-{scope}-{stage}",
        )
        assert writes == [], "revoked owner authority reached the Gmail write"
        app.principal.scopes.add(scope)
        latest = await app.services.email.thread(app.principal, thread.id)
        assert latest["in_inbox"] is True
        assert _archive_status(latest) == "failed"
        assert (await app.runs.get(operation.run_id)).model_call_count == 0


async def _archive_factory(
    writes: list[dict[str, Any]], *, result: str = "success"
) -> MailboxFactory:
    """Return real first-party schemas and a bounded deterministic provider boundary."""
    from agent_core.domain.errors import MCPTransportError

    base = await _current_mail_factory()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name != "modify_labels":
                return await original(name, arguments)
            writes.append(arguments)
            if result == "disconnect":
                raise MCPTransportError()
            receipt = {key: value or [] for key, value in arguments.items()}
            if result == "wrong_thread":
                receipt["thread_ids"] = ["another-thread"]
            if result == "wrong_delta":
                receipt["add_label_ids"] = ["TRASH"]
            return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)

        vars(client)["call_tool"] = call_tool
        return client

    return factory


@pytest.mark.parametrize("result", ["disconnect", "wrong_thread", "wrong_delta"])
async def test_archive_ambiguous_provider_outcome_is_uncertain_and_never_resent(
    result: str,
) -> None:
    """Neither lost transport nor an inconsistent success receipt authorizes another write."""
    from agent_core.domain.errors import ConflictError

    writes: list[dict[str, Any]] = []
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory(writes, result=result),
    ) as app:
        thread, _ = await _seed_draft(app)
        operation = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="uncertain-archive"
        )
        latest = await app.services.email.thread(app.principal, thread.id)
        assert _archive_status(latest) == "uncertain"
        assert latest["in_inbox"] is True
        replay = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="uncertain-archive"
        )
        assert replay.replayed and replay.run_id == operation.run_id
        with pytest.raises(ConflictError, match="uncertain"):
            await app.services.email.archive(
                app.principal, thread.id, 1, archived=True, idempotency_key="new-gesture"
            )
        assert len(writes) == 1


@pytest.mark.parametrize("after_resolution", [False, True])
async def test_archive_expired_consent_cannot_write_or_leave_a_pending_approval(
    after_resolution: bool,
) -> None:
    """Expiry applies on both sides of ordinary approval resolution."""
    from datetime import timedelta

    from agent_core.application.email import save_value

    writes: list[dict[str, Any]] = []
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory(writes),
    ) as app:
        thread, _ = await _seed_draft(app)
        approve = app.services.email.approve_archive

        async def expire(owner: Any, run: Any, lease: Any, approval_id: Any) -> None:
            if after_resolution:
                await approve(owner, run, lease, approval_id)
            task = await app.services.email.get_task(owner, run.id)
            assert task is not None and task.archive_consent is not None
            task = task.model_copy(
                update={
                    "archive_consent": task.archive_consent.model_copy(
                        update={"expires_at": app.clock.now() - timedelta(seconds=1)}
                    )
                }
            )
            async with app.uow_factory() as uow, uow.email.lock(owner):
                await save_value(uow.email, owner, "task", str(run.id), task, app.clock.now())
            if not after_resolution:
                await approve(owner, run, lease, approval_id)

        vars(app.services.email)["approve_archive"] = expire
        operation = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="expire"
        )
        assert writes == []
        assert await app.approvals.list_pending(run_id=operation.run_id) == []
        latest = await app.services.email.thread(app.principal, thread.id)
        assert latest["in_inbox"] is True
        assert _archive_status(latest) == "failed"


@pytest.mark.parametrize("crash_at", ["approval", "dispatch"])
async def test_archive_recovery_reuses_exact_approval_and_never_duplicates_write(
    crash_at: str,
) -> None:
    """A crash on either side of dispatch retains the one exact invocation and audit."""
    from uuid import UUID

    class SimulatedCrash(BaseException):
        pass

    writes: list[dict[str, Any]] = []
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory(writes),
    ) as app:
        thread, _ = await _seed_draft(app)

        async def queued(run_id: UUID) -> None:
            pass

        app.services.email.dispatch = queued
        operation = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="recover"
        )
        resolve = app.services.email.resolve_archive_approval
        pipeline = app.executor._dispatch_tools

        async def after_approval(*args: Any) -> None:
            assert resolve is not None
            await resolve(*args)
            raise SimulatedCrash()

        async def after_dispatch(**kwargs: Any) -> Any:
            value = await pipeline(**kwargs)
            if kwargs["tool_calls"][0].name.endswith("modify_labels"):
                raise SimulatedCrash()
            return value

        if crash_at == "approval":
            app.services.email.resolve_archive_approval = after_approval
        else:
            app.executor._dispatch_tools = after_dispatch
        with pytest.raises(SimulatedCrash):
            await app.executor.execute(operation.run_id)
        app.services.email.resolve_archive_approval = resolve
        app.executor._dispatch_tools = pipeline
        run = await app.runs.get(operation.run_id)
        await app.executor._execute_running(run, lease=None)
        latest = await app.services.email.thread(app.principal, thread.id)
        assert _archive_status(latest) == "completed"
        assert latest["in_inbox"] is False
        assert len(writes) == 1
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run.id, app.principal)
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        assert len([i for i in invocations if i.tool_name.endswith("modify_labels")]) == 1
        assert len([e for e in events if e.event_type == "approval.requested"]) == 1
        assert len([e for e in events if e.event_type == "approval.resolved"]) == 1


async def test_archive_coalesced_gesture_key_replays_after_operation_completion() -> None:
    """Every accepted idempotency key durably names the same pending operation."""
    from uuid import UUID

    from agent_core.domain.errors import ConflictError

    writes: list[dict[str, Any]] = []
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory(writes),
    ) as app:
        thread, _ = await _seed_draft(app)
        dispatch = app.services.email.dispatch

        async def queued(run_id: UUID) -> None:
            pass

        app.services.email.dispatch = queued
        first = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="first-gesture"
        )
        concurrent = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="second-gesture"
        )
        assert concurrent.replayed and concurrent.run_id == first.run_id
        await dispatch(first.run_id)
        app.services.email.dispatch = dispatch
        replay = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="second-gesture"
        )
        assert replay.replayed and replay.run_id == first.run_id
        with pytest.raises(ConflictError, match="idempotency"):
            await app.services.email.archive(
                app.principal, thread.id, 1, archived=False, idempotency_key="second-gesture"
            )
        assert len(writes) == 1


def _archive_status(thread: dict[str, object]) -> object:
    """Narrow the public optional operation projection for boundary assertions."""
    operation = thread["archive_operation"]
    assert isinstance(operation, dict)
    return operation["status"]


@pytest.mark.parametrize("same_history", [True, False])
async def test_archive_checks_large_partial_thread_history_without_loading_or_replacing_bodies(
    same_history: bool,
) -> None:
    """One current provider revision can verify arbitrarily large cached conversations."""
    from agent_core.application.email import save_value

    base = await _current_mail_factory()
    writes: list[str] = []
    reads: list[dict[str, Any]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            if name == "modify_labels":
                writes.append(name)
                receipt = {key: value or [] for key, value in arguments.items()}
                return MCPCallResult(content=(json.dumps(receipt),), structured=receipt)
            if name == "get_thread_page":
                reads.append(arguments)
                value = _page()
                value.update(
                    total_messages=10000,
                    returned_messages=1,
                    next_page_token="later-page",
                    complete=False,
                    history_id="100" if same_history else "101",
                )
                value["messages"][0].update(
                    body="bounded first passage", body_complete=False, next_body_offset=65536
                )
                return MCPCallResult(content=(json.dumps(value),), structured=value)
            return await original(name, arguments)

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        thread, draft = await _seed_draft(app)
        cached = thread.model_copy(update={"complete": False})
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), cached, app.clock.now()
            )
        await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="large-thread"
        )
        latest = await app.services.email.thread(app.principal, thread.id)
        assert _archive_status(latest) == "completed"
        assert writes == ["modify_labels"]
        assert reads == [{"thread_id": "thread-1", "max_messages": 1, "page_token": None}]
        messages = latest["messages"]
        assert isinstance(messages, list)
        assert messages[0]["body"] == thread.messages[0].body
        assert latest["revision"] == 1 and latest["complete"] is False
        assert not (await app.services.email.draft(app.principal, draft.id)).stale


async def test_archive_consent_binding_rejects_a_rewritten_expiry() -> None:
    """An authenticated gesture freezes its entire consent, including its finite lifetime."""
    from datetime import timedelta
    from uuid import UUID

    from agent_core.domain.errors import ConflictError

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory([]),
    ) as app:
        thread, _ = await _seed_draft(app)

        async def queued(run_id: UUID) -> None:
            pass

        app.services.email.dispatch = queued
        operation = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="frozen-consent"
        )
        task = await app.services.email.get_task(app.principal, operation.run_id)
        assert task is not None and task.archive_consent is not None
        changed = task.model_copy(
            update={
                "archive_consent": task.archive_consent.model_copy(
                    update={"expires_at": task.archive_consent.expires_at + timedelta(seconds=1)}
                )
            }
        )
        await app.services.email.save_task(app.principal, changed)
        with pytest.raises(ConflictError, match="gesture"):
            await app.services.email.validate_archive(
                app.principal, await app.runs.get(operation.run_id), None
            )


async def test_archive_feature_disable_cannot_turn_persisted_consent_into_generic_execution() -> (
    None
):
    """A queued owner action cannot execute after the feature has been disabled."""
    from uuid import UUID

    writes: list[dict[str, Any]] = []
    configured = replace(_email_settings(), email_mode_enabled=True)
    async with build(settings=configured, mcp_client_factory=await _archive_factory(writes)) as app:
        thread, _ = await _seed_draft(app)

        async def queued(run_id: UUID) -> None:
            pass

        app.services.email.dispatch = queued
        operation = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="disabled-archive"
        )
        # Configuration is immutable in production; simulate the value observed
        # by the newly composed worker while retaining this in-memory database.
        object.__setattr__(configured, "email_mode_enabled", False)
        await app.executor.execute(operation.run_id)
        assert writes == []
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.model_call_count == 0
        assert (
            _archive_status(await app.services.email.thread(app.principal, thread.id)) == "failed"
        )


@pytest.mark.parametrize("crash_at", ["approval", "effect", "result"])
async def test_archive_disabled_recovery_preserves_known_and_uncertain_effects(
    crash_at: str,
) -> None:
    """Disabling the feature blocks new effects while preserving dispatch evidence."""
    from uuid import UUID

    class SimulatedCrash(BaseException):
        pass

    writes: list[dict[str, Any]] = []
    base = await _archive_factory(writes)

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            result = await original(name, arguments)
            if crash_at == "effect" and name == "modify_labels":
                raise SimulatedCrash()
            return result

        vars(client)["call_tool"] = call_tool
        return client

    configured = replace(_email_settings(), email_mode_enabled=True)
    async with build(settings=configured, mcp_client_factory=factory) as app:
        thread, _ = await _seed_draft(app)

        async def queued(run_id: UUID) -> None:
            pass

        app.services.email.dispatch = queued
        operation = await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="disabled-recovery"
        )
        resolve = app.services.email.resolve_archive_approval
        dispatch = app.executor._dispatch_tools

        async def after_approval(*args: Any) -> None:
            assert resolve is not None
            await resolve(*args)
            raise SimulatedCrash()

        async def after_result(**kwargs: Any) -> Any:
            result = await dispatch(**kwargs)
            if kwargs["tool_calls"][0].name.endswith("modify_labels"):
                raise SimulatedCrash()
            return result

        if crash_at == "approval":
            app.services.email.resolve_archive_approval = after_approval
        if crash_at == "result":
            app.executor._dispatch_tools = after_result
        with pytest.raises(SimulatedCrash):
            await app.executor.execute(operation.run_id)
        app.services.email.resolve_archive_approval = resolve
        app.executor._dispatch_tools = dispatch
        object.__setattr__(configured, "email_mode_enabled", False)
        await app.executor._execute_running(await app.runs.get(operation.run_id), lease=None)
        state = await app.services.email.thread(app.principal, thread.id)
        assert (
            _archive_status(state)
            == {"approval": "failed", "effect": "uncertain", "result": "completed"}[crash_at]
        )
        assert len(writes) == (0 if crash_at == "approval" else 1)
        assert (await app.runs.get(operation.run_id)).model_call_count == 0


async def test_archive_completion_does_not_overwrite_a_newer_source_projection() -> None:
    """A confirmed thread-wide effect cannot pretend its older local source is current."""
    writes: list[dict[str, Any]] = []
    base = await _archive_factory(writes)

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        original = client.call_tool

        async def call_tool(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            result = await original(name, arguments)
            if name == "modify_labels":
                await app.services.email.import_thread(
                    app.principal, "default", _page(changed=True), thread.source_session_ids[0]
                )
            return result

        vars(client)["call_tool"] = call_tool
        return client

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True), mcp_client_factory=factory
    ) as app:
        thread, draft = await _seed_draft(app)
        await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="source-race"
        )
        latest = await app.services.email.thread(app.principal, thread.id)
        assert _archive_status(latest) == "completed"
        assert latest["revision"] == 2 and latest["in_inbox"] is True
        assert (await app.services.email.draft(app.principal, draft.id)).stale
        assert len(writes) == 1


async def test_archive_list_reconciliation_does_not_persist_feedback_projection() -> None:
    """Undo remains reversible when a list read repairs terminal operation state."""
    from uuid import UUID

    from agent_core.application.email import save_value

    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _archive_factory([]),
    ) as app:
        thread, _ = await _seed_draft(app)
        thread = thread.model_copy(update={"priority": 0.9})
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), thread, app.clock.now()
            )
        feedback = await app.services.email.feedback(
            app.principal, thread_id=thread.id, target="thread", judgment="less_important"
        )

        async def defer_projection(*args: Any, **kwargs: Any) -> None:
            pass

        vars(app.services.email)["finish_archive"] = defer_projection
        await app.services.email.archive(
            app.principal, thread.id, 1, archived=True, idempotency_key="projection-recovery"
        )
        await app.services.email.threads(app.principal, view="all")
        undone = await app.services.email.undo_feedback(
            app.principal, UUID(str(feedback["feedback_id"]))
        )
        assert undone["priority"] == 0.9
        latest = await app.services.email.thread(app.principal, thread.id)
        assert _archive_status(latest) == "completed" and latest["in_inbox"] is False
