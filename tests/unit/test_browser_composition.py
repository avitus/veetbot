"""Default-off composition for the browser-provider seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.browser.hosted_provider import (
    HostedBrowserProvider,
    SessionBoundHostedBrowserProvider,
)
from agent_core.adapters.browser.playwright import PlaywrightBrowserProvider
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserActionKind,
    BrowserGrant,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.errors import InvalidStateTransition, NotFoundError
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.registry import RegisteredTool
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment

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
