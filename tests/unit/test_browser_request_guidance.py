"""Production regressions: region targets and offsets guessed before focusing."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserObservationFocus,
    BrowserProviderError,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
    BrowserSnapshot,
)
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_observe import ArticleBrowserObserveTool
from agent_core.tools.messages import message_for
from tests.contract.support import tool_context
from tests.unit.test_browser_tools import FakeBrowserProvider


class ReferenceBrowser(FakeBrowserProvider):
    """Retain one scoped snapshot with distinct read-only and actionable references."""

    def __init__(self) -> None:
        super().__init__()
        self.current = (
            super()
            ._observation("https://example.org/account")
            .model_copy(
                update={
                    "regions": (
                        BrowserSemanticRegion(ref="region-1", kind="main", text="Account"),
                    ),
                    "region_coverage": BrowserRegionCoverage(scanned_nodes=3),
                }
            )
        )
        self.expansions: list[BrowserObservationExpansion] = []
        self.snapshot_sessions: list[UUID] = []
        self.dispatches = 0

    async def snapshot_in_session(self, session_id: UUID) -> BrowserSnapshot:
        self.snapshot_sessions.append(session_id)
        return BrowserSnapshot(observation=self.current)

    async def act(
        self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
    ) -> BrowserObservation:
        self.dispatches += 1
        if action.expected_revision != self.current.revision:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        if action.ref not in {element.ref for element in self.current.elements}:
            raise BrowserProviderError("tool.browser.element_not_found", retryable=False)
        return await super().act(action, constraint=constraint)

    async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
        self.expansions.append(request)
        if request.expected_revision != self.current.revision or request.region_ref != "region-1":
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        if request.text_offset and self.current.focus is None:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False)
        self.current = self.current.model_copy(
            update={
                "revision": self.current.revision + "-focused",
                "text": "focused evidence",
                "focus": BrowserObservationFocus(
                    region_ref="region-1",
                    text_offset=request.text_offset,
                    text_total_bytes=request.text_offset + len("focused evidence"),
                    text_cursor="c" * 32,
                ),
            }
        )
        return self.current


async def test_region_scroll_is_rejected_before_effect_with_specific_correction() -> None:
    provider = ReferenceBrowser()
    marked = False

    async def mark() -> None:
        nonlocal marked
        marked = True

    context = replace(tool_context(), mark_effect_sent=mark)
    result = await BrowserActTool(provider).execute(
        {"kind": "scroll", "expected_revision": "revision-1", "ref": "region-1", "delta_y": 1000},
        context,
    )
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.region_not_actionable"
    assert not result.failure.retryable
    assert not marked and provider.dispatches == 0
    assert provider.snapshot_sessions == [context.session_id]
    assert "elements[].ref" in message_for(result.failure.reason_code)
    assert "regions[].ref" in message_for(result.failure.reason_code)
    assert provider.current.revision == "revision-1"


@pytest.mark.parametrize("offset", [1, 1000])
async def test_unfocused_text_offset_preserves_snapshot_and_explains_first_read(
    offset: int,
) -> None:
    provider = ReferenceBrowser()
    result = await ArticleBrowserObserveTool(provider).execute(
        {"region_ref": "region-1", "expected_revision": "revision-1", "text_offset": offset},
        tool_context(),
    )
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.focus_required"
    assert not result.failure.retryable
    assert provider.expansions == []
    assert "text_offset" in message_for(result.failure.reason_code)
    assert "next_text" in message_for(result.failure.reason_code)
    assert provider.current.revision == "revision-1"
    corrected = await ArticleBrowserObserveTool(provider).execute(
        {"region_ref": "region-1", "expected_revision": "revision-1"}, tool_context()
    )
    assert corrected.ok
    assert len(provider.expansions) == 1 and provider.expansions[0].text_offset == 0


@pytest.mark.parametrize("reference,revision", [("region-1", "stale"), ("unknown", "revision-1")])
async def test_unproven_region_identity_keeps_provider_revision_and_target_checks(
    reference: str, revision: str
) -> None:
    provider = ReferenceBrowser()
    result = await BrowserActTool(provider).execute(
        {"kind": "scroll", "expected_revision": revision, "ref": reference, "delta_y": 1000},
        tool_context(),
    )
    assert result.failure is not None
    assert result.failure.reason_code == (
        "tool.browser.page_changed" if revision == "stale" else "tool.browser.element_not_found"
    )
    assert provider.dispatches == 1 and not provider.actions


@pytest.mark.parametrize("region,revision", [("region-1", "stale"), ("unknown", "revision-1")])
async def test_unproven_focus_identity_still_reaches_provider_fences(
    region: str, revision: str
) -> None:
    provider = ReferenceBrowser()
    result = await ArticleBrowserObserveTool(provider).execute(
        {"region_ref": region, "expected_revision": revision, "text_offset": 1000}, tool_context()
    )
    assert result.failure is not None and result.failure.reason_code == "tool.browser.page_changed"
    assert len(provider.expansions) == 1


async def test_valid_focused_offset_and_control_still_dispatch_once() -> None:
    provider = ReferenceBrowser()
    tool = ArticleBrowserObserveTool(provider)
    first = await tool.execute(
        {"region_ref": "region-1", "expected_revision": "revision-1", "text_offset": 0},
        tool_context(),
    )
    assert first.ok
    continued = await tool.execute(
        {
            "region_ref": "region-1",
            "expected_revision": provider.current.revision,
            "text_offset": 1,
        },
        tool_context(),
    )
    assert continued.ok and len(provider.expansions) == 2
    result = await BrowserActTool(provider).execute(
        {
            "kind": "scroll",
            "expected_revision": provider.current.revision,
            "ref": "element-1",
            "delta_y": 1000,
        },
        tool_context(),
    )
    assert result.ok and provider.dispatches == len(provider.actions) == 1


async def test_authentication_failure_is_not_reclassified_as_request_correction() -> None:
    class NeedsSignIn(ReferenceBrowser):
        async def bind_execution(self, context: object) -> None:
            raise BrowserProviderError("tool.browser.authentication_required", retryable=False)

    provider = NeedsSignIn()
    for tool, arguments in (
        (
            BrowserActTool(provider),
            {
                "kind": "scroll",
                "expected_revision": "revision-1",
                "ref": "region-1",
                "delta_y": 1000,
            },
        ),
        (
            ArticleBrowserObserveTool(provider),
            {"region_ref": "region-1", "expected_revision": "revision-1", "text_offset": 1000},
        ),
    ):
        result = await tool.execute(arguments, tool_context())
        assert result.failure is not None
        assert result.failure.reason_code == "tool.browser.authentication_required"
    assert not provider.snapshot_sessions and not provider.expansions and provider.dispatches == 0


async def test_runtime_can_correct_both_requests_without_prompts_or_recovery_reads() -> None:
    from agent_core.bootstrap import build
    from agent_core.config import load_settings
    from agent_core.domain.messages import (
        FakeModelScript,
        ScriptedToolCall,
        ScriptedTurn,
        StopReason,
    )
    from agent_core.domain.runs import RunStatus
    from tests.unit.test_config import base_environment

    class PageBrowser(ReferenceBrowser):
        def _observation(self, url: str) -> BrowserObservation:
            return self.current.model_copy(update={"url": url})

    requests: list[tuple[str, dict[str, Any]]] = [
        ("browser.navigate", {"url": "https://example.org/account"}),
        (
            "browser.act",
            {
                "kind": "scroll",
                "expected_revision": "revision-1",
                "ref": "region-1",
                "delta_y": 1000,
            },
        ),
        (
            "browser.observe",
            {"region_ref": "region-1", "expected_revision": "revision-1", "text_offset": 1000},
        ),
        ("browser.observe", {"region_ref": "region-1", "expected_revision": "revision-1"}),
        (
            "browser.act",
            {
                "kind": "scroll",
                "expected_revision": "revision-1-focused",
                "ref": "element-1",
                "delta_y": 1000,
            },
        ),
    ]
    script = FakeModelScript(
        turns=[
            *(
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(name=name, arguments=arguments, call_id=f"request-{index}")
                    ],
                    stop_reason=StopReason.TOOL_USE,
                )
                for index, (name, arguments) in enumerate(requests)
            ),
            ScriptedTurn(text="Read the current evidence."),
        ]
    )
    provider = PageBrowser()
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=script,
        sequential_ids=True,
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as composition:
        run_id = await composition.runs.submit("Read this website and scroll through its evidence.")
        result = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            calls = await uow.invocations.list_for_run(run_id, composition.principal)
    assert result.status is RunStatus.COMPLETED
    assert [call.tool_name for call in calls] == [name for name, _ in requests]
    assert calls[1].outcome is not None
    assert calls[1].outcome.reason_code == "tool.browser.region_not_actionable"
    # ADR-0040 marks non-NONE tools before entry, even when the tool refuses
    # before browser dispatch. Keep that conservative crash-recovery contract.
    assert calls[1].effect_sent_at is not None
    assert calls[2].outcome is not None
    assert calls[2].outcome.reason_code == "tool.browser.focus_required"
    assert "elements[].ref" in calls[1].outcome.message
    assert "next_text" in calls[2].outcome.message
    assert provider.observation_count == 0
    assert provider.dispatches == len(provider.actions) == 1
    assert len(provider.expansions) == 1
