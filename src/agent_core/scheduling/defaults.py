"""The pins and limits a schedule gets when the owner does not choose them.

`schedule.create` and the plain-words draft (ADR-0215 part B) both derive
them here, so a schedule made in the app is no weaker than one made in Chat:
the configured agent's identity and policy, run limits no higher than either
the agent's or the deployment's ceilings, the synthesis reserve of ADR-0078,
and the failure pause of ADR-0109.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from agent_core.domain.agents import AgentSpec
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    Cadence,
    CallPlan,
    ScheduleDefinition,
    ScheduleDefinitionLimits,
)

DEFAULT_RUN_TIMEOUT_SECONDS = 300
DEFAULT_MISFIRE_GRACE_SECONDS = 3600
DEFAULT_MAX_CONSECUTIVE_FAILURES = 2
DEFAULT_MAX_COST = Decimal("5")
# Final-synthesis headroom (ADR-0078): near its budget, a research run writes its
# answer from the evidence it already has instead of failing without one.
DEFAULT_SYNTHESIS_RESERVE_STEPS = 2
DEFAULT_SYNTHESIS_RESERVE_MODEL_CALLS = 2
DEFAULT_SYNTHESIS_RESERVE_COST = Decimal("1")


def schedule_run_limits(agent: AgentSpec, limits: ScheduleDefinitionLimits) -> RunLimits:
    """Run limits no higher than the agent's own or the deployment's ceilings."""

    current = agent.limits
    max_steps = min(current.max_steps, limits.max_steps_per_run)
    max_model_calls = min(current.max_model_calls, limits.max_model_calls_per_run)
    max_cost = min(current.max_cost or DEFAULT_MAX_COST, limits.max_cost_per_run)
    return RunLimits(
        max_steps=max_steps,
        max_model_calls=max_model_calls,
        max_tool_calls=min(current.max_tool_calls, limits.max_tool_calls_per_run),
        max_input_tokens=current.max_input_tokens,
        max_output_tokens=current.max_output_tokens,
        max_cost=max_cost,
        synthesis_reserve_steps=_reserve_within(DEFAULT_SYNTHESIS_RESERVE_STEPS, max_steps),
        synthesis_reserve_model_calls=_reserve_within(
            DEFAULT_SYNTHESIS_RESERVE_MODEL_CALLS, max_model_calls
        ),
        synthesis_reserve_cost=_reserve_within(DEFAULT_SYNTHESIS_RESERVE_COST, max_cost),
    )


def derived_definition(
    agent: AgentSpec,
    limits: ScheduleDefinitionLimits,
    *,
    title: str,
    instruction: str,
    cadence: Cadence,
    requested_scopes: frozenset[str] = frozenset(),
    browser_profile_id: UUID | None = None,
    call_plan: CallPlan | None = None,
) -> ScheduleDefinition:
    """A complete definition whose pins and limits the server chose."""

    return ScheduleDefinition(
        title=title,
        instruction=instruction,
        agent_id=agent.id,
        agent_version=agent.version,
        policy_profile=agent.policy_profile,
        requested_scopes=requested_scopes,
        browser_profile_id=browser_profile_id,
        limits=schedule_run_limits(agent, limits),
        cadence=cadence,
        run_timeout_seconds=min(DEFAULT_RUN_TIMEOUT_SECONDS, limits.max_run_timeout_seconds),
        misfire_grace_seconds=min(DEFAULT_MISFIRE_GRACE_SECONDS, limits.max_misfire_grace_seconds),
        max_consecutive_failures=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        call_plan=call_plan,
    )


def _reserve_within[T: (int, Decimal)](reserve: T, limit: T) -> T:
    """Keep a reserve only when its limit leaves room to research before it."""

    return reserve if reserve < limit else type(reserve)(0)
