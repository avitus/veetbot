"""Milestone 13 public-surface proof: a delegated child is an ordinary run."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import httpx

from agent_core.api.app import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.policy.scopes import PLATFORM_SCOPES


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        delegation_enabled=True,
    )


@asynccontextmanager
async def _client(composition: Composition, *, principal: Principal | None = None) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def test_a_delegated_child_is_readable_and_isolated_over_http(tmp_path: Path) -> None:
    """The run view, the child link, the event stream, and principal isolation."""

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="delegate.run",
                        arguments={
                            "briefs": [
                                {
                                    "objective": "Answer the delegated question.",
                                    "success_condition": "A one-line answer.",
                                    "allowed_tools": ["math.calculate"],
                                }
                            ]
                        },
                        call_id="call_delegate_http",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Four.", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="The child reports four.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=_settings(tmp_path), script=script) as composition:
        async with _client(composition) as client:
            created = await client.post(
                "/v1/sessions", json={"agent_id": "general", "metadata": {}}
            )
            assert created.status_code == 201, created.text
            session_id = UUID(created.json()["id"])
            submitted = await client.post(
                f"/v1/sessions/{session_id}/messages",
                json={"content": [{"type": "text", "text": "delegate over the API"}]},
            )
            assert submitted.status_code == 202, submitted.text
            run_id = UUID(submitted.json()["run_id"])

            parent_view = await client.get(f"/v1/runs/{run_id}")
            assert parent_view.status_code == 200, parent_view.text
            assert parent_view.json()["status"] == "COMPLETED"
            assert parent_view.json()["parent_run_id"] is None

            async with composition.uow_factory() as uow:
                [delegation] = await uow.delegations.get_for_parent_run(run_id)
            child_run_id = delegation.children[0].child_run_id
            child_session_id = delegation.children[0].child_session_id
            assert child_run_id is not None
            assert child_session_id is not None

            child_view = await client.get(f"/v1/runs/{child_run_id}")
            assert child_view.status_code == 200, child_view.text
            assert child_view.json()["status"] == "COMPLETED"
            assert child_view.json()["parent_run_id"] == str(run_id)

            sessions = await client.get("/v1/sessions")
            assert sessions.status_code == 200, sessions.text
            listed = {
                UUID(item["id"]): item.get("metadata", {}) for item in sessions.json()["items"]
            }
            assert child_session_id in listed
            assert listed[child_session_id]["run_kind"] == "delegated"
            assert listed[child_session_id]["parent_run_id"] == str(run_id)

        frames = []
        async for frame in composition.services.runs.stream(composition.principal, run_id, None):
            frames.append(frame)
            if len(frames) > 200 or getattr(frame, "event", None) == "run.completed":
                break
        waiting = [
            frame for frame in frames if getattr(frame, "event", None) == "run.waiting_for_approval"
        ]
        assert waiting, [getattr(frame, "event", None) for frame in frames]
        suspension = waiting[0].data["suspension"]
        assert suspension["kind"] == "child_run"
        assert suspension["child_run_ids"] == [str(child_run_id)]
        assert [frame for frame in frames if getattr(frame, "event", None) == "run.completed"]

        stranger = Principal(
            tenant_id="tenant-elsewhere",
            principal_id="stranger",
            roles={"user"},
            scopes=set(PLATFORM_SCOPES),
        )
        async with _client(composition, principal=stranger) as foreign_client:
            assert (await foreign_client.get(f"/v1/runs/{child_run_id}")).status_code == 404
            assert (await foreign_client.get(f"/v1/runs/{run_id}")).status_code == 404
        neighbour = Principal(
            tenant_id="local",
            principal_id="someone-else",
            roles={"user"},
            scopes=set(PLATFORM_SCOPES),
        )
        async with _client(composition, principal=neighbour) as neighbour_client:
            assert (await neighbour_client.get(f"/v1/runs/{child_run_id}")).status_code == 404
