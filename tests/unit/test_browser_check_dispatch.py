"""A checkbox action distinguishes failed actionability from uncertain dispatch."""

from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserProviderError


@pytest.mark.parametrize("phase", ["trial", "dispatch", "success"])
async def test_check_actionability_refusal_is_distinct_from_uncertain_dispatch(phase: str) -> None:
    """Only a failed no-effect trial may be recovered as a missing target."""
    handle = Mock()
    handle.check = AsyncMock(
        side_effect={
            "trial": [PlaywrightTimeoutError("covered")],
            "dispatch": [None, PlaywrightTimeoutError("response lost")],
            "success": [None, None],
        }[phase]
    )
    guard = Mock()
    guard.evaluate = AsyncMock(return_value=False)
    guard.dispose = AsyncMock()
    runtime = PythonPlaywrightRuntime()
    action = BrowserAction(kind=BrowserActionKind.CHECK, ref="e1", expected_revision="r1")
    if phase == "success":
        await runtime._dispatch(Mock(), handle, action, [guard])
    else:
        with pytest.raises(BrowserProviderError) as refused:
            await runtime._dispatch(Mock(), handle, action, [guard])
        assert refused.value.reason_code == (
            "tool.browser.element_not_found" if phase == "trial" else "tool.browser.outcome_unknown"
        )
        assert not refused.value.retryable
    assert [call.kwargs.get("trial") for call in handle.check.await_args_list] == (
        [True] if phase == "trial" else [True, None]
    )
    assert all(call.kwargs["timeout"] <= 5_000 for call in handle.check.await_args_list)
    guard.dispose.assert_awaited_once()
