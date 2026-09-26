"""ADR-0130: a tool result carries a platform evidence key that is never serialized."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserAction,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserObservation,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import FailureReason, RunLimits, RunStatus
from agent_core.domain.tools import ToolResult
from agent_core.tools.browser_results import observation_result
from tests.unit.test_config import base_environment


def test_evidence_key_is_carried_but_never_serialized() -> None:
    result = ToolResult(ok=True, content=[], evidence_key="a" * 32)

    assert getattr(result, "evidence_key", None) == "a" * 32
    assert "evidence_key" not in result.model_dump()
    assert "evidence_key" not in result.model_dump(mode="json")
    assert "evidence_key" not in result.model_dump_json()
    assert "a" * 32 not in result.model_dump_json()


def test_evidence_key_defaults_to_none_and_is_bounded() -> None:
    assert getattr(ToolResult(ok=True, content=[]), "evidence_key", "absent") is None
    with pytest.raises(ValidationError):
        ToolResult(ok=True, content=[], evidence_key="a" * 65)


# ---------------------------------------------------------------------------
# ADR-0130 decision 7: an identical call that observed something new restarts
# its count, at most MAXIMUM_EVIDENCE_RESETS times a run.
# ---------------------------------------------------------------------------


@dataclass
class ChangingPageProvider:
    """A lesson page that changes each time it is observed, or only after an act."""

    name: str = "changing-page"
    allowed_origins: tuple[str, ...] = ("https://example.org",)
    change_on_observe: bool = True
    page: int = 0
    observations: int = 0
    actions: list[BrowserAction] = field(default_factory=list)

    async def bind_execution(self, context: object) -> None:
        del context

    def allows(self, url: str) -> bool:
        return url == "https://example.org" or url.startswith("https://example.org/")

    def _observation(self) -> BrowserObservation:
        return BrowserObservation(
            url="https://example.org/lesson",
            title="Lesson",
            revision=f"revision-{self.observations}",
            text=f"Exercise {self.page}",
            elements=(
                BrowserElement(ref=f"revision-{self.observations}:0", role="button", name="Check"),
                BrowserElement(
                    ref=f"revision-{self.observations}:1", role="button", name=f"Answer {self.page}"
                ),
            ),
        )

    async def navigate(self, url: str) -> BrowserObservation:
        del url
        return self._observation()

    async def observe(self) -> BrowserObservation:
        self.observations += 1
        if self.change_on_observe:
            self.page += 1
        return self._observation()

    async def act(
        self,
        action: BrowserAction,
        *,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        del constraint
        self.actions.append(action)
        self.page += 1
        return self._observation()

    async def close(self) -> None:
        return


LIMITS = RunLimits(max_steps=100, max_model_calls=100, max_tool_calls=100)


def _navigate() -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="browser.navigate",
                arguments={"url": "https://example.org/lesson"},
                call_id="navigate",
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _observe(index: int) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name="browser.observe", arguments={}, call_id=f"obs-{index}")],
        stop_reason=StopReason.TOOL_USE,
    )


def _final() -> ScriptedTurn:
    return ScriptedTurn(text="Lesson done.", stop_reason=StopReason.END_TURN)


def _settings() -> Any:
    return load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})


async def _observe_run(
    provider: ChangingPageProvider, turns: list[ScriptedTurn]
) -> tuple[Any, list[Any], list[Any]]:
    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=turns),
        browser_provider_override=provider,
        limits=LIMITS,
    ) as composition:
        run_id = await composition.runs.submit("Do the lesson.")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            events = await uow.events.list_after(run.session_id, 0, composition.principal)
    return run, invocations, list(events)


def _observes(invocations: list[Any]) -> int:
    return sum(1 for invocation in invocations if invocation.tool_name == "browser.observe")


async def test_reobserving_a_page_that_changed_is_not_a_loop() -> None:
    provider = ChangingPageProvider()

    run, invocations, _events = await _observe_run(
        provider, [_navigate(), *(_observe(index) for index in range(6)), _final()]
    )

    assert run.status is RunStatus.COMPLETED, run.failure
    assert _observes(invocations) == 6


async def test_observing_an_unchanged_page_five_times_is_still_a_loop() -> None:
    provider = ChangingPageProvider(change_on_observe=False)

    run, invocations, _events = await _observe_run(
        provider, [_navigate(), *(_observe(index) for index in range(5)), _final()]
    )

    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.TOOL_LOOP_DETECTED
    assert _observes(invocations) == 4


async def test_evidence_restarts_are_capped_at_32_a_run() -> None:
    """1 first key + 32 restarts + 3 plain counts: the 37th observe fails the run."""

    provider = ChangingPageProvider()

    run, invocations, _events = await _observe_run(
        provider, [_navigate(), *(_observe(index) for index in range(40)), _final()]
    )

    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.reason is FailureReason.TOOL_LOOP_DETECTED
    assert _observes(invocations) == 36


async def test_new_evidence_on_a_batch_completed_after_approval_restarts_the_count() -> None:
    provider = ChangingPageProvider(change_on_observe=False)
    act_and_observe = ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name="browser.act",
                arguments={
                    "kind": "click",
                    "expected_revision": "revision-3",
                    "ref": "revision-3:0",
                },
                call_id="act",
            ),
            ScriptedToolCall(name="browser.observe", arguments={}, call_id="obs-batch"),
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    async with build(
        settings=_settings(),
        script=FakeModelScript(
            turns=[
                _navigate(),
                _observe(1),
                _observe(2),
                _observe(3),
                act_and_observe,
                _observe(5),
                _final(),
            ]
        ),
        browser_provider_override=provider,
        limits=LIMITS,
    ) as composition:
        run_id = await composition.runs.submit("Do the lesson.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        run = await composition.runs.wait_terminal(run_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert len(provider.actions) == 1


async def test_evidence_digest_never_reaches_events_or_invocations() -> None:
    provider = ChangingPageProvider()

    run, invocations, events = await _observe_run(
        provider, [_navigate(), *(_observe(index) for index in range(3)), _final()]
    )
    digests = {
        observation_result(provider, observation, 512 * 1024).evidence_key
        for observation in (provider._observation(),)
    }
    stored = json.dumps(
        [event.payload for event in events]
        + [[invocation.result_item, invocation.structured_result] for invocation in invocations],
        default=str,
    )

    assert run.status is RunStatus.COMPLETED, run.failure
    assert None not in digests
    assert all(digest not in stored for digest in digests if digest)
    assert "evidence_key" not in stored
    assert "tool_evidence" not in json.dumps([event.payload for event in events], default=str)
