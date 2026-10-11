"""Startup recovery cannot become a second browser operation or escape its bounds."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.browser_startup import BrowserStartupRecovery
from agent_core.domain.browser import BrowserLease, BrowserObservation, BrowserProviderError
from agent_core.domain.browser_diagnostics import collect_browser_diagnostics
from agent_core.domain.errors import RunCancelledError
from agent_core.domain.runs import CancelReason
from agent_core.runtime.cancellation import RunCancellationToken
from tests.contract.support import NOW
from tests.unit.test_hosted_browser_provider import CLICK, call_at, hosted_service, ready_provider


class RecoveryClock(FixedClock):
    def __init__(self) -> None:
        super().__init__(NOW)
        self.sleeps: list[float] = []
        self.after_sleep: Callable[[], None] = lambda: None

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)
        self.after_sleep()


@pytest.mark.parametrize("failures", [0, 1, 2, 3, 10])
async def test_acquisition_recovers_or_stops_at_three_with_bounded_diagnostics(
    failures: int,
) -> None:
    clock = RecoveryClock()
    recovery = BrowserStartupRecovery(clock)
    attempts = 0
    failure = BrowserProviderError("tool.browser.provider_unavailable", retryable=True)
    lease = BrowserLease(
        lease_ref="synthetic-lease-reference-00000001", expires_at=NOW + timedelta(minutes=15)
    )

    async def acquire() -> BrowserLease:
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise failure
        return lease

    with collect_browser_diagnostics(clock=monotonic) as diagnostics:
        if failures < 3:
            assert await recovery(acquire, call_at(NOW)) is lease
        else:
            with pytest.raises(BrowserProviderError) as raised:
                await recovery(acquire, call_at(NOW))
            assert raised.value is failure
    assert attempts == min(failures + 1, 3)
    assert len(clock.sleeps) == attempts - 1
    for delay, bounds in zip(clock.sleeps, [(0.25, 0.5), (0.5, 1)], strict=False):
        assert bounds[0] <= delay <= bounds[1]
    phases = diagnostics.snapshot().phases
    assert len(phases) == attempts
    assert all(
        phase.phase == "acquisition" and phase.placement == "orchestrator" for phase in phases
    )
    assert [phase.outcome for phase in phases] == ["failed"] * min(failures, 3) + (
        ["completed"] if failures < 3 else []
    )


@pytest.mark.parametrize(
    "reason,retryable",
    [
        ("tool.browser.authentication_required", True),
        ("tool.browser.needs_user", True),
        ("tool.browser.profile_unavailable", True),
        ("tool.browser.output_invalid", True),
        ("tool.browser.outcome_unknown", True),
        ("tool.browser.page_changed", True),
        ("tool.browser.provider_unavailable", False),
    ],
)
async def test_only_transient_provider_unavailability_can_retry(
    reason: str, retryable: bool
) -> None:
    clock = RecoveryClock()
    attempts = 0
    failure = BrowserProviderError(reason, retryable=retryable)

    async def acquire() -> BrowserLease:
        nonlocal attempts
        attempts += 1
        raise failure

    with pytest.raises(BrowserProviderError) as raised:
        await BrowserStartupRecovery(clock)(acquire, call_at(NOW))
    assert raised.value is failure
    assert attempts == 1 and not clock.sleeps


@pytest.mark.parametrize("bound", ["tool", "run", "oversleep", "expired"])
async def test_deadline_prevents_another_acquisition(bound: str) -> None:
    clock = RecoveryClock()
    context = call_at(NOW)
    if bound == "tool":
        context = replace(context, deadline_at=NOW + timedelta(seconds=0.2))
    elif bound == "run":
        context = replace(context, run_deadline_at=NOW + timedelta(seconds=0.2))
    elif bound == "expired":
        context = replace(context, deadline_at=NOW)
    else:
        clock.after_sleep = lambda: clock.advance(timedelta(minutes=1))
    attempts = 0

    async def acquire() -> BrowserLease:
        nonlocal attempts
        attempts += 1
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)

    expected = TimeoutError if bound in {"oversleep", "expired"} else BrowserProviderError
    with pytest.raises(expected):
        await BrowserStartupRecovery(clock)(acquire, context)
    assert attempts == (0 if bound == "expired" else 1)
    assert len(clock.sleeps) == (1 if bound == "oversleep" else 0)


@pytest.mark.parametrize("when", ["before", "backoff"])
async def test_cancellation_prevents_another_acquisition(when: str) -> None:
    clock = RecoveryClock()
    token = RunCancellationToken(clock, None)
    context = replace(call_at(NOW), cancellation=token)
    attempts = 0

    def cancel() -> None:
        token.cancel(CancelReason.REQUESTED)

    if when == "before":
        cancel()
    else:
        clock.after_sleep = cancel

    async def acquire() -> BrowserLease:
        nonlocal attempts
        attempts += 1
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)

    with pytest.raises(RunCancelledError):
        await BrowserStartupRecovery(clock)(acquire, context)
    assert attempts == (0 if when == "before" else 1)


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_inflight_acquisition_is_cancelled_without_retry(stop: str) -> None:
    clock = RecoveryClock()
    started, closed = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def acquire() -> BrowserLease:
        nonlocal attempts
        attempts += 1
        started.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()
        raise AssertionError("unreachable")

    context = (
        replace(call_at(NOW), deadline_at=NOW + timedelta(seconds=0.05))
        if stop == "timeout"
        else call_at(NOW)
    )

    async def run() -> None:
        with pytest.raises(TimeoutError if stop == "timeout" else asyncio.CancelledError):
            await BrowserStartupRecovery(clock)(acquire, context)

    task = asyncio.create_task(run())
    await started.wait()
    if stop == "cancel":
        task.cancel()
    # The harness timeout must not count as the application's expected timeout.
    await asyncio.wait_for(task, 1)
    assert closed.is_set() and attempts == 1 and not clock.sleeps


@pytest.mark.parametrize("lost", ["acquire", "navigate", "act"])
async def test_lost_response_reattaches_one_lease_but_never_replays_operations(
    tmp_path: Path, lost: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = await hosted_service(tmp_path)
    provider = ready_provider(hosted.sessions, now=hosted.now)
    provider._acquire_session = BrowserStartupRecovery(RecoveryClock())
    hosted.sessions.lose.add(lost)
    requests = []
    acquire = hosted.sessions.acquire

    async def record(*args: Any, **kwargs: Any) -> BrowserLease:
        requests.append((args, kwargs))
        return await acquire(*args, **kwargs)

    monkeypatch.setattr(hosted.sessions, "acquire", record)
    await provider.bind_execution(call_at(NOW))
    runtime = hosted.runtimes[0]
    navigations = []
    navigate = runtime.navigate

    async def record_navigation(url: str) -> BrowserObservation:
        navigations.append(url)
        return await navigate(url)

    monkeypatch.setattr(runtime, "navigate", record_navigation)
    if lost == "navigate":
        with pytest.raises(BrowserProviderError):
            await provider.navigate("https://example.org/lesson")
    else:
        await provider.navigate("https://example.org/lesson")
        if lost == "act":
            with pytest.raises(BrowserProviderError) as raised:
                await provider.act(CLICK)
            assert raised.value.reason_code == "tool.browser.outcome_unknown"
    assert len(requests) == (2 if lost == "acquire" else 1)
    assert all(request == requests[0] for request in requests)
    assert len(hosted.runtimes) == 1
    assert navigations == ["https://example.org/lesson"]
    assert len(runtime.actions) == (1 if lost == "act" else 0)
    await provider.close()
