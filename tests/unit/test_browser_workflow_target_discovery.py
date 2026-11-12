"""Workflow target discovery follows only bounded current control continuations."""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import agent_core.bootstrap as bootstrap
from agent_core.config import load_config_document, load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserElement,
    BrowserObservation,
    BrowserObservationCoverage,
    BrowserObservationExpansion,
    BrowserProviderError,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, TextPart, ToolResultItem
from agent_core.domain.runs import RunStatus
from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_browser_workflows import CATALOG
from tests.unit.test_config import base_environment


class WindowProvider(FakeBrowserProvider):
    def __init__(self, *, target_window: int = 1, mode: str = "normal") -> None:
        super().__init__()
        self.mode = mode
        self.window = 0
        self.target_window = target_window
        self.expansions: list[BrowserObservationExpansion] = []

    def _observation(self, url: str) -> BrowserObservation:
        observed = (
            super()
            ._observation(url)
            .model_copy(
                update={
                    "revision": f"window-{self.window}",
                    "text": "Report ready" if self.actions else "Account overview",
                    "elements": (
                        BrowserElement(
                            ref=f"control-{self.window}",
                            role="link",
                            name="Open activity"
                            if self.window == self.target_window
                            else "Unrelated",
                        ),
                    ),
                    "coverage": BrowserObservationCoverage(
                        candidate_offset=self.window * 256,
                        scanned_candidates=256,
                        next_cursor=f"{self.window + 1:032x}",
                    ),
                }
            )
        )

        if self.mode == "empty" and self.window == 0:
            observed = observed.model_copy(update={"elements": ()})
        if self.mode == "ambiguous" and self.window == self.target_window:
            observed = observed.model_copy(
                update={
                    "elements": (
                        *observed.elements,
                        observed.elements[0].model_copy(update={"ref": "duplicate"}),
                    )
                }
            )
        if self.mode == "no_cursor":
            assert observed.coverage is not None
            observed = observed.model_copy(
                update={"coverage": observed.coverage.model_copy(update={"next_cursor": None})}
            )
        return observed

    async def expand(self, request: BrowserObservationExpansion) -> BrowserObservation:
        assert request.cursor == f"{self.window + 1:032x}"
        self.expansions.append(request)
        reasons = {
            "stale": "page_changed",
            "unavailable": "provider_unavailable",
            "auth": "needs_user",
        }
        if self.mode in reasons:
            raise BrowserProviderError(f"tool.browser.{reasons[self.mode]}", retryable=False)
        self.window += 1
        return self._observation("https://example.org/account")


def use_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap,
        "load_config_document",
        lambda settings, path: (
            CATALOG
            if path == "runtime/browser-workflows.yaml"
            else load_config_document(settings, path)
        ),
    )


@pytest.mark.parametrize("target_window", [0, 1, 3, 4, 100])
async def test_workflow_discovers_a_target_beyond_its_first_control_window(
    monkeypatch: pytest.MonkeyPatch,
    target_window: int,
) -> None:
    use_catalog(monkeypatch)
    provider = WindowProvider(target_window=target_window)
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Checked report evidence.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        found = target_window < 4
        assert len(approvals) == int(found), "target discovery did not respect its window bound"
        assert not provider.actions
        if found:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert state["status"] == ("verified" if found else "target_not_observed")
        assert state["target_search"] == {"step": 1, "windows": min(target_window + 1, 4)}
    assert len(provider.expansions) == min(target_window, 3)
    assert len(provider.actions) == int(found)
    if found:
        assert provider.actions[0].expected_revision == f"window-{target_window}"
        assert provider.actions[0].ref == f"control-{target_window}"


@pytest.mark.parametrize(
    "advertised,expected",
    [
        ({"after": "control-0"}, {"after": "control-0"}),
        (
            {"cursor": "00000000000000000000000000000001"},
            {"cursor": "00000000000000000000000000000001"},
        ),
        ({"after": "foreign"}, None),
        ({"cursor": "f" * 32}, None),
        ({"after": "control-0", "cursor": "00000000000000000000000000000001"}, None),
        ({"region_ref": "control-0", "expected_revision": "window-0"}, None),
        ({"cursor": "00000000000000000000000000000001", "text_offset": 0}, None),
        ({"selector": "button"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_only_the_advertised_current_control_continuation_is_used(
    advertised: Any,
    expected: dict[str, str] | None,
) -> None:
    from agent_core.runtime.browser_workflows import _control_continuation

    observation = WindowProvider()._observation("https://example.org/account")
    payload = {**observation.model_dump(mode="json"), "next_observe": advertised}
    result = ToolResultItem(call_id="current", content=[TextPart(text=json.dumps(payload))])
    # The after case must win over the later coverage cursor: the prefix may be truncated.
    assert _control_continuation(result, observation) == expected
    assert _control_continuation(result.model_copy(update={"is_error": True}), observation) is None
    assert _control_continuation(result, observation.model_copy(update={"coverage": None})) is None


@pytest.mark.parametrize(
    "completed,next_step,previous,expected",
    [
        (1, None, {"step": 1, "windows": 4}, False),
        (2, 3, {"step": 3, "windows": 4}, False),
        (2, 3, {"step": 1, "windows": 4}, True),
        (1, None, {"step": 1, "windows": 3}, True),
    ],
)
async def test_target_allowance_survives_fresh_reads_and_tracks_the_branch_cursor(
    completed: int,
    next_step: int | None,
    previous: dict[str, int],
    expected: bool,
) -> None:
    from agent_core.domain.browser_workflows import BrowserWorkflowCatalog
    from agent_core.runtime.browser_workflows import BrowserWorkflowRunner
    from agent_core.runtime.loop import RunContext

    runner = BrowserWorkflowRunner(BrowserWorkflowCatalog(), policy_version="test")
    runner._queue = AsyncMock()  # type: ignore[method-assign]
    runner._stop = AsyncMock()  # type: ignore[method-assign]
    state: dict[str, Any] = {
        "completed_steps": completed,
        "target_search": previous,
        "status": "running",
    }
    if next_step is not None:
        state["next_step"] = next_step
    context = cast(RunContext, SimpleNamespace())
    assert await runner._read_target_window(context, state, {}) is expected
    assert runner._queue.await_count == int(expected)
    assert runner._stop.await_count == int(not expected)
    if expected:
        assert state["target_search"]["step"] == (next_step if next_step is not None else completed)
        assert state["target_search"]["windows"] <= 4


@pytest.mark.parametrize(
    "search",
    [
        [],
        {"step": 1},
        {"step": 1, "windows": 5},
        {"step": 1, "windows": True},
        {"step": "1", "windows": 0},
    ],
)
async def test_malformed_search_state_cannot_reset_the_allowance(search: Any) -> None:
    from agent_core.domain.browser_workflows import BrowserWorkflowCatalog
    from agent_core.domain.errors import ConflictError
    from agent_core.runtime.browser_workflows import BrowserWorkflowRunner
    from agent_core.runtime.loop import RunContext

    runner = BrowserWorkflowRunner(BrowserWorkflowCatalog(), policy_version="test")
    with pytest.raises(ConflictError, match="target search is malformed"):
        await runner._read_target_window(
            cast(RunContext, SimpleNamespace()),
            {
                "completed_steps": 1,
                "target_search": search,
                "status": "running",
            },
            {},
        )


@pytest.mark.parametrize(
    "mode", ["empty", "ambiguous", "no_cursor", "stale", "unavailable", "auth"]
)
async def test_discovery_failure_never_uses_a_previous_window_or_repeats_an_action(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    use_catalog(monkeypatch)
    provider = WindowProvider(mode=mode)
    async with bootstrap.build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        script=FakeModelScript(turns=[ScriptedTurn(text="Checked available evidence.")]),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        await seed_browser_authority(app)
        session = await app.services.sessions.create(
            app.principal,
            "general",
            {},
            browser_profile_id=PROFILE_ID,
        )
        run_id = await app.runs.submit("Read my saved report.", session.id)
        approvals = await app.approvals.list_pending(run_id=run_id)
        assert len(approvals) == int(mode == "empty")
        assert not provider.actions
        if approvals:
            await app.approvals.resolve(approvals[0].id, ApprovalResolutionType.APPROVE_ONCE)
        result = await app.runs.get(run_id)
        assert result.status is (
            RunStatus.WAITING_FOR_USER if mode == "auth" else RunStatus.COMPLETED
        )
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.working_state["browser_workflow"]
        assert (
            state["status"]
            == {
                "empty": "verified",
                "ambiguous": "ambiguous_target",
                "no_cursor": "target_not_observed",
                "stale": "unverified",
                "unavailable": "unverified",
                "auth": "running",
            }[mode]
        )
        assert state["target_search"]["windows"] == (1 if mode == "no_cursor" else 2)
        events = await app.runs.events(run_id)
        assert sum(event.event_type == "run.waiting_for_user" for event in events) == int(
            mode == "auth"
        )
    assert len(provider.expansions) == int(mode != "no_cursor")
    assert len(provider.actions) == int(mode == "empty")
