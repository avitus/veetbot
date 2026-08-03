"""Run deterministic cases through the ordinary Milestone 1 services."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.runs import Run, RunLimits
from agent_core.evals.cases import EvalCase, load_cases
from agent_core.evals.fixtures import resolve_model_fixture


@dataclass(frozen=True, slots=True)
class EvalResult:
    case: EvalCase
    run: Run
    events: list[EventEnvelope]


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
            raise AssertionError(
                f"event {wanted!r} does not occur in order after index {position}: {actual}"
            ) from exc


def assert_expected(result: EvalResult) -> None:
    case = result.case
    run = result.run
    expected = case.expected
    assert run.status == expected.terminal_status
    if expected.final_text is not None:
        assert run.final_message == expected.final_text
    if expected.failure_reason is not None:
        assert run.failure is not None
        assert run.failure.reason == expected.failure_reason
    if expected.model_calls is not None:
        assert run.model_call_count == expected.model_calls
    if expected.maximum_steps is not None:
        assert run.step_count <= expected.maximum_steps
    event_types = [event.event_type for event in result.events]
    _assert_subsequence(event_types, expected.event_order)
    if expected.tool_started_count is not None:
        assert event_types.count("tool.call.started") == expected.tool_started_count
    observed_reason_codes = {
        reason
        for event in result.events
        if isinstance((reason := event.payload.get("reason_code")), str)
    }
    assert set(expected.reason_codes).issubset(observed_reason_codes)


async def run_case(case: EvalCase, fixture_root: Path) -> EvalResult:
    script = resolve_model_fixture(fixture_root, case.model_fixture)
    limits = RunLimits(**case.fixtures.run_limits.model_dump())
    principal = Principal(
        tenant_id="tenant_eval",
        principal_id=case.principal,
        roles={"user"},
        scopes=set(),
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
        run_id = await composition.runs.submit(case.input.text)
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
    result = EvalResult(case=case, run=run, events=events)
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
