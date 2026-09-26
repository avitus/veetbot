"""ADR-0129: the public task-grant boundary.

The resolve body carries ``task_grant`` exactly when the decision is
``approve_for_task``; the three task-grant routes exist only behind
``BROWSER_TASK_GRANTS_ENABLED``.
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import UUID

import httpx

from agent_core.api import create_app
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser_task_grants import TaskGrantEcho
from agent_core.domain.views import ApprovalView
from tests.contract.support import NOW, principal

APPROVAL_ID = UUID("00000000-0000-0000-0000-0000000001a1")
ORIGIN = "https://www.example.org"
PREFIX = "/lesson"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "people_enabled": False,
        "database_url": "postgresql+asyncpg://unused/agent",
        "deployment_mode": DeploymentMode.DEVELOPMENT,
        "auth_mode": AuthMode.DEV,
        "auth_token": None,
        "sandbox": SandboxMechanism.FAKE,
        "config_dir": None,
        "credentials": MappingProxyType({}),
        "interpolation": MappingProxyType({"OPENAI_MODEL": ""}),
    }
    return Settings(**(values | overrides))


def approval_view(decision: ApprovalResolutionType | None = None) -> ApprovalView:
    return ApprovalView(
        id=APPROVAL_ID,
        run_id=UUID(int=2),
        session_id=UUID(int=1),
        status="APPROVED" if decision is not None else "PENDING",
        tool_name="browser.act",
        action_summary="Click a button on www.example.org/lesson",
        arguments={"view": "browser.act.v1", "described": False, "kind": "click"},
        risk="HIGH",
        policy_reason="policy.matrix.external_write",
        expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        resolved_at=None if decision is None else NOW,
        resolved_by=None if decision is None else "principal-a",
        decision=decision,
    )


class RecordingApprovals:
    def __init__(self) -> None:
        self.resolved: list[tuple[ApprovalResolutionType, TaskGrantEcho | None]] = []

    async def resolve(
        self,
        owner: Principal,
        approval_id: UUID,
        decision: ApprovalResolutionType,
        reason: str | None,
        *,
        task_grant: TaskGrantEcho | None = None,
    ) -> ApprovalView:
        del owner, reason
        assert approval_id == APPROVAL_ID
        self.resolved.append((decision, task_grant))
        return approval_view(decision)


async def _ready() -> bool:
    return True


def owner(*scopes: str) -> Principal:
    return principal().model_copy(update={"scopes": set(scopes)})


def services(approvals: Any = None, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=None,
        runs=None,
        approvals=approvals,
        artifacts=None,
        browser_profiles=None,
        browser_grants=None,
        **extra,
    )


async def test_resolve_body_requires_task_grant_exactly_with_approve_for_task() -> None:
    approvals = RecordingApprovals()
    app = create_app(
        services(approvals),
        settings(),
        owner("approval.resolve", "browser.grant.write"),
        lambda: str(APPROVAL_ID),
        _ready,
    )
    echo = {"origin": ORIGIN, "path_prefix": PREFIX}
    path = f"/v1/approvals/{APPROVAL_ID}/resolve"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        malformed = {
            "no task grant": await client.post(path, json={"decision": "approve_for_task"}),
            "task grant with approve_once": await client.post(
                path, json={"decision": "approve_once", "task_grant": echo}
            ),
            "task grant with deny": await client.post(
                path, json={"decision": "deny", "task_grant": echo}
            ),
            "an extra echo field": await client.post(
                path,
                json={"decision": "approve_for_task", "task_grant": {**echo, "max_actions": 999}},
            ),
            "a null task grant": await client.post(
                path, json={"decision": "approve_for_task", "task_grant": None}
            ),
        }
        accepted = await client.post(
            path, json={"decision": "approve_for_task", "task_grant": echo}
        )
        once = await client.post(path, json={"decision": "approve_once"})

    statuses = {name: response.status_code for name, response in malformed.items()}
    assert statuses == dict.fromkeys(malformed, 400)
    assert {response.json()["error"]["code"] for response in malformed.values()} == {
        "malformed_request"
    }
    assert accepted.status_code == 200, accepted.text
    assert once.status_code == 200, once.text
    assert approvals.resolved == [
        (
            ApprovalResolutionType.APPROVE_FOR_TASK,
            TaskGrantEcho(origin=ORIGIN, path_prefix=PREFIX),
        ),
        (ApprovalResolutionType.APPROVE_ONCE, None),
    ]
