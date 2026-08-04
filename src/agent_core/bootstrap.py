"""The sole composition root: refuse, determinism, resources, freeze, wire."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.artifacts.local import LocalTrajectoryArtifactStore
from agent_core.adapters.determinism import (
    FixedClock,
    RandomIdFactory,
    SequenceIdFactory,
    SystemClock,
    UUID7RequestIdFactory,
)
from agent_core.adapters.dispatch.inline import InlineRunDispatcher
from agent_core.adapters.dispatch.postgres import PostgresRunDispatcher
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.adapters.live_events import (
    InMemoryLiveEventBroadcaster,
    PostgresLiveEventBroadcaster,
)
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
    InMemoryArtifactRepository,
    InMemoryCheckpointRepository,
    InMemoryEventRepository,
    InMemoryExportConsentRepository,
    InMemoryIdempotencyRepository,
    InMemoryMaintenanceRepository,
    InMemoryPolicyProfileRepository,
    InMemoryProcessEventRepository,
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
    PostgresArtifactRepository,
    PostgresCheckpointRepository,
    PostgresEventRepository,
    PostgresExportConsentRepository,
    PostgresIdempotencyRepository,
    PostgresMaintenanceRepository,
    PostgresPolicyProfileRepository,
    PostgresProcessEventRepository,
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
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.application.public_services import (
    PublicApprovalService,
    PublicArtifactService,
    PublicRunService,
    PublicSessionService,
)
from agent_core.application.run_service import RunService
from agent_core.application.services import (
    ApprovalService as PublicApprovalServiceContract,
)
from agent_core.application.services import (
    ArtifactService as PublicArtifactServiceContract,
)
from agent_core.application.services import (
    RunService as PublicRunServiceContract,
)
from agent_core.application.services import (
    SessionService as PublicSessionServiceContract,
)
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
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.compactor import StructuredCompactor
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.events import ProcessEvent
from agent_core.domain.execution import (
    EgressDestination,
    EgressMode,
    EgressPolicy,
    ResourceLimits,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelEvent,
    ReasoningDeltaEvent,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextDeltaEvent,
    UsageEvent,
)
from agent_core.domain.policies import LoadedRuleset, PolicyProfileRecord
from agent_core.domain.runs import CancelReason, RunLimits
from agent_core.execution.egress import validate_destination
from agent_core.execution.manager import SandboxManager
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import load_ruleset_documents
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import WorkerService
from agent_core.ports.live_events import LiveEventBroadcaster
from agent_core.ports.models import ModelProvider
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.tools.artifact_export import ArtifactExportTool
from agent_core.tools.ask_user import AskUserTool
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.context_update import WORKING_STATE_TOOL_NAME, UpdateWorkingStateTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.demo_external_write import DemoExternalWriteTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.sandbox_run_command import SandboxRunCommandTool
from agent_core.tools.workspace.list_files import WorkspaceListFilesTool
from agent_core.tools.workspace.read_text import WorkspaceReadTextTool
from agent_core.tools.workspace.write_text import WorkspaceWriteTextTool

logger = logging.getLogger(__name__)
LIVE_EVENT_PUBLISH_TIMEOUT_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    sessions: PublicSessionServiceContract
    runs: PublicRunServiceContract
    approvals: PublicApprovalServiceContract
    artifacts: PublicArtifactServiceContract


@dataclass(frozen=True, slots=True)
class Composition:
    settings: Settings
    ruleset: LoadedRuleset
    services: ApplicationServices
    principal: Principal
    new_request_id: Callable[[], str]
    readiness_probe: Callable[[], Awaitable[bool]]
    runs: RunService
    approvals: ApprovalService
    sessions: SessionService
    trajectories: TrajectoryExportService
    executor: RunExecutor
    uow_factory: UnitOfWorkFactory
    clock: Clock
    worker_factory: Callable[[str], WorkerService]
    maintenance_factory: Callable[[], WorkerService]
    sandbox: SandboxManager


DEFAULT_AGENT_ID = UUID("8ad3e17d-449f-5ec8-a807-4e14f2b3a716")


def _content_addressed_agent_version(agent: AgentSpec) -> str:
    payload = agent.model_dump(mode="json", exclude={"id", "version"})
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"1.0.0+h{digest}"


class _ActiveToken:
    def __init__(self) -> None:
        self._tokens: dict[UUID, RunCancellationToken] = {}

    def set(self, run_id: UUID, token: RunCancellationToken) -> None:
        self._tokens[run_id] = token

    def discard(self, run_id: UUID) -> None:
        self._tokens.pop(run_id, None)

    def cancel(self, run_id: UUID | None) -> None:
        tokens = tuple(self._tokens.values()) if run_id is None else (self._tokens.get(run_id),)
        for token in tokens:
            if token is not None:
                token.cancel(CancelReason.REQUESTED)


async def _publish_model_event(
    broadcaster: LiveEventBroadcaster,
    session_id: UUID,
    run_id: UUID,
    event: ModelEvent,
) -> None:
    event_name: str | None = None
    data: dict[str, Any] = {
        "run_id": str(run_id),
        "step_number": event.step_number,
        "attempt_id": str(event.attempt_id),
    }
    if isinstance(event, TextDeltaEvent):
        event_name = "message.delta"
        data.update({"item_index": event.item_index, "text": event.text})
    elif isinstance(event, ReasoningDeltaEvent):
        event_name = "reasoning.delta"
        data.update(
            {
                "item_index": event.item_index,
                "text": event.text,
                "is_summary": event.is_summary,
            }
        )
    elif isinstance(event, UsageEvent):
        event_name = "usage.provisional"
        data["usage"] = event.usage.model_dump(mode="json")
    if event_name is None:
        return
    try:
        await asyncio.wait_for(
            broadcaster.publish(session_id, run_id, event_name, data),
            timeout=LIVE_EVENT_PUBLISH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("live_event_publish_failed", extra={"error_class": type(exc).__name__})


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
        process_events=InMemoryProcessEventRepository(),
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
        artifacts=InMemoryArtifactRepository(),
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
            process_events=PostgresProcessEventRepository(session),
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
            artifacts=PostgresArtifactRepository(session),
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
    settings: Settings,
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
    max_internal_attempts: int,
    identical_call_threshold: int,
    identical_denial_threshold: int,
    max_parallel_calls: int,
    hard_ceiling_multiplier: int,
    lease_seconds: float,
    heartbeat_divisor: int,
    worker_poll_interval: float,
    ruleset: LoadedRuleset,
    live_events: LiveEventBroadcaster,
    context_config: Mapping[str, object],
    max_compactions_per_step: int,
) -> tuple[Composition, list[ModelProvider]]:
    sandbox_config = load_config_document(settings, "sandbox/limits.yaml")
    raw_resources = sandbox_config["resources"]
    sandbox_limits = ResourceLimits(
        cpu_millicores=int(raw_resources["cpu_millicores"]),
        memory_bytes=int(raw_resources["memory_bytes"]),
        pids_max=int(raw_resources["pids_max"]),
        workspace_bytes=int(raw_resources["workspace_bytes"]),
        inodes_max=int(raw_resources["inodes_max"]),
        wall_clock_seconds=int(raw_resources["wall_clock_seconds"]),
    )
    raw_egress = sandbox_config["egress"]
    destinations = tuple(
        EgressDestination(
            host=str(item["host"]),
            ports=frozenset(int(port) for port in item["ports"]),
        )
        for item in raw_egress["destinations"]
    )
    for destination in destinations:
        validate_destination(destination)
    egress = EgressPolicy(mode=EgressMode(str(raw_egress["mode"])), destinations=destinations)
    raw_artifacts = sandbox_config["artifacts"]
    artifact_retention_days = int(raw_artifacts["retention_days"])
    artifact_maximum_bytes = int(raw_artifacts["maximum_bytes"])
    if storage == "memory" and settings.sandbox.value != "fake":
        raise ConfigurationError(
            "in-memory storage requires SANDBOX_MECHANISM=fake; configured "
            f"SANDBOX_MECHANISM={settings.sandbox.value}"
        )
    working_config = context_config.get("working_state")
    if not isinstance(working_config, dict):
        raise ConfigurationError("context working_state configuration must be a mapping")
    summary_config = context_config.get("summary")
    if not isinstance(summary_config, dict):
        raise ConfigurationError("context summary configuration must be a mapping")
    if storage == "memory" or settings.sandbox.value == "fake":
        fake_environment = FakeExecutionEnvironment(clock, ids)
        sandbox_manager = SandboxManager(
            fake_environment,
            image_digest=fake_image_digest(),
            limits=sandbox_limits,
            egress=egress,
            parent_environment=os.environ,
            passthrough_names=settings.sandbox_passthrough,
        )
    elif settings.sandbox.value in {"docker", "gvisor"}:
        sandbox_image_digest = await resolve_local_image_digest(settings.sandbox_image)
        logger.info(
            "sandbox_image_resolved",
            extra={
                "sandbox_mechanism": settings.sandbox.value,
                "sandbox_image_digest": sandbox_image_digest,
            },
        )
        docker_environment = DockerExecutionEnvironment(
            clock, ids, runtime="runsc" if settings.sandbox.value == "gvisor" else None
        )
        sandbox_manager = SandboxManager(
            docker_environment,
            image_digest=sandbox_image_digest,
            limits=sandbox_limits,
            egress=egress,
            parent_environment=os.environ,
            passthrough_names=settings.sandbox_passthrough,
        )
    else:
        raise ConfigurationError("microvm sandbox adapter is not configured in this deployment")
    estimator = ConservativeTokenEstimator()
    working_state = WorkingStateManager(clock, working_config, estimator)
    registry = StaticToolRegistry()
    registry.register(CalculatorTool())
    registry.register(AskUserTool())
    registry.register(CurrentTimeTool(clock))
    registry.register(WorkspaceReadTextTool())
    registry.register(WorkspaceWriteTextTool())
    registry.register(WorkspaceListFilesTool())
    registry.register(DemoExternalWriteTool())
    registry.register(
        SandboxRunCommandTool(sandbox_manager, hard_ceiling_multiplier=hard_ceiling_multiplier)
    )
    registry.register(ArtifactExportTool())

    async def validate_source_events(
        session_id: UUID,
        sequences: set[int],
        source_principal: Principal,
    ) -> set[int]:
        async with uow_factory() as uow:
            return await uow.events.existing_sequences(
                session_id,
                sequences,
                source_principal,
            )

    registry.register(UpdateWorkingStateTool(working_state, validate_source_events))
    async with uow_factory() as uow:
        await uow.agents.put(agent)
        await uow.policy_profiles.record(
            PolicyProfileRecord(
                policy_version=ruleset.policy_version,
                profile_name=ruleset.profile_name,
                profile_sha256=ruleset.profile_sha256,
                hardline_sha256=ruleset.hardline_sha256,
                rule_count=len(ruleset.rules) + len(ruleset.hardline),
                loaded_at=clock.now(),
                loaded_by="composition-root",
            )
        )
        derivation_key = f"policy.profile.loaded:{ruleset.policy_version}"
        await uow.process_events.append(
            ProcessEvent(
                id=uuid5(NAMESPACE_URL, derivation_key),
                event_type="policy.profile.loaded",
                actor_type="composition-root",
                payload={
                    "policy_version": ruleset.policy_version,
                    "profile_name": ruleset.profile_name,
                },
                derivation_key=derivation_key,
                created_at=clock.now(),
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
        context_planner = EventContextPlanner(
            uow_factory,
            registry,
            estimator,
            clock,
            principal,
            context_config,
            policy_version=ruleset.policy_version,
        )
        context_builder = BudgetedContextBuilder(
            context_planner,
            estimator,
            clock,
            working_state,
        )
        compactor = StructuredCompactor(
            estimator,
            maximum_depth=int(summary_config["max_depth"]),
        )
        trajectory_artifact_store = LocalTrajectoryArtifactStore(artifact_root)
        general_artifact_store = FilesystemArtifactStore(
            artifact_root, maximum_bytes=artifact_maximum_bytes
        )
        artifact_writers = ArtifactWriterFactory(
            uow_factory,
            general_artifact_store,
            clock,
            ids,
            retention_days=artifact_retention_days,
            maximum_bytes=artifact_maximum_bytes,
        )

        async def reconcile_artifact_orphans() -> int:
            async def metadata_exists(artifact_id: UUID) -> bool:
                async with uow_factory() as uow:
                    return await uow.artifacts.exists(artifact_id)

            return await general_artifact_store.reconcile_orphans(metadata_exists, now=clock.now())

        pipeline = ToolPipeline(
            registry,
            uow_factory,
            clock,
            ids,
            policy=DeterministicPolicyEngine(ruleset),
            workspace_factory=sandbox_manager,
            artifact_writers=artifact_writers,
            current_principal=principal,
            max_parallel_calls=max_parallel_calls,
            hard_ceiling_multiplier=hard_ceiling_multiplier,
            approval_expiry_seconds=dict(ruleset.approval_expiry_seconds),
        )
        token_slot = _ActiveToken()
        checkpoint_seeder = DurableCheckpointSeeder(clock)
        principal_resolver = StaticPrincipalResolver(principal)
        executor = RunExecutor(
            principal=principal,
            principals=principal_resolver,
            uow_factory=uow_factory,
            context_builder=context_builder,
            context_planner=context_planner,
            compactor=compactor,
            token_estimator=estimator,
            model_provider=model_provider,
            resolved_model=resolved_model,
            model_router=model_router,
            model_providers=effective_providers,
            budget_factory=lambda lease: UnitOfWorkBudgetLedger(uow_factory, clock, lease),
            clock=clock,
            ids=ids,
            dispatch_tools=pipeline.dispatch,
            add_open_question=working_state.add_question,
            seed_checkpoint=checkpoint_seeder,
            on_token=token_slot.set,
            on_token_complete=token_slot.discard,
            on_run_complete=sandbox_manager.release_run,
            on_model_event=lambda run, event: _publish_model_event(
                live_events, run.session_id, run.id, event
            ),
            max_internal_attempts=max_internal_attempts,
            identical_call_threshold=identical_call_threshold,
            identical_denial_threshold=identical_denial_threshold,
            max_compactions_per_step=max_compactions_per_step,
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
            artifacts=trajectory_artifact_store,
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
            self_approval_enabled=ruleset.self_approval_enabled,
        )
        public_session_service = PublicSessionService(uow_factory, clock, ids, agent)
        public_services = ApplicationServices(
            sessions=public_session_service,
            runs=PublicRunService(
                uow_factory=uow_factory,
                dispatcher=dispatcher,
                clock=clock,
                ids=ids,
                seed_checkpoint=checkpoint_seeder,
                cancel_active=token_slot.cancel,
                cancel_parked_run=executor.cancel_parked_run,
                resume_waiting_run=executor.requeue_after_input,
                resolve_open_question=working_state.resolve_question,
                trajectory_export_enabled=trajectory_export_enabled,
                live_events=live_events,
            ),
            approvals=PublicApprovalService(
                uow_factory=uow_factory,
                dispatcher=dispatcher,
                resume_waiting_run=executor.requeue_after_approval,
                self_approval_enabled=ruleset.self_approval_enabled,
            ),
            artifacts=PublicArtifactService(
                uow_factory=uow_factory,
                artifacts=trajectory_artifact_store,
                general_artifacts=general_artifact_store,
                clock=clock,
            ),
        )
        request_ids = UUID7RequestIdFactory(clock, RandomIdFactory())
        return (
            Composition(
                settings=settings,
                ruleset=ruleset,
                services=public_services,
                principal=principal,
                new_request_id=lambda: str(request_ids.new_id()),
                readiness_probe=public_session_service.ready,
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
                    sweep_artifacts=artifact_writers.sweep_expired,
                    sweep_sandboxes=None if storage == "memory" else sandbox_manager.reap,
                    sweep_artifact_orphans=reconcile_artifact_orphans,
                ),
                sandbox=sandbox_manager,
            ),
            list(effective_providers.values()),
        )
    except BaseException:
        await sandbox_manager.close()
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

    if principal is not None:
        effective_principal = principal
    elif effective_settings.auth_mode.value == "dev":
        effective_principal = Principal(
            tenant_id="local",
            principal_id="local-user",
            roles={"user"},
            scopes=set(PLATFORM_SCOPES),
        )
    else:
        unknown_scopes = set(effective_settings.auth_scopes) - set(PLATFORM_SCOPES)
        if unknown_scopes:
            raise ConfigurationError(
                f"AUTH_SCOPES contains unknown platform scopes: {', '.join(sorted(unknown_scopes))}"
            )
        effective_principal = Principal(
            tenant_id=effective_settings.auth_tenant_id,
            principal_id=effective_settings.auth_principal_id,
            roles=set(effective_settings.auth_roles),
            scopes=set(effective_settings.auth_scopes),
        )
    validate_runtime_identity(
        effective_settings,
        tenant_id=effective_principal.tenant_id,
        principal_id=effective_principal.principal_id,
        policy_profile=policy_profile,
    )
    runtime_config = load_config_document(effective_settings, "runtime/limits.yaml")
    tool_config = load_config_document(effective_settings, "tools/limits.yaml")
    policy_document = load_config_document(effective_settings, "policy/default.yaml")
    hardline_document = load_config_document(effective_settings, "policy/hardline.yaml")
    ruleset = load_ruleset_documents(policy_document, hardline_document)
    if ruleset.profile_name != policy_profile:
        if policy_profile == f"eval.{ruleset.profile_name}":
            ruleset = ruleset.model_copy(
                update={
                    "profile_name": policy_profile,
                    "policy_version": (
                        f"{policy_profile}@{ruleset.profile_sha256[:12]}"
                        f"+h{ruleset.hardline_sha256[:8]}"
                    ),
                },
                deep=True,
            )
        else:
            raise ConfigurationError(
                f"policy profile {policy_profile!r} does not match loaded profile "
                f"{ruleset.profile_name!r}"
            )
    context_config = load_config_document(effective_settings, "context/plan.yaml")
    run_defaults = runtime_config["run_defaults"]
    model_limits = runtime_config["model"]
    queue_config = runtime_config["queue"]
    worker_config = runtime_config["worker"]
    circuit_breaker = tool_config["circuit_breaker"]
    parallel = tool_config["parallel"]
    output_config = tool_config["output"]

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
        name="Milestone 7 Agent",
        instructions="Answer the user's request and use a declared tool when useful.",
        model_policy=effective_model_policy,
        enabled_tools=(
            enabled_tools
            if enabled_tools is not None
            else [
                "math.calculate",
                "conversation.ask_user",
                "system.current_time",
                "workspace.read_text",
                "workspace.write_text",
                "workspace.list_files",
                "demo.external_write",
                "sandbox.run_command",
                "artifact.export",
                WORKING_STATE_TOOL_NAME,
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
    live_events: LiveEventBroadcaster = (
        InMemoryLiveEventBroadcaster()
        if storage == "memory"
        else PostgresLiveEventBroadcaster(effective_settings.database_url)
    )
    composition: Composition | None = None
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
            settings=effective_settings,
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
            max_internal_attempts=int(model_limits["max_internal_attempts"]),
            identical_call_threshold=int(circuit_breaker["identical_call_threshold"]),
            identical_denial_threshold=int(circuit_breaker["identical_denied_threshold"]),
            max_parallel_calls=int(parallel["maximum_calls"]),
            hard_ceiling_multiplier=int(output_config["hard_ceiling_multiplier"]),
            lease_seconds=float(worker_config["lease_seconds"]),
            heartbeat_divisor=int(worker_config["heartbeat_divisor"]),
            worker_poll_interval=float(queue_config["poll_interval_seconds"]),
            ruleset=ruleset,
            live_events=live_events,
            context_config=context_config,
            max_compactions_per_step=int(runtime_config["context"]["max_compactions_per_step"]),
        )
        yield composition
    finally:
        if composition is not None:
            try:
                await composition.sandbox.close()
            except Exception as exc:
                logger.warning("sandbox_close_failed", extra={"error_class": type(exc).__name__})
        for model_provider in model_providers:
            try:
                await model_provider.close()
            except Exception as exc:
                logger.warning(
                    "model_provider_close_failed",
                    extra={"error_class": type(exc).__name__},
                )
        try:
            await live_events.close()
        except Exception as exc:
            logger.warning("live_events_close_failed", extra={"error_class": type(exc).__name__})
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                logger.warning(
                    "database_engine_close_failed",
                    extra={"error_class": type(exc).__name__},
                )
