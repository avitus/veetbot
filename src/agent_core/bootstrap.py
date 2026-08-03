"""The sole composition root: refuse, determinism, resources, freeze, wire."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from agent_core.adapters.determinism import (
    FixedClock,
    RandomIdFactory,
    SequenceIdFactory,
    SystemClock,
)
from agent_core.adapters.dispatch.inline import InlineRunDispatcher
from agent_core.adapters.dispatch.postgres import PostgresRunDispatcher
from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.database import (
    assert_schema_revision,
    create_engine,
    create_session_factory,
)
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryEventRepository,
    InMemoryRunRepository,
    InMemorySessionRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import (
    MemoryUnitOfWorkFactory,
    PostgresUnitOfWorkFactory,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
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
from agent_core.ports.dispatch import WorkerService
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry


@dataclass(frozen=True, slots=True)
class Composition:
    runs: RunService
    sessions: SessionService
    executor: RunExecutor
    uow_factory: UnitOfWorkFactory
    clock: Clock
    worker_factory: Callable[[str], WorkerService]
    maintenance_factory: Callable[[], WorkerService]


DEFAULT_AGENT_ID = UUID("8ad3e17d-449f-5ec8-a807-4e14f2b3a716")


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


async def _compose(
    *,
    storage: Literal["memory", "postgres"],
    agent: AgentSpec,
    principal: Principal,
    uow_factory: UnitOfWorkFactory,
    clock: Clock,
    ids: IdFactory,
    script: FakeModelScript | None,
) -> tuple[Composition, FakeModelProvider]:
    registry = StaticToolRegistry()
    registry.register(CalculatorTool())
    registry.register(CurrentTimeTool(clock))
    async with uow_factory() as uow:
        await uow.agents.put(agent)
    model_provider = FakeModelProvider(script or default_fake_script(), clock)
    try:
        resolved_model = ResolvedModel(
            provider="fake",
            model="scripted",
            credential_ref="fake",
            policy_name=agent.model_policy,
            resolved_at=clock.now(),
        )
        context_builder = MinimalContextBuilder(registry, clock)
        pipeline = ToolPipeline(registry, uow_factory, clock, ids)
        token_slot = _ActiveToken()
        checkpoint_seeder = DurableCheckpointSeeder(clock)
        principal_resolver = StaticPrincipalResolver(principal)
        executor = RunExecutor(
            principal=principal,
            principals=principal_resolver,
            uow_factory=uow_factory,
            context_builder=context_builder,
            model_provider=model_provider,
            resolved_model=resolved_model,
            budget_factory=lambda lease: UnitOfWorkBudgetLedger(uow_factory, clock, lease),
            clock=clock,
            ids=ids,
            dispatch_tools=pipeline.dispatch,
            seed_checkpoint=checkpoint_seeder,
            on_token=token_slot.set,
        )
        dispatcher = (
            InlineRunDispatcher(executor.execute, unit_of_work_open=uow_factory.is_open)
            if storage == "memory"
            else PostgresRunDispatcher()
        )
        session_service = SessionService(uow_factory, clock, ids, principal, agent)
        run_service = RunService(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            sessions=session_service,
            principal=principal,
            clock=clock,
            ids=ids,
            cancel_active=token_slot.cancel,
            seed_checkpoint=checkpoint_seeder,
        )
        return (
            Composition(
                runs=run_service,
                sessions=session_service,
                executor=executor,
                uow_factory=uow_factory,
                clock=clock,
                worker_factory=lambda worker_id: DurableWorker(
                    uow_factory=uow_factory,
                    executor=executor,
                    clock=clock,
                    worker_id=worker_id,
                ),
                maintenance_factory=lambda: MaintenanceWorker(
                    uow_factory=uow_factory,
                    clock=clock,
                ),
            ),
            model_provider,
        )
    except BaseException:
        await model_provider.close()
        raise


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
    storage: Literal["memory", "postgres"] = "memory",
) -> AsyncIterator[Composition]:
    """Construct and own a Milestone 2 application graph for one process role."""

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

    # Phase 3: resources. PostgreSQL is selected explicitly by normal process roles;
    # deterministic evaluation keeps the contract-backed in-memory tier.
    agent = AgentSpec(
        id=DEFAULT_AGENT_ID if storage == "postgres" else effective_ids.new_id(),
        version="1.0.0",
        name="Milestone 2 Agent",
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
    engine = None
    model_provider = None
    try:
        if storage == "memory":
            agent_repository = InMemoryAgentRepository()
            session_repository = InMemorySessionRepository()
            run_repository = InMemoryRunRepository(session_repository, effective_clock)
            event_repository = InMemoryEventRepository(session_repository, effective_clock)
            invocation_repository = InMemoryToolInvocationRepository(run_repository)
            uow_factory: UnitOfWorkFactory = cast(
                UnitOfWorkFactory,
                MemoryUnitOfWorkFactory(
                    agents=agent_repository,
                    sessions=session_repository,
                    runs=run_repository,
                    events=event_repository,
                    invocations=invocation_repository,
                ),
            )
        else:
            engine = create_engine(effective_settings.database_url)
            await assert_schema_revision(engine)
            uow_factory = cast(
                UnitOfWorkFactory,
                PostgresUnitOfWorkFactory(
                    create_session_factory(engine),
                    effective_clock,
                    EventUpcasterRegistry(),
                    lease_seconds=30,
                    max_attempts=3,
                ),
            )
        composition, model_provider = await _compose(
            storage=storage,
            agent=agent,
            principal=effective_principal,
            uow_factory=uow_factory,
            clock=effective_clock,
            ids=effective_ids,
            script=script,
        )
        yield composition
    finally:
        if model_provider is not None:
            await model_provider.close()
        if engine is not None:
            await engine.dispose()
