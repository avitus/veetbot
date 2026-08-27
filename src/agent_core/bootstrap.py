"""The sole composition root: refuse, determinism, resources, freeze, wire."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.functions import func

from agent_core.adapters.apns import APNsPushTransport
from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.artifacts.local import LocalTrajectoryArtifactStore
from agent_core.adapters.browser.authentications import (
    InMemoryBrowserAuthenticationRepository,
)
from agent_core.adapters.browser.grants import InMemoryBrowserGrantRepository
from agent_core.adapters.browser.hosted_profiles import HostedBrowserProfileControlPlane
from agent_core.adapters.browser.hosted_provider import (
    HostedBrowserProvider,
    SessionBoundHostedBrowserProvider,
)
from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.browser.playwright import PlaywrightBrowserProvider
from agent_core.adapters.browser.profiles import InMemoryBrowserProfileRepository
from agent_core.adapters.browser.unavailable import (
    UnavailableBrowserAuthenticationControlPlane,
    UnavailableBrowserProfileControlPlane,
)
from agent_core.adapters.credentials import MappingCredentialResolver
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
from agent_core.adapters.execution.service import ExecutionServiceClient, ExecutionServiceServer
from agent_core.adapters.identity import (
    ConfiguredSchedulePrincipalDirectory,
    StaticPrincipalResolver,
)
from agent_core.adapters.live_events import (
    InMemoryLiveEventBroadcaster,
    PostgresLiveEventBroadcaster,
)
from agent_core.adapters.mcp.memory import InMemoryMCPServerRepository
from agent_core.adapters.mcp.persistence import PostgresMCPServerRepository
from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.mcp.sdk import SDKMCPClientFactory
from agent_core.adapters.memory.in_memory import (
    InMemoryKnowledgeStore,
    InMemoryMemoryStore,
    InMemoryTraceStore,
)
from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.adapters.models.unavailable import MissingCredentialProvider
from agent_core.adapters.notification_wakeup import PostgresNotificationWakeup
from agent_core.adapters.persistence.database import (
    assert_schema_revision,
    create_engine,
    create_session_factory,
)
from agent_core.adapters.persistence.delegations import (
    InMemoryDelegationRepository,
    PostgresDelegationRepository,
)
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryApprovalRepository,
    InMemoryArtifactRepository,
    InMemoryCapabilityEvaluationRepository,
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
from agent_core.adapters.persistence.memory_repositories import (
    PostgresKnowledgeStore,
    PostgresMemoryStore,
    PostgresTraceStore,
)
from agent_core.adapters.persistence.notifications import (
    InMemoryDeviceRegistrationIdempotencyRepository,
    InMemoryDeviceRegistry,
    InMemoryNotificationOutbox,
    PostgresDeviceRegistrationIdempotencyRepository,
    PostgresDeviceRegistry,
    PostgresNotificationOutbox,
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
    PostgresBrowserAuthenticationRepository,
    PostgresBrowserGrantRepository,
    PostgresBrowserProfileRepository,
    PostgresCapabilityEvaluationRepository,
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
from agent_core.adapters.persistence.schedules import (
    InMemoryScheduleIdempotencyRepository,
    InMemoryScheduleOccurrenceRepository,
    InMemoryScheduleRepository,
    PostgresScheduleIdempotencyRepository,
    PostgresScheduleOccurrenceRepository,
    PostgresScheduleRepository,
)
from agent_core.adapters.persistence.session_deletions import (
    InMemorySessionDeletionRepository,
    PostgresSessionDeletionRepository,
)
from agent_core.adapters.persistence.skills import PostgresSkillRepository
from agent_core.adapters.persistence.unit_of_work import (
    MemoryUnitOfWorkFactory,
    PostgresRepositoryFactory,
    PostgresUnitOfWorkFactory,
    UnitOfWorkRepositories,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.adapters.schedule_admission import (
    AllowScheduleAdmissionController,
    PostgresScheduleAdmissionController,
)
from agent_core.adapters.schedule_wakeup import (
    InMemoryScheduleWakeup,
    PostgresScheduleWakeup,
)
from agent_core.adapters.skills.memory import InMemorySkillRepository
from agent_core.adapters.skills.stores import (
    FilesystemSkillPackageStore,
    InMemorySkillPackageStore,
)
from agent_core.adapters.web.firecrawl import FirecrawlWebProvider
from agent_core.adapters.web.tavily import TavilyWebProvider
from agent_core.application.approval_service import ApprovalService
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.application.browser_grants import ConfiguredBrowserStandingAuthorizer
from agent_core.application.browser_management import (
    BrowserGrantManagementService,
    BrowserProfileManagementService,
    BrowserUnitOfWorkFactory,
)
from agent_core.application.delegations import DelegationJoin, DelegationMaterializer
from agent_core.application.device_management import (
    DeviceManagementService,
    NotificationInboxService,
)
from agent_core.application.notification_dispatcher import (
    NotificationDispatcher,
    NotificationDispatchUnitOfWorkFactory,
)
from agent_core.application.notification_producer import NotificationProducer
from agent_core.application.notification_worker import NotificationWorker
from agent_core.application.public_services import (
    PublicApprovalService,
    PublicArtifactService,
    PublicMemoryService,
    PublicRunService,
    PublicSessionService,
)
from agent_core.application.run_service import RunService
from agent_core.application.schedule_service import ScheduleService
from agent_core.application.services import (
    ApprovalService as PublicApprovalServiceContract,
)
from agent_core.application.services import (
    ArtifactService as PublicArtifactServiceContract,
)
from agent_core.application.services import (
    BrowserGrantService as PublicBrowserGrantServiceContract,
)
from agent_core.application.services import (
    BrowserProfileService as PublicBrowserProfileServiceContract,
)
from agent_core.application.services import (
    DeviceService as PublicDeviceServiceContract,
)
from agent_core.application.services import (
    MemoryReadService as PublicMemoryReadServiceContract,
)
from agent_core.application.services import (
    NotificationService as PublicNotificationServiceContract,
)
from agent_core.application.services import (
    RunService as PublicRunServiceContract,
)
from agent_core.application.services import (
    ScheduleService as PublicScheduleServiceContract,
)
from agent_core.application.services import (
    SessionService as PublicSessionServiceContract,
)
from agent_core.application.session_service import SessionService
from agent_core.application.skill_review import SkillBackgroundReview
from agent_core.application.trajectory_service import (
    TrajectoryExportService,
    TrajectoryRedactor,
)
from agent_core.config import (
    PACKAGE_ROOT,
    AuthMode,
    BrowserProviderKind,
    ConfigurationError,
    DeploymentMode,
    MemoryProviderExtractionMode,
    PushProviderKind,
    Settings,
    WebProviderKind,
    load_config_document,
    load_notification_worker_settings,
    load_provider_extraction_evidence,
    load_schedule_worker_settings,
    load_settings,
    provider_extraction_evidence_paths,
    validate_runtime_identity,
    validate_settings,
)
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.compactor import StructuredCompactor
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.browser import BrowserProfile
from agent_core.domain.delegations import DelegationCaps, DelegationDefaults
from agent_core.domain.devices import PushProvider
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent, ProcessEvent
from agent_core.domain.execution import (
    EgressDestination,
    EgressMode,
    EgressPolicy,
    ResourceLimits,
)
from agent_core.domain.mcp import MCPServerConfig, MCPTransport, ScriptedMCPServer
from agent_core.domain.messages import (
    Capability,
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
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, CancelReason, Run, RunLimits, RunStatus
from agent_core.domain.schedules import ScheduleAdmissionLimits, ScheduleDefinitionLimits
from agent_core.domain.sessions import (
    DEFAULT_PROJECT_SCOPE,
    SESSION_BROWSER_PROFILE_METADATA_KEY,
    Session,
    project_scope,
)
from agent_core.domain.skills import SkillPackage, SkillSource
from agent_core.domain.tools import ToolExecutionContext, ToolSpec
from agent_core.execution.egress import validate_destination
from agent_core.execution.manager import SandboxManager
from agent_core.execution.proxy import WorkerEgressProxy, start_worker_egress_proxy
from agent_core.knowledge.service import KnowledgeService
from agent_core.mcp.configuration import email_server_configs, validate_mcp_config
from agent_core.mcp.runtime import MCPRuntime
from agent_core.memory.formation import (
    FORMATION_POLICY_VERSION,
    SESSION_IDLE_SECONDS,
    DeterministicCandidateExtractor,
    GovernedMemoryService,
)
from agent_core.memory.profiles import MemoryProfiles
from agent_core.memory.provider_extraction import (
    PROVIDER_FORMATION_POLICY_VERSION,
    ProviderAssistedCandidateExtractor,
    provider_extraction_evidence_matches,
)
from agent_core.memory.retrieval import (
    DeterministicQueryFormer,
    EventEpisodeSearch,
    HybridMemoryRetriever,
)
from agent_core.model import NON_ROUTED_MODEL_POLICIES
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from agent_core.observability.schedules import ScheduleMetrics, tenant_hash_key
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import load_ruleset_documents
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.browser import BrowserProvider
from agent_core.ports.browser_profiles import BrowserProfileControlPlane
from agent_core.ports.browser_sessions import (
    BrowserAuthenticationControlPlane,
    BrowserSessionControlPlane,
)
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import WorkerService
from agent_core.ports.live_events import LiveEventBroadcaster
from agent_core.ports.mcp import MCPClientFactory, MCPServerRepository
from agent_core.ports.models import ModelProvider
from agent_core.ports.notifications import PushTransport
from agent_core.ports.persistence import (
    ScheduleUnitOfWork,
    TransactionCallback,
    TransactionCallbackRegistrar,
    UnitOfWorkFactory,
)
from agent_core.ports.skills import SkillPackageStore, SkillRepository
from agent_core.ports.web import WebProvider
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.executor import RunExecutor
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.materializer import ScheduleMaterializer
from agent_core.scheduling.worker import ScheduleWorker
from agent_core.skills.catalog import SkillCatalogService
from agent_core.skills.package import SkillPackageValidator
from agent_core.tools.artifact_export import ArtifactExportTool
from agent_core.tools.ask_user import AskUserTool
from agent_core.tools.browser_act import BrowserActTool
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.context_update import WORKING_STATE_TOOL_NAME, UpdateWorkingStateTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.delegate_run import DelegateRunTool, LegacyDelegateRunTool
from agent_core.tools.demo_external_write import DemoExternalWriteTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.knowledge_ingest import KnowledgeIngestTool
from agent_core.tools.knowledge_search import KnowledgeSearchTool
from agent_core.tools.memory_recall_episodes import MemoryRecallEpisodesTool
from agent_core.tools.memory_remember import LegacyMemoryRememberTool, MemoryRememberTool
from agent_core.tools.memory_search import MemorySearchTool
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.sandbox_run_command import SandboxRunCommandTool
from agent_core.tools.schedule_create import SCHEDULE_CREATE_TOOL_NAME, ScheduleCreateTool
from agent_core.tools.skill_load import (
    SKILL_LOAD_TOOL_NAME,
    LegacySkillLoadTool,
    SkillLoadTool,
)
from agent_core.tools.skill_manage import SkillManageTool
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_search import WebSearchTool
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
    browser_profiles: PublicBrowserProfileServiceContract
    browser_grants: PublicBrowserGrantServiceContract
    schedules: PublicScheduleServiceContract
    devices: PublicDeviceServiceContract
    notifications: PublicNotificationServiceContract
    memory: PublicMemoryReadServiceContract


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
    schedules: ScheduleService
    trajectories: TrajectoryExportService
    executor: RunExecutor
    uow_factory: UnitOfWorkFactory
    clock: Clock
    ids: IdFactory
    worker_factory: Callable[[str], WorkerService]
    async_worker_factory: Callable[[str], WorkerService]
    maintenance_factory: Callable[[], WorkerService]
    schedule_worker_factory: Callable[[], WorkerService]
    sandbox: SandboxManager
    mcp: MCPRuntime
    skill_catalogs: SkillCatalogService
    skill_reviews: SkillBackgroundReview
    tool_pipeline: ToolPipeline
    memory: GovernedMemoryService
    memory_retriever: HybridMemoryRetriever
    memory_profiles: MemoryProfiles
    knowledge: KnowledgeService
    mcp_proxy: WorkerEgressProxy | None


DEFAULT_AGENT_ID = UUID("8ad3e17d-449f-5ec8-a807-4e14f2b3a716")
DEFAULT_AGENT_INSTRUCTIONS = (
    "Answer the user's request and use a declared tool when useful. "
    "Prefer the least-powerful declared tool that can answer. For routine arithmetic, "
    "date/time, and public facts, use read-only tools such as math.calculate, "
    "system.current_time, or web.search when available. Do not use sandbox.run_command "
    "for those requests; use it only when arbitrary code execution is necessary. If no "
    "read-only tool can answer, explain the limitation or ask before proposing sandboxed "
    "code execution."
)
_BROWSER_TOOL_NAMES = frozenset({"browser.navigate", "browser.observe", "browser.act"})


def _session_tool_filter(
    browser_provider: BrowserProvider | None,
) -> Callable[[Session, Sequence[ToolSpec]], list[ToolSpec]] | None:
    if not isinstance(browser_provider, SessionBoundHostedBrowserProvider):
        return None

    def filter_tools(session: Session, tools: Sequence[ToolSpec]) -> list[ToolSpec]:
        selected_profile = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
        if isinstance(selected_profile, str) and selected_profile:
            return list(tools)
        return [tool for tool in tools if tool.name not in _BROWSER_TOOL_NAMES]

    return filter_tools


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


def system_clock() -> Clock:
    """Hand the wall clock to a caller that owns no composition.

    Adapters are constructed here and nowhere else, so a command or an
    evaluation harness that needs the time asks the composition root for a
    clock rather than reading the ambient one.
    """

    return SystemClock()


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
    skills: SkillRepository | None = None,
    mcp_servers: MCPServerRepository | None = None,
    memories: InMemoryMemoryStore | None = None,
    traces: InMemoryTraceStore | None = None,
    knowledge: InMemoryKnowledgeStore | None = None,
) -> UnitOfWorkRepositories:
    approvals = approvals or InMemoryApprovalRepository(clock)
    skills = skills or InMemorySkillRepository(
        InMemorySkillPackageStore(),
        SkillPackageValidator(ConservativeTokenEstimator()),
        clock,
        RandomIdFactory(),
    )
    mcp_servers = mcp_servers or InMemoryMCPServerRepository()
    memories = memories or InMemoryMemoryStore(clock)
    traces = traces or InMemoryTraceStore()
    knowledge = knowledge or InMemoryKnowledgeStore(clock)
    schedules = InMemoryScheduleRepository()
    devices = InMemoryDeviceRegistry()
    notification_outbox = InMemoryNotificationOutbox(clock, devices)
    schedule_occurrences = InMemoryScheduleOccurrenceRepository(schedules)
    delegations = InMemoryDelegationRepository()
    checkpoints = InMemoryCheckpointRepository()
    idempotency = InMemoryIdempotencyRepository(clock)
    usage = InMemoryUsageRepository(runs)
    trajectory_exports = InMemoryTrajectoryExportRepository()
    artifacts = InMemoryArtifactRepository()
    session_deletions = InMemorySessionDeletionRepository(
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=invocations,
        approvals=approvals,
        checkpoints=checkpoints,
        idempotency=idempotency,
        usage=usage,
        trajectory_exports=trajectory_exports,
        artifacts=artifacts,
        memories=memories,
        traces=traces,
        knowledge=knowledge,
        schedules=schedules,
        notification_outbox=notification_outbox,
        delegations=delegations,
    )
    return UnitOfWorkRepositories(
        agents=agents,
        approvals=approvals,
        policy_profiles=InMemoryPolicyProfileRepository(),
        browser_profiles=InMemoryBrowserProfileRepository(),
        browser_grants=InMemoryBrowserGrantRepository(),
        browser_authentications=InMemoryBrowserAuthenticationRepository(),
        process_events=InMemoryProcessEventRepository(),
        sessions=sessions,
        session_deletions=session_deletions,
        runs=runs,
        events=events,
        invocations=invocations,
        checkpoints=checkpoints,
        idempotency=idempotency,
        usage=usage,
        history=InMemorySessionHistoryRepository(events),
        trajectory=InMemoryTrajectoryProjectionRepository(events),
        export_consent=InMemoryExportConsentRepository(),
        trajectory_exports=trajectory_exports,
        artifacts=artifacts,
        maintenance=InMemoryMaintenanceRepository(sessions, events, memories),
        skills=skills,
        mcp_servers=mcp_servers,
        memories=memories,
        traces=traces,
        knowledge=knowledge,
        evaluations=InMemoryCapabilityEvaluationRepository(),
        schedules=schedules,
        schedule_occurrences=schedule_occurrences,
        schedule_idempotency=InMemoryScheduleIdempotencyRepository(schedules),
        schedule_admission=AllowScheduleAdmissionController(),
        devices=devices,
        device_registration_idempotency=InMemoryDeviceRegistrationIdempotencyRepository(),
        notification_outbox=notification_outbox,
        delegations=delegations,
        queue=None,
    )


def _postgres_repository_factory(
    clock: Clock,
    upcasters: EventUpcasterRegistry,
    *,
    lease_seconds: float,
    max_attempts: int,
    skill_store: SkillPackageStore,
    skill_validator: SkillPackageValidator,
    ids: IdFactory,
    schedule_admission_limits: ScheduleAdmissionLimits,
    schedule_metrics: ScheduleMetrics,
) -> PostgresRepositoryFactory:
    def repositories(
        session: AsyncSession,
        register_rollback: TransactionCallbackRegistrar,
    ) -> UnitOfWorkRepositories:
        agents = PostgresAgentRepository(session, clock)
        sessions = PostgresSessionRepository(session)
        runs = PostgresRunRepository(session, clock)
        events = PostgresEventRepository(session, clock, upcasters)
        history = PostgresSessionHistoryRepository(session, clock, upcasters)
        trajectory = PostgresTrajectoryProjectionRepository(session, clock, upcasters)
        checkpoints = PostgresCheckpointRepository(session, clock, history)
        invocations = PostgresToolInvocationRepository(session, runs)
        memories = PostgresMemoryStore(session, clock)
        traces = PostgresTraceStore(session)
        knowledge = PostgresKnowledgeStore(session, clock)
        schedules = PostgresScheduleRepository(session)
        devices = PostgresDeviceRegistry(session)
        return UnitOfWorkRepositories(
            agents=agents,
            approvals=PostgresApprovalRepository(session, clock),
            policy_profiles=PostgresPolicyProfileRepository(session),
            browser_profiles=PostgresBrowserProfileRepository(session),
            browser_grants=PostgresBrowserGrantRepository(session),
            browser_authentications=PostgresBrowserAuthenticationRepository(session),
            process_events=PostgresProcessEventRepository(session),
            sessions=sessions,
            session_deletions=PostgresSessionDeletionRepository(session),
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
            skills=PostgresSkillRepository(
                session,
                skill_store,
                skill_validator,
                clock,
                ids,
                register_rollback,
            ),
            mcp_servers=PostgresMCPServerRepository(session, clock),
            memories=memories,
            traces=traces,
            knowledge=knowledge,
            evaluations=PostgresCapabilityEvaluationRepository(session),
            schedules=schedules,
            schedule_occurrences=PostgresScheduleOccurrenceRepository(schedules),
            schedule_idempotency=PostgresScheduleIdempotencyRepository(schedules),
            schedule_admission=PostgresScheduleAdmissionController(
                session, schedule_admission_limits, schedule_metrics
            ),
            devices=devices,
            device_registration_idempotency=(
                PostgresDeviceRegistrationIdempotencyRepository(session)
            ),
            notification_outbox=PostgresNotificationOutbox(session, clock),
            delegations=PostgresDelegationRepository(session),
            queue=PostgresRunQueue(
                session,
                clock,
                events,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            ),
        )

    return repositories


class _ScheduleUnitOfWork(ScheduleUnitOfWork):
    """Least-privilege repository set for the production scheduler role."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        tenant_id: str,
        clock: Clock,
        lease_seconds: float,
        max_attempts: int,
        admission_limits: ScheduleAdmissionLimits,
        metrics: ScheduleMetrics,
    ) -> None:
        self._maker = maker
        self._tenant_id = tenant_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._admission_limits = admission_limits
        self._metrics = metrics
        self._session: AsyncSession | None = None
        self._rollback_callbacks: list[TransactionCallback] = []

    def on_rollback(self, callback: TransactionCallback) -> None:
        self._rollback_callbacks.append(callback)

    async def __aenter__(self) -> Self:
        session = self._maker()
        self._session = session
        await session.execute(
            select(func.set_config("agent_core.tenant_id", self._tenant_id, True))
        )
        upcasters = EventUpcasterRegistry()
        events = PostgresEventRepository(session, self._clock, upcasters)
        history = PostgresSessionHistoryRepository(session, self._clock, upcasters)
        schedules = PostgresScheduleRepository(session)
        self.agents = PostgresAgentRepository(session, self._clock)
        self.process_events = PostgresProcessEventRepository(session)
        self.sessions = PostgresSessionRepository(session)
        self.runs = PostgresRunRepository(session, self._clock)
        self.events = events
        self.history = history
        self.checkpoints = PostgresCheckpointRepository(session, self._clock, history)
        self.schedules = schedules
        self.schedule_occurrences = PostgresScheduleOccurrenceRepository(schedules)
        self.schedule_admission = PostgresScheduleAdmissionController(
            session, self._admission_limits, self._metrics
        )
        self.notification_outbox = PostgresNotificationOutbox(session, self._clock)
        self.queue = PostgresRunQueue(
            session,
            self._clock,
            events,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        if self._session is None:
            return
        try:
            if exc_type is None:
                await self._session.commit()
                self._rollback_callbacks.clear()
            else:
                await self._run_rollback_callbacks()
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def _run_rollback_callbacks(self) -> None:
        callbacks = list(self._rollback_callbacks)
        self._rollback_callbacks.clear()
        for callback in reversed(callbacks):
            await callback()


class _ScheduleUnitOfWorkFactory:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        tenant_id: str,
        clock: Clock,
        lease_seconds: float,
        max_attempts: int,
        admission_limits: ScheduleAdmissionLimits,
        metrics: ScheduleMetrics,
    ) -> None:
        self._maker = maker
        self._tenant_id = tenant_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._admission_limits = admission_limits
        self._metrics = metrics

    def __call__(self) -> _ScheduleUnitOfWork:
        return _ScheduleUnitOfWork(
            self._maker,
            tenant_id=self._tenant_id,
            clock=self._clock,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
            admission_limits=self._admission_limits,
            metrics=self._metrics,
        )

    def is_open(self) -> bool:
        return False


class _NotificationUnitOfWork:
    """Least-privilege repository set for the production notify role."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        tenant_id: str,
        clock: Clock,
    ) -> None:
        self._maker = maker
        self._tenant_id = tenant_id
        self._clock = clock
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        session = self._maker()
        self._session = session
        await session.execute(
            select(func.set_config("agent_core.tenant_id", self._tenant_id, True))
        )
        upcasters = EventUpcasterRegistry()
        history = PostgresSessionHistoryRepository(session, self._clock, upcasters)
        self.approvals = PostgresApprovalRepository(session, self._clock)
        self.checkpoints = PostgresCheckpointRepository(
            session,
            self._clock,
            history,
        )
        self.devices = PostgresDeviceRegistry(session)
        self.notification_outbox = PostgresNotificationOutbox(session, self._clock)
        self.process_events = PostgresProcessEventRepository(session)
        self.runs = PostgresRunRepository(session, self._clock)
        self.sessions = PostgresSessionRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        if self._session is None:
            return
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None


class _NotificationUnitOfWorkFactory:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        tenant_id: str,
        clock: Clock,
    ) -> None:
        self._maker = maker
        self._tenant_id = tenant_id
        self._clock = clock

    def __call__(self) -> _NotificationUnitOfWork:
        return _NotificationUnitOfWork(
            self._maker,
            tenant_id=self._tenant_id,
            clock=self._clock,
        )


def _validate_notification_role(settings: Settings) -> Principal:
    validate_settings(
        settings,
        require_auth_token=False,
        require_execution_environment=False,
    )
    if not settings.notification_dispatch_enabled:
        raise ConfigurationError(
            "notification dispatch is disabled; set AGENT_NOTIFICATION_DISPATCH_ENABLED=1"
        )
    if not settings.notification_api_enabled:
        raise ConfigurationError(
            "notification API is disabled; set AGENT_NOTIFICATION_API_ENABLED=1 before dispatch"
        )
    if settings.deployment_mode is not DeploymentMode.PRODUCTION:
        raise ConfigurationError("notification worker requires the production process topology")
    if settings.auth_mode is not AuthMode.TOKEN:
        raise ConfigurationError("notification worker requires configured non-development identity")
    if not settings.database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
        raise ConfigurationError("notification worker requires PostgreSQL storage")
    if settings.auth_token is not None:
        raise ConfigurationError("notification worker environment must not contain an API bearer")
    if settings.credentials:
        raise ConfigurationError("notification worker environment must not contain provider keys")
    if settings.push_provider is not PushProviderKind.APNS:
        raise ConfigurationError("notification worker requires PUSH_PROVIDER=apns")
    apns_values = {
        "APNS_KEY_FILE": settings.apns_key_file,
        "APNS_KEY_ID": settings.apns_key_id,
        "APNS_TEAM_ID": settings.apns_team_id,
        "APNS_TOPIC": settings.apns_topic,
    }
    missing = [name for name, value in apns_values.items() if value is None]
    if missing:
        raise ConfigurationError(
            "notification worker requires APNs configuration: " + ", ".join(missing)
        )
    assert settings.apns_key_file is not None
    if not settings.apns_key_file.is_absolute():
        raise ConfigurationError("APNS_KEY_FILE must be an absolute path")
    unknown = set(settings.auth_scopes) - set(PLATFORM_SCOPES)
    if unknown:
        raise ConfigurationError(
            "AUTH_SCOPES contains unknown platform scopes: " + ", ".join(sorted(unknown))
        )
    return Principal(
        tenant_id=settings.auth_tenant_id,
        principal_id=settings.auth_principal_id,
        roles=set(settings.auth_roles),
        scopes=set(settings.auth_scopes),
    )


def _validate_schedule_role(settings: Settings) -> Principal:
    validate_settings(
        settings,
        require_auth_token=False,
        require_execution_environment=False,
    )
    if not settings.schedule_worker_enabled:
        raise ConfigurationError("schedule worker is disabled; set AGENT_SCHEDULE_WORKER_ENABLED=1")
    if not settings.schedule_api_enabled:
        raise ConfigurationError(
            "schedule API is disabled; set AGENT_SCHEDULE_API_ENABLED=1 before the worker"
        )
    if settings.deployment_mode is not DeploymentMode.PRODUCTION:
        raise ConfigurationError("schedule worker requires the production process topology")
    if settings.auth_mode is not AuthMode.TOKEN:
        raise ConfigurationError("schedule worker requires configured non-development identity")
    if not settings.database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
        raise ConfigurationError("schedule worker requires PostgreSQL storage")
    if settings.credentials:
        raise ConfigurationError("schedule worker environment must not contain provider keys")
    unknown = set(settings.auth_scopes) - set(PLATFORM_SCOPES)
    if unknown:
        raise ConfigurationError(
            "AUTH_SCOPES contains unknown platform scopes: " + ", ".join(sorted(unknown))
        )
    return Principal(
        tenant_id=settings.auth_tenant_id,
        principal_id=settings.auth_principal_id,
        roles=set(settings.auth_roles),
        scopes=set(settings.auth_scopes),
    )


async def serve_execution_service(socket_path: Path) -> None:
    """Run the credential-free production execution-service composition."""

    environment = DockerExecutionEnvironment(
        SystemClock(),
        RandomIdFactory(),
        runtime="runsc",
    )
    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve_local_image_digest,
    )
    loop = asyncio.get_running_loop()
    serving = asyncio.create_task(server.serve_forever())
    signal_shutdown = False
    installed_handlers: list[signal.Signals] = []

    def request_shutdown() -> None:
        nonlocal signal_shutdown
        signal_shutdown = True
        serving.cancel()

    for signal_number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_number, request_shutdown)
        except NotImplementedError:
            continue
        installed_handlers.append(signal_number)
    try:
        try:
            await serving
        except asyncio.CancelledError:
            if not signal_shutdown:
                raise
    finally:
        for signal_number in installed_handlers:
            loop.remove_signal_handler(signal_number)
        try:
            await server.close()
        finally:
            await environment.close()


@asynccontextmanager
async def build_schedule_worker(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
) -> AsyncIterator[ScheduleWorker]:
    """Build only the resources needed to materialize scheduled runs."""

    effective_settings = settings or load_schedule_worker_settings()
    principal = _validate_schedule_role(effective_settings)
    runtime = load_config_document(effective_settings, "runtime/limits.yaml")
    queue = runtime["queue"]
    worker = runtime["worker"]
    scheduling = runtime["scheduling"]
    admission_limits = ScheduleAdmissionLimits.model_validate(
        {
            "max_active_runs_per_tenant": scheduling["max_active_runs_per_tenant"],
            "max_materializations_per_minute": scheduling["max_materializations_per_minute"],
            "daily_cost": scheduling["daily_cost"],
            "monthly_cost": scheduling["monthly_cost"],
        }
    )
    effective_clock = clock or SystemClock()
    effective_ids = ids or RandomIdFactory()
    notification_producer = (
        NotificationProducer(clock=effective_clock, ids=effective_ids)
        if effective_settings.notification_dispatch_enabled
        else None
    )
    wakeup = PostgresScheduleWakeup(effective_settings.database_url)
    metrics = ScheduleMetrics(tenant_hash_key=tenant_hash_key(effective_settings.database_url))
    engine = create_engine(effective_settings.database_url)
    try:
        await assert_schema_revision(engine)
        factory = _ScheduleUnitOfWorkFactory(
            create_session_factory(engine),
            tenant_id=principal.tenant_id,
            clock=effective_clock,
            lease_seconds=float(worker["lease_seconds"]),
            max_attempts=int(queue["max_attempts"]),
            admission_limits=admission_limits,
            metrics=metrics,
        )
        materializer = ScheduleMaterializer(
            uow_factory=factory,
            principals=ConfiguredSchedulePrincipalDirectory(principal),
            clock=effective_clock,
            ids=effective_ids,
            seed_checkpoint=DurableCheckpointSeeder(effective_clock),
            metrics=metrics,
            notification_producer=notification_producer,
        )
        yield ScheduleWorker(
            uow_factory=factory,
            materialize=materializer.materialize,
            clock=effective_clock,
            scan_batch=int(scheduling["scan_batch"]),
            fallback_poll_seconds=float(scheduling["fallback_poll_seconds"]),
            admission_backoff_seconds=float(scheduling["admission_backoff_seconds"]),
            wait_for_wakeup=wakeup.wait,
            metrics=metrics,
        )
    finally:
        await wakeup.close()
        await engine.dispose()


@asynccontextmanager
async def build_notification_worker(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
    transport: PushTransport | None = None,
) -> AsyncIterator[NotificationWorker]:
    """Build only the resources needed to dispatch durable notifications."""

    effective_settings = settings or load_notification_worker_settings()
    principal = _validate_notification_role(effective_settings)
    runtime = load_config_document(effective_settings, "runtime/limits.yaml")
    notification_limits = runtime["notifications"]
    effective_clock = clock or SystemClock()
    effective_ids = ids or RandomIdFactory()
    effective_transport = transport
    if effective_transport is None:
        assert effective_settings.apns_key_file is not None
        assert effective_settings.apns_key_id is not None
        assert effective_settings.apns_team_id is not None
        assert effective_settings.apns_topic is not None
        try:
            effective_transport = APNsPushTransport(
                key_file=effective_settings.apns_key_file,
                key_id=effective_settings.apns_key_id,
                team_id=effective_settings.apns_team_id,
                topic=effective_settings.apns_topic,
                clock=effective_clock,
            )
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
    wakeup = PostgresNotificationWakeup(effective_settings.database_url)
    engine = create_engine(effective_settings.database_url)
    try:
        await assert_schema_revision(engine)
        factory = _NotificationUnitOfWorkFactory(
            create_session_factory(engine),
            tenant_id=principal.tenant_id,
            clock=effective_clock,
        )
        dispatcher = NotificationDispatcher(
            uow_factory=cast(NotificationDispatchUnitOfWorkFactory, factory),
            transport=effective_transport,
            providers=frozenset({PushProvider.APNS}),
            clock=effective_clock,
            ids=effective_ids,
            claimant=f"notify:{socket.gethostname()}:{os.getpid()}",
            batch_size=int(notification_limits["claim_batch"]),
            lease_seconds=float(notification_limits["lease_seconds"]),
            retry_delays=tuple(
                float(value) for value in notification_limits["retry_delays_seconds"]
            ),
        )
        yield NotificationWorker(
            dispatch_once=dispatcher.run_once,
            clock=effective_clock,
            fallback_poll_seconds=float(notification_limits["fallback_poll_seconds"]),
            wait_for_wakeup=wakeup.wait,
        )
    finally:
        await wakeup.close()
        close_transport = getattr(effective_transport, "aclose", None)
        if close_transport is not None:
            await close_transport()
        await engine.dispose()


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
    interactive_priority: int,
    async_priority: int,
    schedule_scan_batch: int,
    schedule_fallback_poll_seconds: float,
    schedule_admission_backoff_seconds: float,
    schedule_definition_limits: ScheduleDefinitionLimits,
    delegation_defaults: DelegationDefaults,
    delegation_caps: DelegationCaps,
    notification_expiry_seconds: float,
    schedule_notify: Callable[[], Awaitable[None]],
    schedule_wait: Callable[[float], Awaitable[None]],
    schedule_metrics: ScheduleMetrics,
    ruleset: LoadedRuleset,
    live_events: LiveEventBroadcaster,
    context_config: Mapping[str, object],
    memory_profiles: MemoryProfiles,
    mcp_config: Mapping[str, object],
    max_compactions_per_step: int,
    skill_store: SkillPackageStore,
    mcp_clients: MCPClientFactory | None,
    mcp_scripts: Mapping[str, ScriptedMCPServer] | None,
    credential_resolver: CredentialResolver,
    mcp_server_configs: tuple[MCPServerConfig, ...],
    web_search_provider: WebProvider | None,
    web_fetch_provider: WebProvider | None,
    memory_provider_evaluation_mode: bool,
    browser_provider: BrowserProvider | None,
    browser_profile_lifecycle: BrowserProfileControlPlane,
    browser_authentications: BrowserAuthenticationControlPlane,
) -> tuple[Composition, list[ModelProvider]]:
    """Assemble the complete runtime graph for one selected storage backend."""

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
    mcp_proxy: WorkerEgressProxy | None = None

    def destination_allowed(url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return egress.mode is EgressMode.ALLOWLIST and any(
            destination.host == parsed.hostname and port in destination.ports
            for destination in egress.destinations
        )

    for config in mcp_server_configs:
        if config.tenant_id != principal.tenant_id:
            raise ConfigurationError("MCP server configuration tenant does not match the runtime")
        validate_mcp_config(config, destination_allowed=destination_allowed)
    async with uow_factory() as uow:
        for config in mcp_server_configs:
            await uow.mcp_servers.put(config)
    async with uow_factory() as uow:
        effective_mcp_configs = await uow.mcp_servers.list_enabled(principal.tenant_id)
    for config in effective_mcp_configs:
        validate_mcp_config(config, destination_allowed=destination_allowed)
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
    context_classes = context_config.get("classes")
    if not isinstance(context_classes, dict):
        raise ConfigurationError("context classes configuration must be a mapping")
    skill_catalog_config = context_classes.get("skill_catalog")
    skill_bodies_config = context_classes.get("skill_bodies")
    if not isinstance(skill_catalog_config, dict) or not isinstance(skill_bodies_config, dict):
        raise ConfigurationError("skill context configuration must be a mapping")
    connect_timeout = mcp_config.get("connect_timeout_seconds")
    if not isinstance(connect_timeout, (int, float)) or isinstance(connect_timeout, bool):
        raise ConfigurationError("MCP connect timeout must be numeric")
    if connect_timeout <= 0:
        raise ConfigurationError("MCP connect timeout must be positive")
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
        execution_environment: ExecutionServiceClient | DockerExecutionEnvironment
        if settings.execution_service_socket is not None:
            execution_environment = ExecutionServiceClient(settings.execution_service_socket)
            image_resolver = partial(
                execution_environment.resolve_image_digest,
                settings.sandbox_image,
            )
        else:
            execution_environment = DockerExecutionEnvironment(
                clock, ids, runtime="runsc" if settings.sandbox.value == "gvisor" else None
            )
            image_resolver = partial(resolve_local_image_digest, settings.sandbox_image)
        sandbox_manager = SandboxManager(
            execution_environment,
            resolve_image_digest=image_resolver,
            limits=sandbox_limits,
            egress=egress,
            parent_environment=os.environ,
            passthrough_names=settings.sandbox_passthrough,
        )
    else:
        raise ConfigurationError("microvm sandbox adapter is not configured in this deployment")
    estimator = ConservativeTokenEstimator()
    working_state = WorkingStateManager(clock, working_config, estimator)
    memory_retriever = HybridMemoryRetriever(
        uow_factory,
        clock,
        ids,
        principal,
        profile=memory_profiles.retrieval,
        trace_retention=memory_profiles.traces,
    )
    episode_search = EventEpisodeSearch(uow_factory, principal)
    query_former = DeterministicQueryFormer(principal)
    schedule_service = ScheduleService(
        uow_factory=uow_factory,
        clock=clock,
        ids=ids,
        limits=schedule_definition_limits,
        wake_worker=schedule_notify,
    )
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
    if web_search_provider is not None:
        registry.register(WebSearchTool(web_search_provider))
    if web_fetch_provider is not None:
        registry.register(WebFetchTool(web_fetch_provider))
    if browser_provider is not None:
        registry.register(BrowserNavigateTool(browser_provider))
        registry.register(BrowserObserveTool(browser_provider))
        registry.register(BrowserActTool(browser_provider))
    if settings.delegation_enabled:
        registry.register(LegacyDelegateRunTool())
        registry.register(DelegateRunTool())
    if settings.schedule_api_enabled and settings.schedule_worker_enabled:
        registry.register(ScheduleCreateTool(schedule_service, agent, schedule_definition_limits))

    # A session keeps the exact tool version it was shown. Retain compatible
    # builtin history so a process upgrade cannot turn an advertised tool into
    # an unknown capability for an existing session.
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
    memory_extractor = None
    memory_policy_version = FORMATION_POLICY_VERSION
    memory_mode = settings.memory_provider_extraction_mode
    extraction_model: ResolvedModel | None = None
    evidence_build_ref: str | None = None
    evidence_corpus_sha256: str | None = None
    evidence_source: str | None = None
    selection_outcome = "disabled"
    selection_reason = "configured_off"
    if memory_mode is not MemoryProviderExtractionMode.OFF or memory_provider_evaluation_mode:
        try:
            if agent.model_policy in NON_ROUTED_MODEL_POLICIES:
                extraction_model = ResolvedModel(
                    provider="fake",
                    model="scripted",
                    credential_ref="fake",
                    policy_name=agent.model_policy,
                    resolved_at=clock.now(),
                )
            else:
                extraction_model = await model_router.resolve(
                    agent.model_policy,
                    tenant_id=principal.tenant_id,
                    required=frozenset({Capability.STRUCTURED_OUTPUT, Capability.STREAMING}),
                )
        except ConfigurationError:
            if (
                memory_provider_evaluation_mode
                or memory_mode is MemoryProviderExtractionMode.REQUIRED
            ):
                raise
            selection_outcome = "deterministic_fallback"
            selection_reason = "model_resolution_failed"
        if extraction_model is not None:
            extraction_provider = effective_providers.get(extraction_model.provider)
            provider_unavailable_reason = (
                "provider_unavailable"
                if extraction_provider is None
                else (
                    "provider_credential_unavailable"
                    if isinstance(extraction_provider, MissingCredentialProvider)
                    else None
                )
            )
            if provider_unavailable_reason is not None:
                if (
                    memory_provider_evaluation_mode
                    or memory_mode is MemoryProviderExtractionMode.REQUIRED
                ):
                    raise ConfigurationError(
                        "provider-backed memory extraction resolved to an unavailable "
                        "adapter or credential"
                    )
                selection_outcome = "deterministic_fallback"
                selection_reason = provider_unavailable_reason
            elif memory_provider_evaluation_mode:
                assert extraction_provider is not None
                memory_extractor = ProviderAssistedCandidateExtractor.for_evaluation(
                    provider=extraction_provider,
                    resolved_model=extraction_model,
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    principal=principal,
                    agent_id=agent.id,
                    agent_version=agent.version,
                    policy_profile=agent.policy_profile,
                    policy_version=ruleset.policy_version,
                    fallback=DeterministicCandidateExtractor(),
                )
                selection_outcome = "evaluation"
                selection_reason = "explicit_evaluation_mode"
            else:
                selected_evidence = None
                for evidence_path in provider_extraction_evidence_paths(settings):
                    try:
                        candidate_evidence = load_provider_extraction_evidence(evidence_path)
                    except ConfigurationError:
                        continue
                    if provider_extraction_evidence_matches(
                        candidate_evidence,
                        extraction_model,
                        agent.policy_profile,
                        ruleset.policy_version,
                    ):
                        selected_evidence = candidate_evidence
                        evidence_source = (
                            "operator"
                            if evidence_path == settings.memory_provider_extraction_evidence
                            else "release"
                        )
                        break
                if selected_evidence is None:
                    if memory_mode is MemoryProviderExtractionMode.REQUIRED:
                        raise ConfigurationError(
                            "provider-backed memory extraction requires matching "
                            "evaluation evidence"
                        )
                    selection_outcome = "deterministic_fallback"
                    selection_reason = "no_matching_evidence"
                else:
                    assert extraction_provider is not None
                    memory_extractor = ProviderAssistedCandidateExtractor(
                        provider=extraction_provider,
                        resolved_model=extraction_model,
                        uow_factory=uow_factory,
                        clock=clock,
                        ids=ids,
                        principal=principal,
                        agent_id=agent.id,
                        agent_version=agent.version,
                        policy_profile=agent.policy_profile,
                        policy_version=ruleset.policy_version,
                        evidence=selected_evidence,
                        fallback=DeterministicCandidateExtractor(),
                    )
                    memory_policy_version = PROVIDER_FORMATION_POLICY_VERSION
                    evidence_build_ref = selected_evidence.build_ref
                    evidence_corpus_sha256 = selected_evidence.corpus_sha256
                    selection_outcome = "activated"
                    selection_reason = "matching_evidence"
        if memory_provider_evaluation_mode:
            memory_policy_version = PROVIDER_FORMATION_POLICY_VERSION
    selection_identity = ":".join(
        (
            principal.tenant_id,
            principal.principal_id,
            str(agent.id),
            agent.version,
            agent.model_policy,
            agent.policy_profile,
            ruleset.policy_version,
            memory_mode.value,
            selection_outcome,
            selection_reason,
            evidence_source or "none",
            "none" if extraction_model is None else extraction_model.provider,
            "none" if extraction_model is None else extraction_model.model,
            evidence_build_ref or "none",
            evidence_corpus_sha256 or "none",
        )
    )
    selection_key = f"memory.provider_extraction.selection:v2:{selection_identity}"
    async with uow_factory() as uow:
        await uow.process_events.append(
            ProcessEvent(
                id=uuid5(NAMESPACE_URL, selection_key),
                event_type="memory.provider_extraction.selection",
                actor_type="composition-root",
                actor_id=principal.principal_id,
                payload={
                    "tenant_id": principal.tenant_id,
                    "principal_id": principal.principal_id,
                    "mode": memory_mode.value,
                    "outcome": selection_outcome,
                    "reason": selection_reason,
                    "agent_id": str(agent.id),
                    "agent_version": agent.version,
                    "policy_profile": agent.policy_profile,
                    "policy_version": ruleset.policy_version,
                    "model_policy": agent.model_policy,
                    "provider": None if extraction_model is None else extraction_model.provider,
                    "model": None if extraction_model is None else extraction_model.model,
                    "evidence_source": evidence_source,
                    "evidence_build_ref": evidence_build_ref,
                    "evidence_corpus_sha256": evidence_corpus_sha256,
                },
                derivation_key=selection_key,
                created_at=clock.now(),
            )
        )
    memory_service = GovernedMemoryService(
        uow_factory,
        clock,
        ids,
        principal,
        extractor=memory_extractor,
        policy_version=memory_policy_version,
        formation_profile=memory_profiles.formation,
        decay_tau_days=memory_profiles.retrieval.decay_tau_days,
        usage=memory_profiles.retrieval.usage,
    )
    registry.register(LegacyMemoryRememberTool(memory_service))
    registry.register(MemoryRememberTool(memory_service))
    registry.register(MemorySearchTool(memory_retriever))
    registry.register(MemoryRecallEpisodesTool(episode_search))
    mcp_runtime: MCPRuntime | None = None
    try:
        if mcp_clients is None:
            if mcp_scripts is not None:
                mcp_clients = ScriptedMCPClientFactory(dict(mcp_scripts))
            else:
                if any(config.transport is MCPTransport.HTTP for config in effective_mcp_configs):
                    mcp_proxy = await start_worker_egress_proxy(
                        egress,
                        tenant_id=principal.tenant_id,
                    )
                mcp_clients = SDKMCPClientFactory(
                    http_proxy_url=None if mcp_proxy is None else mcp_proxy.url
                )
        mcp_runtime = MCPRuntime(
            uow_factory,
            registry,
            mcp_clients,
            credential_resolver,
            clock,
            ids,
            connect_timeout_seconds=float(connect_timeout),
        )
        skill_catalogs = SkillCatalogService(
            uow_factory,
            skill_store,
            estimator,
            mcp_prompts=mcp_runtime.prompt_entries,
            maximum_entries=int(skill_catalog_config["max_items"]),
            maximum_tokens=int(skill_catalog_config["max_tokens"]),
            maximum_loaded=int(skill_bodies_config["max_items"]),
            maximum_body_tokens=int(skill_bodies_config["max_tokens"]),
        )
        # A session keeps the exact tool version it was shown. Retain compatible
        # builtin history so the 1.1.0 revision cannot turn an advertised
        # skill.load into an unknown capability for an existing session.
        registry.register(LegacySkillLoadTool(skill_catalogs))
        registry.register(SkillLoadTool(skill_catalogs))
        if settings.skill_authoring_enabled:
            registry.register(SkillManageTool(uow_factory, skill_store))
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
            skill_catalogs=skill_catalogs,
            memory_retriever=memory_retriever,
            session_tool_filter=_session_tool_filter(browser_provider),
        )

        async def session_project_scope(session_id: UUID) -> str:
            """Name the project a session belongs to, for formation and recall."""

            try:
                async with uow_factory() as uow:
                    session = await uow.sessions.get(session_id, principal)
            except NotFoundError:
                return DEFAULT_PROJECT_SCOPE
            return project_scope(session.metadata)

        context_builder = BudgetedContextBuilder(
            context_planner,
            estimator,
            clock,
            working_state,
            memory_retriever,
            query_former,
            session_project_scope,
        )
        compactor = StructuredCompactor(
            estimator,
            maximum_depth=int(summary_config["max_depth"]),
        )
        trajectory_artifact_store = LocalTrajectoryArtifactStore(artifact_root)
        general_artifact_store = FilesystemArtifactStore(
            artifact_root, maximum_bytes=artifact_maximum_bytes
        )
        knowledge_service = KnowledgeService(
            uow_factory,
            general_artifact_store,
            clock,
            ids,
            principal,
            trace_retention=memory_profiles.traces,
        )
        registry.register(KnowledgeIngestTool(knowledge_service, uow_factory))
        registry.register(KnowledgeSearchTool(knowledge_service))
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

        async def sweep_memory() -> int:
            return len(await memory_service.expire())

        async def sweep_traces() -> int:
            return await memory_service.expire_traces()

        async def sweep_memory_decay() -> int:
            if not memory_profiles.formation.scheduled_enabled:
                return 0
            result = await memory_service.decay()
            return result.decayed + result.retired

        async def sweep_memory_consolidation() -> int:
            if not memory_profiles.formation.session_boundary_enabled:
                return 0
            ready_at = clock.now()
            async with uow_factory() as uow:
                sessions = await uow.maintenance.pending_memory_sessions(
                    principal,
                    idle_before=ready_at - timedelta(seconds=SESSION_IDLE_SECONDS),
                    ready_at=ready_at,
                    limit=100,
                )
            completed = 0
            for session_id in sessions:
                try:
                    await memory_service.run(
                        trigger="session_idle",
                        scope=await session_project_scope(session_id),
                        session_id=session_id,
                    )
                except Exception:
                    logger.exception(
                        "memory_session_consolidation_failed",
                        extra={"session_id": str(session_id)},
                    )
                else:
                    completed += 1
            return completed

        policy_engine = DeterministicPolicyEngine(ruleset)
        standing_authorizer = None
        if settings.browser_grant_id is not None:
            if browser_provider is None or settings.browser_profile_id is None:
                raise ConfigurationError("standing browser grant composition is incomplete")
            standing_authorizer = ConfiguredBrowserStandingAuthorizer(
                grant_id=settings.browser_grant_id,
                profile_id=settings.browser_profile_id,
                purpose=settings.browser_run_purpose,
                provider=browser_provider,
                uow_factory=uow_factory,
                policy=policy_engine,
                now=clock.now,
            )
        checkpoint_seeder = DurableCheckpointSeeder(clock)
        delegation_materializer = (
            DelegationMaterializer(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                seed_checkpoint=checkpoint_seeder,
                defaults=delegation_defaults,
                caps=delegation_caps,
            )
            if settings.delegation_enabled
            else None
        )
        pipeline = ToolPipeline(
            registry,
            uow_factory,
            clock,
            ids,
            policy=policy_engine,
            workspace_factory=sandbox_manager,
            artifact_writers=artifact_writers,
            current_principal=principal,
            max_parallel_calls=max_parallel_calls,
            hard_ceiling_multiplier=hard_ceiling_multiplier,
            maximum_loaded_skills=int(skill_bodies_config["max_items"]),
            maximum_skill_body_tokens=int(skill_bodies_config["max_tokens"]),
            approval_expiry_seconds=dict(ruleset.approval_expiry_seconds),
            standing_authorizer=standing_authorizer,
            delegations=delegation_materializer,
        )
        token_slot = _ActiveToken()
        notification_producer = (
            NotificationProducer(clock=clock, ids=ids)
            if settings.notification_dispatch_enabled
            else None
        )
        schedule_accountant = ScheduleOutcomeAccountant(
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            metrics=schedule_metrics,
            notification_producer=notification_producer,
        )
        principal_resolver = StaticPrincipalResolver(principal)
        skill_reviews: SkillBackgroundReview | None = None
        delegation_joins: DelegationJoin | None = None

        async def on_child_suspension(run_id: UUID, delegation_id: UUID) -> None:
            if delegation_joins is not None:
                await delegation_joins.parent_parked(run_id, delegation_id)

        async def complete_run_resources(run_id: UUID, lease_epoch: int | None) -> None:
            try:
                await sandbox_manager.release_run(run_id, lease_epoch)
            except Exception:
                logger.exception("run_resource_cleanup_failed", extra={"run_id": str(run_id)})
            try:
                await schedule_accountant.account(run_id)
            except Exception:
                logger.exception("schedule_run_accounting_failed", extra={"run_id": str(run_id)})
            completed: Run | None = None
            try:
                async with uow_factory() as uow:
                    run = await uow.runs.get(run_id, principal)
                    if run.status not in TERMINAL_RUN_STATUSES:
                        return
                    completed = run
                    await uow.events.append(
                        NewEvent(
                            session_id=run.session_id,
                            run_id=run.id,
                            event_type="memory.formation.requested",
                            actor_type="runtime",
                            payload={
                                "trigger": "run_terminal",
                                "terminal_status": run.status.value,
                                "not_before": (
                                    clock.now() + timedelta(seconds=SESSION_IDLE_SECONDS)
                                ).isoformat(),
                            },
                            derivation_key=f"memory.formation.requested:{run.id}",
                        )
                    )
            except Exception:
                logger.exception("memory_formation_enqueue_failed", extra={"run_id": str(run_id)})
            # What the answer cited is only knowable once the answer exists, so
            # usage feedback is the completion's own step: its own error
            # boundary, its own units of work, and no external call inside one.
            if (
                completed is not None
                and completed.status is RunStatus.COMPLETED
                and completed.final_message
            ):
                try:
                    plan = await context_planner.current(completed.session_id)
                    await memory_service.record_usage(
                        session_id=completed.session_id,
                        run_id=completed.id,
                        final_text=completed.final_message,
                        snapshot_trace_id=None if plan is None else plan.snapshot_id,
                    )
                except Exception:
                    logger.exception("memory_usage_feedback_failed", extra={"run_id": str(run_id)})
            if delegation_joins is not None:
                await delegation_joins.after_run(run_id)
            if skill_reviews is not None:
                await skill_reviews.after_run(run_id)

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
            on_run_complete=complete_run_resources,
            on_child_suspension=on_child_suspension,
            on_model_event=lambda run, event: _publish_model_event(
                live_events, run.session_id, run.id, event
            ),
            max_internal_attempts=max_internal_attempts,
            identical_call_threshold=identical_call_threshold,
            identical_denial_threshold=identical_denial_threshold,
            max_compactions_per_step=max_compactions_per_step,
            notification_producer=notification_producer,
        )
        dispatcher = (
            InlineRunDispatcher(executor.execute, unit_of_work_open=uow_factory.is_open)
            if storage == "memory"
            else PostgresRunDispatcher()
        )
        if settings.delegation_enabled:
            delegation_joins = DelegationJoin(
                uow_factory=uow_factory,
                dispatcher=dispatcher,
                requeue_parent=executor.requeue_after_child,
                fail_parent_on_budget=executor.fail_suspended_on_budget,
                clock=clock,
                ids=ids,
                principal=principal,
                summary_max_bytes=delegation_caps.summary_max_bytes,
            )
        skill_reviews = SkillBackgroundReview(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            catalogs=skill_catalogs,
            principal=principal,
            clock=clock,
            seed_checkpoint=checkpoint_seeder,
            activate_session=mcp_runtime.activate_session,
            enabled=settings.skill_background_review_enabled,
            redactor=trajectory_redactor,
        )
        session_service = SessionService(
            uow_factory,
            clock,
            ids,
            principal,
            agent,
            catalogs=skill_catalogs,
            activate_session=mcp_runtime.activate_session,
            close_session=mcp_runtime.close_session,
        )
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
            on_parked_cancelled=(None if delegation_joins is None else delegation_joins.after_run),
        )
        approval_service = ApprovalService(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            principal=principal,
            clock=clock,
            resume_waiting_run=executor.requeue_after_approval,
            self_approval_enabled=ruleset.self_approval_enabled,
        )

        async def consolidate_closed_session(session_id: UUID) -> None:
            if not memory_profiles.formation.session_boundary_enabled:
                return
            await memory_service.run(
                trigger="session_close",
                scope=await session_project_scope(session_id),
                session_id=session_id,
            )

        public_session_service = PublicSessionService(
            uow_factory,
            clock,
            ids,
            agent,
            catalogs=skill_catalogs,
            activate_session=mcp_runtime.activate_session,
            close_session=mcp_runtime.close_session,
            on_session_closed=consolidate_closed_session,
            trajectory_artifacts=trajectory_artifact_store,
            general_artifacts=general_artifact_store,
        )

        async def sweep_session_deletions() -> int:
            return await public_session_service.purge_pending_artifacts(principal)

        browser_uow_factory = cast(BrowserUnitOfWorkFactory, uow_factory)
        browser_profile_service = BrowserProfileManagementService(
            uow_factory=browser_uow_factory,
            lifecycle=browser_profile_lifecycle,
            authentications=browser_authentications,
            clock=clock,
            ids=ids,
        )
        browser_grant_service = BrowserGrantManagementService(
            uow_factory=browser_uow_factory,
            clock=clock,
            ids=ids,
            agent_version=agent.version,
            policy_version=ruleset.policy_version,
        )

        device_service = DeviceManagementService(
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            notification_expiry_seconds=notification_expiry_seconds,
        )
        notification_inbox = NotificationInboxService(uow_factory=uow_factory)
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
            browser_profiles=browser_profile_service,
            browser_grants=browser_grant_service,
            schedules=schedule_service,
            devices=device_service,
            notifications=notification_inbox,
            memory=PublicMemoryService(uow_factory=uow_factory),
        )
        request_ids = UUID7RequestIdFactory(clock, RandomIdFactory())

        def schedule_worker_factory() -> WorkerService:
            if not settings.schedule_worker_enabled:
                raise ConfigurationError(
                    "schedule worker is disabled; set AGENT_SCHEDULE_WORKER_ENABLED=1"
                )
            if settings.deployment_mode is DeploymentMode.PRODUCTION:
                raise ConfigurationError(
                    "production schedule workers require the lean scheduler composition"
                )
            materializer = ScheduleMaterializer(
                uow_factory=uow_factory,
                principals=ConfiguredSchedulePrincipalDirectory(principal),
                clock=clock,
                ids=ids,
                seed_checkpoint=checkpoint_seeder,
                metrics=schedule_metrics,
                notification_producer=notification_producer,
            )
            return ScheduleWorker(
                uow_factory=uow_factory,
                materialize=materializer.materialize,
                clock=clock,
                scan_batch=schedule_scan_batch,
                fallback_poll_seconds=schedule_fallback_poll_seconds,
                admission_backoff_seconds=schedule_admission_backoff_seconds,
                wait_for_wakeup=schedule_wait,
                metrics=schedule_metrics,
            )

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
                schedules=schedule_service,
                trajectories=trajectory_service,
                executor=executor,
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                worker_factory=lambda worker_id: DurableWorker(
                    uow_factory=uow_factory,
                    executor=executor,
                    clock=clock,
                    worker_id=worker_id,
                    eligible_classes=(interactive_priority,),
                    interactive_priority=interactive_priority,
                    async_priority=async_priority,
                    lease_seconds=lease_seconds,
                    heartbeat_divisor=heartbeat_divisor,
                    poll_interval_seconds=worker_poll_interval,
                    record_claim_metric=lambda worker_class, duration_seconds: (
                        schedule_metrics.record_claim(
                            worker_class=worker_class,
                            duration_seconds=duration_seconds,
                        )
                    ),
                ),
                async_worker_factory=lambda worker_id: DurableWorker(
                    uow_factory=uow_factory,
                    executor=executor,
                    clock=clock,
                    worker_id=worker_id,
                    eligible_classes=(async_priority,),
                    interactive_priority=interactive_priority,
                    async_priority=async_priority,
                    lease_seconds=lease_seconds,
                    heartbeat_divisor=heartbeat_divisor,
                    poll_interval_seconds=worker_poll_interval,
                    record_claim_metric=lambda worker_class, duration_seconds: (
                        schedule_metrics.record_claim(
                            worker_class=worker_class,
                            duration_seconds=duration_seconds,
                        )
                    ),
                ),
                maintenance_factory=lambda: MaintenanceWorker(
                    uow_factory=uow_factory,
                    clock=clock,
                    sweep_approvals=approval_service.expire_due,
                    sweep_exports=trajectory_service.sweep_once,
                    sweep_artifacts=artifact_writers.sweep_expired,
                    sweep_sandboxes=None if storage == "memory" else sandbox_manager.reap,
                    sweep_artifact_orphans=reconcile_artifact_orphans,
                    sweep_memory=sweep_memory,
                    sweep_traces=sweep_traces,
                    sweep_memory_consolidation=sweep_memory_consolidation,
                    sweep_memory_decay=sweep_memory_decay,
                    sweep_session_deletions=sweep_session_deletions,
                    memory_decay_interval_seconds=(
                        memory_profiles.formation.scheduled_interval_seconds
                    ),
                ),
                schedule_worker_factory=schedule_worker_factory,
                sandbox=sandbox_manager,
                mcp=mcp_runtime,
                skill_catalogs=skill_catalogs,
                skill_reviews=skill_reviews,
                tool_pipeline=pipeline,
                memory=memory_service,
                memory_retriever=memory_retriever,
                memory_profiles=memory_profiles,
                knowledge=knowledge_service,
                mcp_proxy=mcp_proxy,
            ),
            list(effective_providers.values()),
        )
    except BaseException:
        if mcp_runtime is not None:
            with suppress(BaseException):
                await mcp_runtime.close()
        if mcp_proxy is not None:
            with suppress(BaseException):
                await mcp_proxy.close()
        with suppress(BaseException):
            await sandbox_manager.close()
        for provider in effective_providers.values():
            with suppress(BaseException):
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


def _web_provider(
    kind: WebProviderKind,
    credentials: CredentialResolver,
) -> WebProvider | None:
    if kind is WebProviderKind.DISABLED:
        return None
    if kind is WebProviderKind.TAVILY:
        return TavilyWebProvider(credentials=credentials)
    if kind is WebProviderKind.FIRECRAWL:
        return FirecrawlWebProvider(credentials=credentials)
    raise ConfigurationError(f"unsupported web provider {kind.value!r}")


def _browser_provider(
    kind: BrowserProviderKind,
    *,
    principal: Principal,
    allowed_origins: tuple[str, ...],
    profile_id: UUID | None,
    profiles: UnitOfWorkFactory,
    sessions: BrowserSessionControlPlane | None,
    now: Callable[[], datetime],
) -> BrowserProvider | None:
    if kind is BrowserProviderKind.DISABLED:
        return None
    if kind is BrowserProviderKind.PLAYWRIGHT:
        return PlaywrightBrowserProvider(
            tenant_id=principal.tenant_id,
            allowed_origins=allowed_origins,
        )
    if kind is BrowserProviderKind.HOSTED:
        if sessions is None:
            raise ConfigurationError("hosted browser provider composition is incomplete")

        async def load_profile(
            owner: Principal,
            requested_profile_id: UUID,
        ) -> BrowserProfile:
            async with profiles() as uow:
                return await uow.browser_profiles.get(requested_profile_id, owner)

        if profile_id is not None:
            return HostedBrowserProvider(
                principal=principal,
                profile_id=profile_id,
                allowed_origins=allowed_origins,
                profiles=load_profile,
                sessions=sessions,
            )

        async def select_session_profile(context: ToolExecutionContext) -> UUID:
            async with profiles() as uow:
                session = await uow.sessions.get(context.session_id, context.principal)
            value = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
            if not isinstance(value, str):
                raise NotFoundError("browser profile binding not found")
            return UUID(value)

        return SessionBoundHostedBrowserProvider(
            principal=principal,
            profiles=load_profile,
            profile_selector=select_session_profile,
            sessions=sessions,
            now=now,
        )
    raise ConfigurationError(f"unsupported browser provider {kind.value!r}")


def _effective_model_policy(
    deployment_mode: DeploymentMode,
    requested_policy: str | None,
) -> str:
    """Keep deterministic development while refusing a fake production default."""

    if requested_policy is not None:
        return requested_policy
    if deployment_mode is DeploymentMode.PRODUCTION:
        return "balanced"
    return "fake-balanced"


@asynccontextmanager
async def build(
    *,
    settings: Settings | None = None,
    script: FakeModelScript | None = None,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
    limits: RunLimits | None = None,
    enabled_tools: list[str] | None = None,
    enabled_skills: list[str] | None = None,
    skill_packages: tuple[tuple[SkillPackage, SkillSource], ...] = (),
    mcp_servers: tuple[MCPServerConfig, ...] = (),
    mcp_client_factory: MCPClientFactory | None = None,
    mcp_scripts: Mapping[str, ScriptedMCPServer] | None = None,
    credential_resolver: CredentialResolver | None = None,
    principal: Principal | None = None,
    policy_profile: str = "default",
    fixed_clock_at: datetime | None = None,
    sequential_ids: bool = False,
    storage: Literal["memory", "postgres"] = "memory",
    model_policy: str | None = None,
    model_provider_overrides: Mapping[str, ModelProvider] | None = None,
    web_search_provider_override: WebProvider | None = None,
    web_fetch_provider_override: WebProvider | None = None,
    browser_provider_override: BrowserProvider | None = None,
    trajectory_redactor: TrajectoryRedactor | None = None,
    memory_provider_evaluation_mode: bool = False,
) -> AsyncIterator[Composition]:
    """Construct and own a Milestone 3 application graph for one process role."""

    # Phase 1: refusal. Loading Settings enforces production sandbox and auth rules.
    effective_settings = settings or load_settings()
    validate_settings(effective_settings)
    if (
        memory_provider_evaluation_mode
        and effective_settings.memory_provider_extraction_mode
        is MemoryProviderExtractionMode.REQUIRED
    ):
        raise ConfigurationError(
            "provider memory extraction evaluation and activation are mutually exclusive"
        )
    if (
        memory_provider_evaluation_mode
        and effective_settings.deployment_mode is DeploymentMode.PRODUCTION
    ):
        raise ConfigurationError(
            "provider memory extraction evaluation mode is unavailable in production"
        )
    if (
        effective_settings.deployment_mode is DeploymentMode.PRODUCTION
        and (effective_settings.schedule_api_enabled or effective_settings.schedule_worker_enabled)
        and storage != "postgres"
    ):
        raise ConfigurationError("production scheduling requires PostgreSQL storage")
    if (
        effective_settings.deployment_mode is DeploymentMode.PRODUCTION
        and (
            effective_settings.notification_api_enabled
            or effective_settings.notification_dispatch_enabled
        )
        and storage != "postgres"
    ):
        raise ConfigurationError("production notifications require PostgreSQL storage")
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
    supplied_email_rows = tuple(
        config
        for config in mcp_servers
        if config.server_id in {"gmail_read", "gmail_write", "gmail_send"}
    )
    if supplied_email_rows:
        raise ConfigurationError(
            "first-party Gmail MCP rows are composed only through AGENT_EMAIL_ENABLED"
        )
    composed_email_rows = email_server_configs(
        effective_principal.tenant_id,
        enabled=effective_settings.email_enabled,
    )
    effective_mcp_servers = (*mcp_servers, *composed_email_rows)
    if composed_email_rows:
        effective_principal = effective_principal.model_copy(
            update={
                "scopes": {
                    *effective_principal.scopes,
                    *(scope for row in composed_email_rows for scope in row.required_scopes),
                }
            },
            deep=True,
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
    memory_profiles = MemoryProfiles.from_document(
        load_config_document(effective_settings, "memory/profiles.yaml")
    )
    run_defaults = runtime_config["run_defaults"]
    model_limits = runtime_config["model"]
    queue_config = runtime_config["queue"]
    worker_config = runtime_config["worker"]
    scheduling_config = runtime_config["scheduling"]
    notification_config = runtime_config["notifications"]
    schedule_admission_limits = ScheduleAdmissionLimits.model_validate(
        {
            "max_active_runs_per_tenant": scheduling_config["max_active_runs_per_tenant"],
            "max_materializations_per_minute": scheduling_config["max_materializations_per_minute"],
            "daily_cost": scheduling_config["daily_cost"],
            "monthly_cost": scheduling_config["monthly_cost"],
        }
    )
    schedule_definition_limits = ScheduleDefinitionLimits.model_validate(
        {
            "max_run_timeout_seconds": scheduling_config["max_run_timeout_seconds"],
            "max_misfire_grace_seconds": scheduling_config["max_misfire_grace_seconds"],
            "max_steps_per_run": scheduling_config["max_steps_per_run"],
            "max_model_calls_per_run": scheduling_config["max_model_calls_per_run"],
            "max_tool_calls_per_run": scheduling_config["max_tool_calls_per_run"],
            "max_cost_per_run": scheduling_config["max_cost_per_run"],
        }
    )
    circuit_breaker = tool_config["circuit_breaker"]
    parallel = tool_config["parallel"]
    output_config = tool_config["output"]
    delegation_config = runtime_config["delegation"]
    delegation_defaults = DelegationDefaults(
        max_steps=int(delegation_config["child_max_steps"]),
        max_model_calls=int(delegation_config["child_max_model_calls"]),
        max_tool_calls=int(delegation_config["child_max_tool_calls"]),
        max_cost=Decimal(str(delegation_config["child_max_cost"])),
        wall_seconds=int(delegation_config["child_wall_seconds"]),
        synthesis_reserve_steps=int(delegation_config["synthesis_reserve_steps"]),
        synthesis_reserve_model_calls=int(delegation_config["synthesis_reserve_model_calls"]),
        synthesis_reserve_cost=Decimal(str(delegation_config["synthesis_reserve_cost"])),
    )
    delegation_caps = DelegationCaps(
        max_children_per_call=int(delegation_config["max_children_per_call"]),
        max_live_children_per_parent=int(delegation_config["max_live_children_per_parent"]),
        max_depth=int(delegation_config["max_depth"]),
        max_live_delegated_runs_per_tenant=int(
            delegation_config["max_live_delegated_runs_per_tenant"]
        ),
        summary_max_bytes=int(delegation_config["summary_max_bytes"]),
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
    model_router = StaticModelRouter(provider_registry, effective_clock)

    # Phase 3: resources. PostgreSQL is selected explicitly by normal process roles;
    # deterministic evaluation keeps the contract-backed in-memory tier.
    effective_model_policy = _effective_model_policy(
        effective_settings.deployment_mode,
        model_policy,
    )
    web_search_enabled = (
        web_search_provider_override is not None
        or effective_settings.web_search_provider is not WebProviderKind.DISABLED
    )
    web_fetch_enabled = (
        web_fetch_provider_override is not None
        or effective_settings.web_fetch_provider is not WebProviderKind.DISABLED
    )
    browser_enabled = (
        browser_provider_override is not None
        or effective_settings.browser_provider is not BrowserProviderKind.DISABLED
    )
    default_enabled_tools = [
        "math.calculate",
        "conversation.ask_user",
        "system.current_time",
        "workspace.read_text",
        "workspace.write_text",
        "workspace.list_files",
        *(
            []
            if web_search_enabled or web_fetch_enabled or browser_enabled
            else ["demo.external_write"]
        ),
        "sandbox.run_command",
        "artifact.export",
        WORKING_STATE_TOOL_NAME,
        SKILL_LOAD_TOOL_NAME,
        "memory.remember",
        "memory.search",
        "memory.recall_episodes",
        *([] if web_search_enabled and web_fetch_enabled else ["knowledge.ingest"]),
        "knowledge.search",
        *(["web.search"] if web_search_enabled else []),
        *(["web.fetch"] if web_fetch_enabled else []),
        *(
            [SCHEDULE_CREATE_TOOL_NAME]
            if effective_settings.schedule_api_enabled
            and effective_settings.schedule_worker_enabled
            else []
        ),
        *(["browser.navigate", "browser.observe", "browser.act"] if browser_enabled else []),
        *(["delegate.run"] if effective_settings.delegation_enabled else []),
    ]
    agent = AgentSpec(
        id=DEFAULT_AGENT_ID if storage == "postgres" else effective_ids.new_id(),
        version=(
            "1.0.0"
            if model_policy is None
            else f"1.0.0+model.{effective_model_policy.replace('_', '-')}"
        ),
        name="Milestone 9 Agent",
        instructions=DEFAULT_AGENT_INSTRUCTIONS,
        model_policy=effective_model_policy,
        enabled_tools=enabled_tools if enabled_tools is not None else default_enabled_tools,
        enabled_skills=list(enabled_skills or []),
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
    web_providers: list[WebProvider] = []
    browser_provider: BrowserProvider | None = None
    browser_profile_http_client: httpx.AsyncClient | None = None
    browser_sessions: BrowserSessionControlPlane | None = None
    live_events: LiveEventBroadcaster = (
        InMemoryLiveEventBroadcaster()
        if storage == "memory"
        else PostgresLiveEventBroadcaster(effective_settings.database_url)
    )
    schedule_wakeup = (
        InMemoryScheduleWakeup()
        if storage == "memory"
        else PostgresScheduleWakeup(effective_settings.database_url)
    )
    schedule_metrics = ScheduleMetrics(
        tenant_hash_key=tenant_hash_key(effective_settings.database_url)
    )
    composition: Composition | None = None
    skill_validator = SkillPackageValidator(ConservativeTokenEstimator())
    skill_store: SkillPackageStore
    if mcp_client_factory is not None and mcp_scripts is not None:
        raise ValueError("mcp_client_factory and mcp_scripts are mutually exclusive")
    try:
        if storage == "memory":
            agent_repository = InMemoryAgentRepository()
            session_repository = InMemorySessionRepository()
            run_repository = InMemoryRunRepository(session_repository, effective_clock)
            event_repository = InMemoryEventRepository(session_repository, effective_clock)
            invocation_repository = InMemoryToolInvocationRepository(run_repository)
            approval_repository = InMemoryApprovalRepository(effective_clock)
            skill_store = InMemorySkillPackageStore()
            skill_repository = InMemorySkillRepository(
                skill_store,
                skill_validator,
                effective_clock,
                effective_ids,
            )
            mcp_repository = InMemoryMCPServerRepository()
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
                        skills=skill_repository,
                        mcp_servers=mcp_repository,
                        clock=effective_clock,
                    )
                ),
            )
        else:
            skill_store = FilesystemSkillPackageStore(
                effective_settings.artifact_root / "skill-packages"
            )
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
                        skill_store=skill_store,
                        skill_validator=skill_validator,
                        ids=effective_ids,
                        schedule_admission_limits=schedule_admission_limits,
                        schedule_metrics=schedule_metrics,
                    ),
                    effective_principal.tenant_id,
                ),
            )
        provider_adapters = _provider_adapters(effective_settings, provider_registry)
        effective_credential_resolver = credential_resolver or MappingCredentialResolver(
            {
                name: secret.get_secret_value()
                for name, secret in effective_settings.credentials.items()
            }
        )
        if effective_settings.browser_profile_service_url is None:
            browser_profile_lifecycle: BrowserProfileControlPlane = (
                UnavailableBrowserProfileControlPlane()
            )
            browser_authentications: BrowserAuthenticationControlPlane = (
                UnavailableBrowserAuthenticationControlPlane()
            )
        else:
            # Public profile management uses this control plane even when browser
            # tools are disabled, so the client is gated by the service URL.
            browser_profile_http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            )
            browser_profile_lifecycle = HostedBrowserProfileControlPlane(
                base_url=effective_settings.browser_profile_service_url,
                credentials=effective_credential_resolver,
                client=browser_profile_http_client,
            )
            hosted_browser_sessions = HostedBrowserSessionControlPlane(
                base_url=effective_settings.browser_profile_service_url,
                credentials=effective_credential_resolver,
                client=browser_profile_http_client,
            )
            browser_authentications = hosted_browser_sessions
            browser_sessions = hosted_browser_sessions
        web_search_provider = (
            web_search_provider_override
            if web_search_provider_override is not None
            else _web_provider(
                effective_settings.web_search_provider,
                effective_credential_resolver,
            )
        )
        if web_search_provider is not None:
            web_providers.append(web_search_provider)
        web_fetch_provider = (
            web_fetch_provider_override
            if web_fetch_provider_override is not None
            else (
                web_search_provider
                if web_search_provider_override is None
                and web_search_provider is not None
                and effective_settings.web_fetch_provider is effective_settings.web_search_provider
                else _web_provider(
                    effective_settings.web_fetch_provider,
                    effective_credential_resolver,
                )
            )
        )
        if web_fetch_provider is not None and web_fetch_provider is not web_search_provider:
            web_providers.append(web_fetch_provider)
        browser_provider = (
            browser_provider_override
            if browser_provider_override is not None
            else _browser_provider(
                effective_settings.browser_provider,
                principal=effective_principal,
                allowed_origins=effective_settings.browser_allowed_origins,
                profile_id=effective_settings.browser_profile_id,
                profiles=uow_factory,
                sessions=browser_sessions,
                now=effective_clock.now,
            )
        )
        for package, source in skill_packages:
            async with uow_factory() as uow:
                await uow.skills.install(
                    effective_principal.tenant_id,
                    package,
                    source,
                    None,
                    None,
                )
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
            interactive_priority=int(queue_config["priorities"]["interactive"]),
            async_priority=int(queue_config["priorities"]["async"]),
            schedule_scan_batch=int(scheduling_config["scan_batch"]),
            schedule_fallback_poll_seconds=float(scheduling_config["fallback_poll_seconds"]),
            schedule_admission_backoff_seconds=float(
                scheduling_config["admission_backoff_seconds"]
            ),
            schedule_definition_limits=schedule_definition_limits,
            delegation_defaults=delegation_defaults,
            delegation_caps=delegation_caps,
            notification_expiry_seconds=float(notification_config["terminal_expiry_seconds"]),
            schedule_notify=schedule_wakeup.notify,
            schedule_wait=schedule_wakeup.wait,
            schedule_metrics=schedule_metrics,
            ruleset=ruleset,
            live_events=live_events,
            context_config=context_config,
            memory_profiles=memory_profiles,
            mcp_config=tool_config["mcp"],
            max_compactions_per_step=int(runtime_config["context"]["max_compactions_per_step"]),
            skill_store=skill_store,
            mcp_clients=mcp_client_factory,
            mcp_scripts=mcp_scripts,
            credential_resolver=effective_credential_resolver,
            mcp_server_configs=effective_mcp_servers,
            web_search_provider=web_search_provider,
            web_fetch_provider=web_fetch_provider,
            memory_provider_evaluation_mode=memory_provider_evaluation_mode,
            browser_provider=browser_provider,
            browser_profile_lifecycle=browser_profile_lifecycle,
            browser_authentications=browser_authentications,
        )
        yield composition
    finally:
        if composition is not None:
            try:
                await composition.mcp.close()
            except Exception as exc:
                logger.warning("mcp_close_failed", extra={"error_class": type(exc).__name__})
            try:
                await composition.sandbox.close()
            except Exception as exc:
                logger.warning("sandbox_close_failed", extra={"error_class": type(exc).__name__})
            if composition.mcp_proxy is not None:
                try:
                    await composition.mcp_proxy.close()
                except Exception as exc:
                    logger.warning(
                        "mcp_proxy_close_failed",
                        extra={"error_class": type(exc).__name__},
                    )
        for model_provider in model_providers:
            try:
                await model_provider.close()
            except Exception as exc:
                logger.warning(
                    "model_provider_close_failed",
                    extra={"error_class": type(exc).__name__},
                )
        for web_provider in web_providers:
            try:
                await web_provider.close()
            except Exception as exc:
                logger.warning(
                    "web_provider_close_failed",
                    extra={"provider": web_provider.name, "error_class": type(exc).__name__},
                )
        if browser_provider is not None:
            try:
                await browser_provider.close()
            except Exception as exc:
                logger.warning(
                    "browser_provider_close_failed",
                    extra={
                        "provider": browser_provider.name,
                        "error_class": type(exc).__name__,
                    },
                )
        if browser_profile_http_client is not None:
            try:
                await browser_profile_http_client.aclose()
            except Exception as exc:
                logger.warning(
                    "browser_profile_client_close_failed",
                    extra={"error_class": type(exc).__name__},
                )
        try:
            await live_events.close()
        except Exception as exc:
            logger.warning("live_events_close_failed", extra={"error_class": type(exc).__name__})
        try:
            await schedule_wakeup.close()
        except Exception as exc:
            logger.warning(
                "schedule_wakeup_close_failed", extra={"error_class": type(exc).__name__}
            )
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                logger.warning(
                    "database_engine_close_failed",
                    extra={"error_class": type(exc).__name__},
                )
