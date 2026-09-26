"""A task grant through the whole pipeline (ADR-0129, 0129 design O11).

The composition runs with task grants on, a session-bound hosted provider and
the isolated session service in process. The service answers with element
facts, as its HTTP API does, and its runtime is a lesson page that rechecks a
grant's constraint against the live page with the same coverage rules the
Playwright runtime uses. Scripted models drive each run.

Once the owner allows a task from the approval card, later actions inside the
scope dispatch without asking, each consuming one use under the task
constraint. Every case below that falls outside the grant asks again.

These cases need no database, so the module replaces the directory's
PostgreSQL reset; the PostgreSQL case at the end runs only with
``DATABASE_URL`` and resets the tables itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from sqlalchemy import text

from agent_core.adapters.browser.hosted_provider import SessionBoundHostedBrowserProvider
from agent_core.adapters.browser.profiles import InMemoryBrowserProfileControlPlane
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import Base
from agent_core.application.browser_leases import browser_run_state
from agent_core.application.browser_management import (
    BrowserProfileManagementService,
    BrowserUnitOfWorkFactory,
)
from agent_core.bootstrap import Composition, build
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.config import Settings, load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalRequest, ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationMode,
    BrowserAuthenticationView,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserGrant,
    BrowserLease,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
    BrowserRunState,
    BrowserSnapshot,
)
from agent_core.domain.browser_classification import dispatch_constraint_coverage
from agent_core.domain.browser_task_grants import (
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    TaskGrantEcho,
)
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus
from agent_core.domain.sessions import SESSION_SCHEDULE_ID_METADATA_KEY
from agent_core.domain.tools import ToolExecutionContext, ToolInvocation, ToolInvocationStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.browser import GRANT_NOT_APPLICABLE
from agent_core.runtime.worker import DurableWorker
from tests.contract.support import principal as contract_principal
from tests.contract.test_hosted_profile_session_service_contract import FakeSessionRuntime
from tests.unit.test_browser_management import FakeAuthenticationControlPlane
from tests.unit.test_config import base_environment

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000012a1")
STANDING_GRANT_ID = UUID("00000000-0000-0000-0000-0000000012a2")
SCHEDULE_ID = UUID("00000000-0000-0000-0000-0000000012a3")
SCHEDULED_SESSION_ID = UUID("00000000-0000-0000-0000-0000000012a4")
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
ORIGIN = "https://www.example.org"
LESSON = f"{ORIGIN}/lesson/unit-1"
SCOPE = f"{ORIGIN}/lesson"
# What the lesson page shows, in element order: name, role and facts.
LESSON_ELEMENTS: tuple[tuple[str, str, BrowserElementFacts], ...] = (
    ("Continue", "button", BrowserElementFacts(field_kind=BrowserFieldKind.NONE)),
    ("Your answer", "textbox", BrowserElementFacts(field_kind=BrowserFieldKind.TEXT)),
    ("Delete", "button", BrowserElementFacts(field_kind=BrowserFieldKind.NONE)),
)
CONTINUE, ANSWER, DELETE = 0, 1, 2
FINAL = ScriptedTurn(text="The lesson step is done.", stop_reason=StopReason.END_TURN)
# Authored call ids: a chat's runs share one history, so no two calls may
# share an id.
CALL_IDS = itertools.count(1)


class HeldClock(FixedClock):
    """A fixed clock whose sleeps wait instead of advancing it.

    A durable worker sleeps between lease heartbeats while a run executes;
    advancing a fixed clock there would age the grant and the browser lease
    mid-run.
    """

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@pytest.fixture(autouse=True)
def isolate_postgres_case() -> None:
    """No database by default: this overrides the directory's PostgreSQL reset."""


@dataclass
class LessonRuntime(FakeSessionRuntime):
    """A lesson page inside the isolated service.

    Each page has a new revision. A grant-constrained act is rechecked against
    the live page first: when the page has moved since it was observed, as a
    late client-side redirect would, the act is refused before dispatch and
    the observation is forgotten (ADR-0129 D16).
    """

    url: str = ""
    page: int = 0
    forgotten: bool = False
    moves_to: str | None = None
    # Where the live page goes after each navigation, before any observation.
    late_redirect: str | None = None
    performed: list[tuple[str, BrowserActionKind, str | None]] = field(default_factory=list)

    @property
    def revision(self) -> str:
        return f"lesson-{self.page}"

    def _observation(self) -> BrowserObservation:
        return BrowserObservation(
            url=self.url,
            title="Lesson",
            revision=self.revision,
            text="Exercise 1",
            elements=tuple(
                BrowserElement(ref=f"{self.revision}:{index}", role=role, name=name)
                for index, (name, role, _facts) in enumerate(LESSON_ELEMENTS)
            ),
        )

    async def navigate(self, url: str) -> BrowserObservation:
        self.url, self.moves_to, self.forgotten = url, self.late_redirect, False
        self.page += 1
        return self._observation()

    async def observe(self) -> BrowserObservation:
        if self.moves_to is not None:
            self.url, self.moves_to = self.moves_to, None
        self.forgotten = False
        self.page += 1
        return self._observation()

    def facts(self, revision: str) -> BrowserObservationFacts | None:
        if self.forgotten or revision != self.revision:
            return None
        return BrowserObservationFacts(
            revision=revision,
            elements={
                f"{revision}:{index}": facts
                for index, (_name, _role, facts) in enumerate(LESSON_ELEMENTS)
            },
        )

    def _element(self, action: BrowserAction) -> tuple[str, str, BrowserElementFacts]:
        if self.forgotten or action.expected_revision != self.revision:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        return LESSON_ELEMENTS[int((action.ref or "").rsplit(":", 1)[1])]

    async def act(self, action: BrowserAction) -> BrowserObservation:
        name, _role, _facts = self._element(action)
        self.actions.append(action)
        self.performed.append((name, action.kind, action.value))
        self.page += 1
        return self._observation()

    async def act_within_grant(
        self,
        action: BrowserAction,
        constraint: BrowserDispatchConstraint,
        *,
        now: datetime,
    ) -> BrowserObservation:
        self.constrained.append((constraint, now))
        name, role, facts = self._element(action)
        coverage = dispatch_constraint_coverage(
            constraint,
            action=action,
            page_url=self.moves_to or self.url,
            role=role,
            labels=[name, *facts.labels.values()],
            facts=facts,
            option_texts=(),
            runtime_origins=self.allowed_origins,
            now=now,
        )
        if not coverage.covered:
            self.forgotten = True
            raise BrowserProviderError(GRANT_NOT_APPLICABLE, retryable=False)
        return await self.act(action)


@dataclass
class InProcessSessions:
    """The isolated session service in process, answering with element facts
    beside each page, as its HTTP routes do (ADR-0129 section 8.4)."""

    service: HostedProfileSessionService

    async def acquire(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        run_id: UUID,
        attempt_number: int,
        deadline_at: datetime,
    ) -> BrowserLease:
        return await self.service.acquire(
            profile_id,
            principal,
            provider_ref,
            run_id=run_id,
            attempt_number=attempt_number,
            deadline_at=deadline_at,
        )

    async def navigate(self, lease_ref: str, url: str) -> BrowserSnapshot:
        return await self.service.navigate_snapshot(lease_ref, url)

    async def observe(self, lease_ref: str) -> BrowserSnapshot:
        return await self.service.observe_snapshot(lease_ref)

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserSnapshot:
        return await self.service.act_snapshot(
            lease_ref, action, sequence=sequence, constraint=constraint
        )

    async def renew(self, lease_ref: str, *, deadline_at: datetime) -> BrowserLease:
        return await self.service.renew(lease_ref, deadline_at=deadline_at)

    async def close(self, lease_ref: str) -> None:
        await self.service.close(lease_ref)


def _call_id() -> str:
    return f"pipeline-call-{next(CALL_IDS)}"


def navigate(url: str = LESSON) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(name="browser.navigate", arguments={"url": url}, call_id=_call_id())
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def observe() -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name="browser.observe", arguments={}, call_id=_call_id())],
        stop_reason=StopReason.TOOL_USE,
    )


def act(page: int, element: int, *, value: str | None = None) -> ScriptedTurn:
    arguments: dict[str, Any] = {
        "kind": "click" if value is None else "type",
        "expected_revision": f"lesson-{page}",
        "ref": f"lesson-{page}:{element}",
    }
    if value is not None:
        arguments["value"] = value
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name="browser.act", arguments=arguments, call_id=_call_id())],
        stop_reason=StopReason.TOOL_USE,
    )


def current_time() -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name="system.current_time", arguments={}, call_id=_call_id())],
        stop_reason=StopReason.TOOL_USE,
    )


def allow_turns() -> list[ScriptedTurn]:
    """Run 1 of every case: the first act asks, and the owner allows the task."""

    return [navigate(), act(1, CONTINUE)]


def task_grant_settings(**extra: str) -> Settings:
    return load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
            "BROWSER_TASK_GRANTS_ENABLED": "1",
            "BROWSER_TASK_GRANT_SCOPES": SCOPE,
            **extra,
        }
    )


def standing_grant_settings() -> Settings:
    """Task grants beside a pinned standing grant (ADR-0129 D18)."""

    return task_grant_settings(
        BROWSER_ALLOWED_ORIGINS=ORIGIN,
        BROWSER_PROFILE_ID=str(PROFILE_ID),
        BROWSER_GRANT_ID=str(STANDING_GRANT_ID),
        BROWSER_RUN_PURPOSE="daily-language-practice",
    )


@dataclass
class Pipeline:
    clock: FixedClock
    owner: Principal
    service: HostedProfileSessionService
    runtimes: list[LessonRuntime]
    profile: BrowserProfile
    provider: SessionBoundHostedBrowserProvider
    composition: Composition
    session_id: UUID
    storage: Literal["memory", "postgres"] = "memory"
    # The late redirect every lesson runtime started from now on shows.
    late_redirect: list[str | None] = field(default_factory=lambda: [None])

    async def drain(self) -> None:
        """With PostgreSQL, runs wait in the durable queue for a worker."""

        if self.storage == "memory":
            return
        worker = self.composition.worker_factory("pipeline-worker")
        assert isinstance(worker, DurableWorker)
        while await worker.run_once():
            pass

    async def bound_session(self) -> UUID:
        """Another chat bound to the same profile."""

        created = await self.composition.services.sessions.create(
            self.owner, "general", {}, browser_profile_id=PROFILE_ID
        )
        return created.id

    async def scheduled_session(self) -> UUID:
        """A chat bound to the same profile that a schedule runs. The public
        route refuses a schedule id, so it is written through the repository."""

        async with self.composition.uow_factory() as uow:
            first = await uow.sessions.get(self.session_id, self.owner)
            await uow.sessions.create(
                first.model_copy(
                    update={
                        "id": SCHEDULED_SESSION_ID,
                        "metadata": {
                            **first.metadata,
                            SESSION_SCHEDULE_ID_METADATA_KEY: str(SCHEDULE_ID),
                        },
                    }
                )
            )
        return SCHEDULED_SESSION_ID

    async def submit(self, message: str, session_id: UUID | None = None) -> UUID:
        run_id = await self.composition.runs.submit(message, session_id or self.session_id)
        await self.drain()
        return run_id

    async def settled(self, run_id: UUID) -> Run:
        """The run once it stops moving: terminal, or parked on the owner.

        Unlike ``wait_terminal``, this never waits on a run parked on a card.
        """

        for _poll in range(500):
            run = await self.composition.runs.get(run_id)
            if run.status in TERMINAL_RUN_STATUSES or run.status in {
                RunStatus.WAITING_FOR_APPROVAL,
                RunStatus.WAITING_FOR_USER,
            }:
                return run
            await asyncio.sleep(0.01)
        return run

    async def pending(self, run_id: UUID) -> ApprovalRequest:
        [approval] = await self.composition.approvals.list_pending(run_id=run_id)
        return approval

    async def allow_task(self, run_id: UUID) -> UUID:
        """Resolve the run's card with approve_for_task, echoing its offer."""

        approval = await self.pending(run_id)
        offer = approval.task_grant_offer
        assert offer is not None
        await self.composition.services.approvals.resolve(
            self.owner,
            approval.id,
            ApprovalResolutionType.APPROVE_FOR_TASK,
            None,
            task_grant=TaskGrantEcho(origin=offer.origin, path_prefix=offer.path_prefix),
        )
        await self.drain()
        run = await self.settled(run_id)
        assert run.status is RunStatus.COMPLETED, run.status
        updated = await self.composition.approvals.get(approval.id)
        assert updated.task_grant_id is not None
        return updated.task_grant_id

    async def grant(self, grant_id: UUID) -> BrowserTaskGrant:
        async with self.composition.uow_factory() as uow:
            return await uow.browser_task_grants.get(grant_id, self.owner)

    async def consume(self, grant_id: UUID, uses: int, *, typed: int = 0) -> None:
        """Spend uses as earlier covered actions would have."""

        async with self.composition.uow_factory() as uow:
            for _use in range(uses):
                used = await uow.browser_task_grants.consume(
                    grant_id,
                    self.owner,
                    session_id=self.session_id,
                    typed=typed,
                    now=self.clock.now(),
                )
                assert used is not None

    async def events(self, session_id: UUID | None = None) -> list[EventEnvelope]:
        async with self.composition.uow_factory() as uow:
            return await uow.events.list_after(session_id or self.session_id, 0, self.owner)

    async def acts(self, run_id: UUID) -> list[ToolInvocation]:
        async with self.composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, self.owner)
        return [item for item in invocations if item.tool_name == "browser.act"]

    async def authorizations(
        self, run_id: UUID, session_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        return [
            event.payload
            for event in await self.events(session_id)
            if event.event_type == "tool.call.authorized"
            and event.run_id == run_id
            and event.payload.get("name") == "browser.act"
            and "authorization_kind" in event.payload
        ]


@asynccontextmanager
async def pipeline(
    tmp_path: Path,
    turns: list[ScriptedTurn],
    *,
    settings: Settings | None = None,
    storage: Literal["memory", "postgres"] = "memory",
) -> AsyncIterator[Pipeline]:
    clock = FixedClock(NOW) if storage == "memory" else HeldClock(NOW)
    owner = contract_principal().model_copy(update={"scopes": set(PLATFORM_SCOPES)})
    store = FilesystemEncryptedProfileStore(
        tmp_path / "profiles",
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-pipeline-key").digest()},
            current_version="key-v1",
        ),
    )
    runtimes: list[LessonRuntime] = []
    late_redirect: list[str | None] = [None]

    def runtime_factory(tenant_id: str) -> LessonRuntime:
        assert tenant_id == owner.tenant_id
        runtime = LessonRuntime(late_redirect=late_redirect[0])
        runtimes.append(runtime)
        return runtime

    service = HostedProfileSessionService(
        store,
        runtime_factory=runtime_factory,
        now=clock.now,
        process_secret=b"synthetic-pipeline-secret-with-32-bytes",
        ceremony_base_url="https://browser-login.example.test",
    )
    lifecycle = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: "opaque-session-reference-0000000000000012",
        invalidate_profile=service.invalidate_profile,
    )
    provisioned = await lifecycle.provision(PROFILE_ID, owner, (ORIGIN,))
    profile = BrowserProfile(
        id=PROFILE_ID,
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        provider_name=provisioned.provider_name,
        provider_ref=provisioned.provider_ref,
        allowed_origins=(ORIGIN,),
        status=BrowserProfileStatus.READY,
        generation=1,
        encryption_key_version=provisioned.encryption_key_version,
        created_at=NOW,
        updated_at=NOW,
    )
    compositions: list[Composition] = []

    async def load(requested: Principal, profile_id: UUID) -> BrowserProfile:
        assert requested == owner
        async with compositions[0].uow_factory() as uow:
            return await uow.browser_profiles.get(profile_id, requested)

    async def select(context: ToolExecutionContext) -> UUID:
        del context
        return PROFILE_ID

    async def run_state(run_id: UUID) -> BrowserRunState:
        return await browser_run_state(compositions[0].uow_factory, owner, run_id)

    provider = SessionBoundHostedBrowserProvider(
        principal=owner,
        profiles=load,
        profile_selector=select,
        sessions=InProcessSessions(service),
        now=clock.now,
        run_state=run_state,
    )
    async with build(
        settings=settings or task_grant_settings(),
        storage=storage,
        script=FakeModelScript(turns=turns),
        clock=clock,
        principal=owner,
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "system.current_time",
        ],
        browser_provider_override=provider,
    ) as composition:
        compositions.append(composition)
        async with composition.uow_factory() as uow:
            await uow.browser_profiles.create(profile)
        created = await composition.services.sessions.create(
            owner, "general", {}, browser_profile_id=PROFILE_ID
        )
        yield Pipeline(
            clock=clock,
            owner=owner,
            service=service,
            runtimes=runtimes,
            profile=profile,
            provider=provider,
            composition=composition,
            session_id=created.id,
            storage=storage,
            late_redirect=late_redirect,
        )


async def allowed_task(harness: Pipeline) -> UUID:
    """Run 1: the first act asks with an offer, and the owner allows the task."""

    run_id = await harness.submit("Do one lesson.")
    approval = await harness.pending(run_id)
    assert approval.task_grant_offer is not None
    assert (approval.task_grant_offer.origin, approval.task_grant_offer.path_prefix) == (
        ORIGIN,
        "/lesson",
    )
    assert approval.task_grant_not_covered is None
    return await harness.allow_task(run_id)


async def asked(harness: Pipeline, run_id: UUID, session_id: UUID | None = None) -> ApprovalRequest:
    """The run parked on an ordinary approval card; nothing was dispatched."""

    run = await harness.composition.runs.get(run_id)
    assert run.status is RunStatus.WAITING_FOR_APPROVAL, run.status
    assert await harness.authorizations(run_id, session_id) == []
    return await harness.pending(run_id)


async def assert_allowed_task_authorizes_the_next_acts(tmp_path: Path) -> None:
    """After approve_for_task, the next two acts dispatch under the grant."""

    turns = [
        *allow_turns(),
        act(2, CONTINUE),
        act(3, ANSWER, value="el gato"),
        FINAL,
    ]
    async with pipeline(tmp_path, turns) as harness:
        run_id = await harness.submit("Do one lesson.")
        approval = await harness.pending(run_id)
        grant_id = await harness.allow_task(run_id)
        grant = await harness.grant(grant_id)
        authorizations = await harness.authorizations(run_id)
        created = [
            event.payload
            for event in await harness.events()
            if event.event_type == "browser.task_grant.created"
        ]
        [runtime] = harness.runtimes

    assert approval.task_grant_offer is not None
    assert [payload["authorization_kind"] for payload in authorizations] == [
        "browser_task_grant",
        "browser_task_grant",
    ]
    assert [payload["authorization_use"] for payload in authorizations] == [1, 2]
    assert {payload["authorization_ref"] for payload in authorizations} == {str(grant_id)}
    for payload in authorizations:
        view = payload["authorization_view"]
        assert view["view"] == "browser.act.v1" and view["page_path"] == "/lesson/unit-1"
        assert "ref" not in view and "expected_revision" not in view
    assert [(name, kind) for name, kind, _value in runtime.performed] == [
        ("Continue", BrowserActionKind.CLICK),
        ("Continue", BrowserActionKind.CLICK),
        ("Your answer", BrowserActionKind.TYPE),
    ]
    # The approved action carries no constraint; the two granted ones do.
    assert [constraint for constraint, _now in runtime.constrained] == [
        BrowserDispatchConstraint(
            grant_kind="task",
            origins=(ORIGIN,),
            path_prefix="/lesson",
            not_after=grant.expires_at,
            consequence_ceiling="unknown",
            max_text_characters=256,
        )
    ] * 2
    assert (grant.actions_used, grant.typed_characters, grant.end_reason) == (2, 7, None)
    assert [payload["grant_id"] for payload in created] == [str(grant_id)]


async def test_allowed_task_authorizes_the_next_acts(tmp_path: Path) -> None:
    await assert_allowed_task_authorizes_the_next_acts(tmp_path)


async def assert_the_two_hundred_and_first_act_asks(tmp_path: Path) -> None:
    turns = [*allow_turns(), FINAL, navigate(), act(1, CONTINUE), act(2, CONTINUE)]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        await harness.consume(grant_id, 199)
        run_id = await harness.submit("Keep going.")
        approval = await asked_after_one_use(harness, run_id)
        grant = await harness.grant(grant_id)
        ended = [
            event.payload
            for event in await harness.events()
            if event.event_type == "browser.task_grant.ended"
        ]
        authorized = [
            event.payload["authorization_use"]
            for event in await harness.events()
            if event.event_type == "tool.call.authorized" and "authorization_use" in event.payload
        ]

    assert authorized == [200]
    assert (grant.actions_used, grant.end_reason) == (200, BrowserTaskGrantEndReason.EXHAUSTED)
    assert [payload["reason"] for payload in ended] == ["exhausted"]
    # The grant ended, so the card offers a new one instead of naming it.
    assert approval.task_grant_not_covered is None


async def test_the_two_hundred_and_first_act_asks(tmp_path: Path) -> None:
    await assert_the_two_hundred_and_first_act_asks(tmp_path)


async def assert_text_past_the_grant_budget_asks(tmp_path: Path) -> None:
    fits = "la casa es grande " * 11  # 198 characters
    past = "el perro come pan " * 6  # 108 characters
    turns = [
        *allow_turns(),
        FINAL,
        navigate(),
        act(1, ANSWER, value=fits),
        act(2, ANSWER, value=past),
    ]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        await harness.consume(grant_id, 15, typed=256)
        run_id = await harness.submit("Answer the exercises.")
        approval = await asked_after_one_use(harness, run_id)
        grant = await harness.grant(grant_id)

    assert grant.typed_characters == 15 * 256 + len(fits) == 4038
    assert approval.task_grant_not_covered is not None
    assert approval.task_grant_not_covered.reason == "browser.task_grant.text_budget_exhausted"


async def asked_after_one_use(harness: Pipeline, run_id: UUID) -> ApprovalRequest:
    run = await harness.composition.runs.get(run_id)
    assert run.status is RunStatus.WAITING_FOR_APPROVAL, run.status
    assert len(await harness.authorizations(run_id)) == 1
    return await harness.pending(run_id)


async def test_text_past_the_grant_budget_asks(tmp_path: Path) -> None:
    await assert_text_past_the_grant_budget_asks(tmp_path)


@pytest.mark.parametrize("ending", ["revoked", "expired"])
async def test_a_revoked_or_expired_grant_asks(tmp_path: Path, ending: str) -> None:
    await assert_a_revoked_or_expired_grant_asks(tmp_path, ending)


async def assert_a_revoked_or_expired_grant_asks(tmp_path: Path, ending: str) -> None:
    turns = [*allow_turns(), FINAL, navigate(), act(1, CONTINUE)]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        if ending == "revoked":
            browser_task_grants = harness.composition.services.browser_task_grants
            assert browser_task_grants is not None
            await browser_task_grants.revoke(harness.owner, grant_id)
        else:
            harness.clock.advance(timedelta(minutes=31))
        run_id = await harness.submit("Keep going.")
        approval = await asked(harness, run_id)
        grant = await harness.grant(grant_id)

    assert grant.actions_used == 0
    assert approval.task_grant_not_covered is None
    if ending == "revoked":
        assert grant.end_reason is BrowserTaskGrantEndReason.REVOKED


async def assert_another_or_scheduled_session_asks(tmp_path: Path) -> None:
    """The grant is the session's own; another chat, or a scheduled one that
    somehow holds a grant, asks."""

    turns = [*allow_turns(), FINAL, navigate(), act(1, CONTINUE), navigate(), act(1, CONTINUE)]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        other = await harness.bound_session()
        other_run = await harness.submit("Do one lesson here too.", other)
        other_approval = await asked(harness, other_run, other)
        await harness.composition.runs.cancel(other_run)

        scheduled = await harness.scheduled_session()
        original = await harness.grant(grant_id)
        async with harness.composition.uow_factory() as uow:
            await uow.browser_task_grants.create(
                original.model_copy(
                    update={"id": UUID(int=0x12C1), "approval_id": UUID(int=0x12C2)}
                ).model_copy(update={"session_id": scheduled})
            )
        scheduled_run = await harness.submit("Do today's lesson.", scheduled)
        scheduled_approval = await asked(harness, scheduled_run, scheduled)
        grant = await harness.grant(grant_id)

    assert other_approval.task_grant_not_covered is None
    assert scheduled_approval.task_grant_not_covered is not None
    assert scheduled_approval.task_grant_not_covered.reason == (
        "browser.task_grant.run_not_eligible"
    )
    assert grant.actions_used == 0


async def test_another_or_scheduled_session_asks(tmp_path: Path) -> None:
    await assert_another_or_scheduled_session_asks(tmp_path)


async def assert_another_tool_in_the_turn_asks(tmp_path: Path) -> None:
    turns = [*allow_turns(), FINAL, navigate(), current_time(), act(1, CONTINUE)]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        run_id = await harness.submit("Check the time, then keep going.")
        approval = await asked(harness, run_id)
        grant = await harness.grant(grant_id)

    assert approval.task_grant_not_covered is not None
    assert approval.task_grant_not_covered.reason == "browser.task_grant.turn_not_browser_only"
    assert grant.actions_used == 0


async def test_another_tool_in_the_turn_asks(tmp_path: Path) -> None:
    await assert_another_tool_in_the_turn_asks(tmp_path)


async def assert_a_destructive_button_asks(tmp_path: Path) -> None:
    turns = [*allow_turns(), FINAL, navigate(), act(1, DELETE)]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        run_id = await harness.submit("Clear my answer.")
        approval = await asked(harness, run_id)
        grant = await harness.grant(grant_id)

    assert approval.task_grant_not_covered is not None
    assert approval.task_grant_not_covered.grant_id == grant_id
    assert approval.task_grant_not_covered.reason == "browser.task_grant.excluded.destructive"
    assert grant.actions_used == 0


async def test_a_destructive_button_asks(tmp_path: Path) -> None:
    await assert_a_destructive_button_asks(tmp_path)


@dataclass
class DeviceSignIn(FakeAuthenticationControlPlane):
    """The isolated service beginning a device sign-in, on the pipeline's clock."""

    clock: FixedClock = field(default_factory=lambda: FixedClock(NOW))

    async def begin_authentication(
        self,
        profile_id: UUID,
        owner: Principal,
        provider_ref: str,
        *,
        login_url: str,
        mode: BrowserAuthenticationMode = BrowserAuthenticationMode.REMOTE,
    ) -> BrowserAuthenticationView:
        begun = await super().begin_authentication(
            profile_id, owner, provider_ref, login_url=login_url, mode=mode
        )
        return begun.model_copy(update={"expires_at": self.clock.now() + timedelta(minutes=5)})


async def assert_a_sign_in_ends_the_grant(tmp_path: Path) -> None:
    """ADR-0128 decision 10 with ADR-0129: a sign-in that begins on the bound
    profile ends its task grant, so the next act asks."""

    turns = [*allow_turns(), FINAL, navigate(), act(1, CONTINUE)]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        management = BrowserProfileManagementService(
            uow_factory=cast(BrowserUnitOfWorkFactory, harness.composition.uow_factory),
            lifecycle=InMemoryBrowserProfileControlPlane(),
            authentications=DeviceSignIn(clock=harness.clock, profile_id=PROFILE_ID),
            clock=harness.clock,
            ids=SequenceIdFactory(),
        )
        await management.begin_authentication(
            harness.owner,
            PROFILE_ID,
            login_url=f"{ORIGIN}/",
            mode=BrowserAuthenticationMode.DEVICE,
        )
        run_id = await harness.submit("Keep going.")
        approval = await asked(harness, run_id)
        grant = await harness.grant(grant_id)
        ended = [
            event.payload["reason"]
            for event in await harness.events()
            if event.event_type == "browser.task_grant.ended"
        ]

    assert grant.end_reason is BrowserTaskGrantEndReason.PROFILE_CHANGED
    assert ended == ["profile_changed"]
    assert grant.actions_used == 0
    assert approval.task_grant_not_covered is None


async def test_a_sign_in_ends_the_grant(tmp_path: Path) -> None:
    await assert_a_sign_in_ends_the_grant(tmp_path)


async def assert_allow_for_task_waits_for_the_sign_in_outcome(tmp_path: Path) -> None:
    """ADR-0128 decision 10: a sign-in begun while a card is pending leaves
    the profile READY at the generation the begin set. A task grant created
    then would pin that generation and keep authorizing on the session the
    service seals, whenever no client records the outcome, so the allow is
    refused until the outcome is recorded, even after the ceremony expires."""

    turns = [*allow_turns(), FINAL]
    async with pipeline(tmp_path, turns) as harness:
        run_id = await harness.submit("Do one lesson.")
        approval = await harness.pending(run_id)
        assert approval.task_grant_offer is not None
        echo = TaskGrantEcho(
            origin=approval.task_grant_offer.origin,
            path_prefix=approval.task_grant_offer.path_prefix,
        )
        management = BrowserProfileManagementService(
            uow_factory=cast(BrowserUnitOfWorkFactory, harness.composition.uow_factory),
            lifecycle=InMemoryBrowserProfileControlPlane(),
            authentications=DeviceSignIn(clock=harness.clock, profile_id=PROFILE_ID),
            clock=harness.clock,
            ids=SequenceIdFactory(),
        )
        begun = await management.begin_authentication(
            harness.owner,
            PROFILE_ID,
            login_url=f"{ORIGIN}/",
            mode=BrowserAuthenticationMode.DEVICE,
        )
        harness.clock.advance(timedelta(minutes=10))
        with pytest.raises(ConflictError) as refused:
            await harness.composition.services.approvals.resolve(
                harness.owner,
                approval.id,
                ApprovalResolutionType.APPROVE_FOR_TASK,
                None,
                task_grant=echo,
            )
        unresolved = await harness.composition.approvals.get(approval.id)
        created_while_open = [
            event
            for event in await harness.events()
            if event.event_type == "browser.task_grant.created"
        ]
        await management.cancel_authentication(harness.owner, begun.id)
        allowed = await harness.composition.services.approvals.resolve(
            harness.owner,
            approval.id,
            ApprovalResolutionType.APPROVE_FOR_TASK,
            None,
            task_grant=echo,
        )

    assert refused.value.reason == "task_grant_unavailable"
    assert (unresolved.resolution, unresolved.task_grant_id) == (None, None)
    assert created_while_open == []
    assert allowed.task_grant_id is not None


async def test_allow_for_task_waits_for_the_sign_in_outcome(tmp_path: Path) -> None:
    await assert_allow_for_task_waits_for_the_sign_in_outcome(tmp_path)


async def assert_a_credential_shaped_type_is_denied_not_granted(tmp_path: Path) -> None:
    # Assembled here so the file never holds a credential-shaped literal.
    credential = "pass" + "word: " + "pipeline-sentinel"
    turns = [*allow_turns(), FINAL, navigate(), act(1, ANSWER, value=credential), FINAL]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        run_id = await harness.submit("Fill in the answer.")
        run = await harness.composition.runs.get(run_id)
        [typed] = await harness.acts(run_id)
        pending = await harness.composition.approvals.list_pending(run_id=run_id)
        grant = await harness.grant(grant_id)
        runtime = harness.runtimes[-1]

    assert run.status is RunStatus.COMPLETED
    assert typed.status is ToolInvocationStatus.DENIED
    assert pending == []
    assert await harness.authorizations(run_id) == []
    assert runtime.performed == []
    assert (grant.actions_used, grant.typed_characters) == (0, 0)


async def test_a_credential_shaped_type_is_denied_not_granted(tmp_path: Path) -> None:
    await assert_a_credential_shaped_type_is_denied_not_granted(tmp_path)


async def assert_a_standing_grant_answers_first(tmp_path: Path) -> None:
    """With both authorizers, a routine click uses the standing grant and
    consumes no task-grant use (D18)."""

    # The standing grant covers only a click on "Continue", so run 1 asks
    # about typing an answer, and the owner allows the task there.
    turns = [
        navigate(),
        act(1, ANSWER, value="el gato"),
        FINAL,
        navigate(),
        act(1, CONTINUE),
        FINAL,
    ]
    async with pipeline(tmp_path, turns, settings=standing_grant_settings()) as harness:
        async with harness.composition.uow_factory() as uow:
            await uow.browser_grants.create(standing_grant(harness))
        grant_id = await allowed_task(harness)
        run_id = await harness.submit("Keep going.")
        run = await harness.settled(run_id)
        authorizations = await harness.authorizations(run_id)
        grant = await harness.grant(grant_id)
        runtime = harness.runtimes[-1]

    assert run.status is RunStatus.COMPLETED
    assert [payload["authorization_kind"] for payload in authorizations] == [
        "standing_browser_grant"
    ]
    assert [payload["authorization_ref"] for payload in authorizations] == [str(STANDING_GRANT_ID)]
    assert grant.actions_used == 0
    [(constraint, _now)] = runtime.constrained
    assert (constraint.grant_kind, constraint.consequence_ceiling) == ("standing", "routine")


def standing_grant(harness: Pipeline) -> BrowserGrant:
    """A standing grant for a routine click on "Continue", pinned to the
    profile, the default agent and the current policy."""

    return BrowserGrant(
        id=STANDING_GRANT_ID,
        tenant_id=harness.owner.tenant_id,
        principal_id=harness.owner.principal_id,
        profile_id=PROFILE_ID,
        profile_generation=harness.profile.generation,
        agent_version="1.0.0",
        policy_version=harness.composition.ruleset.policy_version,
        allowed_origins=(ORIGIN,),
        action_kinds=(BrowserActionKind.CLICK,),
        element_roles=("button",),
        element_names=("Continue",),
        purpose="daily-language-practice",
        starts_at=NOW,
        expires_at=NOW + timedelta(days=7),
        approved_by=harness.owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_a_standing_grant_answers_first(tmp_path: Path) -> None:
    await assert_a_standing_grant_answers_first(tmp_path)


async def assert_a_runtime_refusal_keeps_the_lease_and_forces_an_observe(tmp_path: Path) -> None:
    """The worker saw a lesson page, but the live page moved to settings: the
    runtime refuses before dispatch, the lease and its sequence stay, and
    the model must observe again, after which the grant no longer covers
    the act and the card asks (B2, D16)."""

    turns = [
        *allow_turns(),
        FINAL,
        navigate(),
        act(1, CONTINUE),
        observe(),
        act(2, CONTINUE),
        FINAL,
    ]
    async with pipeline(tmp_path, turns) as harness:
        grant_id = await allowed_task(harness)
        leases_before = len(harness.runtimes)
        harness.late_redirect[0] = f"{ORIGIN}/settings"
        run_id = await harness.submit("Keep going.")
        approval = await harness.pending(run_id)
        [refused, parked] = await harness.acts(run_id)
        runtime = harness.runtimes[-1]
        lease_open_while_parked = not runtime.closed
        await harness.composition.approvals.resolve(
            approval.id, ApprovalResolutionType.APPROVE_ONCE
        )
        run = await harness.settled(run_id)
        grant = await harness.grant(grant_id)

    assert refused.status is ToolInvocationStatus.FAILED
    assert refused.outcome is not None
    assert refused.outcome.reason_code == GRANT_NOT_APPLICABLE
    assert parked.status is ToolInvocationStatus.WAITING_FOR_APPROVAL
    assert len(runtime.constrained) == 1
    assert approval.task_grant_not_covered is not None
    assert approval.task_grant_not_covered.reason == "browser.task_grant.outside_prefix"
    # One lease served the refusal, the observation and the approved act: the
    # refusal kept it, and the approved act reused the refused one's sequence.
    assert len(harness.runtimes) == leases_before + 1
    assert lease_open_while_parked
    assert run.status is RunStatus.COMPLETED
    assert [(name, kind) for name, kind, _value in runtime.performed] == [
        ("Continue", BrowserActionKind.CLICK)
    ]
    assert grant.actions_used == 1


async def test_a_runtime_refusal_keeps_the_lease_and_forces_an_observe(tmp_path: Path) -> None:
    await assert_a_runtime_refusal_keeps_the_lease_and_forces_an_observe(tmp_path)


async def _reset_postgres(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
            await connection.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


async def test_allowed_task_on_postgresql(tmp_path: Path) -> None:
    """The durable path: the grant is created with the resolution and each
    use is a guarded update under row-level security."""

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the PostgreSQL pipeline case")
    await _reset_postgres(database_url)
    settings = task_grant_settings(DATABASE_URL=database_url)
    turns = [*allow_turns(), act(2, CONTINUE), act(3, CONTINUE), FINAL]
    async with pipeline(tmp_path, turns, settings=settings, storage="postgres") as harness:
        run_id = await harness.submit("Do one lesson.")
        grant_id = await harness.allow_task(run_id)
        grant = await harness.grant(grant_id)
        authorizations = await harness.authorizations(run_id)

    assert [payload["authorization_use"] for payload in authorizations] == [1, 2]
    assert grant.actions_used == 2
