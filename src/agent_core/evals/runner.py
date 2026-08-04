"""Run deterministic cases through the ordinary Milestone 1 services."""

from __future__ import annotations

import asyncio
import importlib
import json
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
from agent_core.evals.cases import EvalCase, EvalExpected, load_cases
from agent_core.evals.fixtures import (
    resolve_mcp_fixture,
    resolve_model_fixture,
    resolve_skill_fixture,
)
from agent_core.policy.scopes import PLATFORM_SCOPES


@dataclass(frozen=True, slots=True)
class EvalResult:
    case: EvalCase
    run: Run
    events: list[EventEnvelope]
    pending_approvals: int = 0
    runs: tuple[Run, ...] = ()
    arm_name: str | None = None
    arm_results: tuple[EvalResult, ...] = ()


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


def assert_expected(result: EvalResult, expected: EvalExpected | None = None) -> None:
    case = result.case
    run = result.run
    expected = expected or case.expected
    if expected is None:
        raise EvalExpectationError("comparison cases must supply an arm expectation")
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
    persisted_and_visible = json.dumps(
        {
            "final_message": run.final_message,
            "events": [event.payload for event in result.events],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    leaked = [marker for marker in expected.absent_strings if marker in persisted_and_visible]
    if leaked:
        raise EvalExpectationError(f"forbidden strings were observed: {leaked}")


async def _run_single(
    case: EvalCase,
    fixture_root: Path,
    *,
    expected: EvalExpected,
    enabled_skills: list[str],
    arm_name: str | None = None,
) -> EvalResult:
    script = resolve_model_fixture(fixture_root, case.model_fixture)
    limits = RunLimits(**case.fixtures.run_limits.model_dump())
    principal = Principal(
        tenant_id="tenant_eval",
        principal_id=case.principal,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    event_reader_principal = principal.model_copy(deep=True)
    fixture_base = fixture_root.parent
    skill_packages = tuple(
        resolve_skill_fixture(fixture_base / "skills", name) for name in case.fixtures.skills
    )
    resolved_mcp = tuple(
        resolve_mcp_fixture(
            fixture_base / "mcp",
            name,
            tenant_id=principal.tenant_id,
        )
        for name in case.fixtures.mcp_servers
    )
    bootstrap: Any = importlib.import_module("agent_core.bootstrap")
    async with bootstrap.build(
        settings=_settings(),
        script=script,
        fixed_clock_at=case.clock.start,
        sequential_ids=True,
        limits=limits,
        enabled_tools=case.fixtures.tools,
        enabled_skills=enabled_skills,
        skill_packages=skill_packages,
        mcp_servers=tuple(fixture.config for fixture in resolved_mcp),
        mcp_scripts={fixture.config.server_id: fixture.script for fixture in resolved_mcp},
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
                all_events = await uow.events.list_after(
                    session_id,
                    0,
                    event_reader_principal,
                )
        run = completed_runs[-1]
        if case.session is None:
            async with composition.uow_factory() as uow:
                all_events = await uow.events.list_after(
                    run.session_id,
                    0,
                    event_reader_principal,
                )
    result = EvalResult(
        case=case,
        run=run,
        events=all_events,
        pending_approvals=pending_count,
        runs=tuple(completed_runs),
        arm_name=arm_name,
    )
    assert_expected(result, expected)
    return result


def _policy_failure_count(result: EvalResult) -> int:
    return sum(
        event.event_type == "tool.call.denied"
        or (
            isinstance((reason := event.payload.get("reason_code")), str)
            and reason.startswith("policy.")
        )
        for event in result.events
    )


async def run_case(case: EvalCase, fixture_root: Path) -> EvalResult:
    if not case.arms:
        assert case.expected is not None
        return await _run_single(
            case,
            fixture_root,
            expected=case.expected,
            enabled_skills=case.fixtures.skills,
        )
    arm_results = tuple(
        [
            await _run_single(
                case,
                fixture_root,
                expected=arm.expected,
                enabled_skills=arm.skills,
                arm_name=arm.name,
            )
            for arm in case.arms
        ]
    )
    before, after = arm_results
    assert case.delta is not None
    before_policy = _policy_failure_count(before)
    after_policy = _policy_failure_count(after)
    if case.delta.policy_failures == "same" and before_policy != after_policy:
        raise EvalExpectationError(
            f"policy failures differ between arms: {before_policy} != {after_policy}"
        )
    if case.delta.policy_failures == "not_worse" and after_policy > before_policy:
        raise EvalExpectationError(
            f"policy failures worsened between arms: {before_policy} -> {after_policy}"
        )
    status_rank = {
        RunStatus.FAILED: 0,
        RunStatus.CANCELLED: 0,
        RunStatus.WAITING_FOR_APPROVAL: 1,
        RunStatus.WAITING_FOR_USER: 1,
        RunStatus.QUEUED: 1,
        RunStatus.RUNNING: 1,
        RunStatus.COMPLETED: 2,
    }
    before_rank = status_rank[before.run.status]
    after_rank = status_rank[after.run.status]
    if case.delta.outcome == "improves" and after_rank <= before_rank:
        raise EvalExpectationError(
            f"outcome did not improve between arms: {before.run.status} -> {after.run.status}"
        )
    if case.delta.outcome == "not_worse" and after_rank < before_rank:
        raise EvalExpectationError(
            f"outcome worsened between arms: {before.run.status} -> {after.run.status}"
        )
    return EvalResult(
        case=case,
        run=after.run,
        events=after.events,
        pending_approvals=after.pending_approvals,
        runs=after.runs,
        arm_name=after.arm_name,
        arm_results=arm_results,
    )


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
