"""The sole composition root: refuse, determinism, resources, freeze, wire."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from agent_core.adapters.determinism import (
    FixedClock,
    RandomIdFactory,
    SequenceIdFactory,
    SystemClock,
)
from agent_core.adapters.dispatch.inline import InlineRunDispatcher
from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryEventRepository,
    InMemoryRunRepository,
    InMemorySessionRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.application.run_service import RunService
from agent_core.application.session_service import SessionService
from agent_core.config import (
    Settings,
    load_settings,
    validate_runtime_identity,
    validate_settings,
)
from agent_core.context.builder import MinimalContextBuilder
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.messages import (
    FakeModelScript,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import CancelReason, RunLimits
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.runtime.budgets import InMemoryBudgetLedger
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.executor import RunExecutor
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry


@dataclass(frozen=True, slots=True)
class Composition:
    runs: RunService
    sessions: SessionService


class _ActiveToken:
    def __init__(self) -> None:
        self._token: RunCancellationToken | None = None

    def set(self, token: RunCancellationToken) -> None:
        self._token = token

    def cancel(self) -> None:
        if self._token is not None:
            self._token.cancel(CancelReason.REQUESTED)


def default_fake_script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17 * 23"},
                        call_id="calculator-demo",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="391", stop_reason=StopReason.END_TURN),
        ]
    )


@asynccontextmanager
async def build(
    *,
    settings: Settings | None = None,
    script: FakeModelScript | None = None,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
    limits: RunLimits | None = None,
    enabled_tools: list[str] | None = None,
    principal: Principal | None = None,
    policy_profile: str = "default",
    fixed_clock_at: datetime | None = None,
    sequential_ids: bool = False,
) -> AsyncIterator[Composition]:
    """Construct and own the complete Milestone 1 application graph."""

    # Phase 1: refusal. Loading Settings enforces production sandbox and auth rules.
    effective_settings = settings or load_settings()
    validate_settings(effective_settings)

    effective_principal = principal or Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(),
    )
    validate_runtime_identity(
        effective_settings,
        tenant_id=effective_principal.tenant_id,
        principal_id=effective_principal.principal_id,
        policy_profile=policy_profile,
    )

    # Phase 2: determinism, before any clock or identifier consumer exists.
    if clock is not None and fixed_clock_at is not None:
        raise ValueError("clock and fixed_clock_at are mutually exclusive")
    if ids is not None and sequential_ids:
        raise ValueError("ids and sequential_ids are mutually exclusive")
    effective_clock = clock or (
        FixedClock(fixed_clock_at) if fixed_clock_at is not None else SystemClock()
    )
    effective_ids = ids or (SequenceIdFactory() if sequential_ids else RandomIdFactory())

    # Phase 3: resources. Milestone 1 intentionally has no durable resource.
    agent = AgentSpec(
        id=effective_ids.new_id(),
        version="1.0.0",
        name="Milestone 1 Agent",
        instructions="Answer the user's request and use a declared tool when useful.",
        model_policy="fake-balanced",
        enabled_tools=(
            enabled_tools
            if enabled_tools is not None
            else ["math.calculate", "system.current_time"]
        ),
        policy_profile=policy_profile,
        limits=limits or RunLimits(max_steps=32, max_model_calls=16, max_tool_calls=32),
    )
    agent_repository = InMemoryAgentRepository()
    session_repository = InMemorySessionRepository()
    run_repository = InMemoryRunRepository(session_repository, effective_clock)
    event_repository = InMemoryEventRepository(session_repository, effective_clock)
    invocation_repository = InMemoryToolInvocationRepository(run_repository)

    # Phase 4: freeze versioned assets and the validated tool catalog.
    registry = StaticToolRegistry()
    registry.register(CalculatorTool())
    registry.register(CurrentTimeTool(effective_clock))
    await agent_repository.put(agent)
    model_provider = FakeModelProvider(script or default_fake_script(), effective_clock)
    resolved_model = ResolvedModel(
        provider="fake",
        model="scripted",
        credential_ref="fake",
        policy_name=agent.model_policy,
        resolved_at=effective_clock.now(),
    )

    # Phase 5: wire adapters behind ports and expose application services only.
    context_builder = MinimalContextBuilder(registry, effective_clock)
    budgets = InMemoryBudgetLedger(run_repository, effective_clock)
    pipeline = ToolPipeline(
        registry,
        invocation_repository,
        event_repository,
        effective_clock,
        effective_ids,
    )
    token_slot = _ActiveToken()
    principal_resolver = StaticPrincipalResolver(effective_principal)
    executor = RunExecutor(
        principal=effective_principal,
        principals=principal_resolver,
        runs=run_repository,
        agents=agent_repository,
        events=event_repository,
        context_builder=context_builder,
        model_provider=model_provider,
        resolved_model=resolved_model,
        budgets=budgets,
        clock=effective_clock,
        ids=effective_ids,
        dispatch_tools=pipeline.dispatch,
        on_token=token_slot.set,
    )
    dispatcher = InlineRunDispatcher(executor.execute, unit_of_work_open=lambda: False)
    session_service = SessionService(
        session_repository,
        event_repository,
        effective_clock,
        effective_ids,
        effective_principal,
        agent,
    )
    run_service = RunService(
        runs=run_repository,
        agents=agent_repository,
        events=event_repository,
        dispatcher=dispatcher,
        sessions=session_service,
        principal=effective_principal,
        clock=effective_clock,
        ids=effective_ids,
        cancel_active=token_slot.cancel,
    )
    try:
        yield Composition(runs=run_service, sessions=session_service)
    finally:
        await model_provider.close()
