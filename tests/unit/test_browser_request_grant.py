"""ADR-0302: one approval for a request on a signed-in website.

The browser.act card offers "Allow for this request"; resolving it with
``approve_for_request`` approves the action and makes the approval the grant
for the rest of the run. The authorizer then covers every ordinary action on
the login's origins until the run ends or two hours pass, survives the run's
own sign-in pause, and still refuses every named write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.application.browser_request_grants import (
    BrowserRequestGrantAuthorizer,
    RequestGrantResolution,
)
from agent_core.application.public_services import PublicApprovalService
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.browser import (
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserSnapshot,
)
from agent_core.domain.browser_act_views import TaskGrantSessionContext
from agent_core.domain.browser_request_grants import (
    NO_REQUEST_GRANT,
    REQUEST_GRANT_DURATION,
    BrowserRequestGrantOffer,
    RequestGrantEcho,
    request_grant_summary,
    turn_reads_outside,
)
from agent_core.domain.errors import AuthorizationError, ConflictError
from agent_core.domain.events import NewEvent
from agent_core.domain.policies import (
    ActionKind,
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    RiskLevel,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.tools.browser_act import BrowserActApprovalPresenter
from tests.contract.support import NOW, memory_uow_factory, principal
from tests.unit.test_browser_act_approval_view import SnapshotProvider, snapshot
from tests.unit.test_browser_task_grant_authorizer import FakePolicy, decision, proposed

ORIGIN = "https://secure.chase.com"
DASHBOARD = f"{ORIGIN}/web/auth/dashboard#/dashboard/overview"
SESSION = UUID("00000000-0000-0000-0000-00000000fa01")
PROFILE = UUID("00000000-0000-0000-0000-00000000fa02")
RUN = UUID("00000000-0000-0000-0000-00000000fa03")
APPROVAL = UUID("00000000-0000-0000-0000-00000000fa04")
OTHER_RUN = UUID("00000000-0000-0000-0000-00000000fa05")
BROWSER_TURN = AuthorizationTurn(
    newest_user_trust=TrustLevel.USER,
    tool_names=frozenset({"browser.navigate", "browser.act", "system.current_time"}),
)
MAIL_TURN = AuthorizationTurn(
    newest_user_trust=TrustLevel.USER,
    tool_names=frozenset({"browser.act", "email.search"}),
)
WHEN = NOW + timedelta(minutes=10)
OFFER = BrowserRequestGrantOffer(
    profile_id=PROFILE,
    profile_generation=5,
    origins=(ORIGIN,),
    summary=request_grant_summary((ORIGIN,)),
)


def owner(*scopes: str) -> Any:
    base = principal()
    return base if not scopes else base.model_copy(update={"scopes": set(scopes)})


def chase_page(name: str = "See all transactions", **update: Any) -> BrowserSnapshot:
    return snapshot(url=DASHBOARD, name=name, title="Accounts - chase.com", **update)


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
        action_summary="Click a button on secure.chase.com/web/auth/dashboard",
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
        expires_at=NOW + timedelta(hours=4),
        created_at=NOW,
        request_grant_offer=OFFER,
    ).model_copy(update=update)


@dataclass
class Dispatcher:
    resumed: list[UUID]

    async def dispatch(self, run_id: UUID) -> None:
        del run_id

    async def resume(self, run_id: UUID) -> None:
        self.resumed.append(run_id)


@dataclass
class World:
    uow_factory: Any
    clock: list[datetime]
    provider: SnapshotProvider
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
            request_grants=RequestGrantResolution(clock=_Clock(self.clock)) if composed else None,
        )

    def authorizer(self, policy: FakePolicy | None = None) -> BrowserRequestGrantAuthorizer:
        return BrowserRequestGrantAuthorizer(
            provider=self.provider,
            uow_factory=self.uow_factory,
            policy=policy or FakePolicy(),
            now=lambda: self.clock[0],
        )

    async def authorize(
        self,
        name: str = "See all transactions",
        *,
        arguments: dict[str, Any] | None = None,
        turn: AuthorizationTurn | None = BROWSER_TURN,
        run_update: dict[str, Any] | None = None,
        page: BrowserSnapshot | None = None,
    ) -> StandingAuthorization:
        self.provider.snapshots[SESSION] = page or chase_page(name)
        return await self.authorizer().authorize(
            action=proposed(arguments),
            decision=decision(),
            principal=owner(),
            run=a_run(status=RunStatus.RUNNING, **(run_update or {})),
            agent_version="agent-v1",
            action_deadline=self.clock[0] + timedelta(seconds=30),
            turn=turn,
        )

    async def stored(self) -> ApprovalRequest:
        async with self.uow_factory() as uow:
            found: ApprovalRequest = await uow.approvals.get(APPROVAL, owner())
            return found

    async def events(self, event_type: str) -> list[dict[str, Any]]:
        async with self.uow_factory() as uow:
            events = await uow.events.list_after(SESSION, 0, owner())
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

    async def sign_in(self, authentication_id: UUID, *, at: datetime) -> None:
        async with self.uow_factory() as uow:
            await uow.browser_authentications.create(
                BrowserAuthenticationRecord(
                    id=authentication_id,
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    profile_id=PROFILE,
                    status=BrowserAuthenticationStatus.READY,
                    expires_at=at + timedelta(minutes=5),
                    created_at=at,
                    updated_at=at,
                )
            )
        current = await self.profile()
        await self.change_profile(generation=current.generation + 2, updated_at=at)

    async def profile(self) -> BrowserProfile:
        async with self.uow_factory() as uow:
            found: BrowserProfile = await uow.browser_profiles.get(PROFILE, owner())
            return found

    async def change_profile(self, **update: Any) -> None:
        """The in-memory store has no general update; tests replace the row."""

        current = await self.profile()
        async with self.uow_factory() as uow:
            uow.browser_profiles._profiles[PROFILE] = current.model_copy(update=update)


@dataclass
class _Clock:
    values: list[datetime]

    def now(self) -> datetime:
        return self.values[0]

    async def sleep(self, seconds: float) -> None:
        del seconds


async def world(
    *,
    granted: bool = False,
    profile_update: dict[str, Any] | None = None,
    approval_update: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
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
                metadata={"browser_profile_id": str(PROFILE)} if metadata is None else metadata,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.runs.create(a_run())
        await uow.browser_profiles.create(
            BrowserProfile(
                id=PROFILE,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                provider_name="hosted-isolated",
                provider_ref="opaque-provider-reference",
                allowed_origins=(ORIGIN,),
                status=BrowserProfileStatus.READY,
                generation=5,
                encryption_key_version="key-v1",
                created_at=NOW,
                updated_at=NOW,
            ).model_copy(update=profile_update or {})
        )
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
    provider = SnapshotProvider(allowed_origins=(ORIGIN,))
    provider.snapshots[SESSION] = chase_page()
    subject = World(
        uow_factory=uow_factory, clock=[WHEN], provider=provider, dispatcher=Dispatcher([])
    )
    if granted:
        await subject.append(
            "browser.request_grant.created",
            {"approval_id": str(APPROVAL), "profile_id": str(PROFILE), "origins": [ORIGIN]},
        )
    return subject


# Resolution.


async def test_approve_for_request_approves_once_and_records_the_grant() -> None:
    subject = await world()

    view = await subject.service().resolve(
        owner("approval.resolve", "browser.grant.write"),
        APPROVAL,
        ApprovalResolutionType.APPROVE_FOR_REQUEST,
        None,
        request_grant=RequestGrantEcho(profile_id=PROFILE),
    )

    stored = await subject.stored()
    assert (view.status, view.decision) == ("APPROVED", ApprovalResolutionType.APPROVE_FOR_REQUEST)
    assert view.request_grant_offer == OFFER
    assert stored.status is ApprovalStatus.APPROVED
    assert await subject.events("approval.resolved") == [
        {"approval_id": str(APPROVAL), "resolution": "approve_for_request"}
    ]
    [created] = await subject.events("browser.request_grant.created")
    assert created["approval_id"] == str(APPROVAL)
    assert created["profile_id"] == str(PROFILE)
    assert created["origins"] == [ORIGIN]
    assert stored.resolved_at is not None
    # The window starts when the approval is stored as resolved, the same
    # time the authorizer measures it from.
    assert created["expires_at"] == (stored.resolved_at + REQUEST_GRANT_DURATION).isoformat()
    assert subject.dispatcher.resumed == [RUN]


async def test_a_retried_allow_returns_the_stored_view_and_records_nothing_more() -> None:
    subject = await world()
    service = subject.service()
    allowed = owner("approval.resolve", "browser.grant.write")
    echo = RequestGrantEcho(profile_id=PROFILE)

    await service.resolve(
        allowed, APPROVAL, ApprovalResolutionType.APPROVE_FOR_REQUEST, None, request_grant=echo
    )
    again = await service.resolve(
        allowed, APPROVAL, ApprovalResolutionType.APPROVE_FOR_REQUEST, None, request_grant=echo
    )

    assert again.decision is ApprovalResolutionType.APPROVE_FOR_REQUEST
    assert len(await subject.events("browser.request_grant.created")) == 1


async def test_allowing_a_request_needs_the_grant_scope() -> None:
    subject = await world()

    with pytest.raises(AuthorizationError):
        await subject.service().resolve(
            owner("approval.resolve"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=RequestGrantEcho(profile_id=PROFILE),
        )


@pytest.mark.parametrize(
    ("world_update", "echo", "reason"),
    [
        ({}, RequestGrantEcho(profile_id=UUID(int=9)), "request_grant_offer_mismatch"),
        ({"approval_update": {"request_grant_offer": None}}, None, "request_grant_unavailable"),
        ({"profile_update": {"generation": 7}}, None, "request_grant_unavailable"),
        (
            {"profile_update": {"status": BrowserProfileStatus.AUTHENTICATION_REQUIRED}},
            None,
            "request_grant_unavailable",
        ),
        ({"metadata": {}}, None, "request_grant_unavailable"),
    ],
)
async def test_an_unavailable_or_mismatched_allow_leaves_the_card_pending(
    world_update: dict[str, Any], echo: RequestGrantEcho | None, reason: str
) -> None:
    subject = await world(**world_update)

    with pytest.raises(ConflictError) as raised:
        await subject.service().resolve(
            owner("approval.resolve", "browser.grant.write"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=echo or RequestGrantEcho(profile_id=PROFILE),
        )

    assert raised.value.reason == reason
    assert (await subject.stored()).status is ApprovalStatus.PENDING


async def test_without_request_grants_composed_the_allow_is_unavailable() -> None:
    subject = await world()

    with pytest.raises(ConflictError) as raised:
        await subject.service(composed=False).resolve(
            owner("approval.resolve", "browser.grant.write"),
            APPROVAL,
            ApprovalResolutionType.APPROVE_FOR_REQUEST,
            None,
            request_grant=RequestGrantEcho(profile_id=PROFILE),
        )

    assert raised.value.reason == "request_grant_unavailable"


# Authorization.


def allowed_by_the_grant(result: StandingAuthorization) -> None:
    assert result.allowed, result.reason_code
    assert (result.authorization_kind, result.authorization_ref) == ("request_grant", str(APPROVAL))
    assert result.dispatch_constraint is not None
    assert result.dispatch_constraint.grant_kind == "request"
    assert result.dispatch_constraint.origins == (ORIGIN,)
    assert result.dispatch_constraint.path_prefix is None


def refused_by_the_grant(result: StandingAuthorization, reason: str) -> None:
    assert not result.allowed
    assert result.reason_code == f"browser.request_grant.{reason}"
    assert (result.authorization_kind, result.authorization_ref) == ("request_grant", str(APPROVAL))


async def test_without_a_request_approval_nothing_is_authorized() -> None:
    subject = await world()

    result = await subject.authorize()

    assert not result.allowed
    assert result.reason_code == NO_REQUEST_GRANT
    assert result.authorization_kind is None


async def test_the_grant_covers_ordinary_reading_on_the_authenticated_path() -> None:
    subject = await world(granted=True)

    for name in ("See all transactions", "Activity, Since last statement", "Download"):
        result = await subject.authorize(name)
        allowed_by_the_grant(result)
    assert result.dispatch_constraint is not None
    assert result.dispatch_constraint.free_text is True
    assert result.dispatch_constraint.not_after == WHEN + timedelta(seconds=30)
    assert result.authorization_view is not None


async def test_after_an_outside_read_clicks_pass_and_free_text_does_not() -> None:
    subject = await world(granted=True)
    textbox = chase_page("Search", role="textbox").model_copy(
        update={
            "facts": chase_page().facts.model_copy(  # type: ignore[union-attr]
                update={
                    "elements": {
                        "revision-7:0": BrowserElementFacts(field_kind=BrowserFieldKind.TEXT)
                    }
                }
            )
        }
    )

    click = await subject.authorize(turn=MAIL_TURN)
    date = await subject.authorize(
        arguments={
            "kind": "type",
            "expected_revision": "revision-7",
            "ref": "revision-7:0",
            "value": "12/31/2024",
        },
        turn=MAIL_TURN,
        page=textbox,
    )
    words = await subject.authorize(
        arguments={
            "kind": "type",
            "expected_revision": "revision-7",
            "ref": "revision-7:0",
            "value": "recurring charges from my wine club and Adobe plans",
        },
        turn=MAIL_TURN,
        page=textbox,
    )

    allowed_by_the_grant(click)
    assert click.dispatch_constraint is not None and click.dispatch_constraint.free_text is False
    allowed_by_the_grant(date)
    refused_by_the_grant(words, "text_not_covered")


async def test_named_writes_still_ask_under_the_grant() -> None:
    subject = await world(granted=True)

    refused_by_the_grant(await subject.authorize("Make a payment"), "excluded.payment")
    refused_by_the_grant(await subject.authorize("Upload documents"), "excluded.file_transfer")


async def test_the_grant_ends_after_two_hours() -> None:
    subject = await world(granted=True)
    subject.clock[0] = NOW + REQUEST_GRANT_DURATION

    refused_by_the_grant(await subject.authorize(), "expired")


async def test_the_constraint_never_outlives_the_grant() -> None:
    subject = await world(granted=True)
    subject.clock[0] = NOW + REQUEST_GRANT_DURATION - timedelta(seconds=5)

    result = await subject.authorize()

    allowed_by_the_grant(result)
    assert result.dispatch_constraint is not None
    assert result.dispatch_constraint.not_after == NOW + REQUEST_GRANT_DURATION


async def test_another_runs_grant_does_not_apply() -> None:
    subject = await world(granted=True)

    result = await subject.authorize(run_update={"id": OTHER_RUN})

    assert not result.allowed
    assert result.reason_code == NO_REQUEST_GRANT


@pytest.mark.parametrize(
    "profile_update",
    [
        {"status": BrowserProfileStatus.REVOKED},
        {"allowed_origins": ("https://www.chase.com",)},
        {"generation": 6},
    ],
)
async def test_a_changed_login_ends_the_grant(profile_update: dict[str, Any]) -> None:
    subject = await world(granted=True)
    await subject.change_profile(**profile_update)

    refused_by_the_grant(await subject.authorize(), "profile_changed")


async def test_the_grant_survives_this_runs_own_sign_in() -> None:
    subject = await world(granted=True)
    authentication = UUID(int=0xA11)
    await subject.sign_in(authentication, at=NOW + timedelta(minutes=3))
    await subject.append(
        "browser.authentication.resumed",
        {"authentication_id": str(authentication), "profile_id": str(PROFILE)},
    )

    allowed_by_the_grant(await subject.authorize())


async def test_a_later_sign_in_from_settings_ends_the_grant() -> None:
    subject = await world(granted=True)
    resumed = UUID(int=0xA11)
    await subject.sign_in(resumed, at=NOW + timedelta(minutes=3))
    await subject.append(
        "browser.authentication.resumed",
        {"authentication_id": str(resumed), "profile_id": str(PROFILE)},
    )
    await subject.sign_in(UUID(int=0xA12), at=NOW + timedelta(minutes=6))

    refused_by_the_grant(await subject.authorize(), "profile_changed")


async def test_another_runs_sign_in_does_not_carry_the_grant() -> None:
    subject = await world(granted=True)
    authentication = UUID(int=0xA11)
    await subject.sign_in(authentication, at=NOW + timedelta(minutes=3))
    await subject.append(
        "browser.authentication.resumed",
        {"authentication_id": str(authentication), "profile_id": str(PROFILE)},
        run_id=OTHER_RUN,
    )

    refused_by_the_grant(await subject.authorize(), "profile_changed")


async def test_a_scheduled_or_untrusted_turn_is_not_covered() -> None:
    subject = await world(
        granted=True,
        metadata={"browser_profile_id": str(PROFILE), "schedule_id": str(UUID(int=7))},
    )

    refused_by_the_grant(await subject.authorize(), "run_not_eligible")
    untrusted = AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    refused_by_the_grant(await subject.authorize(turn=untrusted), "run_not_eligible")


async def test_a_child_run_is_not_covered() -> None:
    subject = await world(granted=True)

    result = await subject.authorize(run_update={"parent_run_id": UUID(int=3)})

    refused_by_the_grant(result, "run_not_eligible")


def test_bookkeeping_tools_are_not_outside_reads() -> None:
    for name in (
        "system.current_time",
        "context.update_working_state",
        "math.calculate",
        "artifact.read_capture",
        "artifact.export",
    ):
        turn = AuthorizationTurn(
            newest_user_trust=TrustLevel.USER, tool_names=frozenset({"browser.act", name})
        )
        assert not turn_reads_outside(turn), name
    assert turn_reads_outside(MAIL_TURN)
    assert turn_reads_outside(None)


# The offer on the card.


def presenter(subject: World, *, enabled: bool = True) -> BrowserActApprovalPresenter:
    async def read(session_id: UUID, owner_principal: Any) -> TaskGrantSessionContext:
        async with subject.uow_factory() as uow:
            session = await uow.sessions.get(session_id, owner_principal)
            profile = await uow.browser_profiles.get(PROFILE, owner_principal)
        return TaskGrantSessionContext(session=session, profile=profile, owner_run_id=RUN)

    return BrowserActApprovalPresenter(
        subject.provider, context_reader=read, request_enabled=enabled, now=lambda: WHEN
    )


async def present(
    subject: World,
    name: str = "See all transactions",
    *,
    enabled: bool = True,
    run_update: dict[str, Any] | None = None,
    turn: AuthorizationTurn | None = BROWSER_TURN,
) -> Any:
    subject.provider.snapshots[SESSION] = chase_page(name)
    return await presenter(subject, enabled=enabled).present(
        {"kind": "click", "expected_revision": "revision-7", "ref": "revision-7:0"},
        run=a_run(status=RunStatus.RUNNING, **(run_update or {})),
        principal=owner(),
        turn=turn,
        not_covered=None,
    )


async def test_the_card_offers_the_request_for_an_ordinary_action() -> None:
    subject = await world()

    presentation = await present(subject)

    assert presentation.request_grant_offer == OFFER
    assert presentation.task_grant_offer is None


async def test_the_offer_holds_after_an_outside_read() -> None:
    subject = await world()

    presentation = await present(subject, turn=MAIL_TURN)

    assert presentation.request_grant_offer == OFFER


@pytest.mark.parametrize(
    ("name", "update", "enabled"),
    [
        ("Make a payment", {}, True),
        ("See all transactions", {"parent_run_id": UUID(int=3)}, True),
        ("See all transactions", {}, False),
    ],
)
async def test_no_offer_for_a_write_a_child_run_or_when_off(
    name: str, update: dict[str, Any], enabled: bool
) -> None:
    subject = await world()

    presentation = await present(subject, name, enabled=enabled, run_update=update)

    assert presentation.request_grant_offer is None
