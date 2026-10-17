"""A reviewed personal-feed briefing needs linked posts, not an empty home shell."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from agent_core.bootstrap import Composition, build
from agent_core.config import load_config_document, load_settings
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserLease,
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.browser_workflows import BrowserWorkflowCatalog
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, TextPart, ToolResultItem
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import GRANT_NOW, PROFILE_ID
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment


def test_shipped_morning_briefing_has_a_bounded_feed_collection_step() -> None:
    catalog = BrowserWorkflowCatalog.model_validate(
        load_config_document(load_settings(base_environment()), "runtime/browser-workflows.yaml")
    )
    recipes = [recipe for recipe in catalog.recipes if recipe.id == "x-home-briefing"]
    assert len(recipes) == 1, "the reviewed morning X briefing recipe is missing"
    assert recipes[0].origin == "https://x.com"
    assert [step.kind for step in recipes[0].steps] == ["navigate", "collect_feed"]


INTENT = "Give me my morning X feed briefing."


async def test_feed_workflow_stays_off_until_operator_activation() -> None:
    provider = FeedProvider()
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Ordinary agent path.")]),
        browser_provider_override=provider,
    ) as app:
        await seed_feed_profile(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        await app.runs.submit(INTENT, session.id)
    assert not provider.navigations, "the site workflow activated without its rollout switch"


class FeedProvider(FakeBrowserProvider):
    def __init__(self, *, empty_reads: int = 0, repeated: bool = False) -> None:
        super().__init__(allowed_origins=("https://x.com",))
        self.empty_reads = empty_reads
        self.repeated = repeated
        self.reads = 0
        self.serial = 0

    def _observation(self, url: str) -> BrowserObservation:
        self.serial += 1
        return BrowserObservation(
            url=url,
            revision=f"feed-{self.serial}",
            text="Your Home Timeline",
            elements=(
                BrowserElement(ref=f"feed-{self.serial}:0", role="button", name="Account menu"),
            ),
        )

    async def observe(self) -> BrowserObservation:
        return self._observation("https://x.com/home")

    async def act(
        self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
    ) -> BrowserObservation:
        self.actions.append(action)
        return await self.observe()

    async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
        from agent_core.adapters.browser.extraction import extracted_observation

        self.reads += 1
        observed = await self.observe()
        rows = []
        if self.reads > self.empty_reads:
            offset = 0 if self.repeated else 3 * len(self.actions)
            for index in range(offset, offset + 3):
                rows.append(
                    [
                        {
                            "text": f"Observed post {index}: agent tooling and model serving.",
                            "text_truncated": False,
                        },
                        {
                            "text": f"https://x.com/author/status/{100 + index}",
                            "text_truncated": False,
                        },
                    ]
                )
        raw = {
            "status": "extracted",
            "source_name": "Home",
            "rows": rows,
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": len(rows),
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        }
        result = extracted_observation(raw, request, observed.revision)
        return observed.model_copy(update={"extraction": result})


async def seed_feed_profile(app: Any) -> None:
    async with app.uow_factory() as uow:
        await uow.browser_profiles.create(
            BrowserProfile(
                id=PROFILE_ID,
                tenant_id=app.principal.tenant_id,
                principal_id=app.principal.principal_id,
                provider_name="hosted-isolated",
                provider_ref="synthetic-feed",
                allowed_origins=("https://x.com",),
                status=BrowserProfileStatus.READY,
                generation=3,
                encryption_key_version="key-v1",
                created_at=GRANT_NOW,
                updated_at=GRANT_NOW,
            )
        )


@asynccontextmanager
async def feed_app(
    provider: FakeBrowserProvider, *, real_clock: bool = False
) -> AsyncIterator[Composition]:
    async with build(
        settings=load_settings(
            {
                **base_environment(),
                "SANDBOX_MECHANISM": "fake",
                "BROWSER_FEED_WORKFLOW_ENABLED": "1",
            }
        ),
        fixed_clock_at=None if real_clock else GRANT_NOW,
        script=FakeModelScript(turns=[ScriptedTurn(text="An observed-feed summary.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_feed_profile(app)
        yield app


@pytest.mark.parametrize("empty_reads", [0, 2])
async def test_briefing_waits_for_posts_collects_links_and_reports_without_prompting(
    empty_reads: int,
) -> None:
    provider = FeedProvider(empty_reads=empty_reads)
    async with build(
        settings=load_settings(
            {
                **base_environment(),
                "SANDBOX_MECHANISM": "fake",
                "BROWSER_FEED_WORKFLOW_ENABLED": "1",
            }
        ),
        fixed_clock_at=GRANT_NOW,
        script=FakeModelScript(turns=[ScriptedTurn(text="An observed-feed summary.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_feed_profile(app)
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert run.status is RunStatus.COMPLETED
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert state["status"] == "verified", "the feed never collected linked post evidence"
        assert len(state["feed"]["post_ids"]) == 6
        assert provider.reads >= 2 + empty_reads
        assert len(provider.actions) == 1
        assert all(action.kind.value == "scroll" for action in provider.actions)
        assert checkpoint.working_state["browser_workflow_report_only"]
        assert not await app.approvals.list_pending(run_id=run_id)
        assert not any(
            event.event_type == "run.waiting_for_user" for event in await app.runs.events(run_id)
        )


@pytest.mark.parametrize("problem", ["empty", "repeated", "ambiguous", "redirect", "lost_scroll"])
async def test_feed_failures_stop_bounded_without_mutation_replay(problem: str) -> None:
    class Provider(FeedProvider):
        def _observation(self, url: str) -> BrowserObservation:
            observed = super()._observation(url)
            if problem == "redirect":
                return observed.model_copy(update={"url": "https://x.com/explore"})
            if problem == "ambiguous":
                return observed.model_copy(
                    update={
                        "elements": (
                            *observed.elements,
                            observed.elements[0].model_copy(update={"ref": "duplicate"}),
                        )
                    }
                )
            return observed

        async def act(
            self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
        ) -> BrowserObservation:
            observed = await super().act(action, constraint=constraint)
            if problem == "lost_scroll":
                raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
            return observed

    provider = Provider(
        empty_reads=100 if problem == "empty" else 0, repeated=problem == "repeated"
    )
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert state["status"] == (
            "uncertain_scroll"
            if problem == "lost_scroll"
            else "ambiguous_target"
            if problem == "ambiguous"
            else "insufficient_evidence"
        )
        assert provider.reads <= 7
        assert len(provider.actions) == int(problem in {"repeated", "lost_scroll"})
        assert not await app.approvals.list_pending(run_id=run_id)


class UnlinkedTopProvider(FeedProvider):
    """The first window holds one tall post or ad whose link walk supplies no link."""

    def __init__(self, *, stuck: bool = False) -> None:
        super().__init__()
        self.stuck = stuck

    async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
        from agent_core.adapters.browser.extraction import extracted_observation

        if self.actions and not self.stuck:
            return await super().extract(request)
        self.reads += 1
        observed = await self.observe()
        raw = {
            "status": "extracted",
            "source_name": "",
            "rows": [
                [
                    {
                        "text": "A tall video quote post without a usable link.",
                        "text_truncated": True,
                    },
                    None,
                ]
            ],
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": 1,
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        }
        result = extracted_observation(raw, request, observed.revision)
        return observed.model_copy(update={"extraction": result})


async def test_unlinked_top_post_is_scrolled_past_instead_of_reread() -> None:
    # Production 2026-10-13: one unlinkable post filled the first window, the
    # workflow re-read it six times without scrolling and reported no posts.
    provider = UnlinkedTopProvider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert state["status"] == "verified", "an unlinked first window blocked collection"
        assert len(state["feed"]["post_ids"]) == 6
        assert len(provider.actions) == 2
        assert provider.reads == 3


async def test_unlinked_window_that_never_moves_stops_bounded() -> None:
    provider = UnlinkedTopProvider(stuck=True)
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert state["status"] == "insufficient_evidence"
        assert state["feed"]["post_ids"] == []
        assert len(provider.actions) == 1
        assert provider.reads <= 8


async def test_stale_extraction_recovers_with_fresh_evidence() -> None:
    class Provider(FeedProvider):
        stale = True

        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            if self.stale:
                self.stale = False
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            return await super().extract(request)

    provider = Provider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        assert checkpoint.working_state["browser_workflow"]["status"] == "verified"
        assert checkpoint.working_state["browser_recovery_reads"] == 1
        assert (
            checkpoint.working_state["browser_workflow"]["calls"]
            == (await app.runs.get(run_id)).tool_call_count
        )
        assert len(provider.actions) == 1


@pytest.mark.parametrize("phase", ["navigate", "extract", "scroll"])
async def test_verified_authentication_resumes_without_reply_or_replaying_scroll(
    phase: str,
) -> None:
    from datetime import timedelta
    from typing import cast
    from uuid import UUID

    from agent_core.application.browser_continuation import VerifiedBrowserRunResumer
    from agent_core.domain.browser import BrowserAuthenticationRecord, BrowserAuthenticationStatus

    class Provider(FeedProvider):
        interrupted = False

        def interrupt_once(self, stage: str) -> None:
            if stage == phase and not self.interrupted:
                self.interrupted = True
                raise BrowserProviderError("tool.browser.needs_user", retryable=False)

        async def navigate(self, url: str) -> BrowserObservation:
            self.interrupt_once("navigate")
            return await super().navigate(url)

        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            if self.reads:
                self.interrupt_once("extract")
            return await super().extract(request)

        async def act(
            self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
        ) -> BrowserObservation:
            observed = await super().act(action, constraint=constraint)
            self.interrupt_once("scroll")
            return observed

    provider = Provider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER
        ceremony_id = UUID(int=88559)
        now = app.clock.now()
        async with app.uow_factory() as uow:
            before = await uow.checkpoints.latest(run_id)
            assert before is not None
            old_evidence = (
                before.working_state["browser_workflow"]
                .get("feed", {})
                .get("evidence_call_ids", [])
            )
            profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
            await uow.browser_authentications.create(
                BrowserAuthenticationRecord(
                    id=ceremony_id,
                    profile_id=PROFILE_ID,
                    tenant_id=profile.tenant_id,
                    principal_id=profile.principal_id,
                    status=BrowserAuthenticationStatus.READY,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await uow.browser_profiles.advance_generation(
                PROFILE_ID, app.principal, expected_generation=profile.generation, updated_at=now
            )
        resumer = cast(VerifiedBrowserRunResumer, app.services.runs)
        assert await resumer.resume_verified_browser_authentication(
            app.principal, run_id, ceremony_id
        )
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        assert not await resumer.resume_verified_browser_authentication(
            app.principal, run_id, ceremony_id
        )
        async with app.uow_factory() as uow:
            after = await uow.checkpoints.latest(run_id)
        assert after is not None
        state = after.working_state["browser_workflow"]
        assert state["status"] == ("uncertain_scroll" if phase == "scroll" else "verified")
        assert not set(old_evidence) & set(state["feed"]["evidence_call_ids"])
        assert len(provider.actions) == (2 if phase == "extract" else 1)
        events = await app.runs.events(run_id)
        assert sum(event.event_type == "run.waiting_for_user" for event in events) == 1
        assert sum(event.event_type == "user.message.created" for event in events) == 1


async def test_sign_in_wait_does_not_spend_the_feed_collection_ceiling() -> None:
    """The 2026-10-17 briefing: a three-minute sign-in left the 180-second ceiling spent."""
    from datetime import timedelta
    from typing import cast
    from uuid import UUID

    from agent_core.adapters.determinism import FixedClock
    from agent_core.application.browser_continuation import VerifiedBrowserRunResumer
    from agent_core.domain.browser import BrowserAuthenticationRecord, BrowserAuthenticationStatus

    class Provider(FeedProvider):
        signed_out = True

        async def navigate(self, url: str) -> BrowserObservation:
            if self.signed_out:
                self.signed_out = False
                raise BrowserProviderError("tool.browser.authentication_required", retryable=False)
            return await super().navigate(url)

    provider = Provider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER
        # The owner takes longer to sign in than the recipe allows for collection.
        cast(FixedClock, app.clock).advance(timedelta(seconds=200))
        ceremony_id = UUID(int=88560)
        now = app.clock.now()
        async with app.uow_factory() as uow:
            profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
            await uow.browser_authentications.create(
                BrowserAuthenticationRecord(
                    id=ceremony_id,
                    profile_id=PROFILE_ID,
                    tenant_id=profile.tenant_id,
                    principal_id=profile.principal_id,
                    status=BrowserAuthenticationStatus.READY,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await uow.browser_profiles.advance_generation(
                PROFILE_ID, app.principal, expected_generation=profile.generation, updated_at=now
            )
        resumer = cast(VerifiedBrowserRunResumer, app.services.runs)
        assert await resumer.resume_verified_browser_authentication(
            app.principal, run_id, ceremony_id
        )
        assert (await app.runs.wait_terminal(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            after = await uow.checkpoints.latest(run_id)
        assert after is not None
        state = after.working_state["browser_workflow"]
        assert state["status"] == "verified", state["status"]
        assert len(state["feed"]["post_ids"]) == 6
        assert "suspended_at" not in state


def test_feed_report_names_the_reason_collection_stopped() -> None:
    from agent_core.runtime.browser_feed import feed_report_control

    text = feed_report_control({"status": "bounded_stop", "feed": {}})
    assert "Collection outcome: bounded_stop" in text
    assert "ran out of its time or call ceiling" in text
    assert "say why collection stopped" in text
    verified = feed_report_control({"status": "verified", "feed": {"post_ids": ["1"] * 6}})
    assert "ran out of" not in verified


@pytest.mark.parametrize(
    "intent", [INTENT + " Then follow someone.", "Do not " + INTENT, "Give me public X trends."]
)
async def test_nonmatching_owner_intent_cannot_select_the_briefing(intent: str) -> None:
    provider = FeedProvider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        await app.runs.submit(intent, session.id)
    assert provider.navigations == []


async def test_long_schedule_intent_is_matched_exactly_by_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    import agent_core.bootstrap as bootstrap

    instruction = "Read my personalized X feed. " + "Use observed evidence only. " * 40
    catalog = load_config_document(
        load_settings(base_environment()), "runtime/browser-workflows.yaml"
    )
    catalog["recipes"][0]["intent_sha256"] = [
        hashlib.sha256(" ".join(instruction.casefold().split()).rstrip(".!?").encode()).hexdigest()
    ]
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, relative: (
            catalog
            if relative == "runtime/browser-workflows.yaml"
            else load_config_document(settings, relative)
        ),
    )
    provider = FeedProvider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(instruction, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        assert len(provider.actions) == 1


async def test_final_report_has_no_tools_and_only_current_feed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.runtime.loop as loop
    from agent_core.domain.messages import ModelRequest, UserMessage
    from agent_core.domain.policies import TrustLevel

    original = loop._synthesis_only_request
    captured = []

    def capture(
        request: ModelRequest, dimension: str, workflow: dict[str, Any] | None = None
    ) -> ModelRequest:
        result = original(request, dimension, workflow)
        captured.append(result)
        return result

    monkeypatch.setattr(loop, "_synthesis_only_request", capture)
    async with feed_app(FeedProvider()) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        await app.runs.submit(INTENT, session.id)
    assert len(captured) == 1 and captured[0].tool_choice == "none"
    control = captured[0].conversation[-1]
    assert isinstance(control, UserMessage)
    assert control.trust is TrustLevel.PLATFORM
    text = " ".join(part.text for part in control.content if isinstance(part, TextPart))
    assert "unique linked posts: 6" in text and "not all X activity" in text
    assert "Observed post" not in text
    results = [item for item in captured[0].conversation if isinstance(item, ToolResultItem)]
    assert results and all(item.trust is TrustLevel.EXTERNAL_UNTRUSTED for item in results)


@pytest.mark.parametrize("startup_failures", [0, 1, 2, 3])
async def test_materialized_morning_schedule_finishes_with_its_read_scope_and_budget(
    startup_failures: int,
) -> None:
    from dataclasses import replace
    from typing import cast

    from agent_core.adapters.browser.hosted_provider import SessionBoundHostedBrowserProvider
    from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
    from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
    from agent_core.domain.schedules import ScheduleDefinition
    from agent_core.runtime.checkpoints import DurableCheckpointSeeder
    from agent_core.scheduling.materializer import ScheduleMaterializer
    from agent_core.tools.browser_navigate import BrowserNavigateTool
    from agent_core.tools.registry import RegisteredTool
    from tests.gates.test_schedule_api_m11 import _definition
    from tests.unit.test_browser_composition import session_bound_hosted_settings
    from tests.unit.test_hosted_browser_provider import FakeSessions

    provider = FeedProvider()
    async with build(
        settings=replace(session_bound_hosted_settings(), browser_feed_workflow_enabled=True),
        fixed_clock_at=GRANT_NOW,
        script=FakeModelScript(turns=[ScriptedTurn(text="Observed-feed briefing.")]),
    ) as app:
        await seed_feed_profile(app)

        class Sessions(FakeSessions):
            acquire_attempts = 0

            async def acquire(self, *args: Any, **kwargs: Any) -> BrowserLease:
                self.acquire_attempts += 1
                if self.acquire_attempts <= startup_failures:
                    raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)
                return await super().acquire(*args, **kwargs)

            async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
                return await provider.navigate(url)

            async def observe(self, lease_ref: str) -> BrowserObservation:
                return await provider.observe()

            async def extract(
                self, lease_ref: str, request: BrowserExtractionRequest
            ) -> BrowserObservation:
                return await provider.extract(request)

            async def act(
                self,
                lease_ref: str,
                action: BrowserAction,
                *,
                sequence: int,
                constraint: BrowserDispatchConstraint | None = None,
            ) -> BrowserObservation:
                return await provider.act(action, constraint=constraint)

        registered = cast(RegisteredTool, app.tool_pipeline._registry.get("browser.navigate"))
        tool = cast(BrowserNavigateTool, registered.implementation)
        hosted = cast(SessionBoundHostedBrowserProvider, tool._provider)
        sessions = Sessions()
        hosted._sessions = sessions
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        definition = ScheduleDefinition.model_validate(
            {
                **_definition(),
                "instruction": INTENT,
                "agent_id": str(session.agent_id),
                "agent_version": session.agent_version,
                "requested_scopes": ["browser.profile.read"],
                "browser_profile_id": str(PROFILE_ID),
                "run_timeout_seconds": 300,
                "cadence": {"kind": "ONCE", "at": GRANT_NOW.isoformat()},
                "limits": {
                    "max_steps": 32,
                    "max_tool_calls": 64,
                    "max_model_calls": 24,
                    "max_cost": "5",
                    "synthesis_reserve_steps": 2,
                    "synthesis_reserve_model_calls": 2,
                    "synthesis_reserve_cost": "1",
                },
            }
        )
        record = await app.services.schedules.create(app.principal, definition, "morning-test")
        materializer = ScheduleMaterializer(
            uow_factory=app.uow_factory,
            principals=StaticSchedulePrincipalDirectory(app.principal),
            admission=AllowScheduleAdmissionController(),
            clock=app.clock,
            ids=app.ids,
            seed_checkpoint=DurableCheckpointSeeder(app.clock),
        )
        occurrence = await materializer.materialize(record.schedule.id)
        assert occurrence is not None and occurrence.run_id is not None
        await app.executor.execute(occurrence.run_id)
        run = await app.runs.get(occurrence.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.principal_scopes == {"browser.profile.read"}
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run.id)
        assert checkpoint is not None
        assert checkpoint.working_state["browser_workflow"]["status"] == (
            "verified" if startup_failures < 3 else "unverified"
        )
        assert sessions.acquire_attempts == min(startup_failures + 1, 3)
        assert run.tool_call_count < 32 and run.model_call_count == 1
        assert len(provider.actions) == (1 if startup_failures < 3 else 0)
        assert not await app.approvals.list_pending(run_id=run.id)


async def test_retry_rechecks_workflow_deadline_before_another_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.bootstrap as bootstrap

    catalog = load_config_document(
        load_settings(base_environment()), "runtime/browser-workflows.yaml"
    )
    catalog["recipes"][0]["maximum_seconds"] = 1
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, relative: (
            catalog
            if relative == "runtime/browser-workflows.yaml"
            else load_config_document(settings, relative)
        ),
    )
    provider = FeedProvider(empty_reads=100)
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        assert checkpoint.working_state["browser_workflow"]["status"] == "bounded_stop"
        assert provider.reads == 1 and not provider.actions


async def test_cancelling_during_feed_read_stops_before_scroll() -> None:
    from agent_core.domain.errors import RunCancelledError

    class Provider(FeedProvider):
        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            raise RunCancelledError("cancelled")

    provider = Provider()
    async with feed_app(provider) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await app.runs.submit(INTENT, session.id)
        assert (await app.runs.get(run_id)).status is RunStatus.CANCELLED
        assert not provider.actions


@pytest.mark.parametrize(
    "url",
    [
        "https://[",
        "https://other.example/author/status/1",
        "https://x.com/author",
        "https://x.com/author/status/1?token=secret",
        "https://" + "fixture:fixture@x.com/author/status/1",
    ],
)
async def test_malformed_or_non_post_links_never_count_as_feed_evidence(url: str) -> None:
    from agent_core.runtime.browser_feed import FIELDS, post_ids

    observed = await FeedProvider().extract(
        BrowserExtractionRequest.model_validate(
            {
                "kind": "articles",
                "expected_revision": "before",
                "fields": FIELDS,
                "row_limit": 3,
            }
        )
    )
    extracted = observed.extraction
    assert extracted is not None
    row = extracted.rows[0]
    row = row.model_copy(
        update={
            "cells": (
                row.cells[0],
                row.cells[1].model_copy(update={"value": url, "text": url}),
            )
        }
    )
    observed = observed.model_copy(
        update={"extraction": extracted.model_copy(update={"rows": (row,)})}
    )
    assert post_ids(observed) == []


@pytest.mark.parametrize("change", ["legacy", "mixed", "selector", "oversized", "foreign_revision"])
async def test_article_tool_preserves_pinned_validation_and_revision_boundaries(
    change: str,
) -> None:
    from agent_core.runtime.browser_feed import FIELDS
    from agent_core.tools.browser_observe import ArticleBrowserObserveTool, BrowserObserveTool
    from tests.contract.support import tool_context

    class Provider(FeedProvider):
        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            if request.expected_revision != "current":
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            return await super().extract(request)

    provider = Provider()
    arguments: dict[str, Any] = {
        "extract": {
            "kind": "articles",
            "expected_revision": "current",
            "fields": FIELDS,
            "row_limit": 3,
        }
    }
    if change == "mixed":
        arguments["wait_for"] = {"role": "button", "name": "Like"}
    elif change == "selector":
        arguments["extract"]["selector"] = "#private"
    elif change == "oversized":
        arguments["extract"]["row_limit"] = 51
    elif change == "foreign_revision":
        arguments["extract"]["expected_revision"] = "foreign"
    tool = (
        BrowserObserveTool(provider) if change == "legacy" else ArticleBrowserObserveTool(provider)
    )
    result = await tool.execute(arguments, tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == (
        "tool.browser.page_changed" if change == "foreign_revision" else "tool.arguments_invalid"
    )
    assert provider.reads == 0 and not provider.actions
    if change != "foreign_revision":
        assert not provider.execution_contexts
