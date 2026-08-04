"""Run deterministic cases through the ordinary Milestone 1 services."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import EvalExpectationError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.evals.cases import EvalCase, load_cases
from agent_core.evals.fixtures import resolve_model_fixture
from agent_core.policy.scopes import PLATFORM_SCOPES


@dataclass(frozen=True, slots=True)
class EvalResult:
    case: EvalCase
    run: Run
    events: list[EventEnvelope]
    pending_approvals: int = 0
    runs: tuple[Run, ...] = ()


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/eval",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )


def _assert_subsequence(actual: list[str], expected: list[str]) -> None:
    position = 0
    for wanted in expected:
        try:
            position = actual.index(wanted, position) + 1
        except ValueError as exc:
            raise EvalExpectationError(
                f"event {wanted!r} does not occur in order after index {position}: {actual}"
            ) from exc


def assert_expected(result: EvalResult) -> None:
    case = result.case
    run = result.run
    expected = case.expected
    if run.status != expected.terminal_status:
        raise EvalExpectationError(
            f"expected terminal status {expected.terminal_status}, got {run.status}"
        )
    if expected.final_text is not None and run.final_message != expected.final_text:
        raise EvalExpectationError(
            f"expected final text {expected.final_text!r}, got {run.final_message!r}"
        )
    if expected.failure_reason is not None and (
        run.failure is None or run.failure.reason != expected.failure_reason
    ):
        observed_reason = None if run.failure is None else run.failure.reason
        raise EvalExpectationError(
            f"expected failure reason {expected.failure_reason}, got {observed_reason}"
        )
    if expected.model_calls is not None and run.model_call_count != expected.model_calls:
        raise EvalExpectationError(
            f"expected {expected.model_calls} model calls, got {run.model_call_count}"
        )
    if expected.maximum_steps is not None and run.step_count > expected.maximum_steps:
        raise EvalExpectationError(
            f"expected at most {expected.maximum_steps} steps, got {run.step_count}"
        )
    if (
        expected.pending_approvals is not None
        and result.pending_approvals != expected.pending_approvals
    ):
        raise EvalExpectationError(
            f"expected {expected.pending_approvals} pending approvals, "
            f"got {result.pending_approvals}"
        )
    prefix_hashes = {
        value
        for event in result.events
        if event.event_type == "model.request.started"
        and isinstance((value := event.payload.get("prefix_sha256")), str)
    }
    if expected.distinct_prefixes is not None and len(prefix_hashes) != expected.distinct_prefixes:
        raise EvalExpectationError(
            f"expected {expected.distinct_prefixes} distinct prefixes, got {len(prefix_hashes)}"
        )
    if expected.minimum_compactions is not None:
        compactions = sum(event.event_type == "context.compacted" for event in result.events)
        if compactions < expected.minimum_compactions:
            raise EvalExpectationError(
                f"expected at least {expected.minimum_compactions} compactions, got {compactions}"
            )
    event_types = [event.event_type for event in result.events]
    _assert_subsequence(event_types, expected.event_order)
    if expected.tool_started_count is not None:
        observed_tool_count = event_types.count("tool.call.started")
        if observed_tool_count != expected.tool_started_count:
            raise EvalExpectationError(
                f"expected {expected.tool_started_count} started tools, got {observed_tool_count}"
            )
    observed_reason_codes = {
        reason
        for event in result.events
        if isinstance((reason := event.payload.get("reason_code")), str)
    }
    missing_reason_codes = set(expected.reason_codes) - observed_reason_codes
    if missing_reason_codes:
        raise EvalExpectationError(
            f"expected reason codes were not observed: {sorted(missing_reason_codes)}"
        )


async def run_case(case: EvalCase, fixture_root: Path) -> EvalResult:
    script = resolve_model_fixture(fixture_root, case.model_fixture)
    limits = RunLimits(**case.fixtures.run_limits.model_dump())
    principal = Principal(
        tenant_id="tenant_eval",
        principal_id=case.principal,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    bootstrap: Any = importlib.import_module("agent_core.bootstrap")
    async with bootstrap.build(
        settings=_settings(),
        script=script,
        fixed_clock_at=case.clock.start,
        sequential_ids=True,
        limits=limits,
        enabled_tools=case.fixtures.tools,
        principal=principal,
        policy_profile=case.policy_profile,
    ) as composition:
        session_id = None
        prompts = [case.input.text]
        if case.session is not None:
            session_id = await composition.sessions.create()
            prompts = [
                (
                    f"{case.input.text}\nTurn {turn} of {case.session.turns}.\n"
                    + (f"history-padding-{turn}:" + "x" * case.session.prompt_padding_bytes)
                )
                for turn in range(1, case.session.turns + 1)
            ]
        completed_runs: list[Run] = []
        all_events: list[EventEnvelope] = []
        pending_count = 0
        for turn, prompt in enumerate(prompts, start=1):
            if case.session is not None:
                if turn == case.session.revoke_scope_turn:
                    assert case.session.revoke_scope is not None
                    principal.scopes.discard(case.session.revoke_scope)
                memory_event = (
                    "memory.formed"
                    if turn == case.session.memory_write_turn
                    else "memory.superseded"
                    if turn == case.session.memory_correction_turn
                    else None
                )
                if memory_event is not None:
                    assert session_id is not None
                    async with composition.uow_factory() as uow:
                        await uow.events.append(
                            NewEvent(
                                session_id=session_id,
                                run_id=None,
                                event_type=memory_event,
                                actor_type="eval",
                                payload={"turn": turn, "case": case.name},
                            )
                        )
            run_id = await composition.runs.submit(prompt, session_id)
            if case.cancel_after_submission:
                await composition.runs.cancel(run_id)
            if case.approval_resolution is not None:
                resolution = ApprovalResolutionType(case.approval_resolution)
                while pending := await composition.approvals.list_pending(run_id=run_id):
                    await composition.approvals.resolve(pending[0].id, resolution)
            run = await composition.runs.get(run_id)
            if run.status not in {
                RunStatus.WAITING_FOR_APPROVAL,
                RunStatus.WAITING_FOR_USER,
            }:
                run = await composition.runs.wait_terminal(run_id)
            pending_count += len(await composition.approvals.list_pending(run_id=run_id))
            events = await composition.runs.events(run_id)
            completed_runs.append(run)
            all_events.extend(events)
            if case.session is not None and case.session.advance_seconds:
                advance = getattr(composition.clock, "advance", None)
                if not callable(advance):
                    raise EvalExpectationError("long-session evaluation requires a fixed clock")
                advance(timedelta(seconds=case.session.advance_seconds))
        if case.session is not None:
            assert session_id is not None
            async with composition.uow_factory() as uow:
                all_events = await uow.events.list_after(session_id, 0, principal)
        run = completed_runs[-1]
    result = EvalResult(
        case=case,
        run=run,
        events=all_events,
        pending_approvals=pending_count,
        runs=tuple(completed_runs),
    )
    assert_expected(result)
    return result


async def run_selected(
    repository_root: Path,
    *,
    current_milestone: int = 1,
    tag: str | None = None,
    case_name: str | None = None,
) -> list[EvalResult]:
    cases = load_cases(repository_root / "tests" / "eval_cases")
    selected = [case for case in cases if case.milestone <= current_milestone]
    if tag is not None:
        selected = [case for case in selected if tag in case.tags]
    if case_name is not None:
        selected = [case for case in selected if case.name == case_name]
    if not selected:
        raise ValueError("no evaluation cases matched the requested selection")
    fixture_root = repository_root / "evals" / "fixtures" / "models"
    return [await run_case(case, fixture_root) for case in selected]


def run_selected_sync(
    repository_root: Path,
    *,
    current_milestone: int = 1,
    tag: str | None = None,
    case_name: str | None = None,
) -> list[EvalResult]:
    return asyncio.run(
        run_selected(
            repository_root,
            current_milestone=current_milestone,
            tag=tag,
            case_name=case_name,
        )
    )
