"""ADR-0129: the public task-grant boundary.

The resolve body carries ``task_grant`` exactly when the decision is
``approve_for_task``; the three task-grant routes exist only behind
``BROWSER_TASK_GRANTS_ENABLED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.api import create_app
from agent_core.application.browser_task_grants import (
    PublicBrowserTaskGrantService,
    TaskGrantResolution,
)
from agent_core.application.public_services import PublicApprovalService
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalRequest, ApprovalResolutionType, ApprovalStatus
from agent_core.domain.browser import BrowserProfile, BrowserProfileStatus
from agent_core.domain.browser_act_views import task_grant_offer_summary
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_DURATION,
    BrowserTaskGrant,
    BrowserTaskGrantOffer,
    TaskGrantEcho,
    parse_task_grant_scopes,
    task_grant_id_for_approval,
)
from agent_core.domain.policies import ActionKind, PolicyDecision, PolicyDecisionType, RiskLevel
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.views import ApprovalView
from tests.contract.support import NOW, memory_uow_factory, principal

APPROVAL_ID = UUID("00000000-0000-0000-0000-0000000001a1")
ORIGIN = "https://www.example.org"
PREFIX = "/lesson"
_HTTP_SPEC = Path(__file__).resolve().parents[2] / "docs/plan/http-api-and-streaming.md"


def specified_conflict_details() -> dict[str, set[str]]:
    """The `conflict` table's "details also carries" column, by reason."""

    table = _HTTP_SPEC.read_text(encoding="utf-8").split("details also carries\n", 1)[1]
    carried: dict[str, set[str]] = {}
    for row in table.split("```", 1)[0].splitlines()[1:]:
        _code, reason, fields = row.split(maxsplit=2)
        carried[reason] = set() if fields == "(nothing)" else set(fields.split(", "))
    return carried


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


# ---------------------------------------------------------------------------
# The public boundary over the real services (0129-design section 8, O-19).
# ---------------------------------------------------------------------------

SESSION = UUID("00000000-0000-0000-0000-0000000001b1")
RUN = UUID("00000000-0000-0000-0000-0000000001b2")
PROFILE = UUID("00000000-0000-0000-0000-0000000001b3")
SCOPES = parse_task_grant_scopes(f"{ORIGIN}{PREFIX}")
READ_WRITE = ("approval.read", "approval.resolve", "browser.grant.read", "browser.grant.write")


@dataclass
class Dispatcher:
    resumed: list[UUID] = field(default_factory=list)

    async def dispatch(self, run_id: UUID) -> None:
        del run_id

    async def resume(self, run_id: UUID) -> None:
        self.resumed.append(run_id)


@dataclass
class Stack:
    uow_factory: Any
    clock: FixedClock
    dispatcher: Dispatcher
    approvals: PublicApprovalService
    task_grants: PublicBrowserTaskGrantService

    def app(self, *scopes: str, owner_update: dict[str, Any] | None = None, **flags: Any) -> Any:
        settings_values = {"browser_task_grants_enabled": True, "browser_task_grant_scopes": SCOPES}
        settings_values.update(flags)
        requesting = owner(*(scopes or READ_WRITE)).model_copy(update=owner_update or {})
        return create_app(
            services(self.approvals, browser_task_grants=self.task_grants),
            settings(**settings_values),
            requesting,
            lambda: str(APPROVAL_ID),
            _ready,
        )

    async def approval(self) -> ApprovalRequest:
        async with self.uow_factory() as uow:
            stored: ApprovalRequest = await uow.approvals.get(APPROVAL_ID, principal())
            return stored

    async def grants(self) -> list[BrowserTaskGrant]:
        async with self.uow_factory() as uow:
            found: list[BrowserTaskGrant] = await uow.browser_task_grants.list(
                principal(), now=self.clock.now()
            )
            return found

    async def events(self, event_type: str) -> list[dict[str, Any]]:
        async with self.uow_factory() as uow:
            events = await uow.events.list_after(SESSION, 0, principal())
        return [event.payload for event in events if event.event_type == event_type]


async def stack(
    *,
    offer: bool = True,
    profile_status: BrowserProfileStatus = BrowserProfileStatus.READY,
    scopes: Any = SCOPES,
    task_grants_composed: bool = True,
) -> Stack:
    clock, uow_factory = await memory_uow_factory()
    async with uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=SESSION,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=SessionStatus.ACTIVE,
                metadata={"browser_profile_id": str(PROFILE)},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.runs.create(
            Run(
                id=RUN,
                session_id=SESSION,
                tenant_id=principal().tenant_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=RunStatus.WAITING_FOR_APPROVAL,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.browser_profiles.create(
            BrowserProfile(
                id=PROFILE,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                provider_name="hosted-isolated",
                provider_ref="opaque-provider-reference",
                allowed_origins=(ORIGIN,),
                status=profile_status,
                generation=5,
                encryption_key_version="key-v1",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.approvals.create(
            ApprovalRequest(
                id=APPROVAL_ID,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                session_id=SESSION,
                run_id=RUN,
                action_kind=ActionKind.TOOL_CALL,
                action_id=UUID(int=0x1C1),
                tool_invocation_id=UUID(int=0x1C1),
                status=ApprovalStatus.PENDING,
                action_summary="Click a button on www.example.org/lesson/unit-3",
                tool_name="browser.act",
                arguments={"view": "browser.act.v1", "described": True, "kind": "click"},
                normalized_arguments_hash="hash",
                required_scopes=set(),
                agent_version="agent-v1",
                risk=RiskLevel.HIGH,
                policy_reason="policy.matrix.external_write",
                policy_decision=PolicyDecision(
                    decision=PolicyDecisionType.REQUIRE_APPROVAL,
                    reason_code="policy.matrix.external_write",
                    explanation="External writes require approval.",
                    policy_version="policy-v1",
                ),
                policy_version="policy-v1",
                expires_at=NOW + timedelta(minutes=5),
                created_at=NOW,
                task_grant_offer=(
                    BrowserTaskGrantOffer(
                        origin=ORIGIN,
                        path_prefix=PREFIX,
                        summary=task_grant_offer_summary(SCOPES[0]),
                    )
                    if offer
                    else None
                ),
            )
        )
    dispatcher = Dispatcher()

    async def resume_waiting_run(uow: Any, run: Run) -> Run:
        del uow
        return run

    return Stack(
        uow_factory=uow_factory,
        clock=clock,
        dispatcher=dispatcher,
        approvals=PublicApprovalService(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            resume_waiting_run=resume_waiting_run,
            self_approval_enabled=True,
            task_grants=(
                TaskGrantResolution(scopes=scopes, clock=clock) if task_grants_composed else None
            ),
        ),
        task_grants=PublicBrowserTaskGrantService(uow_factory=uow_factory, clock=clock),
    )


ECHO = {"origin": ORIGIN, "path_prefix": PREFIX}
RESOLVE = f"/v1/approvals/{APPROVAL_ID}/resolve"


async def post(app: Any, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        return await client.post(path, json=body)


async def get(app: Any, path: str, **params: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        return await client.get(path, params=params)


async def test_approve_for_task_approves_once_and_creates_the_grant() -> None:
    subject = await stack()

    response = await post(
        subject.app(), RESOLVE, {"decision": "approve_for_task", "task_grant": ECHO}
    )
    assert response.status_code == 200, response.text
    [grant] = await subject.grants()
    stored = await subject.approval()

    grant_id = task_grant_id_for_approval(APPROVAL_ID)
    assert response.json()["status"] == "APPROVED"
    assert response.json()["decision"] == "approve_for_task"
    assert response.json()["task_grant_id"] == str(grant_id)
    assert (stored.status, stored.task_grant_id) == (ApprovalStatus.APPROVED, grant_id)
    assert (grant.id, grant.origin, grant.path_prefix) == (grant_id, ORIGIN, PREFIX)
    assert (grant.profile_id, grant.profile_generation) == (PROFILE, 5)
    assert (grant.agent_version, grant.policy_version) == ("agent-v1", "policy-v1")
    assert grant.expires_at - grant.created_at == TASK_GRANT_DURATION
    assert await subject.events("approval.resolved") == [
        {
            "approval_id": str(APPROVAL_ID),
            "resolution": "approve_for_task",
            "task_grant_id": str(grant_id),
        }
    ]
    [created] = await subject.events("browser.task_grant.created")
    assert created["grant_id"] == str(grant_id)
    assert (created["max_actions"], created["max_typed_characters"]) == (200, 4096)
    assert subject.dispatcher.resumed == [RUN]


async def test_approve_for_task_needs_the_grant_scope_and_the_owner() -> None:
    subject = await stack()

    no_scope = await post(
        subject.app("approval.resolve"),
        RESOLVE,
        {"decision": "approve_for_task", "task_grant": ECHO},
    )
    foreign = await post(
        subject.app(owner_update={"principal_id": "principal-b"}),
        RESOLVE,
        {"decision": "approve_for_task", "task_grant": ECHO},
    )

    assert no_scope.status_code == 403
    assert "browser.grant.write" in no_scope.text
    assert foreign.status_code == 404
    assert (await subject.approval()).status is ApprovalStatus.PENDING
    assert await subject.grants() == []


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no offer", "task_grant_unavailable"),
        ("flag off", "task_grant_unavailable"),
        ("scope removed", "task_grant_unavailable"),
        ("profile not ready", "task_grant_unavailable"),
        ("echo mismatch", "task_grant_offer_mismatch"),
    ],
)
async def test_approve_for_task_failures_leave_the_approval_pending(case: str, reason: str) -> None:
    subject = await stack(
        offer=case != "no offer",
        task_grants_composed=case != "flag off",
        scopes=parse_task_grant_scopes(f"{ORIGIN}/practice") if case == "scope removed" else SCOPES,
        profile_status=(
            BrowserProfileStatus.NEEDS_USER
            if case == "profile not ready"
            else BrowserProfileStatus.READY
        ),
    )
    echo = {"origin": ORIGIN, "path_prefix": "/practice"} if case == "echo mismatch" else ECHO

    response = await post(
        subject.app(), RESOLVE, {"decision": "approve_for_task", "task_grant": echo}
    )

    assert response.status_code == 409, case
    details = response.json()["error"]["details"]
    assert details["reason"] == reason
    assert set(details) == {"reason", *specified_conflict_details()[reason]}, case
    assert (await subject.approval()).status is ApprovalStatus.PENDING
    assert await subject.grants() == []


async def test_approve_for_task_is_retry_safe() -> None:
    subject = await stack()
    body = {"decision": "approve_for_task", "task_grant": ECHO}

    first = await post(subject.app(), RESOLVE, body)
    again = await post(subject.app(), RESOLVE, body)
    other_echo = await post(
        subject.app(),
        RESOLVE,
        {"decision": "approve_for_task", "task_grant": {**ECHO, "path_prefix": "/practice"}},
    )
    other_decision = await post(subject.app(), RESOLVE, {"decision": "deny"})

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert len(await subject.grants()) == 1
    assert len(await subject.events("browser.task_grant.created")) == 1
    carried = specified_conflict_details()
    assert other_echo.status_code == 409
    assert other_echo.json()["error"]["details"]["reason"] == "task_grant_offer_mismatch"
    assert set(other_echo.json()["error"]["details"]) == {
        "reason",
        *carried["task_grant_offer_mismatch"],
    }
    assert other_decision.status_code == 409
    assert other_decision.json()["error"]["details"]["reason"] == "approval_already_resolved"
    assert set(other_decision.json()["error"]["details"]) == {
        "reason",
        *carried["approval_already_resolved"],
    }


async def test_a_new_task_grant_supersedes_the_sessions_previous_one() -> None:
    subject = await stack()
    async with subject.uow_factory() as uow:
        await uow.browser_task_grants.create(
            BrowserTaskGrant(
                id=UUID(int=0x9A),
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                session_id=SESSION,
                profile_id=PROFILE,
                profile_generation=5,
                agent_version="agent-v1",
                policy_version="policy-v1",
                origin=ORIGIN,
                path_prefix=PREFIX,
                max_actions=200,
                actions_used=3,
                typed_characters=0,
                approval_id=UUID(int=0x9B),
                approved_by=principal().principal_id,
                created_at=NOW,
                expires_at=NOW + TASK_GRANT_DURATION,
            )
        )

    response = await post(
        subject.app(), RESOLVE, {"decision": "approve_for_task", "task_grant": ECHO}
    )

    assert response.status_code == 200, response.text
    ended = await subject.events("browser.task_grant.ended")
    assert ended == [
        {
            "grant_id": str(UUID(int=0x9A)),
            "reason": "superseded",
            "actions_used": 3,
            "typed_characters": 0,
        }
    ]


GRANTS = "/v1/browser-task-grants"


async def resolved_stack() -> tuple[Stack, UUID]:
    subject = await stack()
    response = await post(
        subject.app(), RESOLVE, {"decision": "approve_for_task", "task_grant": ECHO}
    )
    assert response.status_code == 200, response.text
    return subject, task_grant_id_for_approval(APPROVAL_ID)


async def test_task_grants_are_listed_read_and_revoked() -> None:
    subject, grant_id = await resolved_stack()
    app = subject.app()

    listed = await get(app, GRANTS, session_id=str(SESSION), status="active")
    read = await get(app, f"{GRANTS}/{grant_id}")
    revoked = await post(app, f"{GRANTS}/{grant_id}/revoke")
    again = await post(app, f"{GRANTS}/{grant_id}/revoke")
    active_after = await get(app, GRANTS, status="active")
    everything = await get(app, GRANTS, status="all", limit=1)

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [str(grant_id)]
    assert read.json()["status"] == "active"
    assert read.json()["path_prefix"] == PREFIX
    assert "tenant_id" not in read.text and "policy_version" not in read.text
    assert revoked.status_code == 200
    assert (revoked.json()["status"], revoked.json()["end_reason"]) == ("revoked", "revoked")
    assert again.json() == revoked.json()
    assert len(await subject.events("browser.task_grant.ended")) == 1
    assert active_after.json()["items"] == []
    assert [item["id"] for item in everything.json()["items"]] == [str(grant_id)]
    assert everything.json()["next_cursor"] is None


async def test_task_grant_routes_validate_their_input() -> None:
    subject, _grant_id = await resolved_stack()
    app = subject.app()

    bad_status = await get(app, GRANTS, status="finished")
    bad_cursor = await get(app, GRANTS, cursor="not-a-cursor")
    bad_limit = await get(app, GRANTS, limit=0)
    bad_id = await get(app, f"{GRANTS}/not-a-uuid")

    for response in (bad_status, bad_cursor, bad_limit, bad_id):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "malformed_request"


async def test_task_grant_routes_need_their_scopes_and_the_owner() -> None:
    subject, grant_id = await resolved_stack()

    list_without_read = await get(subject.app("browser.grant.write"), GRANTS)
    get_without_read = await get(subject.app("browser.grant.write"), f"{GRANTS}/{grant_id}")
    revoke_without_write = await post(
        subject.app("browser.grant.read"), f"{GRANTS}/{grant_id}/revoke"
    )
    foreign = subject.app(owner_update={"principal_id": "principal-b"})
    foreign_get = await get(foreign, f"{GRANTS}/{grant_id}")
    foreign_revoke = await post(foreign, f"{GRANTS}/{grant_id}/revoke")
    foreign_session = await get(foreign, GRANTS, session_id=str(SESSION))

    assert (
        list_without_read.status_code,
        get_without_read.status_code,
        revoke_without_write.status_code,
    ) == (403, 403, 403)
    assert (foreign_get.status_code, foreign_revoke.status_code, foreign_session.status_code) == (
        404,
        404,
        404,
    )
    assert (await subject.grants())[0].ended_at is None


async def test_a_repository_failure_is_the_generic_envelope() -> None:
    subject, grant_id = await resolved_stack()

    class Failing(PublicBrowserTaskGrantService):
        async def get(self, principal: Principal, grant_id: UUID) -> Any:
            raise RuntimeError(f"database says {grant_id} secret-row-text")

    app = create_app(
        services(subject.approvals, browser_task_grants=Failing(uow_factory=None, clock=None)),  # type: ignore[arg-type]
        settings(browser_task_grants_enabled=True, browser_task_grant_scopes=SCOPES),
        owner(*READ_WRITE),
        lambda: str(APPROVAL_ID),
        _ready,
    )
    response = await get(app, f"{GRANTS}/{grant_id}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret-row-text" not in response.text


def _scoped_routes(app: Any) -> dict[tuple[str, str], str | None]:
    """Every operation and its declared scope, flag-mounted routers included."""

    return {
        (method.upper(), path): operation.get("required_scope")
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
    }


async def test_task_grant_routes_exist_only_with_the_flag() -> None:
    subject, grant_id = await resolved_stack()
    off = subject.app(browser_task_grants_enabled=False)
    on = subject.app()

    missing = [
        await get(off, GRANTS),
        await get(off, f"{GRANTS}/{grant_id}"),
        await post(off, f"{GRANTS}/{grant_id}/revoke"),
    ]
    added = set(_scoped_routes(on).items()) - set(_scoped_routes(off).items())

    assert [response.status_code for response in missing] == [404, 404, 404]
    assert added == {
        (("GET", GRANTS), "browser.grant.read"),
        (("GET", f"{GRANTS}/{{grant_id}}"), "browser.grant.read"),
        (("POST", f"{GRANTS}/{{grant_id}}/revoke"), "browser.grant.write"),
    }
