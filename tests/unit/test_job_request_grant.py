"""ADR-0344: one approval for a request that changes a saved job.

A repair reads untrusted content, so the trust overlay asks before each later
job write. A card for a covered job action offers "Allow for this request";
resolving it with ``approve_for_request`` records the grant, which then covers
that one job's trials, saves, runs and schedule repins whose access fits a
ceiling the owner already approved, for the rest of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from agent_core.application.job_grants import revision_grant_id
from agent_core.application.job_request_grants import (
    JobRequestGrantAuthorizer,
    JobRequestGrantResolution,
    job_request_grant_offer,
)
from agent_core.application.public_services import PublicApprovalService
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.browser_request_grants import RequestGrantEcho
from agent_core.domain.errors import AuthorizationError, ConflictError
from agent_core.domain.events import NewEvent
from agent_core.domain.job_request_grants import (
    JOB_REQUEST_GRANT_AUTHORIZATION_KIND,
    JOB_REQUEST_GRANT_CREATED_EVENT,
    JobAccessCeiling,
    JobRequestGrantOffer,
    job_request_grant_summary,
)
from agent_core.domain.jobs import (
    JobAccess,
    JobGrant,
    JobManifest,
    JobRecord,
    JobRevision,
)
from agent_core.domain.policies import (
    ActionKind,
    AuthorizationTurn,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind, RunStatus
from agent_core.domain.schedules import JobTask
from agent_core.domain.sessions import Session, SessionStatus
from tests.contract.support import NOW, memory_uow_factory, principal, tool_context
from tests.contract.test_schedule_repository_contract import revision as schedule_revision
from tests.contract.test_schedule_repository_contract import schedule as a_schedule

SLUG = "emerging-market-radar"
SESSION = UUID("00000000-0000-0000-0000-00000000fb01")
RUN = UUID("00000000-0000-0000-0000-00000000fb02")
APPROVAL = UUID("00000000-0000-0000-0000-00000000fb03")
OTHER_RUN = UUID("00000000-0000-0000-0000-00000000fb04")
SCHEDULE = UUID("00000000-0000-0000-0000-00000000fb05")
OLD_REVISION = UUID(int=0x101)
NEW_REVISION = UUID(int=0x102)
GRANT = UUID(int=0x201)
WHEN = NOW + timedelta(minutes=10)
OWNER_TURN = AuthorizationTurn(
    newest_user_trust=TrustLevel.USER,
    tool_names=frozenset({"cockpit.read", "run.inspect", "job.manage"}),
)
MANIFEST = {"title": "Radar", "calls": ["mcp.read", "feed.publish"], "feeds": ["agent.radar"]}


def access(**update: Any) -> JobAccess:
    """The radar's approved access: one MCP server and one feed."""

    base = JobAccess(
        inputs={"config": {"news_queries": ["seed"]}},
        hosts={},
        feeds=frozenset({"agent.radar"}),
        timeout_seconds=60,
        maximum_requests=16,
        maximum_response_bytes=49152,
        model_cost="0.15",
        mcp_servers=frozenset({"svp_read"}),
    )
    return replace(base, **update)


def access_json(value: JobAccess) -> dict[str, Any]:
    return {
        "inputs": value.inputs,
        "hosts": value.hosts,
        "feeds": sorted(value.feeds),
        "timeout_seconds": value.timeout_seconds,
        "maximum_requests": value.maximum_requests,
        "maximum_response_bytes": value.maximum_response_bytes,
        "model_cost": value.model_cost,
        "mcp_servers": sorted(value.mcp_servers),
    }


def job(*, slug: str = SLUG, grants: dict[str, JobGrant] | None = None) -> JobRecord:
    owner = principal()
    revisions = {
        str(item): JobRevision(
            id=item,
            manifest=JobManifest.model_validate(MANIFEST),
            source="def main(inputs, job): return 1",
            state="live",
            commit_sha="a" * 40,
            created_at=NOW,
        )
        for item in (OLD_REVISION, NEW_REVISION)
    }
    grant = JobGrant(
        id=revision_grant_id(UUID(int=0x301), OLD_REVISION),
        access=access(),
        created_at=NOW,
        approval_id=UUID(int=0x301),
    )
    return JobRecord(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        slug=slug,
        version=1,
        live_revision_id=OLD_REVISION,
        revisions=revisions,
        grants={str(grant.id): grant} if grants is None else grants,
        updated_at=NOW,
    )


def old_grant_id() -> UUID:
    return revision_grant_id(UUID(int=0x301), OLD_REVISION)


def a_run(**update: Any) -> Run:
    return Run(
        id=RUN,
        session_id=SESSION,
        tenant_id=principal().tenant_id,
        agent_id=UUID(int=0xA6),
        agent_version="agent-v1",
        status=RunStatus.WAITING_FOR_APPROVAL,
        created_at=NOW,
        updated_at=NOW,
    ).model_copy(update=update)


def offer(*ceilings: JobAccess) -> JobRequestGrantOffer:
    return JobRequestGrantOffer(
        slug=SLUG,
        ceilings=tuple(JobAccessCeiling.from_access(item) for item in (ceilings or (access(),))),
        summary=job_request_grant_summary(SLUG),
    )


def approval(**update: Any) -> ApprovalRequest:
    return ApprovalRequest(
        id=APPROVAL,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=SESSION,
        run_id=RUN,
        action_kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=0x1C1),
        tool_invocation_id=UUID(int=0x1C1),
        status=ApprovalStatus.PENDING,
        action_summary="Publish something with job.manage",
        tool_name="job.manage",
        arguments={"operation": "trial", "slug": SLUG, "grant_id": str(old_grant_id())},
        normalized_arguments_hash="hash",
        required_scopes=set(),
        agent_version="agent-v1",
        risk=RiskLevel.MEDIUM,
        policy_reason="policy.tool.job.manage",
        policy_decision=decision(),
        policy_version="policy-v1",
        expires_at=NOW + timedelta(hours=4),
        created_at=NOW,
        job_request_grant_offer=offer(),
    ).model_copy(update=update)


def decision(version: str = "policy-v1") -> PolicyDecision:
    return PolicyDecision(
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        reason_code="policy.tool.job.manage",
        explanation="The default-jobs profile evaluated publication.",
        policy_version=version,
        untrusted_escalation=True,
    )


def proposed(name: str, arguments: dict[str, Any]) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=0xAC),
        tenant_id=principal().tenant_id,
        session_id=SESSION,
        run_id=RUN,
        step_number=3,
        name=name,
        version="1.0.0",
        summary=f"Use {name}.",
        side_effect=SideEffectClass.PUBLICATION
        if name == "job.manage"
        else SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        arguments=arguments,
        normalized_arguments_hash="hash",
        argument_trust=dict.fromkeys(arguments, TrustLevel.EXTERNAL_UNTRUSTED),
        origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        evaluated_at=WHEN,
    )


def trial() -> tuple[str, dict[str, Any]]:
    return "job.manage", {
        "operation": "trial",
        "slug": SLUG,
        "grant_id": str(old_grant_id()),
        "source": "def main(inputs, job): return 2",
        "manifest": MANIFEST,
    }


def update() -> tuple[str, dict[str, Any]]:
    return "job.manage", {
        "operation": "update",
        "slug": SLUG,
        "expected_revision": str(OLD_REVISION),
        "source": "def main(inputs, job): return 2",
        "manifest": MANIFEST,
    }


def job_run(value: JobAccess | None = None) -> tuple[str, dict[str, Any]]:
    return "job.run", {
        "slug": SLUG,
        "revision_id": str(NEW_REVISION),
        "access": access_json(value or access(inputs={"config": {"news_queries": ["launch"]}})),
    }


def repin(**extra: Any) -> tuple[str, dict[str, Any]]:
    return "schedule.update", {
        "schedule_id": str(SCHEDULE),
        "expected_revision": 1,
        "task": {
            "kind": "job",
            "slug": SLUG,
            "revision_id": str(NEW_REVISION),
            "grant_id": str(old_grant_id()),
        },
        **extra,
    }


@dataclass
class Dispatcher:
    resumed: list[UUID]

    async def dispatch(self, run_id: UUID) -> None:
        del run_id

    async def resume(self, run_id: UUID) -> None:
        self.resumed.append(run_id)


@dataclass
class _Clock:
    values: list[datetime]

    def now(self) -> datetime:
        return self.values[0]

    async def sleep(self, seconds: float) -> None:
        del seconds


@dataclass
class World:
    uow_factory: Any
    clock: list[datetime]
    dispatcher: Dispatcher

    def service(self, *, composed: bool = True) -> PublicApprovalService:
        async def resume_waiting_run(uow: Any, run: Run) -> Run:
            del uow
            return run

        return PublicApprovalService(
            uow_factory=self.uow_factory,
            dispatcher=self.dispatcher,
            resume_waiting_run=resume_waiting_run,
            self_approval_enabled=True,
            job_request_grants=(
                JobRequestGrantResolution(clock=_Clock(self.clock)) if composed else None
            ),
        )

    async def authorize(
        self,
        action: tuple[str, dict[str, Any]],
        *,
        run_update: dict[str, Any] | None = None,
        turn: AuthorizationTurn | None = OWNER_TURN,
        policy_version: str = "policy-v1",
        agent_version: str = "agent-v1",
    ) -> StandingAuthorization:
        name, arguments = action
        return await JobRequestGrantAuthorizer(
            uow_factory=self.uow_factory, now=lambda: self.clock[0]
        ).authorize(
            action=proposed(name, arguments),
            decision=decision(policy_version),
            principal=principal(),
            run=a_run(status=RunStatus.RUNNING, **(run_update or {})),
            agent_version=agent_version,
            action_deadline=self.clock[0] + timedelta(seconds=30),
            turn=turn,
        )

    async def offer_for(
        self,
        action: tuple[str, dict[str, Any]],
        *,
        run_update: dict[str, Any] | None = None,
        turn: AuthorizationTurn | None = OWNER_TURN,
    ) -> JobRequestGrantOffer | None:
        name, arguments = action
        async with self.uow_factory() as uow:
            return await job_request_grant_offer(
                uow,
                tool_name=name,
                arguments=arguments,
                run=a_run(**(run_update or {})),
                principal=principal(),
                turn=turn,
            )

    async def events(self, event_type: str) -> list[dict[str, Any]]:
        async with self.uow_factory() as uow:
            events = await uow.events.list_after(SESSION, 0, principal())
        return [event.payload for event in events if event.event_type == event_type]

    async def append(self, event_type: str, payload: dict[str, Any], run_id: UUID = RUN) -> None:
        async with self.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=SESSION,
                    run_id=run_id,
                    event_type=event_type,
                    actor_type="application",
                    payload=payload,
                )
            )

    async def stored(self) -> ApprovalRequest:
        async with self.uow_factory() as uow:
            found: ApprovalRequest = await uow.approvals.get(APPROVAL, principal())
            return found

    async def saved_job(self) -> JobRecord:
        async with self.uow_factory() as uow:
            found: JobRecord = await uow.jobs.get(principal(), SLUG)
            return found


async def world(
    *,
    granted: bool = False,
    metadata: dict[str, Any] | None = None,
    approval_update: dict[str, Any] | None = None,
    saved: JobRecord | None = None,
) -> World:
    _fixed, uow_factory = await memory_uow_factory()
    async with uow_factory() as uow:
        await uow.sessions.create(
            Session(
                id=SESSION,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                agent_id=UUID(int=0xA6),
                agent_version="agent-v1",
                status=SessionStatus.ACTIVE,
                metadata=metadata or {},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.runs.create(a_run())
        await uow.jobs.put(saved or job(), expected_version=None)
        await uow.jobs.put(job(slug="other-job"), expected_version=None)
        pinned = schedule_revision(SCHEDULE).model_copy(
            update={
                "task": JobTask(slug=SLUG, revision_id=OLD_REVISION, grant_id=old_grant_id()),
            }
        )
        await uow.schedules.create(a_schedule(schedule_id=SCHEDULE), pinned)
        stored = approval(**(approval_update or {}))
        if granted:
            stored = stored.model_copy(
                update={
                    "status": ApprovalStatus.APPROVED,
                    "resolution": ApprovalResolutionType.APPROVE_FOR_REQUEST,
                    "resolved_at": NOW,
                    "resolved_by": principal().principal_id,
                }
            )
        await uow.approvals.create(stored)
    subject = World(uow_factory=uow_factory, clock=[WHEN], dispatcher=Dispatcher([]))
    if granted:
        await subject.append(
            JOB_REQUEST_GRANT_CREATED_EVENT, {"approval_id": str(APPROVAL), "slug": SLUG}
        )
    return subject


def owner(*scopes: str) -> Any:
    return principal().model_copy(update={"scopes": set(scopes)})


# Ceilings.


@pytest.mark.parametrize(
    "candidate,fits",
    [
        (access(), True),
        (access(inputs={"config": {"news_queries": ["launch"]}}), True),
        (access(timeout_seconds=30, maximum_requests=8, model_cost="0"), True),
        (access(feeds=frozenset()), True),
        (access(hosts={"api.example.com": None}), False),
        (access(feeds=frozenset({"agent.radar", "agent.other"})), False),
        (access(mcp_servers=frozenset({"svp_read", "gmail"})), False),
        (access(timeout_seconds=61), False),
        (access(maximum_requests=17), False),
        (access(model_cost="0.16"), False),
    ],
)
def test_a_ceiling_holds_equal_or_narrower_access_only(candidate: JobAccess, fits: bool) -> None:
    assert JobAccessCeiling.from_access(access()).covers(candidate) is fits


def test_a_host_fits_only_with_the_same_credential_reference() -> None:
    reader, writer = f"stored:{UUID(int=0xE1)}", f"stored:{UUID(int=0xE2)}"
    ceiling = JobAccessCeiling.from_access(access(hosts={"api.example.com": reader}))
    assert ceiling.covers(access(hosts={"api.example.com": reader}))
    assert not ceiling.covers(access(hosts={"api.example.com": writer}))
    assert not ceiling.covers(access(hosts={"api.example.com": None}))


# The offer.


@pytest.mark.parametrize("action", [trial(), update(), job_run(), repin(title="Radar collector")])
async def test_a_covered_job_card_offers_the_request(action: tuple[str, dict[str, Any]]) -> None:
    subject = await world()
    made = await subject.offer_for(action)
    assert made is not None
    assert made.slug == SLUG
    assert made.duration_seconds == 7200
    assert SLUG in made.summary
    assert JobAccessCeiling.from_access(access()) in made.ceilings


async def test_the_offer_holds_the_cards_own_new_access_and_existing_grants() -> None:
    subject = await world()
    wider = access(maximum_requests=20)
    made = await subject.offer_for(job_run(wider))
    assert made is not None
    assert JobAccessCeiling.from_access(wider) in made.ceilings
    assert JobAccessCeiling.from_access(access()) in made.ceilings


@pytest.mark.parametrize(
    "case",
    [
        "job_run",
        "schedule_session",
        "child_run",
        "refresh_session",
        "foreign_turn",
        "no_turn",
        "archive",
        "create",
        "cadence",
        "mcp_servers",
        "other_tool",
        "other_schedule_job",
    ],
)
async def test_no_offer_outside_an_owner_request_or_for_an_uncovered_action(case: str) -> None:
    markers: dict[str, dict[str, Any]] = {
        "refresh_session": {"cockpit_refresh": True},
        "schedule_session": {"schedule_id": str(SCHEDULE)},
    }
    metadata = markers.get(case)
    subject = await world(metadata=metadata)
    action: tuple[str, dict[str, Any]] = trial()
    run_update: dict[str, Any] = {}
    turn: AuthorizationTurn | None = OWNER_TURN
    if case == "job_run":
        run_update = {"kind": RunKind.JOB}
    elif case == "child_run":
        run_update = {"parent_run_id": OTHER_RUN}
    elif case == "foreign_turn":
        turn = AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    elif case == "no_turn":
        turn = None
    elif case == "archive":
        action = ("job.manage", {**update()[1], "operation": "archive"})
    elif case == "create":
        action = ("job.manage", {**update()[1], "operation": "create"})
    elif case == "cadence":
        action = repin(cadence={"kind": "daily", "local_time": "07:00"})
    elif case == "mcp_servers":
        action = repin(use_mcp_servers=["gmail"])
    elif case == "other_tool":
        action = ("schedule.create", repin()[1])
    elif case == "other_schedule_job":
        action = (
            "schedule.update",
            {**repin()[1], "task": {**repin()[1]["task"], "slug": "other-job"}},
        )
    assert await subject.offer_for(action, run_update=run_update, turn=turn) is None


# Resolution.


async def test_approve_for_request_approves_once_and_records_the_job_grant() -> None:
    subject = await world()
    view = await subject.service().resolve(
        owner("approval.resolve", "jobs.run"),
        APPROVAL,
        ApprovalResolutionType.APPROVE_FOR_REQUEST,
        None,
        request_grant=RequestGrantEcho(job_slug=SLUG),
    )
    assert view.status == "APPROVED"
    assert view.decision is ApprovalResolutionType.APPROVE_FOR_REQUEST
    created = await subject.events(JOB_REQUEST_GRANT_CREATED_EVENT)
    assert [(item["approval_id"], item["slug"]) for item in created] == [(str(APPROVAL), SLUG)]
    stored = await subject.stored()
    assert stored.resolved_at is not None
    assert created[0]["expires_at"] == (stored.resolved_at + timedelta(hours=2)).isoformat()
    assert subject.dispatcher.resumed == [RUN]
    # A retry of the same allow returns the stored view.
    again = await subject.service().resolve(
        owner("approval.resolve", "jobs.run"),
        APPROVAL,
        ApprovalResolutionType.APPROVE_FOR_REQUEST,
        None,
        request_grant=RequestGrantEcho(job_slug=SLUG),
    )
    assert again.decision is ApprovalResolutionType.APPROVE_FOR_REQUEST
    assert len(await subject.events(JOB_REQUEST_GRANT_CREATED_EVENT)) == 1


async def test_allowing_a_job_request_needs_the_jobs_run_scope() -> None:
    subject = await world()
    with pytest.raises(AuthorizationError):
        await subject.service().resolve(
            owner("approval.resolve"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=RequestGrantEcho(job_slug=SLUG),
        )
    assert (await subject.stored()).status is ApprovalStatus.PENDING


@pytest.mark.parametrize(
    "case,reason",
    [
        ("other_slug", "request_grant_offer_mismatch"),
        ("profile_echo", "request_grant_unavailable"),
        ("no_offer", "request_grant_unavailable"),
        ("not_composed", "request_grant_unavailable"),
    ],
)
async def test_a_stale_or_different_job_offer_leaves_the_card_pending(
    case: str, reason: str
) -> None:
    subject = await world(
        approval_update={"job_request_grant_offer": None} if case == "no_offer" else None
    )
    echo = (
        RequestGrantEcho(job_slug="other-job")
        if case == "other_slug"
        else RequestGrantEcho(profile_id=UUID(int=5))
        if case == "profile_echo"
        else RequestGrantEcho(job_slug=SLUG)
    )
    with pytest.raises(ConflictError) as raised:
        await subject.service(composed=case != "not_composed").resolve(
            owner("approval.resolve", "jobs.run", "browser.grant.write"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=echo,
        )
    assert raised.value.reason == reason
    assert (await subject.stored()).status is ApprovalStatus.PENDING
    assert await subject.events(JOB_REQUEST_GRANT_CREATED_EVENT) == []


def test_the_echo_names_exactly_one_of_a_profile_and_a_job() -> None:
    with pytest.raises(ValueError):
        RequestGrantEcho()
    with pytest.raises(ValueError):
        RequestGrantEcho(profile_id=UUID(int=5), job_slug=SLUG)


# Coverage.


@pytest.mark.parametrize("action", [trial(), update(), job_run(), repin(title="Radar collector")])
async def test_the_grant_covers_later_changes_to_the_same_job(
    action: tuple[str, dict[str, Any]],
) -> None:
    subject = await world(granted=True)
    result = await subject.authorize(action)
    assert result.allowed, result.reason_code
    assert result.authorization_kind == JOB_REQUEST_GRANT_AUTHORIZATION_KIND
    assert result.authorization_ref == str(APPROVAL)


@pytest.mark.parametrize(
    "action,reason",
    [
        (job_run(access(hosts={"api.example.com": None})), "job.request_grant.access"),
        (job_run(access(feeds=frozenset({"agent.radar", "agent.x"}))), "job.request_grant.access"),
        (job_run(access(mcp_servers=frozenset({"svp_read", "gmail"}))), "job.request_grant.access"),
        (job_run(access(maximum_requests=50)), "job.request_grant.access"),
        (
            ("job.manage", {**update()[1], "manifest": {**MANIFEST, "feeds": ["agent.x"]}}),
            "job.request_grant.access",
        ),
        (("job.manage", {**update()[1], "operation": "archive"}), "job.request_grant.operation"),
        (("job.manage", {**update()[1], "slug": "other-job"}), "job.request_grant.other_job"),
        (repin(cadence={"kind": "daily", "local_time": "07:00"}), "job.request_grant.schedule"),
        (repin(at="2026-08-01T07:00:00Z"), "job.request_grant.schedule"),
    ],
)
async def test_new_access_other_jobs_and_timing_changes_still_ask(
    action: tuple[str, dict[str, Any]], reason: str
) -> None:
    subject = await world(granted=True)
    result = await subject.authorize(action)
    assert not result.allowed
    assert result.reason_code == reason


async def test_a_schedule_running_another_job_is_not_repinned() -> None:
    subject = await world(granted=True)
    async with subject.uow_factory() as uow:
        other = schedule_revision(OTHER_RUN).model_copy(
            update={
                "task": JobTask(slug="other-job", revision_id=OLD_REVISION, grant_id=old_grant_id())
            }
        )
        await uow.schedules.create(a_schedule(schedule_id=OTHER_RUN), other)
    result = await subject.authorize(repin(schedule_id=str(OTHER_RUN)))
    assert not result.allowed
    assert result.reason_code == "job.request_grant.schedule"


@pytest.mark.parametrize(
    "case", ["expired", "other_run", "follow_up", "agent_changed", "policy_changed", "none"]
)
async def test_the_grant_ends_with_its_request(case: str) -> None:
    subject = await world(granted=case != "none")
    run_update: dict[str, Any] = {}
    if case == "expired":
        subject.clock[0] = NOW + timedelta(hours=2)
    elif case == "other_run":
        run_update = {"id": OTHER_RUN}
    elif case == "follow_up":
        await subject.append(
            "user.message.created", {"pending_input_id": str(UUID(int=9)), "text": "also"}
        )
    result = await subject.authorize(
        job_run(),
        run_update=run_update,
        agent_version="agent-v2" if case == "agent_changed" else "agent-v1",
        policy_version="policy-v2" if case == "policy_changed" else "policy-v1",
    )
    assert not result.allowed


async def test_a_second_run_of_one_revision_with_other_access_asks() -> None:
    bound = JobGrant(
        id=revision_grant_id(APPROVAL, NEW_REVISION),
        access=access(inputs={"config": {"news_queries": ["seed"]}}),
        created_at=NOW,
        approval_id=APPROVAL,
    )
    saved = job()
    saved = saved.model_copy(update={"grants": {**saved.grants, str(bound.id): bound}})
    subject = await world(granted=True, saved=saved)
    result = await subject.authorize(job_run(access(inputs={"config": {"other": True}})))
    assert not result.allowed
    assert result.reason_code == "job.request_grant.grant_collision"


# The run binds its new grant to the request approval.


async def test_a_run_the_request_authorized_binds_its_grant_to_the_request_approval() -> None:
    from agent_core.application.job_execution import JobExecution
    from agent_core.tools.jobs import JobRunTool, JobToolset
    from tests.unit.test_job_publication import setup

    factory, publication, _, _ = await setup()
    async with factory() as uow:
        await uow.jobs.put(job(), expected_version=None)
        await uow.approvals.create(
            approval(
                status=ApprovalStatus.APPROVED,
                resolution=ApprovalResolutionType.APPROVE_FOR_REQUEST,
                resolved_at=NOW,
                resolved_by=principal().principal_id,
                session_id=tool_context().session_id,
                run_id=tool_context().run_id,
            )
        )
    execution = AsyncMock(spec=JobExecution)
    execution.run.return_value = AsyncMock(value=1, diagnostics=[], broker_calls=0)
    context = replace(
        tool_context(),
        principal=tool_context().principal.model_copy(update={"scopes": {"jobs.run"}}),
        request_approval_id=APPROVAL,
    )
    _, arguments = job_run()
    result = await JobRunTool(JobToolset(publication, execution)).execute(arguments, context)
    assert result.ok, result.content
    expected = revision_grant_id(APPROVAL, NEW_REVISION)
    async with factory() as uow:
        saved = await uow.jobs.get(principal(), SLUG)
    assert saved.grants[str(expected)].approval_id == APPROVAL
    assert result.structured is not None and result.structured["grant_id"] == str(expected)
