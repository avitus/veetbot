"""Milestone 5 HTTP, streaming, authorization, and cancellation gates."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr

from agent_core.adapters.artifacts.local import LocalTrajectoryArtifactStore
from agent_core.api import create_app
from agent_core.api.errors import (
    API_ERROR_STATUS,
    ERROR_CODE_VOCABULARY,
    ERROR_STATUS_MAP,
    INTERNAL_ONLY_ERROR_TYPES,
    mapping_for,
)
from agent_core.api.sse import encode_sse, heartbeat
from agent_core.bootstrap import Composition, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain import errors as domain_errors
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.policies import (
    ActionKind,
    PolicyDecision,
    PolicyDecisionType,
    RiskLevel,
    TrustLevel,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import (
    ALLOWED_TOOL_TRANSITIONS,
    ToolExecutionContext,
    ToolInvocationStatus,
    ToolResult,
)
from agent_core.domain.trajectory import ArtifactRef, TrajectoryExport
from agent_core.domain.views import PersistedStreamFrame, TextContentBlock, TransientStreamFrame
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.tools.demo_external_write import DemoExternalWriteTool

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ("127.0.0.1", 43100)


def _settings(tmp_path: Path, *, auth_mode: AuthMode = AuthMode.DEV) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=auth_mode,
        auth_token=SecretStr("test-bearer-token") if auth_mode is AuthMode.TOKEN else None,
        sandbox=(
            SandboxMechanism.MICROVM if auth_mode is AuthMode.TOKEN else SandboxMechanism.FAKE
        ),
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
    )


@asynccontextmanager
async def _composition(
    tmp_path: Path,
    *,
    auth_mode: AuthMode = AuthMode.DEV,
    script: FakeModelScript | None = None,
) -> Any:
    composition_settings = _settings(tmp_path, auth_mode=AuthMode.DEV)
    async with build(
        settings=composition_settings,
        sequential_ids=True,
        script=script,
    ) as composition:
        yield replace(composition, settings=_settings(tmp_path, auth_mode=auth_mode))


@asynccontextmanager
async def _client(
    composition: Composition,
    *,
    principal: Principal | None = None,
    client_address: tuple[str, int] = CLIENT,
) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app,
        client=client_address,
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _create_session(client: httpx.AsyncClient) -> UUID:
    response = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def _create_resources(
    composition: Composition, client: httpx.AsyncClient
) -> tuple[UUID, UUID, UUID, UUID]:
    session_id = await _create_session(client)
    submitted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": [{"type": "text", "text": "make resources"}]},
    )
    assert submitted.status_code == 202
    run_id = UUID(submitted.json()["run_id"])
    now = composition.clock.now()
    approval_id = UUID(int=9001)
    artifact_id = UUID(int=9002)
    approval = ApprovalRequest(
        id=approval_id,
        tenant_id=composition.principal.tenant_id,
        principal_id=composition.principal.principal_id,
        session_id=session_id,
        run_id=run_id,
        action_kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=9003),
        status=ApprovalStatus.PENDING,
        action_summary="Review a fixture action.",
        tool_name="demo.external_write",
        arguments={"destination": "fixture"},
        normalized_arguments_hash="fixture-hash",
        required_scopes={"demo.write"},
        agent_version="1.0.0",
        risk=RiskLevel.HIGH,
        policy_reason="External writes require approval.",
        policy_decision=PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason_code="fixture.approval",
            explanation="External writes require approval.",
            policy_version=composition.ruleset.policy_version,
        ),
        policy_version=composition.ruleset.policy_version,
        expires_at=now + timedelta(hours=1),
        created_at=now,
    )
    content = b"<html>fixture</html>"
    artifact = ArtifactRef(
        id=artifact_id,
        tenant_id=composition.principal.tenant_id,
        principal_id=composition.principal.principal_id,
        session_id=session_id,
        run_id=run_id,
        name="fixture.html",
        media_type="text/html",
        storage_uri="pending",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=now + timedelta(hours=1),
        created_at=now,
    )
    store = LocalTrajectoryArtifactStore(composition.settings.artifact_root)
    artifact = await store.write(artifact, content)
    export = TrajectoryExport(
        export_id=UUID(int=9004),
        tenant_id=artifact.tenant_id,
        principal_id=artifact.principal_id,
        run_id=run_id,
        artifact=artifact,
        builder_version="test",
        ruleset_version="test",
        created_at=now,
    )
    async with composition.uow_factory() as uow:
        await uow.approvals.create(approval)
        await uow.trajectory_exports.create(export)
    return session_id, run_id, approval_id, artifact_id


async def test_error_code_vocabulary_is_closed(tmp_path: Path) -> None:
    expected = {
        "authentication_error",
        "authorization_error",
        "not_found",
        "conflict",
        "invalid_state_transition",
        "tool_not_found",
        "tool_validation_error",
        "tool_policy_denied",
        "approval_required",
        "approval_denied",
        "approval_expired",
        "budget_exceeded",
        "deadline_exceeded",
        "run_deadline_exceeded",
        "run_cancelled",
        "context_overflow",
        "tool_loop_detected",
        "model_transient_error",
        "model_permanent_error",
        "model_protocol_error",
        "tool_timeout",
        "tool_execution_error",
        "tool_result_invalid",
        "sandbox_provision_error",
        "sandbox_execution_error",
        "artifact_storage_error",
        "concurrency_conflict",
        "malformed_request",
        "unsupported_media_type",
        "payload_too_large",
        "rate_limited",
        "internal_error",
    }
    assert expected == ERROR_CODE_VOCABULARY
    assert set(API_ERROR_STATUS) <= expected

    missing = UUID(int=987654)
    error_requests: list[tuple[str, str, dict[str, object] | None]] = [
        ("POST", "/v1/sessions", {}),
        ("GET", f"/v1/sessions/{missing}", None),
        (
            "POST",
            f"/v1/sessions/{missing}/messages",
            {"content": [{"type": "text", "text": "missing"}]},
        ),
        ("GET", f"/v1/runs/{missing}", None),
        ("GET", f"/v1/runs/{missing}/events", None),
        ("POST", f"/v1/runs/{missing}/cancel", {}),
        (
            "POST",
            f"/v1/runs/{missing}/input",
            {"content": [{"type": "text", "text": "missing"}]},
        ),
        ("GET", "/v1/approvals?limit=0", None),
        ("GET", f"/v1/approvals/{missing}", None),
        (
            "POST",
            f"/v1/approvals/{missing}/resolve",
            {"decision": "approve_once"},
        ),
        ("GET", f"/v1/artifacts/{missing}", None),
        ("GET", f"/v1/artifacts/{missing}/content", None),
    ]
    async with (
        _composition(tmp_path, auth_mode=AuthMode.TOKEN) as composition,
        _client(composition) as client,
    ):
        for method, path, body in error_requests:
            response = await client.request(
                method,
                path,
                json=body,
                headers={"Authorization": "Bearer test-bearer-token"},
            )
            assert response.status_code >= 400, (method, path, response.text)
            code = response.json()["error"]["code"]
            assert code in ERROR_CODE_VOCABULARY
            statuses = {
                mapping.status for mapping in ERROR_STATUS_MAP.values() if mapping.code == code
            }
            expected_status = API_ERROR_STATUS.get(code)
            if expected_status is None:
                assert len(statuses) == 1
                expected_status = statuses.pop()
            assert response.status_code == expected_status


def test_error_status_map_is_total_over_the_public_taxonomy() -> None:
    public_types = {
        domain_errors.AuthenticationError,
        domain_errors.AuthorizationError,
        domain_errors.NotFoundError,
        domain_errors.ConflictError,
        domain_errors.InvalidStateTransition,
        domain_errors.ToolNotFoundError,
        domain_errors.ToolValidationError,
        domain_errors.ToolPolicyDenied,
        domain_errors.ApprovalRequired,
        domain_errors.ApprovalDenied,
        domain_errors.ApprovalExpired,
        domain_errors.BudgetExceeded,
        domain_errors.DeadlineExceeded,
        domain_errors.RunDeadlineExceeded,
        domain_errors.RunCancelled,
        domain_errors.ContextOverflow,
        domain_errors.ToolLoopDetected,
        domain_errors.ModelTransientError,
        domain_errors.ModelPermanentError,
        domain_errors.ModelProtocolError,
        domain_errors.ToolTimeoutError,
        domain_errors.ToolExecutionError,
        domain_errors.ToolResultValidationError,
        domain_errors.SandboxProvisionError,
        domain_errors.SandboxExecutionError,
        domain_errors.ArtifactStorageError,
        domain_errors.ConcurrencyConflict,
    }
    assert public_types <= set(ERROR_STATUS_MAP)
    assert INTERNAL_ONLY_ERROR_TYPES.isdisjoint(ERROR_STATUS_MAP)
    assert mapping_for(RuntimeError("unknown")).code == "internal_error"
    assert mapping_for(RuntimeError("unknown")).status == 500


def test_api_handlers_never_bind_a_request_tenant() -> None:
    tree = ast.parse((ROOT / "src/agent_core/api/app.py").read_text(encoding="utf-8"))
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post"}
            for decorator in node.decorator_list
        )
    ]
    assert handlers
    request_models = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "BaseModel" for base in node.bases)
    ]
    assert request_models
    for boundary in [*handlers, *request_models]:
        tenant_references: list[str] = []
        for node in ast.walk(boundary):
            if isinstance(node, ast.arg) and "tenant" in node.arg.lower():
                tenant_references.append(node.arg)
            elif isinstance(node, ast.Name) and "tenant" in node.id.lower():
                tenant_references.append(node.id)
            elif isinstance(node, ast.Attribute) and "tenant" in node.attr.lower():
                tenant_references.append(node.attr)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "tenant" in node.value.lower()
            ):
                tenant_references.append(node.value)
        assert tenant_references == [], (boundary.name, tenant_references)


async def test_every_route_declares_exactly_one_scope_except_health(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert len(routes) == 14
    for route in routes:
        declared = (route.openapi_extra or {}).get("required_scope")
        if route.path in {"/health/live", "/health/ready"}:
            assert declared is None
        else:
            assert isinstance(declared, str)
            assert declared in PLATFORM_SCOPES


async def test_cross_tenant_resource_routes_return_404(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition:
        async with _client(composition) as owner:
            session_id, run_id, approval_id, artifact_id = await _create_resources(
                composition, owner
            )
        foreign = Principal(
            tenant_id="other-tenant",
            principal_id="other-principal",
            roles={"user"},
            scopes=set(PLATFORM_SCOPES),
        )
        async with _client(composition, principal=foreign) as client:
            requests = [
                ("GET", f"/v1/sessions/{session_id}", None),
                (
                    "POST",
                    f"/v1/sessions/{session_id}/messages",
                    {"content": [{"type": "text", "text": "hidden"}]},
                ),
                ("GET", f"/v1/runs/{run_id}", None),
                ("GET", f"/v1/runs/{run_id}/events", None),
                ("POST", f"/v1/runs/{run_id}/cancel", None),
                (
                    "POST",
                    f"/v1/runs/{run_id}/input",
                    {"content": [{"type": "text", "text": "hidden"}]},
                ),
                ("GET", f"/v1/approvals?run_id={run_id}", None),
                ("GET", f"/v1/approvals/{approval_id}", None),
                (
                    "POST",
                    f"/v1/approvals/{approval_id}/resolve",
                    {"decision": "deny"},
                ),
                ("GET", f"/v1/artifacts/{artifact_id}", None),
                ("GET", f"/v1/artifacts/{artifact_id}/content", None),
            ]
            for method, path, body in requests:
                response = await client.request(method, path, json=body)
                assert response.status_code == 404, (method, path, response.text)
                assert response.json()["error"]["code"] == "not_found"


def test_transient_sse_frames_and_heartbeats_have_no_id() -> None:
    for frame in (
        TransientStreamFrame(event="message.delta", data={"text": "part"}),
        TransientStreamFrame(event="reasoning.delta", data={"text": "summary"}),
        TransientStreamFrame(event="stream.overflow", data={"last_sequence": 812}),
    ):
        rendered = encode_sse(frame)
        assert b"\nid:" not in b"\n" + rendered
        assert not rendered.startswith(b"id:")
    assert b"id:" not in heartbeat()


async def test_persisted_replay_is_gapless_duplicate_free_with_session_gaps(
    tmp_path: Path,
) -> None:
    async with _composition(tmp_path) as composition:
        principal = composition.principal
        session = await composition.services.sessions.create(principal, "general", {})
        result = await composition.services.runs.submit(
            principal,
            session.id,
            [TextContentBlock(text="replay")],
            None,
            None,
        )
        async with composition.uow_factory() as uow:
            await asyncio.gather(
                uow.events.append(
                    NewEvent(
                        session_id=session.id,
                        run_id=None,
                        event_type="session.noise.one",
                        actor_type="test",
                    )
                ),
                uow.events.append(
                    NewEvent(
                        session_id=session.id,
                        run_id=result.run_id,
                        event_type="run.replay.one",
                        actor_type="test",
                    )
                ),
            )
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=None,
                    event_type="session.noise.two",
                    actor_type="test",
                )
            )
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=result.run_id,
                    event_type="run.replay.two",
                    actor_type="test",
                )
            )
            expected = [
                event.sequence
                for event in await uow.events.list_after(session.id, 0, principal)
                if event.run_id == result.run_id
            ]
        first_stream = composition.services.runs.stream(principal, result.run_id, None)
        first = await anext(first_stream)
        second = await anext(first_stream)
        await first_stream.aclose()
        received = [first.sequence, second.sequence]
        async for frame in composition.services.runs.stream(
            principal, result.run_id, second.sequence
        ):
            assert isinstance(frame, PersistedStreamFrame)
            received.append(frame.sequence)
        assert received == expected
        assert len(received) == len(set(received))
        assert any(right - left > 1 for left, right in itertools.pairwise(received))


async def test_submission_is_idempotent_and_reuse_conflicts(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        session_id = await _create_session(client)
        path = f"/v1/sessions/{session_id}/messages"
        body = {"content": [{"type": "text", "text": "submit once"}]}
        headers = {"Idempotency-Key": "same-key"}
        first, second = await asyncio.gather(
            client.post(path, json=body, headers=headers),
            client.post(path, json=body, headers=headers),
        )
        assert {first.status_code, second.status_code} == {200, 202}
        assert first.json()["run_id"] == second.json()["run_id"]
        reused = await client.post(
            path,
            json={"content": [{"type": "text", "text": "different"}]},
            headers=headers,
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["details"] == {"reason": "idempotency_key_reused"}

        other_session = await _create_session(client)
        reused_on_other_session = await client.post(
            f"/v1/sessions/{other_session}/messages",
            json=body,
            headers=headers,
        )
        assert reused_on_other_session.status_code == 409
        assert reused_on_other_session.json()["error"]["details"] == {
            "reason": "idempotency_key_reused"
        }


async def test_ask_user_suspends_and_input_resumes_the_same_run(tmp_path: Path) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Which region should I use?"},
                        call_id="ask-region",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Using the EU region.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with (
        _composition(tmp_path, script=script) as composition,
        _client(composition) as client,
    ):
        session_id = await _create_session(client)
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": [{"type": "text", "text": "Choose a region."}]},
        )
        run_id = UUID(submitted.json()["run_id"])
        waiting = await client.get(f"/v1/runs/{run_id}")
        assert waiting.json()["status"] == "WAITING_FOR_USER"
        events = await composition.runs.events(run_id)
        waiting_event = next(
            event for event in events if event.event_type == "run.waiting_for_user"
        )
        question_id = waiting_event.payload["question_id"]
        answered = await client.post(
            f"/v1/runs/{run_id}/input",
            json={
                "content": [{"type": "text", "text": "Use the EU region."}],
                "question_id": question_id,
            },
        )
        assert answered.status_code == 202
        assert answered.json()["run_id"] == str(run_id)
        completed = await client.get(f"/v1/runs/{run_id}")
        assert completed.json()["status"] == "COMPLETED"
        retried = await client.post(
            f"/v1/runs/{run_id}/input",
            json={
                "content": [{"type": "text", "text": "Use the EU region."}],
                "question_id": question_id,
            },
        )
        assert retried.status_code == 202
        assert retried.json()["run_id"] == str(run_id)


async def test_artifact_content_is_always_an_attachment(tmp_path: Path) -> None:
    async with _composition(tmp_path) as composition, _client(composition) as client:
        _session, _run, _approval, first_artifact = await _create_resources(composition, client)
        first = await client.get(f"/v1/artifacts/{first_artifact}/content")
        assert first.status_code == 200
        assert first.headers["content-disposition"].startswith("attachment;")

        svg_session = await _create_session(client)
        svg_run_response = await client.post(
            f"/v1/sessions/{svg_session}/messages",
            json={"content": [{"type": "text", "text": "svg fixture"}]},
        )
        assert svg_run_response.status_code == 202
        svg_run_id = UUID(svg_run_response.json()["run_id"])
        now = composition.clock.now()
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        artifact = ArtifactRef(
            id=UUID(int=9010),
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=svg_session,
            run_id=svg_run_id,
            name="../résumé\uff02.svg",
            media_type="image/svg+xml",
            storage_uri="pending",
            sha256=__import__("hashlib").sha256(svg).hexdigest(),
            size_bytes=len(svg),
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            expires_at=now + timedelta(hours=1),
            created_at=now,
        )
        artifact = await LocalTrajectoryArtifactStore(composition.settings.artifact_root).write(
            artifact, svg
        )
        async with composition.uow_factory() as uow:
            await uow.trajectory_exports.create(
                TrajectoryExport(
                    export_id=UUID(int=9011),
                    tenant_id=artifact.tenant_id,
                    principal_id=artifact.principal_id,
                    run_id=svg_run_id,
                    artifact=artifact,
                    builder_version="test",
                    ruleset_version="test",
                    created_at=now,
                )
            )
        second = await client.get(f"/v1/artifacts/{artifact.id}/content")
        assert second.status_code == 200
        assert second.headers["content-disposition"].startswith("attachment;")
        assert 'filename=".._resume_.svg"' in second.headers["content-disposition"]
        assert (
            "filename*=UTF-8''.._r%C3%A9sum%C3%A9%EF%BC%82.svg"
            in second.headers["content-disposition"]
        )
        assert "../" not in second.headers["content-disposition"]
        cached = await client.get(
            f"/v1/artifacts/{artifact.id}/content",
            headers={"If-None-Match": f'W/"other", "{artifact.sha256}"'},
        )
        assert cached.status_code == 304


async def test_auth_request_ids_content_type_and_body_limits(tmp_path: Path) -> None:
    async with (
        _composition(tmp_path, auth_mode=AuthMode.TOKEN) as composition,
        _client(composition) as client,
    ):
        missing = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        wrong = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
            headers={"Authorization": "Bearer wrong"},
        )
        assert wrong.status_code == 401
        valid_request_id = "client-request_1"
        authenticated = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
            headers={
                "Authorization": "Bearer test-bearer-token",
                "X-Request-Id": valid_request_id,
            },
        )
        assert authenticated.status_code == 201
        assert authenticated.headers["x-request-id"] == valid_request_id
        invalid_id = await client.get("/health/live", headers={"X-Request-Id": "bad request id"})
        generated = UUID(invalid_id.headers["x-request-id"])
        assert generated.version == 7
        unsupported = await client.post(
            "/v1/sessions",
            content=b"{}",
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Content-Type": "text/plain",
            },
        )
        assert unsupported.status_code == 415
        too_large = await client.post(
            "/v1/sessions",
            content=b"{}",
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Content-Type": "application/json",
                "Content-Length": str(1024 * 1024 + 1),
            },
        )
        assert too_large.status_code == 413
        assert UUID(too_large.headers["x-request-id"]).version == 7

        async def oversized_chunks() -> AsyncIterator[bytes]:
            for _ in range(3):
                yield b" " * (512 * 1024)

        chunked = await client.post(
            "/v1/sessions",
            content=oversized_chunks(),
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Content-Type": "application/json",
            },
        )
        assert chunked.status_code == 413, chunked.text
        assert chunked.json()["error"]["code"] == "payload_too_large"

        session_id = authenticated.json()["id"]
        oversized_key = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": [{"type": "text", "text": "bounded"}]},
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Idempotency-Key": "k" * 256,
            },
        )
        assert oversized_key.status_code == 400

        oversized_reason = await client.post(
            f"/v1/approvals/{UUID(int=1)}/resolve",
            json={"decision": "deny", "reason": "r" * 4097},
            headers={"Authorization": "Bearer test-bearer-token"},
        )
        assert oversized_reason.status_code == 400

        clamped_page = await client.get(
            "/v1/approvals?limit=100000",
            headers={"Authorization": "Bearer test-bearer-token"},
        )
        assert clamped_page.status_code == 200
        assert clamped_page.json() == {"items": [], "next_cursor": None}


async def test_dev_authentication_is_loopback_only(tmp_path: Path) -> None:
    async with (
        _composition(tmp_path) as composition,
        _client(composition, client_address=("203.0.113.4", 43100)) as client,
    ):
        response = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert response.status_code == 401

    async with _composition(tmp_path) as composition, _client(composition) as client:
        proxied = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
            headers={"X-Forwarded-For": "203.0.113.4"},
        )
        assert proxied.status_code == 401


async def test_cancellation_cannot_mark_an_effect_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "CANCELLED" not in ToolInvocationStatus.__members__
    assert all(
        ToolInvocationStatus.__members__.get(status.value) is not None
        for status in ALLOWED_TOOL_TRANSITIONS[ToolInvocationStatus.RUNNING]
    )
    assert RunStatus.CANCELLED.value == "CANCELLED"

    effect_sent = asyncio.Event()
    release_effect = asyncio.Event()
    original_execute = DemoExternalWriteTool.execute

    async def slow_effect(
        tool: DemoExternalWriteTool,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        await context.mark_effect_sent()
        effect_sent.set()
        await release_effect.wait()
        return await original_execute(tool, arguments, context)

    monkeypatch.setattr(DemoExternalWriteTool, "execute", slow_effect)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "fixture", "content": "sent"},
                        call_id="cancel-after-effect",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with _composition(tmp_path, script=script) as composition:
        session = await composition.services.sessions.create(composition.principal, "general", {})
        submitted = await composition.services.runs.submit(
            composition.principal,
            session.id,
            [TextContentBlock(text="perform the approved effect")],
            None,
            None,
        )
        approval = (await composition.approvals.list_pending(run_id=submitted.run_id))[0]
        resume = asyncio.create_task(
            composition.services.approvals.resolve(
                composition.principal,
                approval.id,
                ApprovalResolutionType.APPROVE_ONCE,
                None,
            )
        )
        await asyncio.wait_for(effect_sent.wait(), timeout=2)
        try:
            cancellation = await composition.services.runs.cancel(
                composition.principal, submitted.run_id
            )
        finally:
            release_effect.set()
        await resume
        final = await composition.services.runs.get(composition.principal, submitted.run_id)
        async with composition.uow_factory() as uow:
            invocation = (
                await uow.invocations.list_for_run(submitted.run_id, composition.principal)
            )[0]

    assert cancellation.accepted
    assert final.status is RunStatus.CANCELLED
    assert invocation.effect_sent_at is not None
    assert invocation.status in {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.UNCERTAIN,
    }
