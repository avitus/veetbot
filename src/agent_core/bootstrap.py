"""The sole composition root: refuse, determinism, resources, freeze, wire."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.artifacts.local import LocalTrajectoryArtifactStore
from agent_core.adapters.determinism import (
    FixedClock,
    RandomIdFactory,
    SequenceIdFactory,
    SystemClock,
)
from agent_core.adapters.dispatch.inline import InlineRunDispatcher
from agent_core.adapters.dispatch.postgres import PostgresRunDispatcher
from agent_core.adapters.execution.local_workspace import LocalWorkspaceFactory
from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.adapters.models.unavailable import MissingCredentialProvider
from agent_core.adapters.persistence.database import (
    assert_schema_revision,
    create_engine,
    create_session_factory,
)
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryApprovalRepository,
    InMemoryCheckpointRepository,
    InMemoryEventRepository,
    InMemoryExportConsentRepository,
    InMemoryIdempotencyRepository,
    InMemoryMaintenanceRepository,
    InMemoryPolicyProfileRepository,
    InMemoryRunRepository,
    InMemorySessionHistoryRepository,
    InMemorySessionRepository,
    InMemoryToolInvocationRepository,
    InMemoryTrajectoryExportRepository,
    InMemoryTrajectoryProjectionRepository,
    InMemoryUsageRepository,
)
from agent_core.adapters.persistence.projections import (
    PostgresSessionHistoryRepository,
    PostgresTrajectoryProjectionRepository,
)
from agent_core.adapters.persistence.queue import PostgresRunQueue
from agent_core.adapters.persistence.repositories import (
    PostgresAgentRepository,
    PostgresApprovalRepository,
    PostgresCheckpointRepository,
    PostgresEventRepository,
    PostgresExportConsentRepository,
    PostgresIdempotencyRepository,
    PostgresMaintenanceRepository,
    PostgresPolicyProfileRepository,
    PostgresRunRepository,
    PostgresSessionRepository,
    PostgresToolInvocationRepository,
    PostgresTrajectoryExportRepository,
    PostgresUsageRepository,
)
from agent_core.adapters.persistence.unit_of_work import (
    MemoryUnitOfWorkFactory,
    PostgresRepositoryFactory,
    PostgresUnitOfWorkFactory,
    UnitOfWorkRepositories,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.application.approval_service import ApprovalService
from agent_core.application.run_service import RunService
from agent_core.application.session_service import SessionService
from agent_core.application.trajectory_service import (
    TrajectoryExportService,
    TrajectoryRedactor,
)
from agent_core.config import (
    PACKAGE_ROOT,
    ConfigurationError,
    Settings,
    load_config_document,
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
from agent_core.domain.policies import PolicyProfileRecord
from agent_core.domain.runs import CancelReason, RunLimits
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import WorkerService
from agent_core.ports.models import ModelProvider
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.demo_external_write import DemoExternalWriteTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.workspace.list_files import WorkspaceListFilesTool
from agent_core.tools.workspace.read_text import WorkspaceReadTextTool
from agent_core.tools.workspace.write_text import WorkspaceWriteTextTool


@dataclass(frozen=True, slots=True)
class Composition:
    runs: RunService
    approvals: ApprovalService
    sessions: SessionService
    trajectories: TrajectoryExportService
    executor: RunExecutor
    uow_factory: UnitOfWorkFactory
    clock: Clock
    worker_factory: Callable[[str], WorkerService]
    maintenance_factory: Callable[[], WorkerService]


DEFAULT_AGENT_ID = UUID("8ad3e17d-449f-5ec8-a807-4e14f2b3a716")


def _content_addressed_agent_version(agent: AgentSpec) -> str:
    payload = agent.model_dump(mode="json", exclude={"id", "version"})
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"1.0.0+h{digest}"


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


def _memory_uow_repositories(
    *,
    agents: InMemoryAgentRepository,
    sessions: InMemorySessionRepository,
    runs: InMemoryRunRepository,
    events: InMemoryEventRepository,
    invocations: InMemoryToolInvocationRepository,
    clock: Clock,
    approvals: InMemoryApprovalRepository | None = None,
) -> UnitOfWorkRepositories:
    approvals = approvals or InMemoryApprovalRepository(clock)
    return UnitOfWorkRepositories(
        agents=agents,
        approvals=approvals,
        policy_profiles=InMemoryPolicyProfileRepository(),
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=invocations,
        checkpoints=InMemoryCheckpointRepository(),
        idempotency=InMemoryIdempotencyRepository(clock),
        usage=InMemoryUsageRepository(runs),
        history=InMemorySessionHistoryRepository(events),
        trajectory=InMemoryTrajectoryProjectionRepository(events),
        export_consent=InMemoryExportConsentRepository(),
        trajectory_exports=InMemoryTrajectoryExportRepository(),
        maintenance=InMemoryMaintenanceRepository(),
        queue=None,
    )


def _postgres_repository_factory(
    clock: Clock,
    upcasters: EventUpcasterRegistry,
    *,
    lease_seconds: float,
    max_attempts: int,
) -> PostgresRepositoryFactory:
    def repositories(session: AsyncSession) -> UnitOfWorkRepositories:
        agents = PostgresAgentRepository(session, clock)
        sessions = PostgresSessionRepository(session)
        runs = PostgresRunRepository(session, clock)
        events = PostgresEventRepository(session, clock, upcasters)
        history = PostgresSessionHistoryRepository(session, clock, upcasters)
        trajectory = PostgresTrajectoryProjectionRepository(session, clock, upcasters)
        checkpoints = PostgresCheckpointRepository(session, clock, history)
        invocations = PostgresToolInvocationRepository(session, runs)
        return UnitOfWorkRepositories(
            agents=agents,
            approvals=PostgresApprovalRepository(session, clock),
            policy_profiles=PostgresPolicyProfileRepository(session),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=invocations,
            checkpoints=checkpoints,
            idempotency=PostgresIdempotencyRepository(session, clock),
            usage=PostgresUsageRepository(session),
            history=history,
            trajectory=trajectory,
            export_consent=PostgresExportConsentRepository(session),
            trajectory_exports=PostgresTrajectoryExportRepository(session),
            maintenance=PostgresMaintenanceRepository(session),
            queue=PostgresRunQueue(
                session,
                clock,
                events,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            ),
        )

    return repositories


async def _compose(
    *,
    storage: Literal["memory", "postgres"],
    agent: AgentSpec,
    principal: Principal,
    uow_factory: UnitOfWorkFactory,
    clock: Clock,
    ids: IdFactory,
    script: FakeModelScript | None,
    model_router: StaticModelRouter,
    model_providers: dict[str, ModelProvider],
    trajectory_export_enabled: bool,
    artifact_root: Path,
    trajectory_redactor: TrajectoryRedactor | None,
    maximum_tools: int,
    max_internal_attempts: int,
    identical_call_threshold: int,
    identical_denial_threshold: int,
    max_parallel_calls: int,
    lease_seconds: float,
    heartbeat_divisor: int,
    worker_poll_interval: float,
    workspace_root: Path,
) -> tuple[Composition, list[ModelProvider]]:
    registry = StaticToolRegistry()
    registry.register(CalculatorTool())
    registry.register(CurrentTimeTool(clock))
    registry.register(WorkspaceReadTextTool())
    registry.register(WorkspaceWriteTextTool())
    registry.register(WorkspaceListFilesTool())
    registry.register(DemoExternalWriteTool())
    async with uow_factory() as uow:
        await uow.agents.put(agent)
        await uow.policy_profiles.record(
            PolicyProfileRecord(
                policy_version=DEFAULT_RULESET.policy_version,
                profile_name=DEFAULT_RULESET.profile_name,
                profile_sha256=DEFAULT_RULESET.profile_sha256,
                hardline_sha256=DEFAULT_RULESET.hardline_sha256,
                rule_count=len(DEFAULT_RULESET.rules) + len(DEFAULT_RULESET.hardline),
                loaded_at=clock.now(),
                loaded_by="composition-root",
            )
        )
    model_provider = FakeModelProvider(script or default_fake_script(), clock)
    effective_providers = {"fake": model_provider, **model_providers}
    try:
        resolved_model = ResolvedModel(
            provider="fake",
            model="scripted",
            credential_ref="fake",
            policy_name=agent.model_policy,
            resolved_at=clock.now(),
        )
        context_builder = MinimalContextBuilder(registry, clock, maximum_tools=maximum_tools)
        pipeline = ToolPipeline(
            registry,
            uow_factory,
            clock,
            ids,
            policy=DeterministicPolicyEngine(DEFAULT_RULESET),
            workspace_factory=LocalWorkspaceFactory(workspace_root),
            current_principal=principal,
            max_parallel_calls=max_parallel_calls,
        )
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
            model_router=model_router,
            model_providers=effective_providers,
            budget_factory=lambda lease: UnitOfWorkBudgetLedger(uow_factory, clock, lease),
            clock=clock,
            ids=ids,
            dispatch_tools=pipeline.dispatch,
            seed_checkpoint=checkpoint_seeder,
            on_token=token_slot.set,
            max_internal_attempts=max_internal_attempts,
            identical_call_threshold=identical_call_threshold,
            identical_denial_threshold=identical_denial_threshold,
        )
        dispatcher = (
            InlineRunDispatcher(executor.execute, unit_of_work_open=uow_factory.is_open)
            if storage == "memory"
            else PostgresRunDispatcher()
        )
        session_service = SessionService(uow_factory, clock, ids, principal, agent)
        trajectory_service = TrajectoryExportService(
            uow_factory=uow_factory,
            principal=principal,
            clock=clock,
            ids=ids,
            tools=registry,
            artifacts=LocalTrajectoryArtifactStore(artifact_root),
            tenant_enabled=trajectory_export_enabled,
            redactor=trajectory_redactor,
        )
        run_service = RunService(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            sessions=session_service,
            principal=principal,
            clock=clock,
            ids=ids,
            cancel_active=token_slot.cancel,
            seed_checkpoint=checkpoint_seeder,
            cancel_parked_run=executor.cancel_parked_run,
            trajectory_export_enabled=trajectory_export_enabled,
        )
        approval_service = ApprovalService(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            principal=principal,
            clock=clock,
            resume_waiting_run=executor.requeue_after_approval,
        )
        return (
            Composition(
                runs=run_service,
                approvals=approval_service,
                sessions=session_service,
                trajectories=trajectory_service,
                executor=executor,
                uow_factory=uow_factory,
                clock=clock,
                worker_factory=lambda worker_id: DurableWorker(
                    uow_factory=uow_factory,
                    executor=executor,
                    clock=clock,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    heartbeat_divisor=heartbeat_divisor,
                    poll_interval_seconds=worker_poll_interval,
                ),
                maintenance_factory=lambda: MaintenanceWorker(
                    uow_factory=uow_factory,
                    clock=clock,
                    sweep_exports=trajectory_service.sweep_once,
                ),
            ),
            list(effective_providers.values()),
        )
    except BaseException:
        for provider in effective_providers.values():
            await provider.close()
        raise


def _provider_adapters(settings: Settings, registry: ProviderRegistry) -> dict[str, ModelProvider]:
    providers: dict[str, ModelProvider] = {}
    for profile_name, loaded in registry.profiles.items():
        profile = loaded.document
        credential = settings.credentials.get(profile_name)
        api_key = None if credential is None else credential.get_secret_value()
        if profile.adapter == "openai":
            provider: ModelProvider = (
                MissingCredentialProvider("openai")
                if api_key is None
                else OpenAIResponsesProvider(api_key=api_key, base_url=profile.base_url)
            )
        elif profile.adapter == "anthropic":
            provider = (
                MissingCredentialProvider("anthropic")
                if api_key is None
                else AnthropicMessagesProvider(api_key=api_key, base_url=profile.base_url)
            )
        elif profile.adapter == "chat_completions":
            tags = profile.in_band_reasoning
            provider = ChatCompletionsProvider(
                base_url=profile.base_url,
                api_key=api_key,
                think_open="<think>" if tags is None else tags.open,
                think_close="</think>" if tags is None else tags.close,
            )
        else:
            raise ConfigurationError(f"adapter {profile.adapter!r} is not constructible")
        if profile.adapter in providers:
            raise ConfigurationError(
                f"multiple enabled profiles select adapter {profile.adapter!r}"
            )
        providers[profile.adapter] = provider
    return providers


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
    model_policy: str | None = None,
    model_provider_overrides: Mapping[str, ModelProvider] | None = None,
    trajectory_redactor: TrajectoryRedactor | None = None,
) -> AsyncIterator[Composition]:
    """Construct and own a Milestone 3 application graph for one process role."""

    # Phase 1: refusal. Loading Settings enforces production sandbox and auth rules.
    effective_settings = settings or load_settings()
    validate_settings(effective_settings)
    provider_registry = ProviderRegistry.load(
        PACKAGE_ROOT / "models",
        adapters=ADAPTER_DEFINITIONS,
        overlay_root=effective_settings.config_dir,
    )

    effective_principal = principal or Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )
    validate_runtime_identity(
        effective_settings,
        tenant_id=effective_principal.tenant_id,
        principal_id=effective_principal.principal_id,
        policy_profile=policy_profile,
    )
    runtime_config = load_config_document(effective_settings, "runtime/limits.yaml")
    tool_config = load_config_document(effective_settings, "tools/limits.yaml")
    context_config = load_config_document(effective_settings, "context/plan.yaml")
    run_defaults = runtime_config["run_defaults"]
    model_limits = runtime_config["model"]
    queue_config = runtime_config["queue"]
    worker_config = runtime_config["worker"]
    circuit_breaker = tool_config["circuit_breaker"]
    parallel = tool_config["parallel"]
    tool_definitions = context_config["classes"]["tool_definitions"]

    # Phase 2: determinism, before any clock or identifier consumer exists.
    if clock is not None and fixed_clock_at is not None:
        raise ValueError("clock and fixed_clock_at are mutually exclusive")
    if ids is not None and sequential_ids:
        raise ValueError("ids and sequential_ids are mutually exclusive")
    effective_clock = clock or (
        FixedClock(fixed_clock_at) if fixed_clock_at is not None else SystemClock()
    )
    effective_ids = ids or (SequenceIdFactory() if sequential_ids else RandomIdFactory())
    model_router = StaticModelRouter(provider_registry, effective_clock)

    # Phase 3: resources. PostgreSQL is selected explicitly by normal process roles;
    # deterministic evaluation keeps the contract-backed in-memory tier.
    effective_model_policy = model_policy or "fake-balanced"
    agent = AgentSpec(
        id=DEFAULT_AGENT_ID if storage == "postgres" else effective_ids.new_id(),
        version=(
            "1.0.0"
            if model_policy is None
            else f"1.0.0+model.{effective_model_policy.replace('_', '-')}"
        ),
        name="Milestone 4 Agent",
        instructions="Answer the user's request and use a declared tool when useful.",
        model_policy=effective_model_policy,
        enabled_tools=(
            enabled_tools
            if enabled_tools is not None
            else [
                "math.calculate",
                "system.current_time",
                "workspace.read_text",
                "workspace.write_text",
                "workspace.list_files",
                "demo.external_write",
            ]
        ),
        policy_profile=policy_profile,
        limits=limits
        or RunLimits(
            max_steps=int(run_defaults["max_steps"]),
            max_model_calls=int(run_defaults["max_model_calls"]),
            max_tool_calls=int(run_defaults["max_tool_calls"]),
        ),
    )
    if storage == "postgres":
        agent = agent.model_copy(
            update={"version": _content_addressed_agent_version(agent)}, deep=True
        )
    engine = None
    model_providers: list[ModelProvider] = []
    try:
        if storage == "memory":
            agent_repository = InMemoryAgentRepository()
            session_repository = InMemorySessionRepository()
            run_repository = InMemoryRunRepository(session_repository, effective_clock)
            event_repository = InMemoryEventRepository(session_repository, effective_clock)
            invocation_repository = InMemoryToolInvocationRepository(run_repository)
            approval_repository = InMemoryApprovalRepository(effective_clock)
            uow_factory: UnitOfWorkFactory = cast(
                UnitOfWorkFactory,
                MemoryUnitOfWorkFactory(
                    _memory_uow_repositories(
                        agents=agent_repository,
                        approvals=approval_repository,
                        sessions=session_repository,
                        runs=run_repository,
                        events=event_repository,
                        invocations=invocation_repository,
                        clock=effective_clock,
                    )
                ),
            )
        else:
            engine = create_engine(effective_settings.database_url)
            await assert_schema_revision(engine)
            uow_factory = cast(
                UnitOfWorkFactory,
                PostgresUnitOfWorkFactory(
                    create_session_factory(engine),
                    _postgres_repository_factory(
                        effective_clock,
                        EventUpcasterRegistry(),
                        lease_seconds=float(worker_config["lease_seconds"]),
                        max_attempts=int(queue_config["max_attempts"]),
                    ),
                ),
            )
        provider_adapters = _provider_adapters(effective_settings, provider_registry)
        for name, override in (model_provider_overrides or {}).items():
            displaced = provider_adapters.get(name)
            if displaced is not None:
                await displaced.close()
            provider_adapters[name] = override
        composition, model_providers = await _compose(
            storage=storage,
            agent=agent,
            principal=effective_principal,
            uow_factory=uow_factory,
            clock=effective_clock,
            ids=effective_ids,
            script=script,
            model_router=model_router,
            model_providers=provider_adapters,
            trajectory_export_enabled=effective_settings.trajectory_export_enabled,
            artifact_root=effective_settings.artifact_root,
            trajectory_redactor=trajectory_redactor,
            maximum_tools=int(tool_definitions["max_items"]),
            max_internal_attempts=int(model_limits["max_internal_attempts"]),
            identical_call_threshold=int(circuit_breaker["identical_call_threshold"]),
            identical_denial_threshold=int(circuit_breaker["identical_denied_threshold"]),
            max_parallel_calls=int(parallel["maximum_calls"]),
            lease_seconds=float(worker_config["lease_seconds"]),
            heartbeat_divisor=int(worker_config["heartbeat_divisor"]),
            worker_poll_interval=float(queue_config["poll_interval_seconds"]),
            workspace_root=effective_settings.artifact_root.parent / "workspaces",
        )
        yield composition
    finally:
        for model_provider in model_providers:
            await model_provider.close()
        if engine is not None:
            await engine.dispose()
