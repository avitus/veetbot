"""Provider-neutral browser tool behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserElement,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolFailureKind
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.browser_results import bounded_observation_payload
from tests.contract.support import tool_context


@dataclass
class FakeBrowserProvider:
    name: str = "fake-browser"
    allowed_origins: tuple[str, ...] = ("https://example.org",)
    navigations: list[str] = field(default_factory=list)
    observation_count: int = 0
    actions: list[BrowserAction] = field(default_factory=list)
    action_failure: BrowserProviderError | None = None
    execution_contexts: list[object] = field(default_factory=list)

    async def bind_execution(self, context: object) -> None:
        self.execution_contexts.append(context)

    def _observation(self, url: str) -> BrowserObservation:
        return BrowserObservation(
            url=url,
            title="Example account",
            revision="revision-1",
            text="Account overview",
            elements=(
                BrowserElement(
                    ref="element-1",
                    role="link",
                    name="Open activity",
                ),
            ),
        )

    def allows(self, url: str) -> bool:
        return any(url == origin or url.startswith(f"{origin}/") for origin in self.allowed_origins)

    async def navigate(self, url: str) -> BrowserObservation:
        self.navigations.append(url)
        return self._observation(url)

    async def observe(self) -> BrowserObservation:
        self.observation_count += 1
        return self._observation("https://example.org/account")

    async def act(self, action: BrowserAction) -> BrowserObservation:
        if self.action_failure is not None:
            raise self.action_failure
        self.actions.append(action)
        return self._observation("https://example.org/account")

    async def close(self) -> None:
        return


def test_browser_read_tools_are_bounded_external_network_reads() -> None:
    provider = FakeBrowserProvider()

    for tool in (BrowserNavigateTool(provider), BrowserObserveTool(provider)):
        assert tool.spec.side_effect is SideEffectClass.NETWORK_READ
        assert tool.spec.risk is RiskLevel.LOW
        assert tool.spec.idempotency is IdempotencyClass.READ_ONLY
        assert tool.spec.target_kind == "browser_provider"
        assert tool.spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
        assert tool.spec.maximum_output_bytes <= 512 * 1024


async def test_navigate_rejects_disallowed_url_before_provider_dispatch() -> None:
    provider = FakeBrowserProvider()
    tool = BrowserNavigateTool(provider)

    result = await tool.execute({"url": "http://localhost/account"}, tool_context())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.url_disallowed"
    assert provider.navigations == []


async def test_navigate_rejects_public_url_outside_bound_origin_policy() -> None:
    provider = FakeBrowserProvider()
    tool = BrowserNavigateTool(provider)

    result = await tool.execute({"url": "https://other.example/account"}, tool_context())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.url_disallowed"
    assert provider.navigations == []


async def test_browser_tool_binds_trusted_execution_context_before_provider_dispatch() -> None:
    provider = FakeBrowserProvider()
    context = tool_context()

    result = await BrowserNavigateTool(provider).execute(
        {"url": "https://example.org/account"},
        context,
    )

    assert result.ok
    assert provider.execution_contexts == [context]


async def test_navigate_resolves_session_binding_before_checking_its_origin_policy() -> None:
    class SessionBoundProvider(FakeBrowserProvider):
        def __init__(self) -> None:
            super().__init__(allowed_origins=())

        async def bind_execution(self, context: object) -> None:
            await super().bind_execution(context)
            self.allowed_origins = ("https://example.org",)

    provider = SessionBoundProvider()
    context = tool_context()

    result = await BrowserNavigateTool(provider).execute(
        {"url": "https://example.org/account"},
        context,
    )

    assert result.ok
    assert provider.execution_contexts == [context]
    assert provider.navigations == ["https://example.org/account"]


async def test_navigate_returns_bounded_external_untrusted_observation() -> None:
    provider = FakeBrowserProvider()

    result = await BrowserNavigateTool(provider).execute(
        {"url": "https://example.org/account"},
        tool_context(),
    )

    assert result.ok
    assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert result.structured == {
        "provider": "fake-browser",
        "url": "https://example.org/account",
        "title": "Example account",
        "revision": "revision-1",
        "text": "Account overview",
        "elements": [
            {
                "ref": "element-1",
                "role": "link",
                "name": "Open activity",
                "disabled": False,
                "checked": None,
            }
        ],
    }


async def test_navigate_bounds_multibyte_element_names_within_tool_ceiling() -> None:
    class MaximumObservationProvider(FakeBrowserProvider):
        async def navigate(self, url: str) -> BrowserObservation:
            return BrowserObservation(
                url=url,
                title="Maximum observation",
                revision="maximum-revision",
                text="🦉" * 262_144,
                elements=tuple(
                    BrowserElement(
                        ref=f"element-{index}",
                        role="button",
                        name="🦉" * 1_024,
                    )
                    for index in range(256)
                ),
            )

    tool = BrowserNavigateTool(MaximumObservationProvider())

    result = await tool.execute(
        {"url": "https://example.org/account"},
        tool_context(),
    )

    assert result.ok
    assert isinstance(result.content[0], TextPart)
    assert len(result.content[0].text.encode("utf-8")) <= tool.spec.maximum_output_bytes


async def test_navigate_bounds_page_controlled_url_and_keeps_views_consistent() -> None:
    provider = FakeBrowserProvider()
    structured, serialized = bounded_observation_payload(
        provider,
        BrowserObservation(
            url="https://example.org/" + "a" * 4_000,
            revision="revision-long-url",
            text="rendered text",
        ),
        64,
    )

    assert structured["text"] == ""
    assert json.loads(serialized) == structured


@pytest.mark.parametrize("element_count", (256, 257))
def test_observation_payload_enforces_element_schema_ceiling(element_count: int) -> None:
    provider = FakeBrowserProvider()
    structured, serialized = bounded_observation_payload(
        provider,
        BrowserObservation.model_construct(
            url="https://example.org/account",
            revision="revision-elements",
            elements=tuple(
                BrowserElement(ref=f"element-{index}", role="button", name="Continue")
                for index in range(element_count)
            ),
        ),
        1_000_000,
    )

    assert len(structured["elements"]) == 256
    assert json.loads(serialized) == structured


async def test_observe_reads_current_page_without_model_selected_profile() -> None:
    provider = FakeBrowserProvider()

    result = await BrowserObserveTool(provider).execute({}, tool_context())

    assert result.ok
    assert provider.observation_count == 1
    assert isinstance(result.structured, dict)
    assert result.structured["url"] == "https://example.org/account"


def test_browser_act_is_a_serial_non_idempotent_external_write() -> None:
    spec = BrowserActTool(FakeBrowserProvider()).spec

    assert spec.side_effect is SideEffectClass.EXTERNAL_WRITE
    assert spec.risk is RiskLevel.HIGH
    assert spec.idempotency is IdempotencyClass.NON_IDEMPOTENT
    assert spec.allow_parallel is False
    assert spec.target_kind == "browser_provider"
    assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED


async def test_browser_act_marks_effect_before_provider_dispatch() -> None:
    order: list[str] = []

    class OrderedProvider(FakeBrowserProvider):
        async def act(self, action: BrowserAction) -> BrowserObservation:
            order.append("dispatch")
            return await super().act(action)

    async def mark_effect() -> None:
        order.append("watermark")

    provider = OrderedProvider()
    context = replace(tool_context(), mark_effect_sent=mark_effect)

    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "expected_revision": "revision-1",
            "ref": "element-1",
        },
        context,
    )

    assert result.ok
    assert order == ["watermark", "dispatch"]
    assert provider.actions[0].kind.value == "click"


async def test_browser_act_rejects_mismatched_action_fields_before_watermark() -> None:
    marked = False

    async def mark_effect() -> None:
        nonlocal marked
        marked = True

    provider = FakeBrowserProvider()
    result = await BrowserActTool(provider).execute(
        {
            "kind": "type",
            "expected_revision": "revision-1",
            "ref": "element-1",
        },
        replace(tool_context(), mark_effect_sent=mark_effect),
    )

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.arguments_invalid"
    assert not marked
    assert provider.actions == []


async def test_browser_act_normalizes_stale_and_ambiguous_failures() -> None:
    stale = FakeBrowserProvider(
        action_failure=BrowserProviderError("tool.browser.page_changed", retryable=False)
    )
    stale_result = await BrowserActTool(stale).execute(
        {"kind": "click", "expected_revision": "old", "ref": "old:1"},
        tool_context(),
    )
    uncertain = FakeBrowserProvider(
        action_failure=BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
    )
    uncertain_result = await BrowserActTool(uncertain).execute(
        {"kind": "click", "expected_revision": "revision-1", "ref": "element-1"},
        tool_context(),
    )

    assert stale_result.failure is not None
    assert stale_result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert stale_result.failure.reason_code == "tool.browser.page_changed"
    assert uncertain_result.failure is not None
    assert uncertain_result.failure.kind is ToolFailureKind.OUTCOME_UNKNOWN
    assert uncertain_result.failure.reason_code == "tool.browser.outcome_unknown"
