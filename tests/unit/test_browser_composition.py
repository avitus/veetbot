"""Default-off composition for the browser-provider seam."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.browser.hosted_provider import (
    HostedBrowserProvider,
    SessionBoundHostedBrowserProvider,
)
from agent_core.adapters.browser.playwright import PlaywrightBrowserProvider
from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.application.browser_leases import browser_run_state
from agent_core.bootstrap import Composition, build
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.config import ConfigurationError, Settings, load_settings
from agent_core.context.builder import _browser_origins_field
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.agents import BROWSER_TASK_LIMITS_METADATA_KEY, Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserActionKind,
    BrowserGrant,
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
    BrowserRunState,
)
from agent_core.domain.errors import InvalidStateTransition, NotFoundError
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolInvocation,
    ToolInvocationStatus,
    ToolSpec,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.browser import browser_lease_upkeep
from agent_core.ports.browser_sessions import BrowserSessionControlPlane
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.registry import RegisteredTool
from tests.contract.support import principal as contract_principal
from tests.contract.test_hosted_profile_session_service_contract import FakeSessionRuntime
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FakeWebProvider

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000e7")
GRANT_ID = UUID("00000000-0000-0000-0000-0000000000e8")
GRANT_NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
SESSION_BROWSER_PROFILE_METADATA_KEY = "browser_profile_id"


class GrantBrowserProvider(FakeBrowserProvider):
    async def action_context(self, action: BrowserAction) -> BrowserActionContext:
        return BrowserActionContext(
            origin="https://example.org",
            role="button",
            name="Continue",
            consequence=BrowserActionConsequence.ROUTINE,
            revision=action.expected_revision,
            ref=action.ref,
        )


async def seed_browser_authority(
    composition: Composition,
    *,
    revoked: bool = False,
    profile_status: BrowserProfileStatus = BrowserProfileStatus.READY,
) -> None:
    owner = composition.principal
    profile = BrowserProfile(
        id=PROFILE_ID,
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        provider_name="hosted-isolated",
        provider_ref="opaque-provider-reference",
        allowed_origins=("https://example.org",),
        status=profile_status,
        generation=3,
        encryption_key_version="key-v1",
        created_at=GRANT_NOW,
        updated_at=GRANT_NOW,
    )
    grant = BrowserGrant(
        id=GRANT_ID,
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        profile_id=PROFILE_ID,
        profile_generation=3,
        agent_version="1.0.0",
        policy_version=composition.ruleset.policy_version,
        allowed_origins=("https://example.org",),
        action_kinds=(BrowserActionKind.CLICK,),
        element_roles=("button",),
        element_names=("Continue",),
        purpose="daily-language-practice",
        starts_at=GRANT_NOW,
        expires_at=GRANT_NOW + timedelta(days=7),
        approved_by=owner.principal_id,
        revoked_at=GRANT_NOW if revoked else None,
        created_at=GRANT_NOW,
        updated_at=GRANT_NOW,
    )
    async with composition.uow_factory() as uow:
        await uow.browser_profiles.create(profile)
        await uow.browser_grants.create(grant)


async def test_browser_capabilities_are_absent_without_bound_provider() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings) as composition:
        registry = composition.tool_pipeline._registry
        with pytest.raises(NotFoundError):
            registry.get("browser.navigate")
        with pytest.raises(NotFoundError):
            registry.get("browser.observe")
        with pytest.raises(NotFoundError):
            registry.get("browser.act")


async def test_explicit_provider_override_registers_browser_tools() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    provider = FakeBrowserProvider()

    async with build(
        settings=settings,
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as composition:
        registry = composition.tool_pipeline._registry
        navigate = cast(RegisteredTool, registry.get("browser.navigate"))
        observe = cast(RegisteredTool, registry.get("browser.observe"))
        act = cast(RegisteredTool, registry.get("browser.act"))
        assert isinstance(navigate.implementation, BrowserNavigateTool)
        assert isinstance(observe.implementation, BrowserObserveTool)
        assert isinstance(act.implementation, BrowserActTool)


async def test_configured_playwright_provider_registers_browser_tools() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "playwright",
            "BROWSER_ALLOWED_ORIGINS": "https://example.org",
        }
    )

    async with build(settings=settings) as composition:
        registry = composition.tool_pipeline._registry
        navigate = cast(RegisteredTool, registry.get("browser.navigate"))
        observe = cast(RegisteredTool, registry.get("browser.observe"))
        act = cast(RegisteredTool, registry.get("browser.act"))
        assert isinstance(navigate.implementation, BrowserNavigateTool)
        assert isinstance(observe.implementation, BrowserObserveTool)
        assert isinstance(act.implementation, BrowserActTool)
        assert isinstance(navigate.implementation._provider, PlaywrightBrowserProvider)


async def test_configured_hosted_provider_binds_the_trusted_profile_adapter() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_ALLOWED_ORIGINS": "https://example.org",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_ID": "00000000-0000-0000-0000-0000000000e7",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        }
    )

    async with build(settings=settings) as composition:
        navigate = cast(
            RegisteredTool,
            composition.tool_pipeline._registry.get("browser.navigate"),
        )

        assert isinstance(navigate.implementation, BrowserNavigateTool)
        assert isinstance(navigate.implementation._provider, HostedBrowserProvider)


async def test_configured_hosted_provider_can_select_a_profile_from_each_session() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        }
    )

    async with build(settings=settings) as composition:
        navigate = cast(
            RegisteredTool,
            composition.tool_pipeline._registry.get("browser.navigate"),
        )

        assert isinstance(navigate.implementation, BrowserNavigateTool)
        assert isinstance(
            navigate.implementation._provider,
            SessionBoundHostedBrowserProvider,
        )


def session_bound_hosted_settings() -> Settings:
    return load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        }
    )


async def context_plan_payload(composition: Composition, session_id: UUID) -> dict[str, object]:
    async with composition.uow_factory() as uow:
        event = await uow.events.latest_before(
            session_id,
            (1 << 63) - 1,
            "context.plan.created",
            composition.principal,
        )
    assert event is not None
    return cast(dict[str, object], event.payload["plan"])


async def test_session_bound_browser_tools_are_omitted_without_a_selected_profile() -> None:
    provider = FakeWebProvider()
    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)]),
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit("Check the weather.", session_id)
        run = await composition.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED
        plan = await context_plan_payload(composition, session_id)

    tool_names = cast(list[str], plan["tool_names"])
    assert "web.search" in tool_names
    assert "web.fetch" in tool_names
    assert not set(tool_names) & {"browser.navigate", "browser.observe", "browser.act"}


async def test_selected_profile_browser_plan_stays_within_the_tool_definition_cap() -> None:
    provider = FakeWebProvider()
    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)]),
        web_search_provider_override=provider,
        web_fetch_provider_override=provider,
    ) as composition:
        await seed_browser_authority(composition)
        created = await composition.services.sessions.create(
            composition.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        run_id = await composition.runs.submit("Open my selected website.", created.id)
        run = await composition.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED
        plan = await context_plan_payload(composition, created.id)

    tool_names = cast(list[str], plan["tool_names"])
    assert {"browser.navigate", "browser.observe", "browser.act"} <= set(tool_names)
    tool_specs = [
        ToolSpec.model_validate(value)
        for value in cast(list[dict[str, object]], plan["tool_specs"])
    ]
    assert ConservativeTokenEstimator().estimate_tools(tool_specs, "fake:scripted") <= 6_000


BROWSER_TASK_LIMITS = RunLimits(
    max_steps=160,
    max_model_calls=120,
    max_tool_calls=160,
    max_cost=Decimal("30"),
    synthesis_reserve_model_calls=2,
    synthesis_reserve_tool_calls=4,
    synthesis_reserve_cost=Decimal("3"),
)
INTERACTIVE_LIMITS = RunLimits(
    max_steps=32,
    max_model_calls=24,
    max_tool_calls=64,
    synthesis_reserve_model_calls=2,
    synthesis_reserve_tool_calls=4,
)


def one_text_turn() -> FakeModelScript:
    return FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])


async def test_bound_chat_runs_under_the_browser_task_limits() -> None:
    """ADR-0130 decision 1: a chat bound to a website profile gets the overlay."""

    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        created = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await composition.runs.submit("Open my selected website.", created.id)
        run = await composition.runs.wait_terminal(run_id)

    assert run.status is RunStatus.COMPLETED
    assert run.limits == BROWSER_TASK_LIMITS


async def test_the_default_agent_version_pins_the_overlay() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        created = await composition.services.sessions.create(composition.principal, "general", {})
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(created.id, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)

    assert BROWSER_TASK_LIMITS_METADATA_KEY in agent.metadata
    assert RunLimits.model_validate(agent.metadata[BROWSER_TASK_LIMITS_METADATA_KEY]) == (
        BROWSER_TASK_LIMITS
    )
    assert agent.limits == INTERACTIVE_LIMITS


async def test_an_overlay_below_the_defaults_is_refused_at_startup(tmp_path: Path) -> None:
    overlay = tmp_path / "runtime" / "limits.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("browser_task:\n  max_model_calls: 2\n", encoding="utf-8")
    settings = load_settings(
        {**base_environment(), "SANDBOX_MECHANISM": "fake", "AGENT_CONFIG_DIR": str(tmp_path)}
    )

    with pytest.raises(ConfigurationError, match=r"browser_task\.max_model_calls"):
        async with build(settings=settings):
            pass


async def test_unbound_chat_keeps_the_interactive_limits() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        created = await composition.services.sessions.create(composition.principal, "general", {})
        run_id = await composition.runs.submit("Check the weather.", created.id)
        run = await composition.runs.wait_terminal(run_id)

    assert run.limits == INTERACTIVE_LIMITS


async def test_chat_pinned_before_the_overlay_keeps_the_interactive_limits() -> None:
    """A bound session pinned to a version without the overlay keeps 32/24/64."""

    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        created = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(created.id, composition.principal)
            current = await uow.agents.get_version(session.agent_id, session.agent_version)
            earlier = current.model_copy(
                update={
                    "version": "0.9.0",
                    "metadata": {
                        key: value
                        for key, value in current.metadata.items()
                        if key != BROWSER_TASK_LIMITS_METADATA_KEY
                    },
                },
                deep=True,
            )
            await uow.agents.put(earlier)
            pinned = session.model_copy(
                update={"id": UUID(int=0xF0F0), "agent_version": "0.9.0"}, deep=True
            )
            await uow.sessions.create(pinned)
        run_id = await composition.runs.submit("Open my selected website.", pinned.id)
        run = await composition.runs.wait_terminal(run_id)

    assert run.limits == INTERACTIVE_LIMITS


REQUIRED_TOOLS_ENABLED = [
    "web.fetch",
    "system.current_time",
    "math.calculate",
    "browser.navigate",
    "browser.observe",
    "browser.act",
    "tool.call",
]


async def _plan_with_four_definitions(tmp_path: Path, *, bound: bool) -> dict[str, object]:
    overlay = tmp_path / "context" / "plan.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("classes:\n  tool_definitions:\n    max_items: 4\n", encoding="utf-8")
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
            "AGENT_CONFIG_DIR": str(tmp_path),
        }
    )
    provider = FakeWebProvider()
    async with build(
        settings=settings,
        script=one_text_turn(),
        enabled_tools=REQUIRED_TOOLS_ENABLED,
        web_fetch_provider_override=provider,
    ) as composition:
        await seed_browser_authority(composition)
        created = await composition.services.sessions.create(
            composition.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID if bound else None,
        )
        run_id = await composition.runs.submit("Open my selected website.", created.id)
        run = await composition.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED
        return await context_plan_payload(composition, created.id)


async def test_bound_chat_defines_the_browser_tools_ahead_of_every_configured_tool(
    tmp_path: Path,
) -> None:
    """ADR-0130 decision 6: the item cap never defers a bound chat's browser tools."""

    plan = await _plan_with_four_definitions(tmp_path, bound=True)

    assert set(cast(list[str], plan["tool_names"])) == {
        "tool.call",
        "browser.navigate",
        "browser.observe",
        "browser.act",
    }
    assert set(cast(list[str], plan["deferred_tool_names"])) == {
        "web.fetch",
        "system.current_time",
        "math.calculate",
    }
    assert tuple(cast(list[str], plan["skipped_tool_names"])) == ()


async def test_unbound_chat_keeps_its_configured_tools_under_the_same_cap(
    tmp_path: Path,
) -> None:
    plan = await _plan_with_four_definitions(tmp_path, bound=False)

    assert {"web.fetch", "system.current_time", "math.calculate"} <= set(
        cast(list[str], plan["tool_names"])
    )
    assert not set(cast(list[str], plan["tool_names"])) & {
        "browser.navigate",
        "browser.observe",
        "browser.act",
    }


def runtime_metadata_rows(composition: Composition) -> list[str]:
    # A non-routed development policy answers from the composition's scripted
    # provider, which records every request it receives.
    provider = cast(FakeModelProvider, composition.executor._model_provider)
    return [
        part.text
        for item in provider.requests[0].conversation
        if isinstance(item, UserMessage)
        for part in item.content
        if isinstance(part, TextPart) and part.text.startswith("Runtime metadata")
    ]


async def test_bound_chat_request_names_the_origins_navigate_accepts() -> None:
    """ADR-0130 decision 9: the model is told which origins the profile allows."""

    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        created = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        run_id = await composition.runs.submit("Open my selected website.", created.id)
        run = await composition.runs.wait_terminal(run_id)
        [row] = runtime_metadata_rows(composition)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert "; browser_origins=https://example.org" in row
    assert str(PROFILE_ID) not in row


async def test_unbound_chat_request_names_no_browser_origins() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        created = await composition.services.sessions.create(composition.principal, "general", {})
        run_id = await composition.runs.submit("Check the weather.", created.id)
        await composition.runs.wait_terminal(run_id)
        [row] = runtime_metadata_rows(composition)

    assert "browser_origins" not in row


def test_the_runtime_row_names_at_most_sixteen_origins() -> None:
    origins = tuple(f"https://site{index}.example.org" for index in range(20))

    assert _browser_origins_field(()) == ""
    assert _browser_origins_field(origins[:2]) == (
        "; browser_origins=https://site0.example.org,https://site1.example.org"
    )
    rendered = _browser_origins_field(origins)
    assert rendered.endswith(",https://site15.example.org,+4 more")
    assert "site16" not in rendered


async def test_session_creation_binds_only_a_ready_principal_owned_browser_profile() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings) as composition:
        await seed_browser_authority(composition)

        created = await composition.services.sessions.create(
            composition.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        async with composition.uow_factory() as uow:
            stored = await uow.sessions.get(created.id, composition.principal)

    assert created.metadata == {SESSION_BROWSER_PROFILE_METADATA_KEY: str(PROFILE_ID)}
    assert stored.metadata == {SESSION_BROWSER_PROFILE_METADATA_KEY: str(PROFILE_ID)}


async def test_session_metadata_cannot_impersonate_the_trusted_browser_profile_binding() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings) as composition:
        await seed_browser_authority(composition)

        with pytest.raises(ValueError, match="reserved"):
            await composition.services.sessions.create(
                composition.principal,
                "general",
                {SESSION_BROWSER_PROFILE_METADATA_KEY: str(PROFILE_ID)},
            )


async def test_session_creation_refuses_a_profile_that_still_requires_login() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings) as composition:
        await seed_browser_authority(
            composition,
            profile_status=BrowserProfileStatus.AUTHENTICATION_REQUIRED,
        )

        with pytest.raises(InvalidStateTransition, match="browser profile is not ready"):
            await composition.services.sessions.create(
                composition.principal,
                "general",
                {},
                browser_profile_id=PROFILE_ID,
            )


async def test_browser_navigation_persists_policy_checked_untrusted_result() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    provider = FakeBrowserProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.navigate",
                        arguments={"url": "https://example.org/account"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The account page is open.", stop_reason=StopReason.END_TURN),
        ]
    )

    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe"],
        browser_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Open my account page.")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert run.final_message == "The account page is open."
    assert provider.navigations == ["https://example.org/account"]
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.SUCCEEDED
    assert invocations[0].result_item is not None
    assert invocations[0].result_item.trust is TrustLevel.EXTERNAL_UNTRUSTED


async def test_browser_action_waits_for_approval_then_records_effect_watermark() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    provider = FakeBrowserProvider()
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments={
                            "kind": "click",
                            "expected_revision": "revision-1",
                            "ref": "element-1",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The approved action completed.", stop_reason=StopReason.END_TURN),
        ]
    )

    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Click the continue control.")
        waiting = await composition.runs.get(run_id)
        approvals = await composition.approvals.list_pending(run_id=run_id)
        assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert len(approvals) == 1
        assert provider.actions == []

        await composition.approvals.resolve(
            approvals[0].id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert completed.status is RunStatus.COMPLETED
    assert len(provider.actions) == 1
    assert invocations[0].status is ToolInvocationStatus.SUCCEEDED
    assert invocations[0].effect_sent_at is not None


async def test_browser_action_ambiguous_dispatch_is_persisted_as_uncertain() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    provider = FakeBrowserProvider(
        action_failure=BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments={
                            "kind": "click",
                            "expected_revision": "revision-1",
                            "ref": "element-1",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="The action outcome is uncertain.",
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )

    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        enabled_tools=["browser.act"],
        browser_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Click the continue control.")
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert invocations[0].status is ToolInvocationStatus.UNCERTAIN
    assert invocations[0].effect_sent_at is not None
    assert invocations[0].outcome is not None
    assert invocations[0].outcome.reason_code == "tool.browser.outcome_unknown"


def hosted_grant_settings() -> Settings:
    return load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "BROWSER_PROVIDER": "hosted",
            "BROWSER_ALLOWED_ORIGINS": "https://example.org",
            "BROWSER_PROFILE_SERVICE_URL": "https://browser.internal.example",
            "BROWSER_PROFILE_ID": str(PROFILE_ID),
            "BROWSER_GRANT_ID": str(GRANT_ID),
            "BROWSER_RUN_PURPOSE": "daily-language-practice",
            "BROWSER_PROFILE_CONTROL_PLANE_API_KEY": "opaque-control-plane-token",
        }
    )


def browser_action_script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments={
                            "kind": "click",
                            "expected_revision": "revision-1",
                            "ref": "element-1",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The standing action completed.", stop_reason=StopReason.END_TURN),
        ]
    )


async def test_exact_standing_browser_grant_authorizes_without_interactive_approval() -> None:
    provider = GrantBrowserProvider()
    async with build(
        settings=hosted_grant_settings(),
        script=browser_action_script(),
        browser_provider_override=provider,
        fixed_clock_at=GRANT_NOW,
        enabled_tools=["browser.act"],
    ) as composition:
        await seed_browser_authority(composition)

        run_id = await composition.runs.submit("Continue my language practice.")
        run = await composition.runs.get(run_id)
        pending = await composition.approvals.list_pending(run_id=run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, composition.principal)

    assert run.status is RunStatus.COMPLETED
    assert pending == []
    assert len(provider.actions) == 1
    authorized = next(event for event in events if event.event_type == "tool.call.authorized")
    assert authorized.payload["authorization_kind"] == "standing_browser_grant"
    assert authorized.payload["authorization_ref"] == str(GRANT_ID)


async def test_a_standing_grant_dispatch_carries_its_routine_constraint() -> None:
    """ADR-0129 D17: the executor hands the grant's constraint to the tool,
    which carries it to the provider's runtime."""

    provider = GrantBrowserProvider()
    async with build(
        settings=hosted_grant_settings(),
        script=browser_action_script(),
        browser_provider_override=provider,
        fixed_clock_at=GRANT_NOW,
        enabled_tools=["browser.act"],
    ) as composition:
        await seed_browser_authority(composition)
        run_id = await composition.runs.submit("Continue my language practice.")
        run = await composition.runs.get(run_id)

    assert run.status is RunStatus.COMPLETED
    [constraint] = provider.constraints
    assert constraint is not None
    assert (constraint.grant_kind, constraint.consequence_ceiling, constraint.origins) == (
        "standing",
        "routine",
        ("https://example.org",),
    )
    assert constraint.not_after == GRANT_NOW + timedelta(days=7)


async def test_revoked_standing_browser_grant_falls_back_to_interactive_approval() -> None:
    provider = GrantBrowserProvider()
    async with build(
        settings=hosted_grant_settings(),
        script=browser_action_script(),
        browser_provider_override=provider,
        fixed_clock_at=GRANT_NOW,
        enabled_tools=["browser.act"],
    ) as composition:
        await seed_browser_authority(composition, revoked=True)

        run_id = await composition.runs.submit("Continue my language practice.")
        run = await composition.runs.get(run_id)
        pending = await composition.approvals.list_pending(run_id=run_id)

    assert run.status is RunStatus.WAITING_FOR_APPROVAL
    assert len(pending) == 1
    assert provider.actions == []


@dataclass
class RevisionCheckingRuntime(FakeSessionRuntime):
    """A hosted runtime that, like the Playwright one, acts only on the page it rendered."""

    revision: str | None = None

    async def navigate(self, url: str) -> BrowserObservation:
        observation = await super().navigate(url)
        self.revision = observation.revision
        return observation

    async def act(self, action: BrowserAction) -> BrowserObservation:
        if action.expected_revision != self.revision:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        return await super().act(action)


def navigate_then_act_script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.navigate",
                        arguments={"url": "https://example.org/lesson"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments={
                            "kind": "click",
                            "expected_revision": "revision-1",
                            "ref": "revision-1:0",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="The lesson continued.", stop_reason=StopReason.END_TURN),
        ]
    )


@dataclass
class HostedBrowserHarness:
    """A session-bound provider over the real isolated session service."""

    clock: FixedClock
    owner: Principal
    sessions: HostedProfileSessionService
    runtimes: list[RevisionCheckingRuntime]
    profile: BrowserProfile
    provider: SessionBoundHostedBrowserProvider
    compositions: list[Composition] = field(default_factory=list)

    async def run_state(self, run_id: UUID) -> BrowserRunState:
        composition = self.compositions[-1]
        return await browser_run_state(composition.uow_factory, self.owner, run_id)


async def hosted_browser_harness(tmp_path: Path) -> HostedBrowserHarness:
    clock = FixedClock(GRANT_NOW)
    owner = contract_principal().model_copy(update={"scopes": set(PLATFORM_SCOPES)})
    store = FilesystemEncryptedProfileStore(
        tmp_path / "profiles",
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-session-key").digest()},
            current_version="key-v1",
        ),
    )
    runtimes: list[RevisionCheckingRuntime] = []

    def runtime_factory(tenant_id: str) -> RevisionCheckingRuntime:
        assert tenant_id == owner.tenant_id
        runtime = RevisionCheckingRuntime()
        runtimes.append(runtime)
        return runtime

    sessions = HostedProfileSessionService(
        store,
        runtime_factory=runtime_factory,
        now=clock.now,
        process_secret=b"synthetic-process-secret-with-32-bytes",
        ceremony_base_url="https://browser-login.example.test",
    )
    lifecycle = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: "opaque-session-reference-0000000000000001",
        invalidate_profile=sessions.invalidate_profile,
    )
    provisioned = await lifecycle.provision(PROFILE_ID, owner, ("https://example.org",))
    profile = BrowserProfile(
        id=PROFILE_ID,
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        provider_name=provisioned.provider_name,
        provider_ref=provisioned.provider_ref,
        allowed_origins=("https://example.org",),
        status=BrowserProfileStatus.READY,
        generation=1,
        encryption_key_version=provisioned.encryption_key_version,
        created_at=GRANT_NOW,
        updated_at=GRANT_NOW,
    )

    async def load(requested_owner: Principal, profile_id: UUID) -> BrowserProfile:
        assert requested_owner == owner
        assert profile_id == PROFILE_ID
        return profile

    async def select(context: ToolExecutionContext) -> UUID:
        del context
        return PROFILE_ID

    harness_state: list[HostedBrowserHarness] = []

    async def read_run_state(run_id: UUID) -> BrowserRunState:
        return await harness_state[0].run_state(run_id)

    harness = HostedBrowserHarness(
        clock=clock,
        owner=owner,
        sessions=sessions,
        runtimes=runtimes,
        profile=profile,
        provider=SessionBoundHostedBrowserProvider(
            principal=owner,
            profiles=load,
            profile_selector=select,
            # The in-process service's act gains the dispatch constraint in
            # Track R (ADR-0129 R2); these runs never carry one.
            sessions=cast(BrowserSessionControlPlane, sessions),
            now=clock.now,
            run_state=read_run_state,
        ),
    )
    harness_state.append(harness)
    return harness


@asynccontextmanager
async def hosted_browser_session(
    harness: HostedBrowserHarness,
    script: FakeModelScript,
) -> AsyncIterator[tuple[Composition, UUID]]:
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=script,
        clock=harness.clock,
        principal=harness.owner,
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=harness.provider,
    ) as composition:
        harness.compositions.append(composition)
        async with composition.uow_factory() as uow:
            await uow.browser_profiles.create(harness.profile)
        created = await composition.services.sessions.create(
            harness.owner,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        yield composition, created.id


async def act_invocation(composition: Composition, run_id: UUID) -> ToolInvocation:
    async with composition.uow_factory() as uow:
        invocations = await uow.invocations.list_for_run(run_id, composition.principal)
    return next(item for item in invocations if item.tool_name == "browser.act")


async def test_hosted_lease_spans_the_run_attempt_and_closes_when_the_run_ends(
    tmp_path: Path,
) -> None:
    harness = await hosted_browser_harness(tmp_path)
    runtimes = harness.runtimes
    async with hosted_browser_session(harness, navigate_then_act_script()) as (
        composition,
        session_id,
    ):
        run_id = await composition.runs.submit("Continue my lesson.", session_id)
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        parked_lease_open = not runtimes[0].closed
        # The owner answers after the navigation call's own deadline has passed.
        harness.clock.advance(timedelta(minutes=2))
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await composition.runs.wait_terminal(run_id)
        act = await act_invocation(composition, run_id)

        assert run.status is RunStatus.COMPLETED
        assert act.status is ToolInvocationStatus.SUCCEEDED, act.outcome
        # One browser served the whole attempt, stayed open while it waited for
        # approval, and closed when the run ended rather than at build teardown.
        assert parked_lease_open
        assert len(runtimes) == 1
        assert len(runtimes[0].actions) == 1
        assert runtimes[0].closed
        await harness.sessions.acquire(
            PROFILE_ID,
            harness.owner,
            harness.profile.provider_ref or "",
            run_id=UUID("00000000-0000-0000-0000-0000000000e9"),
            attempt_number=1,
            deadline_at=harness.clock.now() + timedelta(minutes=1),
        )

    # The next run starts from the state the finished run sealed.
    assert runtimes[1].initial_material == runtimes[0].sealed_material


async def test_hosted_lease_closes_when_the_run_waits_for_the_user_instead(
    tmp_path: Path,
) -> None:
    harness = await hosted_browser_harness(tmp_path)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.navigate",
                        arguments={"url": "https://example.org/lesson"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Which lesson should I continue?"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
        ]
    )
    async with hosted_browser_session(harness, script) as (composition, session_id):
        run_id = await composition.runs.submit("Continue my lesson.", session_id)
        run = await composition.runs.get(run_id)

        # Only the run's own approval keeps the page; any other parking frees it.
        assert run.status is RunStatus.WAITING_FOR_USER
        assert harness.runtimes[0].closed


async def test_hosted_lease_closes_when_a_parked_run_is_cancelled(tmp_path: Path) -> None:
    harness = await hosted_browser_harness(tmp_path)
    async with hosted_browser_session(harness, navigate_then_act_script()) as (
        composition,
        session_id,
    ):
        run_id = await composition.runs.submit("Continue my lesson.", session_id)
        parked_lease_open = not harness.runtimes[0].closed

        cancelled = await composition.runs.cancel(run_id)

        assert parked_lease_open
        assert cancelled.status is RunStatus.CANCELLED
        assert harness.runtimes[0].closed


async def test_hosted_lease_survives_a_twenty_minute_approval_wait_under_upkeep(
    tmp_path: Path,
) -> None:
    harness = await hosted_browser_harness(tmp_path)
    upkeep = browser_lease_upkeep(harness.provider)
    assert upkeep is not None
    async with hosted_browser_session(harness, navigate_then_act_script()) as (
        composition,
        session_id,
    ):
        run_id = await composition.runs.submit("Continue my lesson.", session_id)
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        for _minute in range(20):
            harness.clock.advance(timedelta(minutes=1))
            await upkeep()
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await composition.runs.wait_terminal(run_id)
        act = await act_invocation(composition, run_id)

        assert run.status is RunStatus.COMPLETED
        assert act.status is ToolInvocationStatus.SUCCEEDED, act.outcome
        assert len(harness.runtimes) == 1


class UpkeepSpy(FakeBrowserProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.stop: Callable[[], None] = lambda: None

    async def maintain_leases(self) -> None:
        self.calls += 1
        self.stop()


async def test_run_workers_keep_hosted_leases_on_a_periodic_upkeep() -> None:
    provider = UpkeepSpy()
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        fixed_clock_at=GRANT_NOW,
        enabled_tools=["browser.navigate"],
        browser_provider_override=provider,
    ) as composition:
        for factory in (composition.worker_factory, composition.async_worker_factory):
            worker = factory("worker-under-test")
            provider.stop = worker.stop
            await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert provider.calls == 2
