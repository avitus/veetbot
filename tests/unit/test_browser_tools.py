"""Provider-neutral browser tool behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserObservationFacts,
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
from agent_core.ports.browser import BrowserProvider
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import BrowserObserveTool, LegacyBrowserObserveTool
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
    constraints: list[BrowserDispatchConstraint | None] = field(default_factory=list)

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

    async def act(
        self,
        action: BrowserAction,
        *,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        if self.action_failure is not None:
            raise self.action_failure
        self.actions.append(action)
        self.constraints.append(constraint)
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
        async def act(
            self,
            action: BrowserAction,
            *,
            constraint: BrowserDispatchConstraint | None = None,
        ) -> BrowserObservation:
            order.append("dispatch")
            return await super().act(action, constraint=constraint)

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


async def test_model_visible_observation_excludes_facts() -> None:
    """ADR-0129 D14: facts never reach a model-visible result."""

    @dataclass
    class FactCachingProvider(FakeBrowserProvider):
        facts: BrowserObservationFacts | None = None

        async def navigate(self, url: str) -> BrowserObservation:
            observation = await super().navigate(url)
            self.facts = BrowserObservationFacts(
                revision=observation.revision,
                elements={
                    "element-1": BrowserElementFacts(
                        field_kind=BrowserFieldKind.NONE,
                        labels={BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"},
                        context_name="Try Super free",
                    )
                },
            )
            return observation

    plain = await BrowserNavigateTool(FakeBrowserProvider()).execute(
        {"url": "https://example.org/account"}, tool_context()
    )
    with_facts = await BrowserNavigateTool(FactCachingProvider()).execute(
        {"url": "https://example.org/account"}, tool_context()
    )

    assert with_facts.model_dump_json() == plain.model_dump_json()
    assert "Pay $12.99" not in with_facts.model_dump_json()
    schema = BrowserNavigateTool.spec.output_schema
    assert schema is not None
    assert "facts" not in json.dumps(schema)
    assert set(schema["properties"]) == {
        "provider",
        "url",
        "title",
        "revision",
        "text",
        "elements",
        "coverage",
        "next_observe",
        "readiness",
        "condition",
        "regions",
        "region_coverage",
        "text_coverage",
        "extraction",
        "extraction_omitted",
        "focus",
        "next_text",
        "interruption",
    }


TASK_CONSTRAINT = BrowserDispatchConstraint(
    grant_kind="task",
    origins=("https://example.org",),
    path_prefix="/lesson",
    not_after=datetime(2026, 9, 25, 18, 30, tzinfo=UTC),
    consequence_ceiling="unknown",
    max_text_characters=256,
)
CLICK_ARGUMENTS = {"kind": "click", "expected_revision": "revision-1", "ref": "element-1"}


async def test_act_passes_the_dispatch_constraint_to_the_provider() -> None:
    """ADR-0129: a grant-authorized act carries its constraint to the runtime."""

    provider = FakeBrowserProvider()
    context = replace(tool_context(), dispatch_constraint=TASK_CONSTRAINT)

    granted = await BrowserActTool(provider).execute(CLICK_ARGUMENTS, context)
    approved = await BrowserActTool(provider).execute(CLICK_ARGUMENTS, tool_context())

    assert granted.ok and approved.ok
    assert provider.constraints == [TASK_CONSTRAINT, None]


async def test_invalid_dispatch_constraint_fails_closed() -> None:
    """A constraint that fails its validator is refused before the watermark."""

    marked: list[str] = []

    async def mark_effect() -> None:
        marked.append("watermark")

    provider = FakeBrowserProvider()
    forged = BrowserDispatchConstraint.model_construct(
        grant_kind="standing",
        origins=("https://example.org",),
        path_prefix=None,
        not_after=TASK_CONSTRAINT.not_after,
        consequence_ceiling="unknown",
        max_text_characters=None,
    )
    context = replace(tool_context(), dispatch_constraint=forged, mark_effect_sent=mark_effect)

    result = await BrowserActTool(provider).execute(CLICK_ARGUMENTS, context)

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.grant_not_applicable"
    assert (provider.actions, marked) == ([], [])


class ConstraintlessProvider(FakeBrowserProvider):
    """A provider whose runtime cannot recheck a grant's constraint.

    It falls outside the port, which takes the constraint; the tool still
    refuses a constrained act on it rather than dropping the constraint.
    """

    async def act(self, action: BrowserAction) -> BrowserObservation:  # type: ignore[override]
        self.actions.append(action)
        return self._observation("https://example.org/account")


async def test_a_provider_that_cannot_recheck_refuses_a_constrained_act() -> None:
    provider = ConstraintlessProvider()
    outside_the_port = cast(BrowserProvider, provider)
    context = replace(tool_context(), dispatch_constraint=TASK_CONSTRAINT)

    refused = await BrowserActTool(outside_the_port).execute(CLICK_ARGUMENTS, context)
    approved = await BrowserActTool(outside_the_port).execute(CLICK_ARGUMENTS, tool_context())

    assert refused.failure is not None
    assert refused.failure.reason_code == "tool.browser.grant_not_applicable"
    assert approved.ok
    assert len(provider.actions) == 1


def test_browser_descriptions_say_the_returned_page_is_settled() -> None:
    """ADR-0130 decision 6: navigate and act return the settled page, so the
    model acts on it directly instead of observing again. ADR-0157 versions
    the additive expansion contract."""

    provider = FakeBrowserProvider()

    assert BrowserNavigateTool(provider).spec.description == (
        "Open an https:// URL within runtime browser_origins; returns the settled page and fresh "
        "refs. After navigation_cancelled, observe the existing page."
    )
    assert LegacyBrowserObserveTool(provider).spec.description == (
        "Read the current page of this chat's website profile again. navigate and act "
        "already return the settled page, so observe only to refresh a page that changes "
        "on its own."
    )
    assert BrowserObserveTool(provider).spec.description == (
        "Read modes: {}; next_observe after/cursor; "
        "region_ref+expected_revision with text_offset; extract table/list/form; wait_for "
        "role/name or evidence. Evidence: text=complete line, region=kind/text, "
        "location=exact url, row=collection/index/typed fields/value. timeout_ms: 2000 "
        "default, 5000 max. failure_evidence: text/region/location. Form columns: "
        "label/role/disabled/checked/required, never values. Checks are window-scoped."
    )
    assert BrowserActTool(provider).spec.description == (
        "Act once with approval and current revision/ref; returns settled page and fresh refs. "
        "Optional postcondition uses observe wait_for. On element_not_found, refresh for "
        "overlays. Never repeat an uncertain write; observed success is not causal proof."
    )
    assert {
        tool.spec.version
        for tool in (
            BrowserNavigateTool(provider),
            BrowserObserveTool(provider),
            BrowserActTool(provider),
        )
    } == {"1.7.0"}


async def test_observe_continuation_uses_the_bound_provider_without_refreshing() -> None:
    class ExpandingProvider(FakeBrowserProvider):
        async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
            assert self.execution_contexts
            assert request.after == "element-1"
            return self._observation("https://example.org/account").model_copy(
                update={"revision": "expanded-revision"}
            )

    provider = ExpandingProvider()
    result = await BrowserObserveTool(provider).execute({"after": "element-1"}, tool_context())
    assert result.ok
    assert result.structured is not None
    assert result.structured["revision"] == "expanded-revision"
    assert provider.observation_count == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"after": "a", "cursor": "c" * 32},
        {"after": ""},
        {"cursor": "short"},
        {"after": None},
        {"after": "a", "cursor": None},
        {"after": 1},
        {"offset": 256},
        {"selector": "button"},
    ],
)
async def test_observe_rejects_malformed_expansion_before_binding(
    arguments: dict[str, Any],
) -> None:
    provider = FakeBrowserProvider()
    result = await BrowserObserveTool(provider).execute(arguments, tool_context())
    assert result.failure is not None and result.failure.reason_code == "tool.arguments_invalid"
    assert not provider.execution_contexts and provider.observation_count == 0


async def test_legacy_provider_cannot_silently_drop_expansion() -> None:
    provider = FakeBrowserProvider()
    result = await BrowserObserveTool(provider).execute({"after": "element-1"}, tool_context())
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.action_not_allowed"
    assert provider.observation_count == 0


def test_canonical_byte_bound_continues_after_the_retained_prefix() -> None:
    from agent_core.domain.browser import BrowserObservationCoverage

    observation = BrowserObservation(
        url="https://example.org",
        revision="current",
        elements=tuple(
            BrowserElement(ref=f"ref-{i}", role="button", name="雪" * 1024) for i in range(20)
        ),
        coverage=BrowserObservationCoverage(
            candidate_offset=0, scanned_candidates=300, next_cursor="c" * 32
        ),
    )
    payload, serialized = bounded_observation_payload(FakeBrowserProvider(), observation, 5000)
    assert len(serialized.encode()) <= 5000
    assert 0 < len(payload["elements"]) < 20
    assert payload["next_observe"] == {"after": payload["elements"][-1]["ref"]}


async def test_observe_waits_for_exact_visible_condition_without_acting() -> None:
    provider = FakeBrowserProvider()
    result = await BrowserObserveTool(provider).execute(
        {"wait_for": {"role": "link", "name": "Open activity", "timeout_ms": 0}}, tool_context()
    )
    assert result.ok, "bounded visible postconditions are not supported"
    assert result.structured is not None
    assert result.structured["condition"]["status"] == "satisfied"
    assert result.structured["condition"]["scope"] == "observation_window"
    assert provider.observation_count == 1 and not provider.actions


async def test_act_checks_postcondition_but_dispatches_only_once() -> None:
    provider = FakeBrowserProvider()
    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "expected_revision": "revision-1",
            "ref": "element-1",
            "postcondition": {"role": "button", "name": "Never appeared", "timeout_ms": 0},
        },
        tool_context(),
    )
    assert result.ok and result.structured is not None
    assert result.structured.get("condition", {}).get("status") == "not_observed"
    assert len(provider.actions) == 1


@pytest.mark.parametrize(
    "predicate",
    [
        {"role": "link", "name": "Open activity", "timeout_ms": 5001},
        {"role": "link", "name": "Open activity", "timeout_ms": True},
        {"role": "link", "name": "Open activity", "selector": "#secret"},
        {"role": "link", "name": "Open activity", "checked": "yes"},
    ],
)
async def test_invalid_conditions_refuse_before_binding_or_action(
    predicate: dict[str, Any],
) -> None:
    provider = FakeBrowserProvider()
    cases: list[tuple[BrowserObserveTool | BrowserActTool, dict[str, Any]]] = [
        (BrowserObserveTool(provider), {"wait_for": predicate}),
        (
            BrowserActTool(provider),
            {"kind": "click", "expected_revision": "r", "ref": "e", "postcondition": predicate},
        ),
    ]
    for tool, args in cases:
        result = await tool.execute(args, tool_context())
        assert not result.ok and result.failure is not None
        assert result.failure.kind == ToolFailureKind.INVALID_ARGUMENTS
    assert not provider.execution_contexts and not provider.actions


async def test_ambiguous_and_wrong_state_conditions_are_not_satisfied() -> None:
    class Provider(FakeBrowserProvider):
        duplicate = True

        async def observe(self) -> BrowserObservation:
            observed = await super().observe()
            if self.duplicate:
                observed = observed.model_copy(
                    update={
                        "elements": (
                            *observed.elements,
                            observed.elements[0].model_copy(update={"ref": "other"}),
                        )
                    }
                )
            return observed

    provider = Provider()
    for duplicate, status, state in [
        (True, "ambiguous", {}),
        (False, "not_observed", {"checked": True}),
    ]:
        provider.duplicate = duplicate
        result = await BrowserObserveTool(provider).execute(
            {"wait_for": {"role": "link", "name": "Open activity", "timeout_ms": 0, **state}},
            tool_context(),
        )
        assert result.structured is not None and result.structured["condition"]["status"] == status
    assert not provider.actions


async def test_failed_post_action_read_is_uncertain_and_never_repeats_the_write() -> None:
    class Provider(FakeBrowserProvider):
        async def observe(self) -> BrowserObservation:
            raise RuntimeError("secret-cookie-canary")

    provider = Provider()
    result = await BrowserActTool(provider).execute(
        {
            "kind": "click",
            "expected_revision": "revision-1",
            "ref": "element-1",
            "postcondition": {"role": "button", "name": "Success", "timeout_ms": 500},
        },
        tool_context(),
    )
    assert result.failure is not None and result.failure.kind == ToolFailureKind.OUTCOME_UNKNOWN
    assert not result.failure.retryable and not result.content
    assert len(provider.actions) == 1


async def test_wait_capture_timeout_discards_prior_references_and_does_not_act() -> None:
    import asyncio

    class Provider(FakeBrowserProvider):
        captures = 0
        cancelled = False

        async def observe(self) -> BrowserObservation:
            self.captures += 1
            if self.captures > 1:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return await super().observe()

    provider = Provider()
    result = await BrowserObserveTool(provider).execute(
        {"wait_for": {"role": "button", "name": "Not ready", "timeout_ms": 350}}, tool_context()
    )
    assert result.failure is not None and result.failure.reason_code == "tool.browser.page_changed"
    assert not result.content and result.structured is None
    assert provider.cancelled and not provider.actions


@pytest.mark.parametrize("extra", [{"after": "ref"}, {"cursor": "cursor" * 8}])
async def test_wait_and_expansion_cannot_be_combined(extra: dict[str, Any]) -> None:
    provider = FakeBrowserProvider()
    result = await BrowserObserveTool(provider).execute(
        {"wait_for": {"role": "link", "name": "Open activity"}, **extra}, tool_context()
    )
    assert not result.ok and not provider.execution_contexts


async def test_focus_refuses_a_provider_that_silently_ignores_region_scope() -> None:
    class OldExpander(FakeBrowserProvider):
        async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
            return self._observation("https://example.org/account")

    result = await BrowserObserveTool(OldExpander()).execute(
        {"region_ref": "current-region", "expected_revision": "current-revision"},
        tool_context(),
    )
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.output_invalid"


@pytest.mark.parametrize(
    "arguments",
    [
        {"region_ref": "region"},
        {"region_ref": "region", "expected_revision": "revision", "text_offset": -1},
        {"region_ref": "region", "expected_revision": "revision", "text_offset": 262145},
        {"region_ref": "region", "expected_revision": "revision", "after": "control"},
        {"region_ref": "region", "expected_revision": "revision", "selector": "form"},
        {"text_offset": 10},
    ],
)
async def test_focus_validation_refuses_malformed_or_mixed_modes(arguments: dict[str, Any]) -> None:
    provider = FakeBrowserProvider()
    result = await BrowserObserveTool(provider).execute(arguments, tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert not provider.execution_contexts
