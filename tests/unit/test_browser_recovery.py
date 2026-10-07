"""Recovery is a separately audited read; sign-in suspends without replay."""

import asyncio
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.config import Settings, load_settings
from agent_core.domain.browser import BrowserObservation, BrowserProviderError
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from tests.gates.test_api_m5 import _client
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "recorded_ready",
        "wrong_profile",
        "old_ceremony",
        "newer_ceremony",
        "unchanged_generation",
        "expired_wait",
        "cancelled",
        "revoked",
    ],
)
async def test_verified_browser_authentication_resumes_once(
    change: str,
    *,
    settings: Settings | None = None,
    storage: Literal["memory", "postgres"] = "memory",
) -> None:
    from datetime import timedelta

    from agent_core.domain.browser import (
        BrowserAuthenticationRecord,
        BrowserAuthenticationStatus,
        BrowserProfileStatus,
    )
    from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority

    class FreshLeaseBrowser(InterruptedBrowser):
        async def navigate(self, url: str) -> BrowserObservation:
            if self.navigations:
                return await FakeBrowserProvider.navigate(self, url)
            return await super().navigate(url)

        async def observe(self) -> BrowserObservation:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)

    provider = FreshLeaseBrowser()
    provider.reason = "tool.browser.needs_user"
    async with build(
        settings=settings or load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        storage=storage,
        script=FakeModelScript(turns=[navigation(), ScriptedTurn(text="Read fresh evidence.")]),
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe", "conversation.ask_user"],
        browser_provider_override=provider,
    ) as composition:
        await seed_browser_authority(composition)
        created = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await composition.runs.submit("Read the selected account.", created.id)
        worker = None
        if storage == "postgres":
            from agent_core.runtime.worker import DurableWorker

            worker = DurableWorker(
                uow_factory=composition.uow_factory,
                executor=composition.executor,
                clock=composition.clock,
                worker_id="browser-continuation-fixture",
            )
            assert await worker.run_once()
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
            assert checkpoint is not None
            wait = checkpoint.working_state.get("browser_auth_wait")
            assert wait is not None, "browser interruptions have no durable verified-resume binding"
            assert wait["profile_id"] == str(PROFILE_ID)
            assert wait["question_id"] == checkpoint.working_state["outstanding_question_id"]
            assert "reply" not in checkpoint.working_state["outstanding_question_text"]
            profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
            now = checkpoint.created_at
            ceremony = BrowserAuthenticationRecord(
                id=UUID(int=987654),
                tenant_id=profile.tenant_id,
                principal_id=profile.principal_id,
                profile_id=PROFILE_ID,
                status=(
                    BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
                    if change == "recorded_ready"
                    else BrowserAuthenticationStatus.READY
                ),
                expires_at=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
            if change == "wrong_profile":
                ceremony = ceremony.model_copy(update={"profile_id": UUID(int=987655)})
            if change == "old_ceremony":
                ceremony = ceremony.model_copy(update={"created_at": now - timedelta(days=1)})
            await uow.browser_authentications.create(ceremony)
            if change == "newer_ceremony":
                await uow.browser_authentications.create(
                    ceremony.model_copy(
                        update={
                            "id": UUID(int=987656),
                            "created_at": now + timedelta(seconds=1),
                            "status": BrowserAuthenticationStatus.CANCELLED,
                        }
                    )
                )
            if change not in {"unchanged_generation", "recorded_ready"}:
                profile = await uow.browser_profiles.advance_generation(
                    PROFILE_ID,
                    composition.principal,
                    expected_generation=profile.generation,
                    updated_at=now,
                )
            if change == "revoked":
                await uow.browser_profiles.transition(
                    PROFILE_ID,
                    composition.principal,
                    expected_generation=profile.generation,
                    status=BrowserProfileStatus.REVOKED,
                    updated_at=now,
                )
            if change == "expired_wait":
                checkpoint.working_state["browser_auth_wait"]["expires_at"] = now.isoformat()
                checkpoint.version += 1
                await uow.checkpoints.write(run_id, checkpoint, full=True)
        if change == "recorded_ready":
            from datetime import datetime

            from agent_core.adapters.browser.profiles import InMemoryBrowserProfileControlPlane
            from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
            from agent_core.application.browser_management import (
                BrowserProfileManagementService,
                BrowserUnitOfWorkFactory,
            )
            from tests.unit.test_browser_management import FakeAuthenticationControlPlane

            class AdvancingClock(FixedClock):
                def now(self) -> datetime:
                    self.advance(timedelta(microseconds=1))
                    return super().now()

            profiles = BrowserProfileManagementService(
                uow_factory=cast(BrowserUnitOfWorkFactory, composition.uow_factory),
                lifecycle=InMemoryBrowserProfileControlPlane(),
                authentications=FakeAuthenticationControlPlane(
                    status=BrowserAuthenticationStatus.READY,
                    profile_id=PROFILE_ID,
                ),
                clock=AdvancingClock(now),
                ids=SequenceIdFactory([]),
            )
            await profiles.authentication_status(composition.principal, ceremony.id)
        if change == "cancelled":
            await composition.services.runs.cancel(composition.principal, run_id)
        # A new service instance must be able to recover a missed delivery from persisted state.
        from agent_core.application.browser_continuation import (
            BrowserContinuationService,
            VerifiedBrowserRunResumer,
        )

        continuation = BrowserContinuationService(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            runs=cast(VerifiedBrowserRunResumer, composition.services.runs),
            profiles=composition.services.browser_profiles,
            principal=composition.principal,
        )
        delivered = await asyncio.gather(
            continuation.sweep(), continuation.resume_profile(composition.principal, PROFILE_ID)
        )
        assert sum(delivered) == int(change in {"none", "recorded_ready"})
        assert await continuation.sweep() == 0
        if change not in {"none", "recorded_ready"}:
            assert provider.observation_count == 0 and len(provider.navigations) == 1
            return
        if worker is not None:
            assert await worker.run_once()
        result = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    assert result.status is RunStatus.COMPLETED
    assert provider.observation_count == 0 and len(provider.navigations) == 2
    assert len([event for event in events if event.event_type == "user.message.created"]) == 1
    from agent_core.context.history import validate_tool_pairs
    from agent_core.domain.events import conversation_items

    validate_tool_pairs([item for event in events for item in conversation_items(event)])


class InterruptedBrowser(FakeBrowserProvider):
    reason = "tool.browser.page_changed"

    async def navigate(self, url: str) -> BrowserObservation:
        self.navigations.append(url)
        raise BrowserProviderError(self.reason, retryable=False)


def navigation(call_id: str = "open") -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="browser.navigate",
                arguments={"url": "https://example.org/account"},
                call_id=call_id,
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


@pytest.mark.parametrize(
    "reason,reads",
    [
        ("tool.browser.page_changed", 1),
        ("tool.browser.element_not_found", 1),
        ("tool.browser.outcome_unknown", 0),
        ("tool.browser.provider_unavailable", 0),
        ("tool.browser.profile_unavailable", 0),
    ],
)
async def test_only_stale_failures_get_one_audited_read(reason: str, reads: int) -> None:
    provider = InterruptedBrowser()
    provider.reason = reason
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[navigation(), ScriptedTurn(text="Report evidence.")]),
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe"],
        browser_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Read the account.")
        result = await composition.runs.wait_terminal(run_id)
        recorded_events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
    assert result.status is RunStatus.COMPLETED
    assert provider.navigations == ["https://example.org/account"]
    assert provider.observation_count == reads
    from agent_core.context.history import validate_tool_pairs
    from agent_core.domain.events import conversation_items

    validate_tool_pairs([item for event in recorded_events for item in conversation_items(event)])
    assert len(invocations) == 1 + reads
    assert invocations[0].status is (
        ToolInvocationStatus.UNCERTAIN if "unknown" in reason else ToolInvocationStatus.FAILED
    )


async def test_authentication_interrupts_and_resumes_without_replaying_the_failed_call(
    tmp_path: Path,
) -> None:
    provider = InterruptedBrowser()
    provider.reason = "tool.browser.needs_user"
    async with (
        build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(
                turns=[navigation(), ScriptedTurn(text="Ready to read fresh evidence.")]
            ),
            sequential_ids=True,
            enabled_tools=["browser.navigate", "browser.observe", "conversation.ask_user"],
            browser_provider_override=provider,
        ) as composition,
        _client(composition) as client,
    ):
        run_id = await composition.runs.submit("Read my account after I sign in.")
        view = await client.get(f"/v1/runs/{run_id}")
        assert view.json()["status"] == "WAITING_FOR_USER"
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
        assert checkpoint is not None
        assert invocations[0].status is ToolInvocationStatus.FAILED
        assert [c["name"] for c in checkpoint.pending_tool_calls] == ["conversation.ask_user"]
        question_id = UUID(checkpoint.working_state["outstanding_question_id"])
        assert "sign in" in checkpoint.working_state["outstanding_question_text"]
        reply = await client.post(
            f"/v1/runs/{run_id}/input",
            json={
                "question_id": str(question_id),
                "content": [{"type": "text", "text": "Done"}],
            },
        )
        assert reply.status_code == 202
        result = await composition.runs.wait_terminal(run_id)
        recorded_events = await composition.runs.events(run_id)
    from agent_core.context.history import validate_tool_pairs
    from agent_core.domain.events import conversation_items

    validate_tool_pairs([item for event in recorded_events for item in conversation_items(event)])
    assert result.status is RunStatus.COMPLETED
    assert provider.navigations == ["https://example.org/account"]


async def test_recovery_respects_tool_budget() -> None:
    provider = InterruptedBrowser()
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[navigation(), ScriptedTurn(text="Incomplete.")]),
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe"],
        browser_provider_override=provider,
        limits=RunLimits(max_tool_calls=1),
    ) as composition:
        run_id = await composition.runs.submit("Read account.")
        result = await composition.runs.wait_terminal(run_id)
    assert result.status is RunStatus.COMPLETED
    assert provider.observation_count == 0


async def test_recovery_read_failures_do_not_recurse_and_total_reads_are_bounded() -> None:
    class StillChanging(InterruptedBrowser):
        async def observe(self) -> BrowserObservation:
            self.observation_count += 1
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)

    provider = StillChanging()
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(
            turns=[
                navigation("one"),
                navigation("two"),
                navigation("three"),
                navigation("four"),
                ScriptedTurn(text="The page keeps changing."),
            ]
        ),
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe"],
        browser_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Read account.")
        result = await composition.runs.wait_terminal(run_id)
    assert result.status is RunStatus.COMPLETED
    assert provider.observation_count == 3
    assert len(provider.navigations) == 4


async def test_sign_in_questions_are_bounded_even_when_owner_replies_without_signing_in() -> None:
    provider = InterruptedBrowser()
    provider.reason = "tool.browser.authentication_required"
    async with (
        build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=FakeModelScript(
                turns=[
                    navigation("one"),
                    navigation("two"),
                    navigation("three"),
                    ScriptedTurn(text="Sign-in remains incomplete."),
                ]
            ),
            sequential_ids=True,
            enabled_tools=["browser.navigate", "browser.observe", "conversation.ask_user"],
            browser_provider_override=provider,
        ) as composition,
        _client(composition) as client,
    ):
        run_id = await composition.runs.submit("Read account.")
        for _ in range(2):
            async with composition.uow_factory() as uow:
                state = await uow.checkpoints.latest(run_id)
            assert state is not None
            response = await client.post(
                f"/v1/runs/{run_id}/input",
                json={
                    "question_id": state.working_state["outstanding_question_id"],
                    "content": [{"type": "text", "text": "Continue"}],
                },
            )
            assert response.status_code == 202
        result = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
    assert result.status is RunStatus.COMPLETED
    assert len([i for i in invocations if i.tool_name == "conversation.ask_user"]) == 2
    assert len(provider.navigations) == 3


async def test_sign_in_after_an_approved_action_suspends_and_cancellation_never_replays() -> None:
    from agent_core.domain.approvals import ApprovalResolutionType
    from agent_core.domain.browser import BrowserAction, BrowserDispatchConstraint

    class AfterClick(FakeBrowserProvider):
        async def act(
            self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
        ) -> BrowserObservation:
            self.actions.append(action)
            return BrowserObservation(
                url="https://example.org/login", revision="signed-out", interruption="needs_user"
            )

    provider = AfterClick()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments={
                            "kind": "click",
                            "ref": "element-1",
                            "expected_revision": "revision-1",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Must not run while waiting."),
        ]
    )
    async with (
        build(
            settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
            script=script,
            sequential_ids=True,
            enabled_tools=["browser.act", "browser.observe", "conversation.ask_user"],
            browser_provider_override=provider,
        ) as composition,
        _client(composition) as client,
    ):
        run_id = await composition.runs.submit("Continue my task.")
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        view = await client.get(f"/v1/runs/{run_id}")
        assert view.json()["status"] == "WAITING_FOR_USER"
        async with composition.uow_factory() as uow:
            state = await uow.checkpoints.latest(run_id)
        assert state is not None
        assert [c["name"] for c in state.pending_tool_calls] == ["conversation.ask_user"]
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
        assert invocations[0].status is ToolInvocationStatus.UNCERTAIN
        assert invocations[0].outcome is not None
        assert invocations[0].outcome.reason_code == "tool.browser.outcome_unknown"
        await composition.runs.cancel(run_id)
        result = await composition.runs.wait_terminal(run_id)
    assert result.status is RunStatus.CANCELLED
    assert len(provider.actions) == 1
