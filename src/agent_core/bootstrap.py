"""The sole composition root: refuse, determinism, resources, freeze, wire."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.functions import func

from agent_core.adapters.apns import APNsPushTransport
from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.artifacts.inspection import SignatureAttachmentInspector
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
from agent_core.adapters.browser.task_grants import InMemoryBrowserTaskGrantRepository
from agent_core.adapters.browser.unavailable import (
    UnavailableBrowserAuthenticationControlPlane,
    UnavailableBrowserProfileControlPlane,
)
from agent_core.adapters.cockpit.builder import SandboxWidgetBuilder, cockpit_build_sandbox
from agent_core.adapters.cockpit.git import FakeWidgetRepository
from agent_core.adapters.cockpit.github import (
    GitHubAppTokens,
    GitHubWidgetRepository,
    load_app_private_key,
)
from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck, FileCockpitRuntime
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.determinism import (
    FixedClock,
    RandomIdFactory,
    SequenceIdFactory,
    SystemClock,
    UUID7RequestIdFactory,
    elapsed_time,
    heartbeat_wait_for,
)
from agent_core.adapters.device_channel import PushWakeDeviceChannel
from agent_core.adapters.dispatch.inline import InlineRunDispatcher
from agent_core.adapters.dispatch.postgres import PostgresRunDispatcher
from agent_core.adapters.embeddings.openai import OpenAIEmbeddingProvider
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.adapters.execution.jobs import SandboxJobRunner
from agent_core.adapters.execution.service import ExecutionServiceClient, ExecutionServiceServer
from agent_core.adapters.identity import (
    ConfiguredSchedulePrincipalDirectory,
    StaticPrincipalResolver,
)
from agent_core.adapters.jobs.credentials import GitHubReaderCredentials
from agent_core.adapters.jobs.http import JobHTTP
from agent_core.adapters.jobs.screen import SandboxJobScreen
from agent_core.adapters.jobs.stored_credentials import StoredJobCredentials
from agent_core.adapters.judgment import TypeSafeJudgmentProvider
from agent_core.adapters.knowledge.pdf import PdfTextExtractor
from agent_core.adapters.live_events import (
    InMemoryLiveEventBroadcaster,
    PostgresLiveEventBroadcaster,
)
from agent_core.adapters.mcp.calls import BlandCallProvider
from agent_core.adapters.mcp.memory import InMemoryMCPServerRepository
from agent_core.adapters.mcp.oauth import HTTPMCPOAuthGateway
from agent_core.adapters.mcp.persistence import PostgresMCPServerRepository
from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.mcp.sealing import AESGCMSecretSealer
from agent_core.adapters.media.tensorscale import TensorScaleMediaProvider
from agent_core.adapters.memory.dreaming import InMemoryDreamingSchedule
from agent_core.adapters.memory.in_memory import (
    InMemoryIntegratedEpisodeStore,
    InMemoryKnowledgeStore,
    InMemoryMemoryStore,
    InMemoryTraceStore,
)
from agent_core.adapters.memory.reconsolidation import InMemoryReconsolidationStore
from agent_core.adapters.memory.reconsolidation_evidence import ReconsolidationEvidence
from agent_core.adapters.memory.reconsolidation_index import ReconsolidationIndex
from agent_core.adapters.memory.semantic_memory import InMemorySemanticMemoryIndex
from agent_core.adapters.memory.semantic_work import InMemorySemanticWorkStore
from agent_core.adapters.memory.transactions import MemoryTransaction
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.adapters.models.unavailable import MissingCredentialProvider
from agent_core.adapters.notification_wakeup import PostgresNotificationWakeup
from agent_core.adapters.persistence.calls import InMemoryCallStore, PostgresCallStore
from agent_core.adapters.persistence.cockpit import (
    InMemoryCockpitEventLog,
    InMemoryFeedStore,
    InMemoryWidgetCommitOutbox,
    InMemoryWidgetStore,
    PostgresCockpitEventLog,
    PostgresFeedStore,
    PostgresWidgetCommitOutbox,
    PostgresWidgetStore,
)
from agent_core.adapters.persistence.database import (
    assert_schema_revision,
    create_engine,
    create_session_factory,
)
from agent_core.adapters.persistence.delegations import (
    InMemoryDelegationRepository,
    PostgresDelegationRepository,
)
from agent_core.adapters.persistence.device_channel import (
    InMemoryDeviceIngestStore,
    InMemoryDeviceInvocationStore,
    PostgresDeviceIngestStore,
    PostgresDeviceInvocationStore,
)
from agent_core.adapters.persistence.dreaming import PostgresDreamingSchedule
from agent_core.adapters.persistence.email import InMemoryEmailStore, PostgresEmailStore
from agent_core.adapters.persistence.folder_repositories import PostgresFolderStore
from agent_core.adapters.persistence.integration_secrets import (
    InMemoryIntegrationSecretStore,
    PostgresIntegrationSecretStore,
)
from agent_core.adapters.persistence.jobs import InMemoryJobStore, PostgresJobStore
from agent_core.adapters.persistence.latency_report import chat_latency_report
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryApprovalRepository,
    InMemoryArtifactRepository,
    InMemoryCapabilityEvaluationRepository,
    InMemoryCheckpointRepository,
    InMemoryEventRepository,
    InMemoryExportConsentRepository,
    InMemoryFolderStore,
    InMemoryIdempotencyRepository,
    InMemoryMaintenanceRepository,
    InMemoryModelSettingsStore,
    InMemoryPersonaStore,
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
from agent_core.adapters.persistence.memory_health_report import memory_health_report
from agent_core.adapters.persistence.memory_repositories import (
    PostgresIntegratedEpisodeStore,
    PostgresKnowledgeStore,
    PostgresMemoryStore,
    PostgresTraceStore,
)
from agent_core.adapters.persistence.model_settings import PostgresModelSettingsStore
from agent_core.adapters.persistence.notifications import (
    InMemoryDeviceRegistrationIdempotencyRepository,
    InMemoryDeviceRegistry,
    InMemoryNotificationOutbox,
    PostgresDeviceRegistrationIdempotencyRepository,
    PostgresDeviceRegistry,
    PostgresNotificationOutbox,
)
from agent_core.adapters.persistence.people import InMemoryPeopleStore, PostgresPeopleStore
from agent_core.adapters.persistence.persona_repositories import (
    PostgresPersonaStore,
)
from agent_core.adapters.persistence.projections import (
    PostgresSessionHistoryRepository,
    PostgresTrajectoryProjectionRepository,
)
from agent_core.adapters.persistence.queue import PostgresRunQueue
from agent_core.adapters.persistence.reconsolidation import PostgresReconsolidationStore
from agent_core.adapters.persistence.records import InMemoryRecordStore, PostgresRecordStore
from agent_core.adapters.persistence.reliability_monitor import check_reliability
from agent_core.adapters.persistence.reliability_report import reliability_report
from agent_core.adapters.persistence.repositories import (
    PostgresAgentRepository,
    PostgresApprovalRepository,
    PostgresArtifactRepository,
    PostgresBrowserAuthenticationRepository,
    PostgresBrowserGrantRepository,
    PostgresBrowserProfileRepository,
    PostgresBrowserTaskGrantRepository,
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
from agent_core.adapters.persistence.semantic_memory import PostgresSemanticMemoryIndex
from agent_core.adapters.persistence.semantic_work import PostgresSemanticWorkStore
from agent_core.adapters.persistence.session_deletions import (
    InMemorySessionDeletionRepository,
    PostgresSessionDeletionRepository,
)
from agent_core.adapters.persistence.skills import PostgresSkillRepository
from agent_core.adapters.persistence.surfaces import (
    InMemorySurfacePairingRepository,
    InMemorySurfaceReceiptRepository,
    InMemorySurfaceReplyOutbox,
    InMemorySurfaceRepositories,
    InMemorySurfaceSessionRepository,
    PostgresSurfacePairingRepository,
    PostgresSurfaceReceiptRepository,
    PostgresSurfaceReplyOutbox,
    PostgresSurfaceRepositories,
    PostgresSurfaceSessionRepository,
)
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
from agent_core.adapters.sealing import AESGCMSecretSealer as IntegrationSecretSealer
from agent_core.adapters.skills.memory import InMemorySkillRepository
from agent_core.adapters.skills.stores import (
    FilesystemSkillPackageStore,
    InMemorySkillPackageStore,
)
from agent_core.adapters.surface_admission import (
    AllowSurfaceAdmissionController,
    PostgresSurfaceAdmissionController,
)
from agent_core.adapters.telegram import (
    PostgresTelegramPollLock,
    TelegramBotTransport,
    TelegramPoller,
)
from agent_core.adapters.unsubscribe.http import HttpOneClickTransport
from agent_core.adapters.web.firecrawl import FirecrawlWebProvider
from agent_core.adapters.web.keenable import KeenableWebProvider
from agent_core.adapters.web.public_image import PublicImageReader
from agent_core.adapters.web.routing import WeightedWebProviderRouter
from agent_core.adapters.web.tavily import TavilyWebProvider
from agent_core.adapters.whatsapp import (
    WhatsAppCloudTransport,
    WhatsAppDeliveryService,
    create_whatsapp_webhook_app,
)
from agent_core.api.call_ingress import create_call_ingress
from agent_core.application.approval_service import ApprovalService
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.application.attachments import StoredAttachmentResolver
from agent_core.application.browser_continuation import BrowserContinuationService
from agent_core.application.browser_follow_consent import BrowserFollowConsentAuthorizer
from agent_core.application.browser_grants import ConfiguredBrowserStandingAuthorizer
from agent_core.application.browser_leases import (
    browser_run_state,
    release_browser_lease_if_done,
)
from agent_core.application.browser_management import (
    BrowserGrantManagementService,
    BrowserProfileManagementService,
    BrowserUnitOfWorkFactory,
)
from agent_core.application.browser_research import BrowserResearchAuthorizer
from agent_core.application.browser_startup import BrowserStartupRecovery
from agent_core.application.browser_task_grants import (
    BrowserTaskGrantAuthorizer,
    CompositeStandingAuthorizer,
    PublicBrowserTaskGrantService,
    TaskGrantResolution,
    read_task_grant_context,
    sweep_expired_task_grants,
)
from agent_core.application.call_authorization import OwnerCallAuthorizer
from agent_core.application.call_plans import CallPlanSweep
from agent_core.application.call_worker import CallWorker
from agent_core.application.calling import CallService
from agent_core.application.cockpit import (
    BranchImporter,
    CockpitPublication,
    CockpitPublicationLimits,
    CommitOutboxWorker,
)
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.cockpit_installation import WidgetInstallationAuthorizer
from agent_core.application.cockpit_platform_feeds import platform_feed_sources
from agent_core.application.cockpit_projector import (
    CockpitFeedRetention,
    PlatformFeedProjector,
    PlatformFeedSource,
)
from agent_core.application.cockpit_views import CockpitBoard, CockpitRetention
from agent_core.application.delegations import DelegationJoin, DelegationMaterializer
from agent_core.application.device_ingest import DeviceMessageIngestService
from agent_core.application.device_management import (
    DeviceManagementService,
    NotificationInboxService,
)
from agent_core.application.email import EmailExperienceService
from agent_core.application.folder_service import PublicFolderService
from agent_core.application.integration_secrets import IntegrationSecrets
from agent_core.application.job_execution import JobExecution
from agent_core.application.job_grants import JobGrantAuthorizer
from agent_core.application.jobs import JobPublication
from agent_core.application.mcp_servers import MCPServerSettingsService
from agent_core.application.media_inputs import StoredMediaInputResolver
from agent_core.application.model_settings import PublicModelSettingsService
from agent_core.application.notification_dispatcher import (
    NotificationDispatcher,
    NotificationDispatchUnitOfWorkFactory,
)
from agent_core.application.notification_producer import NotificationProducer
from agent_core.application.notification_worker import NotificationWorker
from agent_core.application.people import PublicPeopleService, RetainedEmailReader
from agent_core.application.people_context import PeopleAwareMemoryRetriever, PeopleContextService
from agent_core.application.people_duplicates import PeopleDeduplicator
from agent_core.application.people_erasure import PeopleErasureService
from agent_core.application.people_identity import PeopleIdentityService
from agent_core.application.people_repair import PeopleDirectoryRepair
from agent_core.application.public_services import (
    PublicApprovalService,
    PublicArtifactService,
    PublicMemoryService,
    PublicPersonaService,
    PublicRunService,
    PublicSessionService,
)
from agent_core.application.reconsolidation import PublicReconsolidationService
from agent_core.application.records import RecordsService
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
    BrowserTaskGrantService as PublicBrowserTaskGrantServiceContract,
)
from agent_core.application.services import (
    DeviceIngestService as PublicDeviceIngestServiceContract,
)
from agent_core.application.services import (
    DeviceService as PublicDeviceServiceContract,
)
from agent_core.application.services import (
    FolderService as PublicFolderServiceContract,
)
from agent_core.application.services import (
    MemoryReadService as PublicMemoryReadServiceContract,
)
from agent_core.application.services import (
    ModelSettingsService as PublicModelSettingsServiceContract,
)
from agent_core.application.services import (
    NotificationService as PublicNotificationServiceContract,
)
from agent_core.application.services import (
    PeopleService as PublicPeopleServiceContract,
)
from agent_core.application.services import (
    PersonaService as PublicPersonaServiceContract,
)
from agent_core.application.services import (
    ReconsolidationService,
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
from agent_core.application.services import SurfaceService as PublicSurfaceServiceContract
from agent_core.application.session_service import SessionService, bootstrap_session
from agent_core.application.skill_review import SkillBackgroundReview
from agent_core.application.surface_worker import (
    SurfaceDelivery,
    SurfaceNotificationTransport,
    SurfaceReplyDispatcher,
    SurfaceTemplatePolicy,
    SurfaceWorker,
)
from agent_core.application.surfaces import (
    SurfaceIngressService,
    SurfaceManagementService,
    surface_notice_text,
)
from agent_core.application.trajectory_service import (
    TrajectoryExportService,
    TrajectoryRedactor,
)
from agent_core.cockpit.catalogue import load_kit_catalogue
from agent_core.cockpit.profiles import CockpitProfiles
from agent_core.config import (
    MEMORY_DISTILLATION_CORPUS_PATH,
    MEMORY_DISTILLATION_HOLDOUT_PATH,
    MEMORY_FORMATION_CORPUS_PATH,
    PACKAGE_ROOT,
    PRODUCTION_MODEL_POLICY,
    REPOSITORY_ROOT,
    AuthMode,
    BrowserProviderKind,
    ConfigurationError,
    DeploymentMode,
    JudgmentProviderKind,
    MemoryFormationPolicyPin,
    MemoryProviderExtractionMode,
    PushProviderKind,
    Settings,
    WebProviderAllocation,
    WebProviderKind,
    load_call_worker_settings,
    load_config_document,
    load_memory_distillation_evidence,
    load_notification_worker_settings,
    load_provider_extraction_evidence,
    load_schedule_worker_settings,
    load_settings,
    load_surface_worker_settings,
    provider_extraction_evidence_paths,
    shipped_corpus_sha256,
    validate_runtime_identity,
    validate_settings,
)
from agent_core.context.budget import records_catalog_caps
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.compactor import StructuredCompactor
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.context.rendering import render_email_context
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import (
    BROWSER_TASK_LIMITS_METADATA_KEY,
    DEFERRED_TOOLS_METADATA_KEY,
    AgentSpec,
    Principal,
    content_addressed_agent_version,
)
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserProfile, BrowserRunState
from agent_core.domain.browser_act_views import TaskGrantSessionContext
from agent_core.domain.captures import CAPTURE_READER_TOOL_NAME
from agent_core.domain.delegations import DelegationCaps, DelegationDefaults
from agent_core.domain.devices import Device, DeviceKind, DeviceStatus, PushProvider
from agent_core.domain.email import EmailBudgetLimits, EmailRecord
from agent_core.domain.email_subscriptions import SUBSCRIPTIONS_TOOL_NAME, UNSUBSCRIBE_TOOL_NAME
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import EventEnvelope, NewEvent, ProcessEvent
from agent_core.domain.execution import (
    EgressDestination,
    EgressMode,
    EgressPolicy,
    ResourceLimits,
)
from agent_core.domain.failures import log_failure
from agent_core.domain.local_embeddings import LocalEmbeddingConfig
from agent_core.domain.mcp import MCPServerConfig, MCPTransport, ScriptedMCPServer
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import (
    Capability,
    FakeModelScript,
    ModelAttempt,
    ModelEvent,
    ModelRequest,
    ReasoningDeltaEvent,
    ReasoningEffort,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextDeltaEvent,
    UsageEvent,
)
from agent_core.domain.model_settings import (
    ChatModelOption,
    MemoryModelOption,
    ModelChoice,
    ModelSettingsCatalog,
    default_chat_effort,
)
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import (
    LoadedRuleset,
    PolicyCondition,
    PolicyDecisionType,
    PolicyProfileRecord,
)
from agent_core.domain.runs import (
    TERMINAL_RUN_STATUSES,
    CancelReason,
    Run,
    RunKind,
    RunLimits,
    RunOutcome,
    RunStatus,
)
from agent_core.domain.schedules import ScheduleAdmissionLimits, ScheduleDefinitionLimits
from agent_core.domain.sessions import (
    DEFAULT_PROJECT_SCOPE,
    SESSION_BROWSER_PROFILE_METADATA_KEY,
    Session,
    SessionStatus,
    project_scope,
)
from agent_core.domain.skills import SkillPackage, SkillSource
from agent_core.domain.surfaces import (
    SurfaceInboundMessage,
    SurfaceLimits,
    SurfaceTransportResult,
)
from agent_core.domain.tools import ToolExecutionContext, ToolInvocation, ToolSpec
from agent_core.execution.egress import validate_destination
from agent_core.execution.manager import SandboxManager
from agent_core.execution.proxy import (
    WorkerEgressProxy,
    start_owner_mcp_egress_proxy,
    start_unsubscribe_egress_proxy,
    start_worker_egress_proxy,
)
from agent_core.folders.clustering import ThreadGrouper
from agent_core.folders.grouping import ModelAssistedThreadGrouper
from agent_core.folders.matching import JudgmentFolderMatcher
from agent_core.folders.profiles import FolderProfiles
from agent_core.folders.proposals import FolderProposalPass
from agent_core.knowledge.chunking import (
    MAX_KNOWLEDGE_SOURCE_BYTES,
    PlainTextExtractor,
    RoutingExtractor,
)
from agent_core.knowledge.service import KnowledgeService
from agent_core.knowledge.uploads import UploadAutoIngest
from agent_core.mcp.configuration import (
    calling_server_configs,
    email_server_configs,
    is_email_server_id,
    svp_server_configs,
    validate_mcp_config,
)
from agent_core.mcp.runtime import MCPRuntime
from agent_core.memory.bounded_recall_admission import BoundedAdmission
from agent_core.memory.communication_sources import AttributedCommunicationCandidateExtractor
from agent_core.memory.distillation import (
    NemoriAssistedCandidateExtractor,
    distillation_evidence_matches,
)
from agent_core.memory.email_semantics import (
    EmailSemanticFormationService,
    load_email_semantic_evidence,
)
from agent_core.memory.formation import (
    FORMATION_POLICY_VERSION,
    NEMORI_FORMATION_POLICY_VERSION,
    SESSION_IDLE_SECONDS,
    DeterministicCandidateExtractor,
    GovernedMemoryService,
    HighRecallCandidateExtractor,
)
from agent_core.memory.model_choice import OwnerSelectedCandidateExtractor
from agent_core.memory.people_kinship import link_owner_kinship
from agent_core.memory.people_legacy import link_existing_beliefs
from agent_core.memory.people_owner_formation import OwnerPeopleCandidateExtractor
from agent_core.memory.profiles import MemoryProfiles
from agent_core.memory.provider_extraction import (
    PROVIDER_FORMATION_POLICY_VERSION,
    REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,
    ProviderAssistedCandidateExtractor,
    provider_extraction_evidence_matches,
)
from agent_core.memory.recall_preparation import SemanticRecallPreparation
from agent_core.memory.reconsolidation_evidence import bind_evidence
from agent_core.memory.reconsolidation_policy import local_egress_policy
from agent_core.memory.reconsolidation_worker import ReconsolidationPass
from agent_core.memory.retrieval import (
    DeterministicQueryFormer,
    EventEpisodeSearch,
    HybridMemoryRetriever,
)
from agent_core.memory.semantic_deployment import (
    SemanticCompositionInputs,
    SemanticServices,
    compose_indexing,
    configuration_digest,
)
from agent_core.memory.source_learning import SourceLearningService
from agent_core.model import NON_ROUTED_MODEL_POLICIES
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from agent_core.observability.latency import ChatLatencyReport
from agent_core.observability.logging import configure_logging
from agent_core.observability.memory_health import MemoryHealthReport
from agent_core.observability.policy import AdvisoryMetrics
from agent_core.observability.reliability import ReliabilityReport, ReliabilityThresholds
from agent_core.observability.schedules import ScheduleMetrics, tenant_hash_key
from agent_core.persona.boundaries import persona_boundaries
from agent_core.persona.preview import PersonaPreviewer
from agent_core.policy.advised import AdvisedPolicyEngine
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.jobs import JobPolicyEngine, job_publication_ruleset
from agent_core.policy.judgment_advisor import JudgmentPolicyAdvisor
from agent_core.policy.loader import load_ruleset_documents
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.artifacts import AttachmentResolver
from agent_core.ports.browser import (
    BrowserProvider,
    browser_lease_upkeep,
)
from agent_core.ports.browser_profiles import BrowserProfileControlPlane
from agent_core.ports.browser_sessions import (
    BrowserAuthenticationControlPlane,
    BrowserSessionControlPlane,
)
from agent_core.ports.browser_upload import BrowserImageUploader
from agent_core.ports.cockpit import WidgetBuilder, WidgetRepository
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.dispatch import WorkerService
from agent_core.ports.embeddings import LocalEmbeddingProvider as LocalEmbeddingPort
from agent_core.ports.jobs import JobRunner, JobScreen
from agent_core.ports.judgment import JudgmentProvider
from agent_core.ports.live_events import LiveEventBroadcaster
from agent_core.ports.mcp import MCPClientFactory, MCPOAuthGateway, MCPServerRepository
from agent_core.ports.media import MediaGenerationProvider
from agent_core.ports.memory import MemoryCandidateExtractor
from agent_core.ports.models import ModelProvider
from agent_core.ports.notifications import PushTransport
from agent_core.ports.persistence import (
    RepositoryUnitOfWork,
    ScheduleUnitOfWork,
    TransactionCallback,
    TransactionCallbackRegistrar,
    UnitOfWorkFactory,
)
from agent_core.ports.policies import PolicyEngine, StandingAuthorizer
from agent_core.ports.skills import SkillPackageStore, SkillRepository
from agent_core.ports.tools import DeviceChannel
from agent_core.ports.unsubscribe import OneClickTransport
from agent_core.ports.web import WebProvider, WebProviderRouter
from agent_core.runtime.budgets import UnitOfWorkBudgetLedger
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.email_tasks import EmailTaskRunner
from agent_core.runtime.executor import RunExecutor, SurfaceRunStateWriter
from agent_core.runtime.job_model import JobModelCompletion
from agent_core.runtime.jobs import execute_job
from agent_core.runtime.loop import RunContext
from agent_core.runtime.scheduled_browser import (
    ScheduledBrowserUnavailableError,
    is_browser_schedule,
)
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.drafts import ScheduleDrafter, ScheduleDraftInterpreter
from agent_core.scheduling.materializer import ScheduleMaterializer
from agent_core.scheduling.worker import ScheduleWorker
from agent_core.skills.catalog import SkillCatalogService
from agent_core.skills.package import SkillPackageValidator
from agent_core.titles.generator import ConversationTitleGenerator
from agent_core.titles.profiles import TitleProfiles
from agent_core.titles.titling import ConversationTitlePass, request_title_after_run
from agent_core.tools.artifact_export import ArtifactExportTool
from agent_core.tools.artifact_read_capture import ArtifactReadCaptureTool
from agent_core.tools.ask_user import AskUserTool
from agent_core.tools.browser_act import (
    BrowserActApprovalPresenter,
    BrowserActTool,
)
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.browser_observe import (
    ArticleBrowserObserveTool,
    BrowserObserveTool,
    PagedBrowserObserveTool,
)
from agent_core.tools.browser_upload import BrowserUploadTool
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.cockpit import (
    COCKPIT_TOOL_NAMES,
    CockpitToolset,
    PreviousCockpitCheckTool,
    PreviousCockpitReadTool,
    cockpit_tools,
    offers_widget_writes,
)
from agent_core.tools.cockpit import WIDGET_WRITE_TOOL as COCKPIT_WIDGET_WRITE_TOOL
from agent_core.tools.cockpit_installation import (
    INSTALL_TOOL_NAMES,
    CockpitInstallTool,
    CockpitPrepareTool,
)
from agent_core.tools.context_update import WORKING_STATE_TOOL_NAME, UpdateWorkingStateTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.delegate_run import DelegateRunTool
from agent_core.tools.demo_external_write import DemoExternalWriteTool
from agent_core.tools.device_tools import DEVICE_SMS_SEND_TOOL_NAME, DeviceToolRuntime
from agent_core.tools.email_context import EmailContextTool, EmailFeedbackTool
from agent_core.tools.email_unsubscribe import EmailSubscriptionsTool, EmailUnsubscribeTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.job_schedules import JobScheduleCreateTool, JobScheduleUpdateTool
from agent_core.tools.jobs import (
    JOB_TOOL_NAMES,
    JobManageTool,
    JobRunTool,
    JobToolset,
    StoredKeyJobRunTool,
)
from agent_core.tools.knowledge_ingest import KnowledgeIngestTool
from agent_core.tools.knowledge_search import KnowledgeSearchTool
from agent_core.tools.media_generation import (
    ImageGenerationTool,
    VideoGenerationTool,
)
from agent_core.tools.memory_learn import MemoryLearnTool
from agent_core.tools.memory_recall_episodes import MemoryRecallEpisodesTool
from agent_core.tools.memory_remember import (
    MemoryRememberTool,
    PeopleMemoryRememberTool,
    SessionScopedMemoryRememberTool,
)
from agent_core.tools.memory_search import MemorySearchTool
from agent_core.tools.people import (
    PeopleContextTool,
    PeopleHistoryTool,
    PeopleSearchTool,
)
from agent_core.tools.records import (
    RecordsAppendTool,
    RecordsCreateSeriesTool,
    RecordsDeleteTool,
    RecordsImportTool,
    RecordsListTool,
    RecordsQueryTool,
)
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.sandbox_run_command import SandboxRunCommandTool
from agent_core.tools.schedule_create import (
    COCKPIT_FEED_SCOPES,
    SCHEDULE_CREATE_TOOL_NAME,
    CockpitScheduleCreateTool,
    PreviousCockpitScheduleCreateTool,
    ScheduleCreateTool,
    SingleCallScheduleCreateTool,
)
from agent_core.tools.schedule_lifecycle import (
    SCHEDULE_CANCEL_TOOL_NAME,
    SCHEDULE_LIFECYCLE_TOOL_NAMES,
    SCHEDULE_PAUSE_TOOL_NAME,
    SCHEDULE_RESUME_TOOL_NAME,
    SCHEDULE_UPDATE_TOOL_NAME,
    CockpitScheduleUpdateTool,
    PreviousScheduleListTool,
    ScheduleCancelTool,
    ScheduleListTool,
    SchedulePauseTool,
    ScheduleResumeTool,
    ScheduleUpdateTool,
)
from agent_core.tools.skill_load import (
    SKILL_LOAD_TOOL_NAME,
    SkillLoadTool,
)
from agent_core.tools.skill_manage import SkillManageTool
from agent_core.tools.tool_call import TOOL_CALL_TOOL_NAME, ToolCallTool
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.web_fetch_json import WebFetchJsonTool
from agent_core.tools.web_inspect_image import WebInspectImageTool
from agent_core.tools.web_search import WebSearchTool
from agent_core.tools.workspace.list_files import WorkspaceListFilesTool
from agent_core.tools.workspace.read_text import WorkspaceReadTextTool
from agent_core.tools.workspace.write_text import WorkspaceWriteTextTool

POST_RUN_HOOK_TIMEOUT_SECONDS = 15.0

logger = logging.getLogger(__name__)
LIVE_EVENT_PUBLISH_TIMEOUT_SECONDS = 0.1


def _duplicate_sweep(
    duplicates: PeopleDeduplicator | None, principal: Principal
) -> Callable[[], Awaitable[int]] | None:
    """The maintenance pass that merges decisive duplicates and asks about the rest."""
    if duplicates is None or "people.write" not in principal.scopes:
        return None

    async def sweep() -> int:
        report = await duplicates.run(principal, apply=True)
        return len(report.merges) + len(report.suggestions) + report.withdrawn

    return sweep


def _correspondence_reprojector(
    uow_factory: UnitOfWorkFactory, clock: Clock, ids: IdFactory
) -> Callable[[Principal, EmailRecord], Awaitable[bool]]:
    """The directory repair's header backfill (ADR-0121); it calls no model."""

    async def reproject(owner: Principal, record: EmailRecord) -> bool:
        from agent_core.memory.email_people import EmailPeopleFormationService

        return await EmailPeopleFormationService(
            uow_factory, clock, ids, owner, provider="people-repair", model="none"
        ).reproject_correspondence(record)

    return reproject


def _retained_email_reader(
    uow_factory: UnitOfWorkFactory, clock: Clock, ids: IdFactory
) -> Callable[[Principal], RetainedEmailReader]:
    """People history's view of the original email Veetbot kept (ADR-0126); no model."""

    def reader(owner: Principal) -> RetainedEmailReader:
        from agent_core.memory.email_people import EmailPeopleFormationService

        return EmailPeopleFormationService(
            uow_factory, clock, ids, owner, provider="people-source", model="none"
        )

    return reader


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
    device_ingest: PublicDeviceIngestServiceContract
    notifications: PublicNotificationServiceContract
    surfaces: PublicSurfaceServiceContract
    memory: PublicMemoryReadServiceContract
    persona: PublicPersonaServiceContract
    folders: PublicFolderServiceContract
    email: EmailExperienceService
    reconsolidation: ReconsolidationService | None = None
    calls: CallService | None = None
    people: PublicPeopleServiceContract | None = None
    model_settings: PublicModelSettingsServiceContract | None = None
    # ADR-0129: present only when BROWSER_TASK_GRANTS_ENABLED is set.
    browser_task_grants: PublicBrowserTaskGrantServiceContract | None = None
    # ADR-0225 part B: absent when drafts are off or titles use no routed model.
    schedule_drafts: ScheduleDrafter | None = None
    # ADR-0225 part C: Run now materializes on the API's own unit of work.
    schedule_runs: ScheduleMaterializer | None = None
    # Milestone 35: present only when AGENT_COCKPIT_ENABLED is set.
    cockpit: CockpitBoard | None = None
    # ADR-0275: present only when AGENT_INTEGRATION_SECRETS_ENABLED is set.
    integration_secrets: IntegrationSecrets | None = None
    # ADR-0255: present only when AGENT_MCP_SERVERS_ENABLED is set.
    mcp_servers: MCPServerSettingsService | None = None


class _CockpitMaintenance:
    """The maintenance role's GitHub App wiring (docs/plan/cockpit.md, the credential).

    Only the maintenance role reads the App's key, when it builds its worker, and
    it refuses to start the outbox or the importer without the App's settings.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        builder: WidgetBuilder,
        owner_names: tuple[str, ...],
        profiles: CockpitProfiles,
        feed_sources: Sequence[PlatformFeedSource] = (),
    ) -> None:
        self._settings = settings
        self._feed_sources = tuple(feed_sources)
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._builder = builder
        self._owner_names = owner_names
        self._profiles = profiles
        self._client: httpx.AsyncClient | None = None
        self.jobs: JobPublication | None = None

    def sweeps(self) -> dict[str, Any]:
        """Bind maintenance credentials and compose publication, import, and feed sweeps."""
        settings = self._settings
        required = {
            "AGENT_COCKPIT_GITHUB_REPOSITORY": settings.cockpit_github_repository,
            "AGENT_COCKPIT_GITHUB_APP_ID": settings.cockpit_github_app_id,
            "AGENT_COCKPIT_GITHUB_INSTALLATION_ID": settings.cockpit_github_installation_id,
            "AGENT_COCKPIT_GITHUB_KEY_FILE": settings.cockpit_github_key_file,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ConfigurationError(
                "the Cockpit outbox and importer need the GitHub App: set " + ", ".join(missing)
            )
        assert settings.cockpit_github_key_file is not None
        assert settings.cockpit_github_repository is not None
        assert settings.cockpit_github_app_id is not None
        assert settings.cockpit_github_installation_id is not None
        try:
            key = load_app_private_key(settings.cockpit_github_key_file)
        except ValueError as exc:
            raise ConfigurationError(f"AGENT_COCKPIT_GITHUB_KEY_FILE {exc}") from exc
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        tokens = GitHubAppTokens(
            self._client,
            app_id=settings.cockpit_github_app_id,
            installation_id=settings.cockpit_github_installation_id,
            private_key_pem=key,
            clock=self._clock,
        )
        repository = GitHubWidgetRepository(
            self._client,
            tokens,
            repository=settings.cockpit_github_repository,
            branch=settings.cockpit_github_branch,
        )
        publication = self._profiles.publication
        limits = CockpitPublicationLimits(
            commits_per_widget_per_hour=publication.commits_per_widget_per_hour,
            commits_per_principal_per_hour=publication.commits_per_principal_per_hour,
        )
        common: dict[str, Any] = {
            "uow_factory": self._uow_factory,
            "clock": self._clock,
            "ids": self._ids,
            "repository": repository,
            "principal": self._principal,
            "limits": limits,
        }
        retention = CockpitRetention(
            uow_factory=self._uow_factory,
            clock=self._clock,
            tenant_id=self._principal.tenant_id,
            seconds=self._profiles.stream.event_retention_seconds,
        )
        feeds = self._profiles.feeds
        projector = PlatformFeedProjector(
            uow_factory=self._uow_factory,
            clock=self._clock,
            feeds=CockpitFeeds(
                uow_factory=self._uow_factory,
                clock=self._clock,
                schema_check=Draft202012SchemaCheck(),
                history_max=feeds.history_max,
            ),
            principal=self._principal,
            sources=self._feed_sources,
            refresh_seconds=feeds.refresh_seconds,
        )
        feed_history = CockpitFeedRetention(
            uow_factory=self._uow_factory,
            clock=self._clock,
            principal=self._principal,
            history_days=feeds.history_days,
        )
        widget_outbox = CommitOutboxWorker(**common)
        widget_importer = BranchImporter(
            **common, builder=self._builder, owner_names=self._owner_names
        )
        if self.jobs is not None:
            self.jobs.repository = GitHubWidgetRepository(
                self._client,
                tokens,
                repository=settings.cockpit_github_repository,
                branch="cockpit/jobs",
                package_kind="job",
            )

        async def flush_packages() -> int:
            """Flush enabled widget and job queues through their separate repositories."""
            count = await widget_outbox.run_once() if settings.cockpit_enabled else 0
            return count + (await self.jobs.flush(self._principal) if self.jobs is not None else 0)

        async def import_packages() -> int:
            """Import enabled widget and job branches without mixing their package namespaces."""
            count = await widget_importer.run_once() if settings.cockpit_enabled else 0
            return count + (
                await self.jobs.import_changes(self._principal) if self.jobs is not None else 0
            )

        return {
            "sweep_cockpit_outbox": flush_packages,
            "sweep_cockpit_events": retention.run_once,
            "sweep_cockpit_import": import_packages,
            "cockpit_import_interval_seconds": publication.import_interval_seconds,
            "sweep_cockpit_feeds": projector.run_once,
            "sweep_cockpit_feed_history": feed_history.run_once,
            "cockpit_feed_interval_seconds": feeds.projection_interval_seconds,
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


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
    surface_ingress: SurfaceIngressService
    sandbox: SandboxManager
    mcp: MCPRuntime
    skill_catalogs: SkillCatalogService
    skill_reviews: SkillBackgroundReview
    tool_pipeline: ToolPipeline
    memory: GovernedMemoryService
    source_learning: SourceLearningService
    people_erasure: PeopleErasureService
    memory_retriever: HybridMemoryRetriever
    memory_profiles: MemoryProfiles
    recall_composition: SemanticCompositionInputs
    folder_profiles: FolderProfiles
    knowledge: KnowledgeService
    mcp_proxy: WorkerEgressProxy | None
    semantic_services: SemanticServices | None = None
    # ADR-0255: the owner MCP transport, when owner-added servers are enabled.
    owner_mcp_proxy: WorkerEgressProxy | None = None
    local_import_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    folder_proposals: FolderProposalPass | None = None
    judgment_provider: JudgmentProvider | None = None
    attachment_resolver: AttachmentResolver | None = None
    people_repair: PeopleDirectoryRepair | None = None
    call_plans: CallPlanSweep | None = None
    # The Cockpit builder and its own sandbox manager, only behind the flag (M35).
    cockpit_builder: WidgetBuilder | None = None
    cockpit_sandbox: SandboxManager | None = None
    cockpit_publication: CockpitPublication | None = None
    job_publication: JobPublication | None = None
    job_execution: JobExecution | None = None
    job_credentials: GitHubReaderCredentials | None = None
    cockpit_maintenance: _CockpitMaintenance | None = None
    # An evaluation's in-memory widget branch and the outbox that commits to it.
    cockpit_local_branch: WidgetRepository | None = None
    cockpit_local_outbox: CommitOutboxWorker | None = None
    # Read-only Chat latency aggregates; only PostgreSQL keeps the event log (ADR-0131).
    latency_report: Callable[[datetime], Awaitable[ChatLatencyReport]] | None = None
    reliability_report: Callable[[datetime, datetime], Awaitable[ReliabilityReport]] | None = None
    reliability_check: (
        Callable[[int, ReliabilityThresholds], Awaitable[ReliabilityReport]] | None
    ) = None

    def start_mcp_discovery_warmup(self) -> asyncio.Task[None]:
        """Remember this tenant's MCP catalogs in the background (ADR-0131).

        Only the API and the interactive worker call this; serving never waits for it.
        """
        return self.mcp.start_discovery_warmup((self.principal.tenant_id,))


@dataclass(frozen=True, slots=True)
class SurfaceWorkerComposition:
    worker: SurfaceWorker
    webhook_app: FastAPI | None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8002


@dataclass(frozen=True, slots=True)
class CallWorkerComposition:
    worker: CallWorker | None
    webhook_app: FastAPI | None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8003


DEFAULT_AGENT_ID = UUID("8ad3e17d-449f-5ec8-a807-4e14f2b3a716")
DEFAULT_AGENT_INSTRUCTIONS = (
    "Answer the user's request and use a declared tool when useful. "
    "Prefer the least-powerful declared tool that can answer. For routine arithmetic, "
    "date/time, and public facts, use read-only tools such as math.calculate, "
    "system.current_time, or web.search when available. Do not use sandbox.run_command "
    "for those requests; use it only when arbitrary code execution is necessary. If no "
    "read-only tool can answer, explain the limitation or ask before proposing sandboxed "
    "code execution. To give the user a text or workspace file, call artifact.export with "
    "the text as content or a workspace path. For data charts and exact diagrams, use "
    "the offline sandbox renderer: python -m agent_core.execution.visuals SPEC.json. "
    "Read /opt/agent/visualization-guide.md inside the sandbox for its JSON format. "
    "Use observed data, label units and sources, and explain missing values or transformations. "
    "Export its PNG preview, chart CSV or diagram DOT, and JSON source with artifact.export; "
    "request SVG/PDF in the spec when wanted. Check the exit code and returned files before "
    "exporting. Do not use image.generate for numerical charts or exact diagrams. "
    "For other generated images or videos, use "
    "image.generate or video.generate when available. These tools attach the file to "
    "your reply automatically. Never say a file is attached unless its export or "
    "generation tool succeeded. When the owner asks you to learn from or remember a named "
    "website, proactively retain useful separate observations with memory.learn after reading "
    "it. Use observations with call_id and an exact 8-512 character quote; omit claim and "
    "subject to save the excerpt itself, without a paraphrase or explanatory wrapper. "
    "Call it through tool.call if deferred. Follow rejection guidance to correct a proposal; "
    "never weaken or bypass a refusal. Search existing memories and reinforce "
    "tentative beliefs only when the cited text supports their full claim. Keep historical "
    "observations and uncertain relationships tentative; text-only reads do not inspect photos. "
    "Report only confirmed memory tool outcomes. A failed manual save is separate from "
    "background formation of owner-stated facts after the reply; do not claim that nothing "
    "was remembered or that pending formation succeeded. Save with memory.remember only what "
    "the owner stated, in their words, when they asked to remember it; a plan, schedule or "
    "calendar you derived from web or document content belongs in the reply or an artifact, "
    "never in memory. Never mention a save, or a failed save, the owner did not ask for. "
    "For widget creation, when cockpit.prepare is available, prepare and test the entire "
    "widget and collector before calling cockpit.install for one final summarized approval. "
    "Do not publish components or request new job access separately while preparing. Use "
    "offline trials or existing grants, then include all new access and the schedule "
    "in the proposal."
)
_BROWSER_TOOL_NAMES = frozenset(
    {"browser.navigate", "browser.observe", "browser.act", "browser.upload"}
)


def _session_tool_filter(
    browser_provider: BrowserProvider | None,
) -> Callable[[Session, Sequence[ToolSpec]], list[ToolSpec]]:
    """Build the browser and widget tool filter for each session."""
    session_bound = isinstance(browser_provider, SessionBoundHostedBrowserProvider)

    def filter_tools(session: Session, tools: Sequence[ToolSpec]) -> list[ToolSpec]:
        """Hide browser tools without a binding and widget writes outside owner chat."""
        kept = list(tools)
        if session_bound:
            selected_profile = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
            if not (isinstance(selected_profile, str) and selected_profile):
                kept = [tool for tool in kept if tool.name not in _BROWSER_TOOL_NAMES]
        # Widgets are written only in the owner's interactive chat (docs/plan/cockpit.md).
        if not offers_widget_writes(session.metadata):
            kept = [
                tool
                for tool in kept
                if tool.name not in {COCKPIT_WIDGET_WRITE_TOOL, *INSTALL_TOOL_NAMES}
            ]
        return kept

    return filter_tools


def _scheduled_browser_binding_validator(
    browser_provider: BrowserProvider | None,
) -> Callable[[Session], None]:
    """Refuse a schedule pin the composed provider cannot honor, including resume."""

    def validate(session: Session) -> None:
        if not is_browser_schedule(session):
            return
        if isinstance(browser_provider, SessionBoundHostedBrowserProvider):
            return
        if isinstance(browser_provider, HostedBrowserProvider):
            selected = UUID(str(session.metadata[SESSION_BROWSER_PROFILE_METADATA_KEY]))
            if selected == browser_provider.profile_id:
                return
            raise ScheduledBrowserUnavailableError("tool.browser.profile_unavailable")
        raise ScheduledBrowserUnavailableError("tool.browser.provider_unavailable")

    return validate


def _task_grants_composed(settings: Settings, browser_provider: BrowserProvider) -> bool:
    """ADR-0129: task grants need the flag and a provider that resolves each
    session's own profile."""

    return settings.browser_task_grants_enabled and isinstance(
        browser_provider, SessionBoundHostedBrowserProvider
    )


def _task_grant_context_reader(
    uow_factory: UnitOfWorkFactory, clock: Clock
) -> Callable[[UUID, Principal], Awaitable[TaskGrantSessionContext]]:
    async def read(session_id: UUID, principal: Principal) -> TaskGrantSessionContext:
        async with uow_factory() as uow:
            return await read_task_grant_context(uow, session_id, principal, now=clock.now())

    return read


def _session_browser_origins(
    browser_provider: BrowserProvider | None,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
) -> Callable[[Session, Principal], Awaitable[tuple[str, ...]]] | None:
    """ADR-0130: the origins browser.navigate accepts, by provider mode.

    A session-bound hosted provider reads the session's profile (nothing when
    it is gone); an injected provider names its own; a pinned hosted or local
    provider uses the configured origins. The profile id is never rendered.
    """

    if browser_provider is None:
        return None
    if isinstance(browser_provider, SessionBoundHostedBrowserProvider):

        async def profile_origins(session: Session, principal: Principal) -> tuple[str, ...]:
            selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
            if not isinstance(selected, str) or not selected:
                return ()
            try:
                async with uow_factory() as uow:
                    profile = await uow.browser_profiles.get(UUID(selected), principal)
            except (NotFoundError, ValueError):
                return ()
            return profile.allowed_origins

        return profile_origins
    declared = getattr(browser_provider, "allowed_origins", None)
    origins = (
        tuple(declared)
        if isinstance(declared, tuple) and all(isinstance(origin, str) for origin in declared)
        else settings.browser_allowed_origins
    )

    async def configured_origins(session: Session, principal: Principal) -> tuple[str, ...]:
        del session, principal
        return origins

    return configured_origins


def _session_required_tools(session: Session) -> frozenset[str]:
    """ADR-0130: a chat bound to a website profile always defines the browser
    tools, so the item cap can never push them into the deferred index."""

    selected_profile = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    if isinstance(selected_profile, str) and selected_profile:
        return _BROWSER_TOOL_NAMES
    return frozenset()


def _run_limits_from_defaults(run_defaults: Mapping[str, Any]) -> RunLimits:
    return RunLimits(
        max_steps=int(run_defaults["max_steps"]),
        max_model_calls=int(run_defaults["max_model_calls"]),
        max_tool_calls=int(run_defaults["max_tool_calls"]),
        synthesis_reserve_model_calls=int(run_defaults.get("synthesis_reserve_model_calls", 0)),
        synthesis_reserve_tool_calls=int(run_defaults.get("synthesis_reserve_tool_calls", 0)),
    )


_BROWSER_TASK_COUNT_LIMITS = ("max_steps", "max_model_calls", "max_tool_calls")


def _browser_task_limits(
    run_defaults: Mapping[str, Any], browser_task: Mapping[str, Any]
) -> RunLimits:
    """ADR-0130: run_defaults with the browser_task block's keys replaced.

    Built at every start, whatever the browser setting, so a bad overlay
    fails startup. The model-call and tool-call reserves stay inherited.
    """

    defaults = _run_limits_from_defaults(run_defaults)
    for name in _BROWSER_TASK_COUNT_LIMITS:
        value = int(browser_task[name])
        if value < getattr(defaults, name):
            raise ConfigurationError(f"browser_task.{name} must not be below run_defaults.{name}")
    try:
        return RunLimits(
            **(
                defaults.model_dump()
                | {name: int(browser_task[name]) for name in _BROWSER_TASK_COUNT_LIMITS}
                | {
                    "max_cost": Decimal(str(browser_task["max_cost"])),
                    "synthesis_reserve_cost": Decimal(str(browser_task["synthesis_reserve_cost"])),
                }
            )
        )
    except ValueError as exc:
        raise ConfigurationError(f"browser_task limits are invalid: {exc}") from None


def default_run_limits(settings: Settings) -> RunLimits:
    """The interactive run limits the composition root gives the default agent."""

    runtime_config = load_config_document(settings, "runtime/limits.yaml")
    return _run_limits_from_defaults(runtime_config["run_defaults"])


def _content_addressed_agent_version(agent: AgentSpec) -> str:
    return content_addressed_agent_version(agent)


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


def random_ids() -> IdFactory:
    """Supply opaque identities to offline commands without a full composition."""
    return RandomIdFactory()


async def operator_memory_health(hours: int) -> MemoryHealthReport:
    """Observe only the database: recall traces, usage events and the store."""

    if not 1 <= hours <= 2160:
        raise ValueError("Recall health windows must be between one hour and ninety days.")
    settings = load_settings()
    principal = Principal(
        tenant_id="local" if settings.auth_mode.value == "dev" else settings.auth_tenant_id,
        principal_id=(
            "local-user" if settings.auth_mode.value == "dev" else settings.auth_principal_id
        ),
    )
    engine = create_engine(settings.database_url)
    try:
        async with asyncio.timeout(60):
            await assert_schema_revision(engine)
            until = system_clock().now()
            return await memory_health_report(
                create_session_factory(engine),
                until - timedelta(hours=hours),
                until,
                principal=principal,
            )
    finally:
        await engine.dispose()


async def operator_reliability(
    hours: int, notify: bool, thresholds: ReliabilityThresholds
) -> ReliabilityReport:
    """Observe only the database; do not seed agents, tools, providers or artifacts."""
    if not 1 <= hours <= 2160:
        raise ValueError("Reliability windows must be between one hour and ninety days.")
    settings = load_settings()
    if notify and not settings.notification_dispatch_enabled:
        raise ConfigurationError("Reliability alerts require enabled notifications.")
    principal = Principal(
        tenant_id="local" if settings.auth_mode.value == "dev" else settings.auth_tenant_id,
        principal_id=(
            "local-user" if settings.auth_mode.value == "dev" else settings.auth_principal_id
        ),
    )
    clock = system_clock()
    engine = create_engine(settings.database_url)
    try:
        async with asyncio.timeout(60):
            await assert_schema_revision(engine)
            sessions = create_session_factory(engine)
            if notify:
                return await check_reliability(
                    sessions,
                    hours,
                    thresholds,
                    principal=principal,
                    outbox_factory=lambda session: PostgresNotificationOutbox(session, clock),
                    events_factory=PostgresProcessEventRepository,
                    clock=clock,
                    ids=random_ids(),
                    release_id=settings.release_id,
                )
            until = clock.now()
            return await reliability_report(
                sessions, until - timedelta(hours=hours), until, principal=principal
            )
    finally:
        await engine.dispose()


def fixed_clock(at: datetime) -> FixedClock:
    return FixedClock(at)


def sequence_ids(values: Iterable[UUID]) -> SequenceIdFactory:
    return SequenceIdFactory(values)


def scripted_provider(script: FakeModelScript, clock: Clock) -> ModelProvider:
    return FakeModelProvider(script, clock)


def local_embedding_provider(path: Path, config: LocalEmbeddingConfig) -> LocalEmbeddingPort:
    # Disabled deployments never import or initialize the native runtime.
    from agent_core.adapters.models.local_embeddings import LocalEmbeddingProvider

    return LocalEmbeddingProvider(path, config)


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
    episodes: InMemoryIntegratedEpisodeStore | None = None,
    traces: InMemoryTraceStore | None = None,
    knowledge: InMemoryKnowledgeStore | None = None,
) -> UnitOfWorkRepositories:
    """Assemble the in-memory repositories shared by the composition's units of work."""
    approvals = approvals or InMemoryApprovalRepository(clock)
    skills = skills or InMemorySkillRepository(
        InMemorySkillPackageStore(),
        SkillPackageValidator(ConservativeTokenEstimator()),
        clock,
        RandomIdFactory(),
    )
    mcp_servers = mcp_servers or InMemoryMCPServerRepository()
    memories = memories or InMemoryMemoryStore(clock, ReconsolidationIndex(MemoryTransaction()))
    events.bind_memory_sources(memories._reconsolidation_index)
    episodes = episodes or InMemoryIntegratedEpisodeStore()
    people = InMemoryPeopleStore(
        clock,
        memories,
        memory_guard=memories._transaction.owner,
        memory_index=memories._reconsolidation_index,
    )
    memories.bind_people(people)
    reconsolidation = InMemoryReconsolidationStore(
        memories,
        RandomIdFactory(),
        ReconsolidationEvidence(events, people, memories),
        dreaming=InMemoryDreamingSchedule(memories._transaction, RandomIdFactory()),
    )
    traces = traces or InMemoryTraceStore(people, reconsolidation=reconsolidation, clock=clock)
    knowledge = knowledge or InMemoryKnowledgeStore(clock)
    schedules = InMemoryScheduleRepository()
    devices = InMemoryDeviceRegistry()
    notification_outbox = InMemoryNotificationOutbox(clock, devices)
    schedule_occurrences = InMemoryScheduleOccurrenceRepository(schedules)
    delegations = InMemoryDelegationRepository()
    surfaces = InMemorySurfaceRepositories(
        admission=AllowSurfaceAdmissionController(),
        pairings=InMemorySurfacePairingRepository(),
        sessions=InMemorySurfaceSessionRepository(),
        receipts=InMemorySurfaceReceiptRepository(),
        replies=InMemorySurfaceReplyOutbox(),
    )
    device_invocations = InMemoryDeviceInvocationStore()
    device_ingest = InMemoryDeviceIngestStore()
    checkpoints = InMemoryCheckpointRepository()
    idempotency = InMemoryIdempotencyRepository(clock)
    usage = InMemoryUsageRepository(runs)
    trajectory_exports = InMemoryTrajectoryExportRepository()
    artifacts = InMemoryArtifactRepository()
    browser_task_grants = InMemoryBrowserTaskGrantRepository()
    records = InMemoryRecordStore()
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
        people=people,
        records=records,
        episodes=episodes,
        traces=traces,
        knowledge=knowledge,
        schedules=schedules,
        notification_outbox=notification_outbox,
        delegations=delegations,
        browser_task_grants=browser_task_grants,
    )
    return UnitOfWorkRepositories(
        agents=agents,
        approvals=approvals,
        policy_profiles=InMemoryPolicyProfileRepository(),
        browser_profiles=InMemoryBrowserProfileRepository(),
        browser_grants=InMemoryBrowserGrantRepository(),
        browser_task_grants=browser_task_grants,
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
        episodes=episodes,
        traces=traces,
        personas=InMemoryPersonaStore(),
        semantic_memory=InMemorySemanticMemoryIndex(memories),
        semantic_work=InMemorySemanticWorkStore(memories._transaction, RandomIdFactory()),
        reconsolidation=reconsolidation,
        memory_transaction=memories._transaction.transaction,
        model_settings=InMemoryModelSettingsStore(),
        folders=InMemoryFolderStore(),
        email=InMemoryEmailStore(),
        calls=InMemoryCallStore(),
        people=people,
        records=records,
        integration_secrets=InMemoryIntegrationSecretStore(),
        jobs=InMemoryJobStore(),
        job_outbox=InMemoryWidgetCommitOutbox(),
        cockpit_widgets=InMemoryWidgetStore(),
        cockpit_outbox=InMemoryWidgetCommitOutbox(),
        cockpit_feeds=InMemoryFeedStore(),
        cockpit_events=InMemoryCockpitEventLog(),
        knowledge=knowledge,
        evaluations=InMemoryCapabilityEvaluationRepository(),
        schedules=schedules,
        schedule_occurrences=schedule_occurrences,
        schedule_idempotency=InMemoryScheduleIdempotencyRepository(schedules),
        schedule_admission=AllowScheduleAdmissionController(),
        devices=devices,
        device_registration_idempotency=InMemoryDeviceRegistrationIdempotencyRepository(),
        device_invocations=device_invocations,
        device_ingest=device_ingest,
        notification_outbox=notification_outbox,
        delegations=delegations,
        surfaces=surfaces,
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
    surface_admission_limits: SurfaceLimits,
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
        episodes = PostgresIntegratedEpisodeStore(session)
        people = PostgresPeopleStore(session, clock)
        records = PostgresRecordStore(session, clock)
        memories.bind_people(people)
        reconsolidation = PostgresReconsolidationStore(
            session,
            ids,
            ReconsolidationEvidence(events, people, memories),
            dreaming=PostgresDreamingSchedule(session, ids),
        )
        traces = PostgresTraceStore(session, people, reconsolidation=reconsolidation, clock=clock)
        knowledge = PostgresKnowledgeStore(session, clock)
        schedules = PostgresScheduleRepository(session)
        devices = PostgresDeviceRegistry(session)
        return UnitOfWorkRepositories(
            agents=agents,
            approvals=PostgresApprovalRepository(session, clock),
            policy_profiles=PostgresPolicyProfileRepository(session),
            browser_profiles=PostgresBrowserProfileRepository(session),
            browser_grants=PostgresBrowserGrantRepository(session),
            browser_task_grants=PostgresBrowserTaskGrantRepository(session),
            browser_authentications=PostgresBrowserAuthenticationRepository(session),
            process_events=PostgresProcessEventRepository(session),
            sessions=sessions,
            session_deletions=PostgresSessionDeletionRepository(session, people, traces),
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
            episodes=episodes,
            traces=traces,
            personas=PostgresPersonaStore(session),
            semantic_memory=PostgresSemanticMemoryIndex(session, memories, clock),
            semantic_work=PostgresSemanticWorkStore(session, RandomIdFactory()),
            reconsolidation=reconsolidation,
            model_settings=PostgresModelSettingsStore(session),
            folders=PostgresFolderStore(session),
            email=PostgresEmailStore(session),
            calls=PostgresCallStore(session),
            people=people,
            records=records,
            integration_secrets=PostgresIntegrationSecretStore(session),
            jobs=PostgresJobStore(session),
            job_outbox=PostgresWidgetCommitOutbox(session, package_kind="job"),
            cockpit_widgets=PostgresWidgetStore(session),
            cockpit_outbox=PostgresWidgetCommitOutbox(session),
            cockpit_feeds=PostgresFeedStore(session),
            cockpit_events=PostgresCockpitEventLog(session),
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
            device_invocations=PostgresDeviceInvocationStore(session),
            device_ingest=PostgresDeviceIngestStore(session),
            notification_outbox=PostgresNotificationOutbox(session, clock),
            delegations=PostgresDelegationRepository(session),
            surfaces=PostgresSurfaceRepositories(
                admission=PostgresSurfaceAdmissionController(
                    session,
                    surface_admission_limits,
                ),
                pairings=PostgresSurfacePairingRepository(session),
                sessions=PostgresSurfaceSessionRepository(session),
                receipts=PostgresSurfaceReceiptRepository(session),
                replies=PostgresSurfaceReplyOutbox(session),
            ),
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
        # This unit of work commits only on exit, so sessions it creates stay
        # invisible to erasure for as long as it can project them.
        created_sessions: set[UUID] = set()
        events = PostgresEventRepository(session, self._clock, upcasters)
        history = PostgresSessionHistoryRepository(
            session, self._clock, upcasters, uncommitted_sessions=created_sessions
        )
        schedules = PostgresScheduleRepository(session)
        self.agents = PostgresAgentRepository(session, self._clock)
        self.process_events = PostgresProcessEventRepository(session)
        self.sessions = PostgresSessionRepository(session, created=created_sessions)
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
        require_email_credentials=False,
        require_call_credentials=False,
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
    return _schedule_principal(settings)


def _schedule_principal(settings: Settings) -> Principal:
    """The scheduler vouches for the same first-party rosters the application composes."""

    email_scopes = {
        scope
        for row in email_server_configs(
            settings.auth_tenant_id,
            enabled=settings.email_enabled,
            account_ids=settings.email_account_ids,
        )
        for scope in row.required_scopes
    }
    # ADR-0213: a call schedule's occurrence keeps its calling scopes.
    call_scopes = {
        scope
        for row in calling_server_configs(settings.auth_tenant_id, enabled=settings.call_enabled)
        for scope in row.required_scopes
    }
    # ADR-0254: a feed-keeping schedule's occurrence keeps its Cockpit scopes.
    feed_scopes = COCKPIT_FEED_SCOPES if settings.cockpit_enabled else ()
    return Principal(
        tenant_id=settings.auth_tenant_id,
        principal_id=settings.auth_principal_id,
        roles=set(settings.auth_roles),
        scopes={*settings.auth_scopes, *email_scopes, *call_scopes, *feed_scopes},
    )


async def serve_execution_service(
    socket_path: Path, *, allowed_images: tuple[str, ...] = ()
) -> None:
    """Run the credential-free production execution-service composition.

    Production names the sandbox and Cockpit builder images; the service then
    provisions nothing else (docs/plan/cockpit.md).
    """

    environment = DockerExecutionEnvironment(
        SystemClock(),
        RandomIdFactory(),
        runtime="runsc",
    )
    server = ExecutionServiceServer(
        environment,
        socket_path,
        resolve_image_digest=resolve_local_image_digest,
        allowed_images=allowed_images,
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


def _configure_service_logging(settings: Settings, *, enabled: bool) -> None:
    """Phase 1 of a long-running service: nothing before this point may log.

    A one-shot command's standard output is its result, so it does not opt in.
    """

    if enabled:
        configure_logging(settings.deployment_mode, release_id=settings.release_id)


@asynccontextmanager
async def build_schedule_worker(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
    service_logging: bool = False,
) -> AsyncIterator[ScheduleWorker]:
    """Build only the resources needed to materialize scheduled runs."""

    effective_settings = settings or load_schedule_worker_settings()
    _configure_service_logging(effective_settings, enabled=service_logging)
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
    service_logging: bool = False,
) -> AsyncIterator[NotificationWorker]:
    """Build only the resources needed to dispatch durable notifications."""

    effective_settings = settings or load_notification_worker_settings()
    _configure_service_logging(effective_settings, enabled=service_logging)
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


def _validate_surface_role(settings: Settings) -> Principal:
    validate_settings(
        settings,
        require_auth_token=False,
        require_execution_environment=False,
        require_email_credentials=False,
        require_call_credentials=False,
        require_surface_credentials=True,
    )
    if not settings.surface_worker_enabled or not settings.surface_api_enabled:
        raise ConfigurationError(
            "surface worker and API must both be enabled before the role starts"
        )
    if settings.deployment_mode is not DeploymentMode.PRODUCTION:
        raise ConfigurationError("surface worker requires the production process topology")
    if settings.auth_mode is not AuthMode.TOKEN:
        raise ConfigurationError("surface worker requires configured non-development identity")
    if not settings.database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
        raise ConfigurationError("surface worker requires PostgreSQL storage")
    if settings.auth_token is not None:
        raise ConfigurationError("surface worker environment must not contain an API bearer")
    if settings.credentials:
        raise ConfigurationError("surface worker environment must not contain provider keys")
    unknown = set(settings.auth_scopes) - set(PLATFORM_SCOPES)
    if unknown:
        raise ConfigurationError(
            "AUTH_SCOPES contains unknown platform scopes: " + ", ".join(sorted(unknown))
        )
    required = {"run.write", "surface.read", "surface.write"}
    if not required <= settings.auth_scopes:
        raise ConfigurationError(
            "surface worker identity requires run.write, surface.read, and surface.write"
        )
    return Principal(
        tenant_id=settings.auth_tenant_id,
        principal_id=settings.auth_principal_id,
        roles=set(settings.auth_roles),
        scopes=set(settings.auth_scopes),
    )


@asynccontextmanager
async def build_call_worker(
    *,
    settings: Settings | None = None,
    ingress: bool = False,
    clock: Clock | None = None,
    service_logging: bool = False,
) -> AsyncIterator[CallWorkerComposition]:
    """Compose either signed ingress or reconciliation without model credentials."""
    selected = settings or load_call_worker_settings(ingress=ingress)
    _configure_service_logging(selected, enabled=service_logging)
    validate_settings(
        selected,
        require_auth_token=False,
        require_execution_environment=False,
        require_email_credentials=False,
        require_surface_credentials=False,
        require_call_credentials=not ingress,
        require_owner_scopes=False,
    )
    if not selected.call_enabled or selected.call_configuration is None:
        raise ConfigurationError("calling is disabled")
    if (
        selected.deployment_mode is not DeploymentMode.PRODUCTION
        or selected.auth_mode is not AuthMode.TOKEN
        or not selected.database_url.startswith(("postgresql://", "postgresql+asyncpg://"))
    ):
        raise ConfigurationError(
            "calling roles require production PostgreSQL and a configured owner"
        )
    expected = set() if ingress else {"bland_read", "bland_call"}
    if set(selected.credentials) != expected or selected.auth_token is not None:
        raise ConfigurationError("calling role contains unrelated credentials")
    if ingress and (not selected.call_ingress_enabled or selected.call_webhook_secret is None):
        raise ConfigurationError("calling ingress is disabled or its signing secret is missing")
    owner = Principal(
        tenant_id=selected.auth_tenant_id,
        principal_id=selected.auth_principal_id,
        roles=set(selected.auth_roles),
        scopes=set(),
    )
    effective_clock = clock or SystemClock()
    runtime = load_config_document(selected, "runtime/limits.yaml")
    scheduling = runtime["scheduling"]
    admission = ScheduleAdmissionLimits.model_validate(
        {
            name: scheduling[name]
            for name in (
                "max_active_runs_per_tenant",
                "max_materializations_per_minute",
                "daily_cost",
                "monthly_cost",
            )
        }
    )
    engine = create_engine(selected.database_url)
    try:
        await assert_schema_revision(engine)
        factory = cast(
            UnitOfWorkFactory,
            PostgresUnitOfWorkFactory(
                create_session_factory(engine),
                _postgres_repository_factory(
                    effective_clock,
                    EventUpcasterRegistry(),
                    lease_seconds=float(runtime["worker"]["lease_seconds"]),
                    max_attempts=int(runtime["queue"]["max_attempts"]),
                    skill_store=FilesystemSkillPackageStore(
                        selected.artifact_root / "skill-packages"
                    ),
                    skill_validator=SkillPackageValidator(ConservativeTokenEstimator()),
                    ids=RandomIdFactory(),
                    schedule_admission_limits=admission,
                    surface_admission_limits=SurfaceLimits.model_validate(runtime["surfaces"]),
                    schedule_metrics=ScheduleMetrics(
                        tenant_hash_key=tenant_hash_key(selected.database_url)
                    ),
                ),
                owner.tenant_id,
            ),
        )
        provider = (
            None
            if ingress
            else BlandCallProvider(
                owner.tenant_id,
                _sdk_mcp_clients(),
                MappingCredentialResolver(
                    {
                        name: secret.get_secret_value()
                        for name, secret in selected.credentials.items()
                    }
                ),
            )
        )
        service = CallService(
            factory,
            effective_clock,
            owner,
            selected.call_configuration,
            provider,
            notifications=selected.call_notifications_enabled and not ingress,
        )
        yield CallWorkerComposition(
            worker=None if ingress else CallWorker(service),
            webhook_app=create_call_ingress(
                service, selected.call_webhook_secret.get_secret_value()
            )
            if ingress and selected.call_webhook_secret is not None
            else None,
        )
    finally:
        await engine.dispose()


@asynccontextmanager
async def build_surface_worker(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
    service_logging: bool = False,
) -> AsyncIterator[SurfaceWorkerComposition]:
    """Build the credential-minimized Telegram and optional WhatsApp surface role."""

    effective_settings = settings or load_surface_worker_settings()
    _configure_service_logging(effective_settings, enabled=service_logging)
    principal = _validate_surface_role(effective_settings)
    telegram_token = effective_settings.surface_telegram_token
    assert telegram_token is not None

    whatsapp_enabled = effective_settings.surface_whatsapp_enabled
    access_token = effective_settings.surface_whatsapp_token
    app_secret = effective_settings.surface_whatsapp_app_secret
    hook_key = effective_settings.surface_whatsapp_verify_token
    phone_number_id = effective_settings.surface_whatsapp_phone_number_id
    graph_api_version = effective_settings.surface_whatsapp_graph_api_version
    if whatsapp_enabled:
        assert access_token is not None
        assert app_secret is not None
        assert hook_key is not None
        assert phone_number_id is not None
        assert graph_api_version is not None

    runtime = load_config_document(effective_settings, "runtime/limits.yaml")
    surface_ruleset = load_ruleset_documents(
        load_config_document(effective_settings, "policy/default.yaml"),
        load_config_document(effective_settings, "policy/hardline.yaml"),
    )
    queue_limits = runtime["queue"]
    worker_limits = runtime["worker"]
    notification_limits = runtime["notifications"]
    scheduling = runtime["scheduling"]
    limits = SurfaceLimits.model_validate(runtime["surfaces"])
    schedule_limits = ScheduleAdmissionLimits.model_validate(
        {
            "max_active_runs_per_tenant": scheduling["max_active_runs_per_tenant"],
            "max_materializations_per_minute": scheduling["max_materializations_per_minute"],
            "daily_cost": scheduling["daily_cost"],
            "monthly_cost": scheduling["monthly_cost"],
        }
    )
    effective_clock = clock or SystemClock()
    effective_ids = ids or RandomIdFactory()
    engine = create_engine(effective_settings.database_url)
    telegram_transport = TelegramBotTransport(token=telegram_token)
    whatsapp_transport: WhatsAppCloudTransport | None = None
    if whatsapp_enabled:
        assert access_token is not None
        assert phone_number_id is not None
        assert graph_api_version is not None
        whatsapp_transport = WhatsAppCloudTransport(
            access_token=access_token,
            phone_number_id=phone_number_id,
            graph_api_version=graph_api_version,
            template_name=effective_settings.surface_whatsapp_template_name,
            template_language=effective_settings.surface_whatsapp_template_language,
        )
    live_events = InMemoryLiveEventBroadcaster()
    telegram_poller: TelegramPoller | None = None
    try:
        await assert_schema_revision(engine)
        skill_store = FilesystemSkillPackageStore(
            effective_settings.artifact_root / "skill-packages"
        )
        factory = cast(
            UnitOfWorkFactory,
            PostgresUnitOfWorkFactory(
                create_session_factory(engine),
                _postgres_repository_factory(
                    effective_clock,
                    EventUpcasterRegistry(),
                    lease_seconds=float(worker_limits["lease_seconds"]),
                    max_attempts=int(queue_limits["max_attempts"]),
                    skill_store=skill_store,
                    skill_validator=SkillPackageValidator(ConservativeTokenEstimator()),
                    ids=effective_ids,
                    schedule_admission_limits=schedule_limits,
                    surface_admission_limits=limits,
                    schedule_metrics=ScheduleMetrics(
                        tenant_hash_key=tenant_hash_key(effective_settings.database_url)
                    ),
                ),
                principal.tenant_id,
            ),
        )
        dispatcher = PostgresRunDispatcher()
        surface_state_writer = SurfaceRunStateWriter(effective_clock, effective_ids)

        public_runs = PublicRunService(
            uow_factory=factory,
            dispatcher=dispatcher,
            clock=effective_clock,
            ids=effective_ids,
            seed_checkpoint=DurableCheckpointSeeder(effective_clock),
            cancel_active=lambda _run_id: None,
            cancel_parked_run=surface_state_writer.stop,
            resume_waiting_run=surface_state_writer.requeue_after_input,
            resolve_open_question=WorkingStateManager.resolve_question,
            trajectory_export_enabled=False,
            live_events=live_events,
        )

        async def create_surface_session(
            uow: RepositoryUnitOfWork,
            bound_principal: Principal,
            update: SurfaceInboundMessage,
        ) -> Session:
            agent = await uow.agents.latest_version(DEFAULT_AGENT_ID)
            now = effective_clock.now()
            created = Session(
                id=effective_ids.new_id(),
                tenant_id=bound_principal.tenant_id,
                principal_id=bound_principal.principal_id,
                agent_id=agent.id,
                agent_version=agent.version,
                status=SessionStatus.ACTIVE,
                metadata={
                    "surface": update.provider.value,
                    "surface_id": str(update.surface_id),
                },
                created_at=now,
                updated_at=now,
            )
            await uow.sessions.create(created)
            await uow.events.append(
                NewEvent(
                    session_id=created.id,
                    run_id=None,
                    event_type="session.created",
                    payload_schema_version=2,
                    actor_type="surface",
                    actor_id=bound_principal.principal_id,
                    payload={"agent_id": str(agent.id), "title": None, "skill_pins": []},
                )
            )
            return created

        now = effective_clock.now()
        async with factory() as uow:

            async def register_surface(
                *,
                provider: PushProvider,
                client_device_id: str,
                name: str,
            ) -> Device:
                existing = await uow.devices.get_by_client_device_id(
                    client_device_id,
                    principal,
                )
                has_matching_route = (
                    existing is not None
                    and existing.push_provider is provider
                    and existing.push_token is not None
                )
                stored_push_token = (
                    existing.push_token if existing is not None and has_matching_route else None
                )
                stored_push_token_updated_at = (
                    existing.push_token_updated_at
                    if existing is not None and has_matching_route
                    else None
                )
                surface = Device(
                    id=(
                        uuid5(
                            NAMESPACE_URL,
                            f"veetbot:surface:{principal.tenant_id}:{client_device_id}",
                        )
                        if existing is None
                        else existing.id
                    ),
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    client_device_id=client_device_id,
                    name=name,
                    kind=DeviceKind.SURFACE,
                    platform=provider.value,
                    push_provider=provider if has_matching_route else None,
                    push_token=stored_push_token,
                    push_token_updated_at=stored_push_token_updated_at,
                    push_token_invalidated_at=(
                        None if existing is None else existing.push_token_invalidated_at
                    ),
                    muted_kinds=frozenset(),
                    capabilities=frozenset(),
                    status=DeviceStatus.ACTIVE,
                    last_seen_at=now,
                    created_at=now if existing is None else existing.created_at,
                    updated_at=now,
                )
                surface = await uow.devices.upsert(surface, principal)
                if (
                    await uow.process_events.get_by_derivation(f"surface.registered:{surface.id}")
                    is None
                ):
                    await uow.process_events.append(
                        ProcessEvent(
                            id=effective_ids.new_id(),
                            event_type="surface.registered",
                            actor_type="surface",
                            actor_id=principal.principal_id,
                            payload={
                                "tenant_id": principal.tenant_id,
                                "principal_id": principal.principal_id,
                                "surface_id": str(surface.id),
                                "provider": provider.value,
                            },
                            derivation_key=f"surface.registered:{surface.id}",
                            created_at=now,
                        )
                    )
                return surface

            telegram_surface = await register_surface(
                provider=PushProvider.TELEGRAM,
                client_device_id="telegram:configured",
                name="Veetbot Telegram",
            )
            whatsapp_surface = (
                None
                if not whatsapp_enabled
                else await register_surface(
                    provider=PushProvider.WHATSAPP,
                    client_device_id=f"whatsapp:{phone_number_id}",
                    name="Veetbot WhatsApp",
                )
            )

        async def send_telegram_notice(chat_ref: str, reason_code: str) -> None:
            await telegram_transport.send_text(chat_ref, surface_notice_text(reason_code))

        async def send_whatsapp_notice(chat_ref: str, reason_code: str) -> None:
            assert whatsapp_transport is not None
            await whatsapp_transport.send_text(chat_ref, surface_notice_text(reason_code))

        principal_directory = ConfiguredSchedulePrincipalDirectory(principal)
        ingress = SurfaceIngressService(
            uow_factory=factory,
            principals=principal_directory,
            run_budget=limits.run_budget,
            self_approval_enabled=surface_ruleset.self_approval_enabled,
            clock=effective_clock,
            ids=effective_ids,
            create_session=create_surface_session,
            submit=public_runs.submit_surface_in,
            dispatch=public_runs.dispatch_surface_submission,
            notice=send_telegram_notice,
            stop_run=surface_state_writer.stop,
            resume_after_approval=surface_state_writer.requeue_after_approval,
            notices={
                PushProvider.TELEGRAM: send_telegram_notice,
                **(
                    {}
                    if whatsapp_transport is None
                    else {PushProvider.WHATSAPP: send_whatsapp_notice}
                ),
            },
            session_idle_seconds=limits.session_idle_seconds,
            max_code_attempts=limits.code_max_attempts,
            lockout_seconds=limits.lockout_seconds,
            per_sender_messages_per_minute=limits.per_sender_messages_per_minute,
            inbound_text_max_chars=limits.inbound_text_max_chars,
        )

        async def deliver_telegram_reply(
            chat_ref: str,
            text: str,
            *,
            last_inbound_at: datetime | None,
            now: datetime,
        ) -> SurfaceTransportResult:
            del last_inbound_at, now
            return await telegram_transport.send_text(chat_ref, text)

        deliveries: dict[PushProvider, SurfaceDelivery] = {
            PushProvider.TELEGRAM: deliver_telegram_reply
        }
        template_policies: dict[PushProvider, SurfaceTemplatePolicy] = {}
        whatsapp_delivery: WhatsAppDeliveryService | None = None
        if whatsapp_transport is not None:
            whatsapp_delivery = WhatsAppDeliveryService(transport=whatsapp_transport)
            deliveries[PushProvider.WHATSAPP] = whatsapp_delivery.deliver
            template_policies[PushProvider.WHATSAPP] = whatsapp_delivery
        replies = SurfaceReplyDispatcher(
            uow_factory=factory,
            deliveries=deliveries,
            template_policies=template_policies,
            clock=effective_clock,
            redactor=TrajectoryRedactor(),
            worker_id=f"surface:{socket.gethostname()}:{os.getpid()}",
            batch_size=limits.claim_batch,
            lease_seconds=limits.lease_seconds,
            retry_delays=limits.retry_delays_seconds,
            max_attempts=limits.reply_max_attempts,
            chunk_size=limits.chunk_size,
        )
        notification_transport = SurfaceNotificationTransport(
            uow_factory=factory,
            principals=principal_directory,
            deliveries=deliveries,
            template_policies=template_policies,
            clock=effective_clock,
            redactor=TrajectoryRedactor(),
            chunk_size=limits.chunk_size,
        )
        notification_dispatcher = NotificationDispatcher(
            uow_factory=cast(NotificationDispatchUnitOfWorkFactory, factory),
            transport=notification_transport,
            providers=frozenset(deliveries),
            clock=effective_clock,
            ids=effective_ids,
            claimant=f"surface-notify:{socket.gethostname()}:{os.getpid()}",
            batch_size=int(notification_limits["claim_batch"]),
            lease_seconds=float(notification_limits["lease_seconds"]),
            retry_delays=tuple(
                float(value) for value in notification_limits["retry_delays_seconds"]
            ),
        )

        async def dispatch_surface_outbound() -> int:
            replies_processed = await replies.run_once()
            notifications_processed = await notification_dispatcher.run_once()
            return replies_processed + notifications_processed

        async def latest_telegram_update_id() -> int | None:
            async with factory() as uow:
                return await uow.surfaces.receipts.latest_numeric_update_id(telegram_surface.id)

        poll_resumed_recorded = False

        async def telegram_poll_succeeded(offset: int) -> None:
            nonlocal poll_resumed_recorded
            async with factory() as uow:
                surface = await uow.devices.get(telegram_surface.id, principal)
                poll_now = effective_clock.now()
                await uow.devices.upsert(
                    surface.model_copy(update={"last_seen_at": poll_now, "updated_at": poll_now}),
                    principal,
                )
                if not poll_resumed_recorded:
                    await uow.process_events.append(
                        ProcessEvent(
                            id=effective_ids.new_id(),
                            event_type="surface.poll.resumed",
                            actor_type="surface",
                            actor_id=principal.principal_id,
                            payload={
                                "tenant_id": principal.tenant_id,
                                "principal_id": principal.principal_id,
                                "surface_id": str(telegram_surface.id),
                                "offset": offset,
                            },
                            derivation_key=(
                                f"surface.poll.resumed:{telegram_surface.id}:{poll_now.isoformat()}"
                            ),
                            created_at=poll_now,
                        )
                    )
                    poll_resumed_recorded = True

        telegram_poller = TelegramPoller(
            surface_id=telegram_surface.id,
            transport=telegram_transport,
            latest_committed_update_id=latest_telegram_update_id,
            ingest=ingress.ingest,
            record_unreadable=ingress.record_unreadable,
            poll_lock=PostgresTelegramPollLock(
                engine,
                tenant_id=principal.tenant_id,
                surface_id=telegram_surface.id,
            ),
            timeout_seconds=limits.poll_timeout_seconds,
            fallback_poll_seconds=limits.fallback_poll_seconds,
            poll_succeeded=telegram_poll_succeeded,
        )
        webhook: FastAPI | None = None
        if whatsapp_surface is not None:
            assert phone_number_id is not None
            assert app_secret is not None
            assert hook_key is not None
            webhook = create_whatsapp_webhook_app(
                surface_id=whatsapp_surface.id,
                phone_number_id=phone_number_id,
                app_secret=app_secret,
                verify_token=hook_key,
                ingest=ingress.ingest,
            )
        yield SurfaceWorkerComposition(
            worker=SurfaceWorker(
                dispatch_once=dispatch_surface_outbound,
                clock=effective_clock,
                fallback_poll_seconds=limits.fallback_poll_seconds,
                additional_services=(telegram_poller,),
            ),
            webhook_app=webhook,
        )
    finally:
        if telegram_poller is not None:
            await telegram_poller.aclose()
        await live_events.close()
        await telegram_transport.aclose()
        if whatsapp_transport is not None:
            await whatsapp_transport.aclose()
        await engine.dispose()


async def _compose(
    *,
    storage: Literal["memory", "postgres"],
    recall_schema_sha256: str | None,
    settings: Settings,
    agent: AgentSpec,
    email_budget_limits: EmailBudgetLimits,
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
    inline_output_bytes: int,
    capture_step_maximum_reads: int,
    capture_run_maximum_reads: int,
    lease_seconds: float,
    heartbeat_divisor: int,
    worker_poll_interval: float,
    interactive_priority: int,
    async_priority: int,
    schedule_scan_batch: int,
    schedule_fallback_poll_seconds: float,
    schedule_admission_backoff_seconds: float,
    schedule_terminal_retention_days: int,
    schedule_terminal_purge_interval_seconds: float,
    schedule_terminal_purge_batch: int,
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
    folder_profiles: FolderProfiles,
    title_profiles: TitleProfiles,
    mcp_config: Mapping[str, object],
    max_compactions_per_step: int,
    skill_store: SkillPackageStore,
    mcp_clients: MCPClientFactory | None,
    mcp_scripts: Mapping[str, ScriptedMCPServer] | None,
    credential_resolver: CredentialResolver,
    mcp_server_configs: tuple[MCPServerConfig, ...],
    web_search_provider: WebProvider | WebProviderRouter | None,
    web_fetch_provider: WebProvider | WebProviderRouter | None,
    media_provider: MediaGenerationProvider | None,
    one_click_transport: OneClickTransport | None,
    judgment_provider: JudgmentProvider | None,
    memory_provider_evaluation_mode: bool,
    memory_provider_evaluation_policy: str,
    memory_distillation_evaluation_mode: bool,
    memory_people_evaluation_mode: bool,
    memory_legacy_formation_control: bool,
    memory_reasoning_effort: ReasoningEffort | None,
    browser_provider: BrowserProvider | None,
    browser_profile_lifecycle: BrowserProfileControlPlane,
    browser_authentications: BrowserAuthenticationControlPlane,
    device_channel: DeviceChannel | None,
    device_invocation_timeout_seconds: int,
    device_invocation_poll_seconds: float,
    device_ingest_daily_cap: int,
    surface_limits: SurfaceLimits,
    recall_capture: Callable[[SemanticCompositionInputs], None] | None = None,
    cockpit_builder_override: WidgetBuilder | None = None,
    cockpit_local_branch: bool = False,
    job_runner_override: JobRunner | None = None,
    job_screen_override: JobScreen | None = None,
    mcp_oauth_gateway: MCPOAuthGateway | None = None,
) -> tuple[Composition, list[ModelProvider]]:
    """Assemble the complete runtime graph for one selected storage backend."""

    if settings.browser_task_grants_enabled:
        async with (
            uow_factory() as uow,
            uow.browser_task_grants.locked_scopes(
                principal, defaults=settings.browser_task_grant_scopes
            ),
        ):
            pass
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
    owner_mcp_proxy: WorkerEgressProxy | None = None

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
        if config.owner_managed:
            raise ConfigurationError("owner-added MCP servers are added only from Settings")
        validate_mcp_config(config, destination_allowed=destination_allowed)
    async with uow_factory() as uow:
        for config in mcp_server_configs:
            stored = await uow.mcp_servers.get(config.tenant_id, config.server_id)
            if stored is not None and stored.owner_managed:
                raise ConfigurationError(
                    f"MCP server {config.server_id} is owner-added; rename the configured row"
                )
            await uow.mcp_servers.put(config)
    async with uow_factory() as uow:
        effective_mcp_configs = [
            config
            for config in await uow.mcp_servers.list_enabled(principal.tenant_id)
            # ADR-0255: owner-added rows exist only while their feature is on.
            if settings.mcp_servers_enabled or not config.owner_managed
        ]
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
    idle_timeout = mcp_config.get("idle_timeout_seconds")
    if not isinstance(idle_timeout, (int, float)) or isinstance(idle_timeout, bool):
        raise ConfigurationError("MCP idle timeout must be numeric")
    if idle_timeout <= 0:
        raise ConfigurationError("MCP idle timeout must be positive")
    discovery_reuse = mcp_config.get("discovery_reuse_seconds")
    if not isinstance(discovery_reuse, (int, float)) or isinstance(discovery_reuse, bool):
        raise ConfigurationError("MCP discovery reuse must be numeric")
    if discovery_reuse <= 0:
        raise ConfigurationError("MCP discovery reuse must be positive")
    warmup_limits: dict[str, int] = {}
    for name in ("warmup_connect_timeout_seconds", "warmup_retry_seconds", "warmup_attempts"):
        value = mcp_config.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigurationError(f"MCP {name} must be a positive integer")
        warmup_limits[name] = value
    cockpit_sandbox: SandboxManager | None = None
    if storage == "memory" or settings.sandbox.value == "fake":
        fake_environment = FakeExecutionEnvironment(clock, ids)
        job_environment: Any = fake_environment
        job_image_options: dict[str, Any] = {"image_digest": fake_image_digest()}
        sandbox_manager = SandboxManager(
            fake_environment,
            image_digest=fake_image_digest(),
            limits=sandbox_limits,
            egress=egress,
            parent_environment=os.environ,
            passthrough_names=settings.sandbox_passthrough,
        )
        if settings.cockpit_enabled or settings.jobs_enabled:
            cockpit_sandbox = cockpit_build_sandbox(
                fake_environment, image_digest=fake_image_digest(), limits=sandbox_limits
            )
    elif settings.sandbox.value in {"docker", "gvisor"}:
        execution_environment: ExecutionServiceClient | DockerExecutionEnvironment
        if settings.execution_service_socket is not None:
            execution_environment = ExecutionServiceClient(settings.execution_service_socket)
            image_resolver = partial(
                execution_environment.resolve_image_digest,
                settings.sandbox_image,
            )
            builder_image_resolver = partial(
                execution_environment.resolve_image_digest,
                settings.cockpit_builder_image,
            )
        else:
            execution_environment = DockerExecutionEnvironment(
                clock, ids, runtime="runsc" if settings.sandbox.value == "gvisor" else None
            )
            image_resolver = partial(resolve_local_image_digest, settings.sandbox_image)
            builder_image_resolver = partial(
                resolve_local_image_digest, settings.cockpit_builder_image
            )
        job_environment = execution_environment
        job_image_options = {"resolve_image_digest": image_resolver}
        sandbox_manager = SandboxManager(
            execution_environment,
            resolve_image_digest=image_resolver,
            limits=sandbox_limits,
            egress=egress,
            parent_environment=os.environ,
            passthrough_names=settings.sandbox_passthrough,
        )
        if settings.cockpit_enabled or settings.jobs_enabled:
            cockpit_sandbox = cockpit_build_sandbox(
                execution_environment,
                resolve_image_digest=builder_image_resolver,
                limits=sandbox_limits,
            )
    else:
        raise ConfigurationError("microvm sandbox adapter is not configured in this deployment")
    cockpit_builder: WidgetBuilder | None = None
    if cockpit_sandbox is not None:
        # An evaluation judges the agent's package without the builder image.
        cockpit_builder = cockpit_builder_override or SandboxWidgetBuilder(cockpit_sandbox, ids)
    cockpit_publication: CockpitPublication | None = None
    cockpit_service: CockpitBoard | None = None
    cockpit_maintenance: _CockpitMaintenance | None = None
    cockpit_toolset: CockpitToolset | None = None
    local_branch: FakeWidgetRepository | None = None
    local_outbox: CommitOutboxWorker | None = None
    if cockpit_builder is not None and settings.cockpit_enabled:
        cockpit_profiles = CockpitProfiles.from_document(
            load_config_document(settings, "cockpit/profiles.yaml")
        )
        # The owner's name as the calling configuration records it, when calling is set up.
        owner_names = (
            (settings.call_configuration.public_name,)
            if settings.call_configuration is not None
            else ()
        )
        cockpit_publication = CockpitPublication(
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            builder=cockpit_builder,
            owner_names=owner_names,
            limits=CockpitPublicationLimits(
                commits_per_widget_per_hour=(
                    cockpit_profiles.publication.commits_per_widget_per_hour
                ),
                commits_per_principal_per_hour=(
                    cockpit_profiles.publication.commits_per_principal_per_hour
                ),
            ),
        )
        cockpit_service = CockpitBoard(
            uow_factory=uow_factory,
            clock=clock,
            live_events=live_events,
            publication=cockpit_publication,
            runtime=FileCockpitRuntime(settings.artifact_root / "cockpit-runtime"),
            settings_schema=Draft202012SchemaCheck(),
            stream_max_seconds=cockpit_profiles.stream.max_seconds,
            repository=settings.cockpit_github_repository,
        )
        cockpit_toolset = CockpitToolset(
            uow_factory=uow_factory,
            clock=clock,
            board=cockpit_service,
            publication=cockpit_publication,
            feeds=CockpitFeeds(
                uow_factory=uow_factory,
                clock=clock,
                schema_check=Draft202012SchemaCheck(),
                history_max=cockpit_profiles.feeds.history_max,
            ),
            catalogue=load_kit_catalogue(),
            commit_wait_seconds=cockpit_profiles.publication.commit_wait_seconds,
        )
        if cockpit_local_branch:
            local_branch = FakeWidgetRepository()
            local_outbox = CommitOutboxWorker(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                repository=local_branch,
                principal=principal,
            )
        cockpit_maintenance = _CockpitMaintenance(
            settings,
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            principal=principal,
            builder=cockpit_builder,
            owner_names=owner_names,
            profiles=cockpit_profiles,
            feed_sources=platform_feed_sources(
                email_account_ids=(
                    (settings.email_account_ids or ("default",))
                    if settings.email_enabled and settings.email_mode_enabled
                    else ()
                ),
                schedules=settings.schedule_api_enabled,
                calls=settings.call_enabled and settings.call_configuration is not None,
                people=settings.people_enabled,
            ),
        )
    integration_secrets = None
    if settings.integration_secrets_enabled:
        if settings.integration_secrets_key is None:
            raise ConfigurationError("Integration keys require the configured master key")
        integration_secrets = IntegrationSecrets(
            uow_factory,
            IntegrationSecretSealer(
                base64.urlsafe_b64decode(settings.integration_secrets_key.get_secret_value()),
                purpose="veetbot.integration-secret",
            ),
            clock,
            ids,
        )
    job_publication: JobPublication | None = None
    job_execution: JobExecution | None = None
    job_credentials: GitHubReaderCredentials | None = None
    if settings.jobs_enabled:
        assert cockpit_sandbox is not None
        job_credentials = GitHubReaderCredentials(
            token_factory=GitHubAppTokens,
            app_id=settings.jobs_github_app_id,
            installation_id=settings.jobs_github_installation_id,
            key_file=settings.jobs_github_key_file,
            clock=clock,
        )
        job_publication = JobPublication(
            integration_secrets=integration_secrets,
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            schema_check=Draft202012SchemaCheck(),
            screen=job_screen_override or SandboxJobScreen(cockpit_sandbox, ids),
            owner_names=(
                (settings.call_configuration.public_name,)
                if settings.call_configuration is not None
                else ()
            ),
            repository=FakeWidgetRepository(package_kind="job") if cockpit_local_branch else None,
        )

        def job_runner(timeout: int) -> JobRunner:
            """Use the supplied test runner or build a sandbox runner with the grant's timeout."""
            if job_runner_override is not None:
                return job_runner_override
            return SandboxJobRunner(
                SandboxManager(
                    job_environment,
                    limits=replace(sandbox_limits, wall_clock_seconds=timeout),
                    **job_image_options,
                ),
                timeout_seconds=timeout,
            )

        job_execution = JobExecution(
            uow_factory=uow_factory,
            clock=clock,
            runner=job_runner,
            schema_check=Draft202012SchemaCheck(),
            http=JobHTTP(job_credentials, operator_hosts=settings.jobs_allowed_hosts).request,
            http_for_access=(
                lambda owner, access: (
                    JobHTTP(
                        job_credentials,
                        operator_hosts=settings.jobs_allowed_hosts,
                        stored_credentials=StoredJobCredentials(
                            integration_secrets, owner, access
                        ).resolve,
                    ).request
                )
            )
            if integration_secrets is not None
            else None,
            feeds=CockpitFeeds(
                uow_factory=uow_factory,
                clock=clock,
                schema_check=Draft202012SchemaCheck(),
                history_max=288,
            ),
        )
    if job_publication is not None:
        if cockpit_maintenance is None:
            assert cockpit_builder is not None
            cockpit_maintenance = _CockpitMaintenance(
                settings,
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                principal=principal,
                builder=cockpit_builder,
                owner_names=(),
                profiles=CockpitProfiles.from_document(
                    load_config_document(settings, "cockpit/profiles.yaml")
                ),
            )
        cockpit_maintenance.jobs = job_publication
    estimator = ConservativeTokenEstimator()
    working_state = WorkingStateManager(clock, working_config, estimator)
    episode_search = EventEpisodeSearch(uow_factory, principal)
    schedule_service = ScheduleService(
        uow_factory=uow_factory,
        clock=clock,
        ids=ids,
        limits=schedule_definition_limits,
        wake_worker=schedule_notify,
    )
    registry = StaticToolRegistry()
    if job_publication is not None and job_execution is not None:
        job_toolset = JobToolset(job_publication, job_execution)
        registry.register(JobManageTool(job_toolset))
        registry.register(JobRunTool(job_toolset))
        if integration_secrets is not None:
            registry.register(StoredKeyJobRunTool(job_toolset))
    registry.register(CalculatorTool())
    registry.register(AskUserTool())
    registry.register(ToolCallTool())
    registry.register(CurrentTimeTool(clock))
    registry.register(WorkspaceReadTextTool())
    registry.register(WorkspaceWriteTextTool())
    registry.register(WorkspaceListFilesTool())
    registry.register(DemoExternalWriteTool())
    registry.register(
        SandboxRunCommandTool(sandbox_manager, hard_ceiling_multiplier=hard_ceiling_multiplier)
    )
    registry.register(ArtifactExportTool())
    if media_provider is not None:
        media_inputs = StoredMediaInputResolver(
            uow_factory=uow_factory, store=FilesystemArtifactStore(artifact_root), clock=clock
        )
        registry.register(ImageGenerationTool(media_provider, image_resolver=media_inputs))
        registry.register(VideoGenerationTool(media_provider, image_resolver=media_inputs))
    if web_search_provider is not None:
        registry.register(WebSearchTool(web_search_provider))
    if web_fetch_provider is not None:
        registry.register(WebFetchTool(web_fetch_provider))
        registry.register(WebFetchJsonTool(web_fetch_provider))
        registry.register(WebInspectImageTool(PublicImageReader().read))
    if browser_provider is not None:
        registry.register(BrowserNavigateTool(browser_provider))
        registry.register(
            PagedBrowserObserveTool(browser_provider, inline_output_bytes=inline_output_bytes)
        )
        registry.register(BrowserObserveTool(browser_provider))
        registry.register(ArticleBrowserObserveTool(browser_provider))
        if isinstance(browser_provider, BrowserImageUploader):
            registry.register(
                BrowserUploadTool(
                    browser_provider,
                    image_resolver=StoredMediaInputResolver(
                        uow_factory=uow_factory,
                        store=FilesystemArtifactStore(artifact_root),
                        clock=clock,
                    ),
                )
            )
        # ADR-0129: the approval card describes the action whatever the flag;
        # it offers a task grant only when task grants are enabled.
        registry.register(
            BrowserActTool(
                browser_provider,
                presenter=BrowserActApprovalPresenter(
                    browser_provider,
                    context_reader=_task_grant_context_reader(uow_factory, clock),
                    enabled=_task_grants_composed(settings, browser_provider),
                    now=clock.now,
                ),
            )
        )
    if settings.delegation_enabled:
        registry.register(DelegateRunTool())
    if settings.schedule_api_enabled and settings.schedule_worker_enabled:
        registry.register(
            SingleCallScheduleCreateTool(
                schedule_service,
                agent,
                schedule_definition_limits,
                uow_factory=uow_factory,
                calling_enabled=settings.call_enabled,
            )
        )
        registry.register(
            ScheduleCreateTool(
                schedule_service,
                agent,
                schedule_definition_limits,
                uow_factory=uow_factory,
                calling_enabled=settings.call_enabled,
            )
        )
        if settings.cockpit_enabled:
            for cockpit_create in (PreviousCockpitScheduleCreateTool, CockpitScheduleCreateTool):
                registry.register(
                    cockpit_create(
                        schedule_service,
                        agent,
                        schedule_definition_limits,
                        uow_factory=uow_factory,
                        calling_enabled=settings.call_enabled,
                    )
                )
        registry.register(PreviousScheduleListTool(schedule_service))
        registry.register(ScheduleListTool(schedule_service))
        registry.register(ScheduleUpdateTool(schedule_service))
        if settings.cockpit_enabled:
            registry.register(CockpitScheduleUpdateTool(schedule_service))
        if settings.jobs_enabled:
            registry.register(
                JobScheduleCreateTool(
                    schedule_service,
                    agent,
                    schedule_definition_limits,
                    uow_factory=uow_factory,
                    calling_enabled=settings.call_enabled,
                )
            )
            registry.register(
                JobScheduleUpdateTool(
                    schedule_service, uow_factory, agent, schedule_definition_limits
                )
            )
        if settings.jobs_enabled and cockpit_toolset is not None:
            for installation_tool in (CockpitPrepareTool, CockpitInstallTool):
                registry.register(
                    installation_tool(
                        cockpit_toolset,
                        job_toolset,
                        JobScheduleCreateTool(
                            schedule_service,
                            agent,
                            schedule_definition_limits,
                            uow_factory=uow_factory,
                        ),
                        settings.jobs_allowed_hosts,
                    )
                )
        registry.register(SchedulePauseTool(schedule_service))
        registry.register(ScheduleResumeTool(schedule_service))
        registry.register(ScheduleCancelTool(schedule_service))

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
    job_ruleset = job_publication_ruleset(ruleset) if settings.jobs_enabled else None
    async with uow_factory() as uow:
        await uow.agents.put(agent)
        if job_ruleset is not None:
            await uow.policy_profiles.record(
                PolicyProfileRecord(
                    policy_version=job_ruleset.policy_version,
                    profile_name=job_ruleset.profile_name,
                    profile_sha256=job_ruleset.profile_sha256,
                    hardline_sha256=job_ruleset.hardline_sha256,
                    rule_count=len(job_ruleset.rules)
                    + len(job_ruleset.tool_rules)
                    + len(job_ruleset.hardline),
                    loaded_at=clock.now(),
                    loaded_by="composition-root",
                )
            )
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
    memory_extractor: MemoryCandidateExtractor | None = None
    memory_policy_version = FORMATION_POLICY_VERSION
    memory_mode = settings.memory_provider_extraction_mode
    extraction_model_policy = (
        agent.model_policy
        if (
            agent.model_policy in NON_ROUTED_MODEL_POLICIES
            or memory_provider_evaluation_mode
            or memory_distillation_evaluation_mode
            or memory_people_evaluation_mode
        )
        else memory_profiles.formation.model_policy
    )
    evaluating_memory = (
        memory_provider_evaluation_mode
        or memory_distillation_evaluation_mode
        or memory_people_evaluation_mode
    )
    # ADR-0119: evidence binds the effort, so formation sends exactly the
    # evaluated one: the requested effort under evaluation, the profile's
    # otherwise, and none for a deterministic development policy.
    extraction_effort = (
        memory_reasoning_effort
        if evaluating_memory
        else None
        if agent.model_policy in NON_ROUTED_MODEL_POLICIES
        else memory_profiles.formation.reasoning_effort
    )
    if memory_provider_evaluation_mode and extraction_effort is not None:
        raise ConfigurationError("provider-assisted formation evaluates the provider default only")
    extraction_model: ResolvedModel | None = None
    evidence_build_ref: str | None = None
    evidence_corpus_sha256: str | None = None
    evidence_source: str | None = None
    selection_outcome = "disabled"
    selection_reason = "configured_off"
    if (
        memory_mode is not MemoryProviderExtractionMode.OFF
        or memory_provider_evaluation_mode
        or memory_distillation_evaluation_mode
        or memory_people_evaluation_mode
    ):
        try:
            if extraction_model_policy in NON_ROUTED_MODEL_POLICIES:
                extraction_model = ResolvedModel(
                    provider="fake",
                    model="scripted",
                    credential_ref="fake",
                    policy_name=extraction_model_policy,
                    resolved_at=clock.now(),
                )
            else:
                extraction_model = await model_router.resolve(
                    extraction_model_policy,
                    tenant_id=principal.tenant_id,
                    required=frozenset({Capability.STRUCTURED_OUTPUT, Capability.STREAMING}),
                )
        except ConfigurationError:
            if (
                memory_provider_evaluation_mode
                or memory_distillation_evaluation_mode
                or memory_people_evaluation_mode
                or memory_mode is MemoryProviderExtractionMode.REQUIRED
            ):
                raise
            selection_outcome = "deterministic_fallback"
            selection_reason = "model_resolution_failed"
        if (
            extraction_model is not None
            and extraction_effort is not None
            and extraction_effort not in extraction_model.reasoning_efforts
        ):
            raise ConfigurationError(
                f"memory model policy {extraction_model_policy!r} does not accept "
                f"reasoning effort {extraction_effort.value!r}"
            )
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
                    or memory_distillation_evaluation_mode
                    or memory_people_evaluation_mode
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
                    formation_policy_version=memory_provider_evaluation_policy,
                )
                selection_outcome = "evaluation"
                selection_reason = "explicit_evaluation_mode"
            elif memory_people_evaluation_mode:
                assert extraction_provider is not None
                memory_extractor = OwnerPeopleCandidateExtractor(
                    provider=extraction_provider,
                    resolved_model=extraction_model,
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    fallback=HighRecallCandidateExtractor(),
                    reasoning_effort=extraction_effort,
                )
                selection_outcome = "evaluation"
                selection_reason = "explicit_people_evaluation_mode"
            elif memory_distillation_evaluation_mode:
                assert extraction_provider is not None
                memory_extractor = NemoriAssistedCandidateExtractor(
                    provider=extraction_provider,
                    resolved_model=extraction_model,
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    fallback=HighRecallCandidateExtractor(),
                    reasoning_effort=extraction_effort,
                )
                selection_outcome = "evaluation"
                selection_reason = "explicit_distillation_evaluation_mode"
            elif settings.people_enabled and settings.memory_formation_policy_pin in (
                None,
                MemoryFormationPolicyPin.PEOPLE,
            ):
                # ADR-0101: quality evidence measures People; it does not gate access.
                assert extraction_provider is not None
                memory_extractor = OwnerPeopleCandidateExtractor(
                    provider=extraction_provider,
                    resolved_model=extraction_model,
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    fallback=HighRecallCandidateExtractor(),
                    reasoning_effort=extraction_effort,
                )
                memory_policy_version = "formation@11"
                selection_outcome = "activated"
                selection_reason = "people_default"
            else:
                selected_evidence = None
                selected_distillation_evidence = None
                evidence_paths = provider_extraction_evidence_paths(settings)
                policy_pin = settings.memory_formation_policy_pin
                distillation_corpus_sha256, formation_corpus_sha256, distillation_holdout_sha256 = (
                    _shipped_evidence_digests()
                )

                provider_pins = (
                    MemoryFormationPolicyPin.PROVIDER_ASSISTED,
                    MemoryFormationPolicyPin.REPAIRED_PROVIDER_ASSISTED,
                )
                selected_provider_policy = PROVIDER_FORMATION_POLICY_VERSION
                for evidence_path in evidence_paths:
                    if policy_pin in (
                        *provider_pins,
                        MemoryFormationPolicyPin.PEOPLE,
                    ):
                        break
                    try:
                        candidate_distillation_evidence = load_memory_distillation_evidence(
                            evidence_path
                        )
                    except ConfigurationError:
                        pass
                    else:
                        if distillation_evidence_matches(
                            candidate_distillation_evidence,
                            extraction_model,
                            agent.policy_profile,
                            ruleset.policy_version,
                            corpus_sha256=distillation_corpus_sha256,
                            holdout_sha256=distillation_holdout_sha256,
                            reasoning_effort=extraction_effort,
                        ) and _operator_artifact_is_bound(
                            settings, evidence_path, candidate_distillation_evidence.build_ref
                        ):
                            selected_distillation_evidence = candidate_distillation_evidence
                            evidence_source = (
                                "operator"
                                if evidence_path == settings.memory_provider_extraction_evidence
                                else "release"
                            )
                            break
                # The repaired policy outranks the frozen control; a pin narrows
                # the search to exactly one provider-assisted policy.
                if policy_pin is MemoryFormationPolicyPin.PROVIDER_ASSISTED:
                    provider_policies: tuple[str, ...] = (PROVIDER_FORMATION_POLICY_VERSION,)
                elif policy_pin is MemoryFormationPolicyPin.REPAIRED_PROVIDER_ASSISTED:
                    provider_policies = (REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,)
                elif policy_pin in (
                    MemoryFormationPolicyPin.DISTILLATION,
                    MemoryFormationPolicyPin.PEOPLE,
                ):
                    provider_policies = ()
                else:
                    provider_policies = (
                        REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,
                        PROVIDER_FORMATION_POLICY_VERSION,
                    )
                # The provider-assisted artifacts were evaluated at the
                # provider default and record no effort.
                if selected_distillation_evidence is None and extraction_effort is None:
                    for provider_policy in provider_policies:
                        for evidence_path in evidence_paths:
                            try:
                                candidate_evidence = load_provider_extraction_evidence(
                                    evidence_path
                                )
                            except ConfigurationError:
                                continue
                            if provider_extraction_evidence_matches(
                                candidate_evidence,
                                extraction_model,
                                agent.policy_profile,
                                ruleset.policy_version,
                                formation_policy_version=provider_policy,
                                corpus_sha256=formation_corpus_sha256,
                            ) and _operator_artifact_is_bound(
                                settings, evidence_path, candidate_evidence.build_ref
                            ):
                                selected_evidence = candidate_evidence
                                selected_provider_policy = provider_policy
                                evidence_source = (
                                    "operator"
                                    if evidence_path == settings.memory_provider_extraction_evidence
                                    else "release"
                                )
                                break
                        if selected_evidence is not None:
                            break
                if selected_distillation_evidence is None and selected_evidence is None:
                    if memory_mode is MemoryProviderExtractionMode.REQUIRED:
                        raise ConfigurationError(
                            "provider-backed memory extraction requires matching "
                            "evaluation evidence"
                        )
                    selection_outcome = "deterministic_fallback"
                    selection_reason = (
                        "pinned_policy_unevidenced"
                        if policy_pin is not None
                        else "no_matching_evidence"
                    )
                else:
                    assert extraction_provider is not None
                    if selected_distillation_evidence is not None:
                        memory_extractor = NemoriAssistedCandidateExtractor(
                            provider=extraction_provider,
                            resolved_model=extraction_model,
                            uow_factory=uow_factory,
                            clock=clock,
                            ids=ids,
                            fallback=HighRecallCandidateExtractor(),
                            reasoning_effort=extraction_effort,
                        )
                        memory_policy_version = NEMORI_FORMATION_POLICY_VERSION
                        evidence_build_ref = selected_distillation_evidence.build_ref
                        evidence_corpus_sha256 = selected_distillation_evidence.corpus_sha256
                    else:
                        assert selected_evidence is not None
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
                            formation_policy_version=selected_provider_policy,
                        )
                        memory_policy_version = selected_provider_policy
                        evidence_build_ref = selected_evidence.build_ref
                        evidence_corpus_sha256 = selected_evidence.corpus_sha256
                    selection_outcome = "activated"
                    selection_reason = "matching_evidence"
        if memory_provider_evaluation_mode:
            memory_policy_version = memory_provider_evaluation_policy
        elif memory_people_evaluation_mode:
            memory_policy_version = "formation@11"
        elif memory_distillation_evaluation_mode:
            memory_policy_version = NEMORI_FORMATION_POLICY_VERSION
    selection_identity = ":".join(
        (
            principal.tenant_id,
            principal.principal_id,
            str(agent.id),
            agent.version,
            agent.model_policy,
            extraction_model_policy,
            agent.policy_profile,
            ruleset.policy_version,
            memory_mode.value,
            memory_policy_version,
            selection_outcome,
            selection_reason,
            evidence_source or "none",
            "none" if extraction_model is None else extraction_model.provider,
            "none" if extraction_model is None else extraction_model.model,
            "default" if extraction_effort is None else extraction_effort.value,
            evidence_build_ref or "none",
            evidence_corpus_sha256 or "none",
            "none"
            if settings.memory_formation_policy_pin is None
            else settings.memory_formation_policy_pin.value,
        )
    )
    selection_key = f"memory.provider_extraction.selection:v5:{selection_identity}"
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
                    "formation_policy_version": memory_policy_version,
                    "model_policy": extraction_model_policy,
                    "chat_model_policy": agent.model_policy,
                    "provider": None if extraction_model is None else extraction_model.provider,
                    "model": None if extraction_model is None else extraction_model.model,
                    "reasoning_effort": (
                        None if extraction_effort is None else extraction_effort.value
                    ),
                    "evidence_source": evidence_source,
                    "evidence_build_ref": evidence_build_ref,
                    "evidence_corpus_sha256": evidence_corpus_sha256,
                    "policy_pin": (
                        None
                        if settings.memory_formation_policy_pin is None
                        else settings.memory_formation_policy_pin.value
                    ),
                },
                derivation_key=selection_key,
                created_at=clock.now(),
            )
        )
    memory_default_choice = ModelChoice(
        model_policy=extraction_model_policy, reasoning_effort=extraction_effort
    )
    memory_alternatives: dict[ModelChoice, MemoryCandidateExtractor] = {}
    memory_alternative_options: list[MemoryModelOption] = []
    if selection_reason == "people_default" and not evaluating_memory:
        # ADR-0119: the owner may move People formation to another tuple only
        # when a formation@9 artifact on this tree evaluated exactly it.
        distillation_digest, _, holdout_digest = _shipped_evidence_digests()
        for evidence_path in provider_extraction_evidence_paths(settings):
            try:
                alternative_evidence = load_memory_distillation_evidence(evidence_path)
            except ConfigurationError:
                continue
            choice = ModelChoice(
                model_policy=alternative_evidence.model_policy,
                reasoning_effort=alternative_evidence.reasoning_effort,
            )
            if choice == memory_default_choice or choice in memory_alternatives:
                continue
            try:
                alternative_model = await model_router.resolve(
                    choice.model_policy,
                    tenant_id=principal.tenant_id,
                    required=frozenset({Capability.STRUCTURED_OUTPUT, Capability.STREAMING}),
                )
            except ConfigurationError:
                continue
            alternative_provider = effective_providers.get(alternative_model.provider)
            if alternative_provider is None or isinstance(
                alternative_provider, MissingCredentialProvider
            ):
                continue
            if (
                choice.reasoning_effort is not None
                and choice.reasoning_effort not in alternative_model.reasoning_efforts
            ):
                continue
            if not (
                distillation_evidence_matches(
                    alternative_evidence,
                    alternative_model,
                    agent.policy_profile,
                    ruleset.policy_version,
                    corpus_sha256=distillation_digest,
                    holdout_sha256=holdout_digest,
                    reasoning_effort=choice.reasoning_effort,
                )
                and _operator_artifact_is_bound(
                    settings, evidence_path, alternative_evidence.build_ref
                )
            ):
                continue
            memory_alternatives[choice] = AttributedCommunicationCandidateExtractor(
                OwnerPeopleCandidateExtractor(
                    provider=alternative_provider,
                    resolved_model=alternative_model,
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    fallback=HighRecallCandidateExtractor(),
                    reasoning_effort=choice.reasoning_effort,
                )
            )
            memory_alternative_options.append(
                MemoryModelOption(
                    model_policy=choice.model_policy,
                    display_name=alternative_model.display_name or alternative_model.model,
                    provider=alternative_model.provider,
                    model=alternative_model.model,
                    reasoning_effort=choice.reasoning_effort,
                )
            )
    model_settings_catalog = await _model_settings_catalog(
        agent=agent,
        model_router=model_router,
        providers=effective_providers,
        tenant_id=principal.tenant_id,
        memory_default=memory_default_choice,
        memory_alternatives=memory_alternative_options,
    )
    if not evaluating_memory:
        memory_extractor = AttributedCommunicationCandidateExtractor(
            memory_extractor
            or DeterministicCandidateExtractor(
                strict_possessives=not memory_legacy_formation_control
            )
        )
        if memory_alternatives:
            memory_extractor = OwnerSelectedCandidateExtractor(
                uow_factory=uow_factory,
                catalog=model_settings_catalog,
                extractors={memory_default_choice: memory_extractor, **memory_alternatives},
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
        people_enabled=settings.people_enabled,
    )
    registry.register(MemoryRememberTool(memory_service))
    source_learning = SourceLearningService(memory_service, uow_factory, clock)
    registry.register(MemoryLearnTool(source_learning))
    if settings.people_enabled:
        registry.register(PeopleMemoryRememberTool(memory_service))
    registry.register(
        SessionScopedMemoryRememberTool(
            PeopleMemoryRememberTool(memory_service)
            if settings.people_enabled
            else MemoryRememberTool(memory_service),
            uow_factory,
        )
    )

    def recon_admitted() -> bool:
        return False

    recon_pass = None
    if settings.memory_reconsolidation_enabled:
        if (
            settings.memory_reconsolidation_evidence is None
            or extraction_model is None
            or settings.release_id is None
            or evidence_build_ref is None
            or evidence_corpus_sha256 is None
        ):
            raise ConfigurationError(
                "reconsolidation needs release-bound comparative and upstream evidence"
            )
        try:
            recon_admitted = bind_evidence(
                REPOSITORY_ROOT,
                settings.memory_reconsolidation_evidence,
                extraction_model,
                release_sha=settings.release_id.rsplit("-", 1)[-1],
                formation_policy=memory_policy_version,
                upstream_build_ref=evidence_build_ref,
                upstream_corpus_sha256=evidence_corpus_sha256,
                residency_provider=settings.memory_reconsolidation_residency_provider,
            )
        except (OSError, ValueError):
            raise ConfigurationError(
                "reconsolidation activation evidence is missing or mismatched"
            ) from None
        recon_pass = ReconsolidationPass(
            uow_factory,
            clock,
            principal,
            worker_id="maintenance",
            admitted=recon_admitted,
            ids=ids,
            model=extraction_model,
            provider=effective_providers[extraction_model.provider],
            egress_policy=local_egress_policy(settings.memory_reconsolidation_residency_provider),
        )
    if settings.memory_dreaming_review_enabled:
        if settings.memory_reconsolidation_enabled:
            raise ConfigurationError(
                "reviewed dreaming and unattended reconsolidation are separate modes"
            )
        if not settings.memory_api_enabled or not settings.memory_reconsolidation_api_enabled:
            raise ConfigurationError("reviewed dreaming requires memory and reconsolidation APIs")
        if extraction_model is None:
            if extraction_model_policy in NON_ROUTED_MODEL_POLICIES:
                extraction_model = ResolvedModel(
                    provider="fake",
                    model="scripted",
                    credential_ref="fake",
                    policy_name=extraction_model_policy,
                    resolved_at=clock.now(),
                )
            else:
                extraction_model = await model_router.resolve(
                    extraction_model_policy,
                    tenant_id=principal.tenant_id,
                    required=frozenset({Capability.STRUCTURED_OUTPUT, Capability.STREAMING}),
                )
        if extraction_model.provider not in effective_providers or isinstance(
            effective_providers[extraction_model.provider], MissingCredentialProvider
        ):
            raise ConfigurationError("reviewed dreaming provider is unavailable")
        if (
            extraction_model.provider != "fake"
            and settings.memory_reconsolidation_residency_provider != extraction_model.provider
        ):
            raise ConfigurationError(
                "reviewed dreaming requires an explicit matching residency provider"
            )
        recon_pass = ReconsolidationPass(
            uow_factory,
            clock,
            principal,
            worker_id="reviewed-dreaming",
            admitted=lambda: True,
            ids=ids,
            model=extraction_model,
            provider=effective_providers[extraction_model.provider],
            egress_policy=local_egress_policy(settings.memory_reconsolidation_residency_provider),
            owner_review=True,
        )

    registry_identity = {
        "policies": model_router.registry.policies.model_dump(mode="json"),
        "profiles": {
            name: {
                "profile_hash": profile.profile_hash,
                "registry_version": profile.registry_version,
                "models": [
                    {
                        "id": model.model_id,
                        "aliases": model.aliases,
                        "limits": model.limits.model_dump(mode="json"),
                        "pricing": model.pricing.model_dump(mode="json"),
                        "reasoning_efforts": list(model.reasoning_efforts),
                    }
                    for model in profile.models
                ],
            }
            for name, profile in model_router.registry.profiles.items()
        },
    }
    recall_composition = SemanticCompositionInputs(
        configuration={
            "policy_version": ruleset.policy_version,
            "agent": agent.model_dump(mode="json"),
            "principal": {
                "tenant": principal.tenant_id,
                "owner": principal.principal_id,
                "scopes": sorted(principal.scopes),
                "roles": sorted(principal.roles),
            },
            "context": dict(context_config),
            "memory": memory_profiles.model_dump(mode="json"),
            "storage": storage,
            "schema_sha256": recall_schema_sha256,
            "people_enabled": settings.people_enabled,
            "reviewed_dreaming": settings.memory_dreaming_review_enabled,
            "unattended_dreaming": settings.memory_reconsolidation_enabled,
            "query_authority": "local-reviewed-reader@1;remote-withheld",
            "query_preparation": "explicit-runtime@1",
        },
        formation={
            "policy": memory_policy_version,
            "profiles": memory_profiles.formation.model_dump(mode="json"),
            "registry": registry_identity,
            "default": memory_default_choice.model_dump(mode="json"),
            "alternatives": [
                option.model_dump(mode="json") for option in memory_alternative_options
            ],
            "upstream_build": evidence_build_ref,
            "upstream_corpus": evidence_corpus_sha256,
        },
        reader={
            "registry": registry_identity,
            "catalog": model_settings_catalog.model_dump(mode="json"),
        },
    )
    if recall_capture is not None:
        try:
            recall_capture(recall_composition)
            raise ConfigurationError("Recall capture callback must stop composition")
        finally:
            # The complete-graph cleanup below has not been entered yet.
            with suppress(BaseException):
                await sandbox_manager.close()
            if cockpit_sandbox is not None:
                with suppress(BaseException):
                    await cockpit_sandbox.close()
            for provider in effective_providers.values():
                with suppress(BaseException):
                    await provider.close()
    semantic_index_pass = None
    if settings.memory_semantic_index_enabled:
        try:
            semantic_index_pass = await compose_indexing(
                root=REPOSITORY_ROOT,
                settings=settings,
                principal=principal,
                factory=uow_factory,
                clock=clock,
                credentials=credential_resolver,
                configuration=recall_composition.configuration,
                formation=recall_composition.formation,
                reader=recall_composition.reader,
                provider_factory=OpenAIEmbeddingProvider,
                local_provider_factory=local_embedding_provider,
            )
            if semantic_index_pass and isinstance(semantic_index_pass.admitted, BoundedAdmission):
                logger.info(
                    "semantic_trial_composed binding=%s jobs=%s",
                    semantic_index_pass.admitted.receipt_sha256,
                    int(settings.jobs_enabled),
                )
        except ConfigurationError:
            # Admission remains strict; only the optional runtime arm is unavailable.
            logger.warning("semantic_recall_unavailable_using_sparse")

    async def sweep_semantic_index() -> int:
        assert semantic_index_pass is not None
        return (await semantic_index_pass.index.run_slice()).indexed

    query_former = DeterministicQueryFormer(
        principal,
        candidate_selection=memory_profiles.retrieval.candidate_selection,
        planning=memory_profiles.retrieval.query_planning,
        admitted=None if semantic_index_pass is None else semantic_index_pass.admitted,
    )
    memory_retriever = HybridMemoryRetriever(
        uow_factory,
        clock,
        ids,
        principal,
        # Keep the configured profile in the admission identity, but use the
        # ordinary sparse policy (including owner anchors) without semantic services.
        profile=memory_profiles.retrieval
        if semantic_index_pass is not None
        else memory_profiles.retrieval.model_copy(update={"semantic_enabled": False}),
        trace_retention=memory_profiles.traces,
        reconsolidation_enabled=True if settings.memory_dreaming_review_enabled else recon_admitted,
        reconsolidation_owner_approved_only=settings.memory_dreaming_review_enabled,
        semantic_binding_sha256=(
            semantic_index_pass.admitted.receipt_sha256
            if semantic_index_pass and isinstance(semantic_index_pass.admitted, BoundedAdmission)
            else None
        ),
        runtime_semantic_queries=None
        if semantic_index_pass is None
        else semantic_index_pass.queries,
        source_recovery=None
        if semantic_index_pass is None
        else semantic_index_pass.source_recovery,
        reranking_admitted=None if semantic_index_pass is None else semantic_index_pass.admitted,
    )
    registry.register(MemorySearchTool(memory_retriever, inline_output_bytes=inline_output_bytes))
    people_context = (
        PeopleContextService(uow_factory, memory_retriever) if settings.people_enabled else None
    )
    registry.register(
        MemorySearchTool(
            memory_retriever,
            model_planning=True,
            factory=uow_factory,
            people_context=people_context,
            inline_output_bytes=inline_output_bytes,
        )
    )
    registry.register(
        MemorySearchTool(
            memory_retriever,
            model_planning=True,
            overview_enabled=True,
            factory=uow_factory,
            people_context=people_context,
            inline_output_bytes=inline_output_bytes,
        )
    )
    people_erasure = PeopleErasureService(uow_factory, clock)
    people_identity = PeopleIdentityService(uow_factory, clock, ids)
    people_duplicates = (
        PeopleDeduplicator(uow_factory, clock, people_identity) if settings.people_enabled else None
    )
    people_service = (
        PublicPeopleService(
            uow_factory,
            clock,
            identity=people_identity,
            duplicates=people_duplicates,
            erasure=people_erasure,
            memory_for=lambda owner: GovernedMemoryService(uow_factory, clock, ids, owner),
            legacy_linker=lambda owner, limit, cursor: link_existing_beliefs(
                uow_factory, clock, owner, limit=limit, cursor=cursor
            ),
            kinship_linker=lambda owner, confirm: link_owner_kinship(
                uow_factory, clock, owner, confirm=confirm
            ),
            email_sources=_retained_email_reader(uow_factory, clock, ids),
        )
        if settings.people_enabled
        else None
    )
    people_repair = (
        PeopleDirectoryRepair(
            uow_factory,
            clock,
            memory_for=lambda owner: GovernedMemoryService(uow_factory, clock, ids, owner),
            reproject=_correspondence_reprojector(uow_factory, clock, ids),
        )
        if settings.people_enabled
        else None
    )
    people_import_runner = None
    if people_service is not None:
        from agent_core.memory.communication_sources import FormationSourceKind, formation_source
        from agent_core.memory.people_evidence import implementation_digest
        from agent_core.runtime.people_imports import (
            ImportProvider,
            ImportSlice,
            PeopleImportRunner,
        )

        def admitted_people_source(event: EventEnvelope, owner: Principal) -> str | None:
            source = formation_source(event, owner)
            if source is None or (
                source.kind is not FormationSourceKind.OWNER_ASSERTION and source.channel != "sms"
            ):
                return None
            return source.text

        people_service.imports.implementation_identity = implementation_digest
        people_service.imports.source_text = admitted_people_source
        people_service.imports.capture_available = memory_policy_version == "formation@11"
        people_service.imports.queue_priority = async_priority

        def import_memory(worker: ImportSlice) -> GovernedMemoryService:
            if extraction_model is None:
                raise ConflictError("People import model is unavailable")
            selected_provider = effective_providers[extraction_model.provider]

            async def guard(uow: RepositoryUnitOfWork) -> None:
                await worker.guard(uow)

            return GovernedMemoryService(
                uow_factory,
                clock,
                ids,
                worker.context.principal,
                extractor=OwnerPeopleCandidateExtractor(
                    provider=ImportProvider(worker, selected_provider),
                    resolved_model=extraction_model,
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    commit_guard=guard,
                    source_isolated=True,
                    reasoning_effort=extraction_effort,
                ),
                policy_version="formation@11",
                people_enabled=True,
                commit_guard=guard,
                formation_profile=memory_profiles.formation,
            )

        people_import_runner = PeopleImportRunner(
            people_service.imports, import_memory, dispatch_continuations=storage != "memory"
        )
    people_retriever = None
    if people_service is not None:
        assert people_context is not None
        people_retriever = PeopleAwareMemoryRetriever(people_context, principal)
        registry.register(PeopleSearchTool(uow_factory, clock))
        registry.register(PeopleContextTool(people_context))
        registry.register(PeopleHistoryTool(people_service))
    records_service: RecordsService | None = None
    try:
        records_catalog_max_series, _ = records_catalog_caps(context_classes)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    if settings.records_enabled:
        records_service = RecordsService(uow_factory, clock, ids)
        records_zone = ZoneInfo("UTC")
        registry.register(RecordsListTool(records_service, records_zone))
        registry.register(RecordsCreateSeriesTool(records_service, records_zone))
        registry.register(RecordsAppendTool(records_service, records_zone))
        registry.register(RecordsQueryTool(records_service, records_zone))
        registry.register(RecordsDeleteTool(records_service, records_zone))
        registry.register(RecordsImportTool(records_service, records_zone))
    if cockpit_toolset is not None:
        registry.register(PreviousCockpitReadTool(cockpit_toolset))
        registry.register(PreviousCockpitCheckTool(cockpit_toolset))
        for cockpit_tool in cockpit_tools(cockpit_toolset):
            registry.register(cockpit_tool)
    registry.register(MemoryRecallEpisodesTool(episode_search))
    mcp_runtime: MCPRuntime | None = None
    owner_mcp_servers: MCPServerSettingsService | None = None
    try:
        uses_sdk_clients = mcp_clients is None and mcp_scripts is None
        if settings.mcp_servers_enabled and (uses_sdk_clients or mcp_oauth_gateway is None):
            # ADR-0255 decision 7: owner-added servers and their sign-in have
            # their own audited public-HTTPS transport.
            owner_mcp_proxy = await start_owner_mcp_egress_proxy(tenant_id=principal.tenant_id)
        if mcp_clients is None:
            if mcp_scripts is not None:
                mcp_clients = ScriptedMCPClientFactory(dict(mcp_scripts))
            else:
                if any(
                    config.transport is MCPTransport.HTTP and not config.owner_managed
                    for config in effective_mcp_configs
                ):
                    mcp_proxy = await start_worker_egress_proxy(
                        egress,
                        tenant_id=principal.tenant_id,
                    )
                mcp_clients = _sdk_mcp_clients(
                    None if mcp_proxy is None else mcp_proxy.url,
                    None if owner_mcp_proxy is None else owner_mcp_proxy.url,
                )
        if settings.mcp_servers_enabled:
            assert settings.mcp_authorization_key is not None
            assert settings.mcp_oauth_callback_url is not None
            owner_mcp_servers = MCPServerSettingsService(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                gateway=(
                    mcp_oauth_gateway
                    or HTTPMCPOAuthGateway(
                        proxy_url=None if owner_mcp_proxy is None else owner_mcp_proxy.url
                    )
                ),
                sealer=AESGCMSecretSealer.from_text(
                    settings.mcp_authorization_key.get_secret_value()
                ),
                callback_url=settings.mcp_oauth_callback_url,
                tenant_id=principal.tenant_id,
            )
        call_service = (
            None
            if not settings.call_enabled or settings.call_configuration is None
            else CallService(
                uow_factory,
                clock,
                principal,
                settings.call_configuration,
                BlandCallProvider(principal.tenant_id, mcp_clients, credential_resolver),
                notifications=settings.call_notifications_enabled,
            )
        )
        mcp_runtime = MCPRuntime(
            uow_factory,
            registry,
            mcp_clients,
            credential_resolver,
            clock,
            ids,
            connect_timeout_seconds=float(connect_timeout),
            idle_timeout_seconds=float(idle_timeout),
            discovery_reuse_seconds=float(discovery_reuse),
            warmup_connect_timeout_seconds=float(warmup_limits["warmup_connect_timeout_seconds"]),
            warmup_retry_seconds=float(warmup_limits["warmup_retry_seconds"]),
            warmup_attempts=int(warmup_limits["warmup_attempts"]),
            call_interceptor=None if call_service is None else call_service.invoke,
            access_tokens=owner_mcp_servers,
            owner_servers_enabled=settings.mcp_servers_enabled,
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
        notification_producer = (
            NotificationProducer(clock=clock, ids=ids)
            if settings.notification_dispatch_enabled
            else None
        )
        # Both flags, exactly as the schedule tool pairs its two. Without the
        # notification producer there is no wake, so the channel stays dark.
        device_flags_enabled = settings.device_channel_enabled and settings.device_sms_enabled
        device_channel_ready = device_flags_enabled and (
            device_channel is not None or notification_producer is not None
        )
        device_tool_runtime: DeviceToolRuntime | None = None
        if device_flags_enabled and not device_channel_ready:
            logger.warning("device_channel_disabled_without_notification_dispatch")
        if device_channel_ready:
            effective_device_channel = device_channel or PushWakeDeviceChannel(
                uow_factory=uow_factory,
                notification_producer=cast(NotificationProducer, notification_producer),
                clock=clock,
                invocation_timeout_seconds=device_invocation_timeout_seconds,
                poll_seconds=device_invocation_poll_seconds,
            )
            device_tool_runtime = DeviceToolRuntime(
                uow_factory,
                registry,
                effective_device_channel,
                clock,
                ids,
                invocation_timeout_seconds=device_invocation_timeout_seconds,
            )

        async def attach_device_tools(session_id: UUID, session_principal: Principal) -> None:
            if device_tool_runtime is not None:
                await device_tool_runtime.prepare(session_id, session_principal)

        async def close_session(session_id: UUID) -> None:
            await mcp_runtime.close_session(session_id)
            if device_tool_runtime is not None:
                await device_tool_runtime.close_session(session_id)

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
            session_required_tools=_session_required_tools,
            session_browser_origins=_session_browser_origins(
                browser_provider, settings, uow_factory
            ),
            attach_device_tools=attach_device_tools,
            snapshot_profiles=memory_profiles.snapshots,
            memory_candidate_selection=memory_profiles.retrieval.candidate_selection,
            owner_server_ids=mcp_runtime.owner_server_ids,
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
            people_retriever or memory_retriever,
            query_former,
            session_project_scope,
            # Memory/People watermarks do not cover elapsed expiry, semantic
            # cache availability or admission. Read current evidence per assembly.
            inline_output_bytes=inline_output_bytes,
            catalog_source=records_service,
            catalog_max_series=records_catalog_max_series,
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
            # ADR-0120: PDFs the owner sends reach knowledge through their text layer.
            extractor=RoutingExtractor(
                [
                    PlainTextExtractor(),
                    PdfTextExtractor(maximum_bytes=MAX_KNOWLEDGE_SOURCE_BYTES),
                ]
            ),
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
        registry.register(
            ArtifactReadCaptureTool(
                uow_factory,
                general_artifact_store,
                clock,
                step_maximum_reads=capture_step_maximum_reads,
                run_maximum_reads=capture_run_maximum_reads,
            )
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

        # The deterministic engine is always built and stays the recovery policy and
        # the standing authorizer's engine: an advisory escalation can never be
        # satisfied by a standing grant, and a resumed invocation never re-asks.
        deterministic_engine = DeterministicPolicyEngine(ruleset)
        tool_deterministic_engine: PolicyEngine = deterministic_engine
        if job_ruleset is not None:
            tool_deterministic_engine = JobPolicyEngine(
                deterministic_engine, DeterministicPolicyEngine(job_ruleset)
            )
        policy_engine: PolicyEngine = tool_deterministic_engine
        if ruleset.advisory_enabled or settings.policy_advisory_observe_enabled:
            if judgment_provider is None:
                # Never fail closed on the advisor's availability.
                logger.warning(
                    "policy_advisory_unavailable", extra={"selector": "JUDGMENT_PROVIDER"}
                )
            else:
                policy_engine = AdvisedPolicyEngine(
                    tool_deterministic_engine,
                    JudgmentPolicyAdvisor(judgment_provider),
                    # Enforcing is a profile value and hashes into `policy_version`;
                    # observing changes no decision and is an environment flag.
                    enforce=ruleset.advisory_enabled,
                    clock=clock,
                    observer=AdvisoryMetrics(),
                )
        # ADR-0172: specific owner consent, then ADR-0129 standing/task grants.
        standing_authorizers: list[StandingAuthorizer] = []
        if settings.jobs_enabled:
            standing_authorizers.append(JobGrantAuthorizer(uow_factory))
            standing_authorizers.append(WidgetInstallationAuthorizer(uow_factory))
        if browser_provider is not None:
            standing_authorizers.append(
                BrowserFollowConsentAuthorizer(
                    provider=browser_provider,
                    uow_factory=uow_factory,
                    now=clock.now,
                )
            )
            if settings.browser_research_enabled and any(
                rule.tool_name == "browser.act"
                and rule.condition is PolicyCondition.BOUNDED_BROWSER_SCROLL
                and rule.decision is PolicyDecisionType.ALLOW
                and rule.otherwise is PolicyDecisionType.REQUIRE_APPROVAL
                for rule in ruleset.tool_rules
            ):
                standing_authorizers.append(
                    BrowserResearchAuthorizer(
                        provider=browser_provider,
                        uow_factory=uow_factory,
                        policy=deterministic_engine,
                        now=clock.now,
                    )
                )
        if settings.browser_grant_id is not None:
            if browser_provider is None or settings.browser_profile_id is None:
                raise ConfigurationError("standing browser grant composition is incomplete")
            standing_authorizers.append(
                ConfiguredBrowserStandingAuthorizer(
                    grant_id=settings.browser_grant_id,
                    profile_id=settings.browser_profile_id,
                    purpose=settings.browser_run_purpose,
                    provider=browser_provider,
                    uow_factory=uow_factory,
                    policy=deterministic_engine,
                    now=clock.now,
                )
            )
        if browser_provider is not None and _task_grants_composed(settings, browser_provider):
            standing_authorizers.append(
                BrowserTaskGrantAuthorizer(
                    provider=browser_provider,
                    uow_factory=uow_factory,
                    policy=deterministic_engine,
                    now=clock.now,
                )
            )
        if settings.call_enabled and settings.call_owner_requests_enabled:
            standing_authorizers.append(OwnerCallAuthorizer(uow_factory=uow_factory, now=clock.now))
        standing_authorizer = (
            CompositeStandingAuthorizer(standing_authorizers) if standing_authorizers else None
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

        async def guard_email_effect(
            run: Run,
            owner: Principal,
            invocation: ToolInvocation,
            lease: WorkerLease | None,
        ) -> None:
            """Revalidate the exact owner gesture at the provider dispatch watermark."""
            task = await public_services.email.get_task(owner, run.id)
            if task is not None and task.kind == "subscription":
                if not settings.email_unsubscribe_enabled:
                    raise ConflictError("Email unsubscribe assistance is disabled")
                await public_services.email.subscriptions.guard(owner, run, invocation, lease)
            if task is not None and task.kind == "archive":
                if task.archive_consent is not None and invocation.tool_name.startswith(
                    f"mcp.{task.archive_consent.read_server_id}."
                ):
                    return
                if not settings.email_mode_enabled:
                    raise ConflictError("Email mode is disabled")
                if (
                    task.archive_consent is None
                    or invocation.tool_name != task.archive_consent.tool_name
                ):
                    raise ConflictError("archive task cannot dispatch another external action")
                await public_services.email.validate_archive(owner, run, lease)
            # A consented send revalidates the press at the dispatch watermark too.
            if (
                task is not None
                and task.kind == "send"
                and task.send_consent is not None
                and invocation.tool_name == task.send_consent.tool_name
            ):
                await public_services.email.validate_send(owner, run, lease)

        async def job_model_factory(
            current_run: Run, selected_agent: AgentSpec, lease: WorkerLease | None
        ) -> object:
            """Bind job completions to the current run, pinned provider, and usage ledger."""
            resolved = resolved_model
            provider: ModelProvider = model_provider
            if current_run.provider_pin is not None:
                resolved = await model_router.resolve_pinned(current_run.provider_pin)
                resolved = resolved.model_copy(update={"policy_name": selected_agent.model_policy})
                provider = effective_providers[resolved.provider]
            return JobModelCompletion(
                run=current_run,
                provider=provider,
                resolved=resolved,
                budgets=UnitOfWorkBudgetLedger(uow_factory, clock, lease),
                clock=clock,
                ids=ids,
            ).complete

        pipeline = ToolPipeline(
            registry,
            uow_factory,
            clock,
            ids,
            policy=policy_engine,
            recovery_policy=tool_deterministic_engine,
            workspace_factory=sandbox_manager,
            artifact_writers=artifact_writers,
            current_principal=principal,
            max_parallel_calls=max_parallel_calls,
            hard_ceiling_multiplier=hard_ceiling_multiplier,
            inline_output_bytes=inline_output_bytes,
            maximum_loaded_skills=int(skill_bodies_config["max_items"]),
            maximum_skill_body_tokens=int(skill_bodies_config["max_tokens"]),
            approval_expiry_seconds=dict(ruleset.approval_expiry_seconds),
            standing_authorizer=standing_authorizer,
            job_model_factory=job_model_factory,
            delegations=delegation_materializer,
            before_effect=guard_email_effect,
        )
        token_slot = _ActiveToken()
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

        async def after_parked_cancel(run_id: UUID) -> None:
            if delegation_joins is not None:
                await delegation_joins.after_run(run_id)
            if browser_provider is not None:
                # In one process the parked run's lease is here; elsewhere the
                # run worker's lease upkeep releases it.
                try:
                    await release_browser_lease_if_done(
                        browser_provider, uow_factory, principal, run_id
                    )
                except Exception:
                    logger.exception("browser_run_cleanup_failed", extra={"run_id": str(run_id)})

        async def complete_run_resources(run_id: UUID, lease_epoch: int | None) -> None:
            """Bound independent cleanup hooks while leaving parent continuation under its
            run budget.
            """
            try:
                async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                    async with uow_factory() as uow:
                        finished = await uow.runs.get(run_id, principal)
                    await mcp_runtime.release_session_transports(finished.session_id)
            except Exception:
                logger.exception("mcp_run_cleanup_failed", extra={"run_id": str(run_id)})
            try:
                async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                    await public_services.email.settle(principal, run_id)
            except Exception:
                logger.exception("email_run_accounting_failed", extra={"run_id": str(run_id)})
            try:
                async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                    await sandbox_manager.release_run(run_id, lease_epoch)
            except Exception:
                logger.exception("run_resource_cleanup_failed", extra={"run_id": str(run_id)})
            if browser_provider is not None:
                try:
                    async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                        await release_browser_lease_if_done(
                            browser_provider, uow_factory, principal, run_id
                        )
                except Exception:
                    logger.exception("browser_run_cleanup_failed", extra={"run_id": str(run_id)})
            try:
                async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                    await schedule_accountant.account(run_id)
            except Exception:
                logger.exception("schedule_run_accounting_failed", extra={"run_id": str(run_id)})
            completed: Run | None = None
            try:
                async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
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
            if completed is not None:
                try:
                    async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                        await request_title_after_run(
                            uow_factory, principal, completed, clock.now()
                        )
                except Exception:
                    logger.exception("title_request_failed", extra={"run_id": str(run_id)})
            # What the answer cited is only knowable once the answer exists, so
            # usage feedback is the completion's own step: its own error
            # boundary, its own units of work, and no external call inside one.
            if (
                completed is not None
                and completed.status is RunStatus.COMPLETED
                and completed.final_message
            ):
                try:
                    async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                        plan = await context_planner.current(completed.session_id)
                        await memory_service.record_usage(
                            session_id=completed.session_id,
                            run_id=completed.id,
                            final_text=completed.final_message,
                            snapshot_trace_id=None if plan is None else plan.snapshot_id,
                        )
                except Exception:
                    logger.exception("memory_usage_feedback_failed", extra={"run_id": str(run_id)})
            hooks: list[tuple[str, Callable[[], Awaitable[object]]]] = []
            if skill_reviews is not None:
                hooks.append(("skill_review", partial(skill_reviews.after_run, run_id)))
            if people_import_runner is not None:
                hooks.append(
                    ("people_import", partial(people_import_runner.after_run, run_id, principal))
                )
            hooks.append(("people_erasure", partial(people_erasure.resume_pending, principal)))
            for name, hook in hooks:
                try:
                    async with asyncio.timeout(POST_RUN_HOOK_TIMEOUT_SECONDS):
                        await hook()
                except Exception as exc:
                    log_failure(
                        logger,
                        "post_run_hook_failed",
                        exc,
                        phase=name,
                        diagnostic_id=str(ids.new_id()),
                        run_id=str(run_id),
                        release_id=settings.release_id,
                    )

            # Joining can execute a resumed parent in-process. Its own run budget,
            # cancellation and lease govern that work, not the optional-hook timeout.
            if delegation_joins is not None:
                await delegation_joins.after_run(run_id)

        def email_semantics(context: RunContext) -> EmailSemanticFormationService:
            from agent_core.memory.email_people import EmailPeopleFormationService
            from agent_core.memory.email_people_evidence import email_evidence_policy

            if settings.people_enabled:
                return EmailPeopleFormationService(
                    uow_factory,
                    clock,
                    ids,
                    context.principal,
                    provider=context.resolved_model.provider,
                    model=context.resolved_model.model,
                )
            # A retained People quality artifact must not reactivate an explicit shutdown.
            evidence_path = settings.email_semantic_evidence
            if (
                evidence_path is not None
                and email_evidence_policy(evidence_path) == "email-semantic@2"
            ):
                evidence_path = None
            evidence = (
                None
                if evidence_path is None
                else load_email_semantic_evidence(
                    evidence_path,
                    provider=context.resolved_model.provider,
                    model=context.resolved_model.model,
                    build_ref=settings.release_id,
                )
            )
            return EmailSemanticFormationService(
                uow_factory,
                clock,
                ids,
                context.principal,
                provider=context.resolved_model.provider,
                model=context.resolved_model.model,
                evidence=evidence,
            )

        if people_import_runner is not None and people_service is not None:
            from agent_core.context.rendering import envelope_items
            from agent_core.domain.people import PeopleImportJob
            from agent_core.memory.email_people import EmailPeopleFormationService
            from agent_core.runtime.people_email_imports import PeopleEmailImportProcessor
            from agent_core.runtime.people_mailbox_imports import PeopleMailboxImporter

            def import_email(
                worker: ImportSlice, job: PeopleImportJob
            ) -> PeopleEmailImportProcessor:
                selected = email_semantics(worker.context)
                if not isinstance(selected, EmailPeopleFormationService) or not selected.enabled:
                    raise ConflictError("Email People import is unavailable")

                async def guard(uow: RepositoryUnitOfWork) -> None:
                    await worker.guard(uow)

                return PeopleEmailImportProcessor(
                    worker,
                    EmailPeopleFormationService(
                        uow_factory,
                        clock,
                        ids,
                        worker.context.principal,
                        provider=worker.context.resolved_model.provider,
                        model=worker.context.resolved_model.model,
                        import_window=(job.scope.since, job.scope.until),
                        import_guard=guard,
                    ),
                    envelope=envelope_items,
                    check_budget=public_services.email._check_budget,
                    provider=effective_providers[worker.context.resolved_model.provider],
                )

            people_import_runner.email_factory = import_email

            def discover_email(worker: ImportSlice, job: PeopleImportJob) -> PeopleMailboxImporter:
                processor = import_email(worker, job)
                return PeopleMailboxImporter(
                    worker,
                    job,
                    registry=registry,
                    service=public_services.email,
                    semantics=processor.semantics,
                    render_context=render_email_context,
                    selected=processor.selected,
                )

            people_import_runner.mailbox_factory = discover_email
            people_service.imports.email_capture_available = True

        from agent_core.domain.browser_workflows import BrowserWorkflowCatalog
        from agent_core.runtime.browser_workflows import BrowserWorkflowRunner

        workflow_catalog = BrowserWorkflowCatalog.model_validate(
            load_config_document(settings, "runtime/browser-workflows.yaml")
        )
        if not settings.browser_feed_workflow_enabled:
            workflow_catalog = workflow_catalog.model_copy(
                update={
                    "recipes": [
                        recipe
                        for recipe in workflow_catalog.recipes
                        if not any(step.kind == "collect_feed" for step in recipe.steps)
                    ]
                }
            )
        browser_workflows = BrowserWorkflowRunner(
            workflow_catalog,
            policy_version=ruleset.policy_version,
        )

        from agent_core.domain.email_junk import (
            EmailJunkMode,
            JunkAuthorization,
            JunkCalibration,
            JunkPilot,
            JunkStandingAuthorization,
        )
        from agent_core.runtime.email_junk import EmailJunkFilter

        async def execute_email_task(context: RunContext) -> RunOutcome | None:
            """Dispatch persisted typed work before the general agent loop."""
            if context.run.kind is RunKind.JOB:
                return await execute_job(
                    context, None if job_execution is None else job_execution.run
                )
            await browser_workflows(context)
            if people_import_runner is None:
                async with uow_factory() as uow:
                    typed_session = await uow.sessions.get(
                        context.run.session_id, context.principal
                    )
                if typed_session.metadata.get("purpose") == "people-import":
                    raise ConflictError("People imports are disabled")
            if people_import_runner is not None:
                imported = await people_import_runner(context)
                if imported is not None:
                    return imported
            if not settings.email_mode_enabled:
                task = await public_services.email.get_task(context.principal, context.run.id)
                if task is None:
                    return None
                if task.kind != "archive":
                    raise ConflictError("Email mode is disabled")
                # Archive recovery classifies already-dispatched effects before
                # checking current authority; disabled mode grants no new write.
            junk_admitted = False
            junk_digest = "none"
            junk_pilot: JunkAuthorization | None = None
            if (
                settings.email_junk_filter_mode
                in {EmailJunkMode.FILTER, EmailJunkMode.PILOT, EmailJunkMode.OWNER_FILTER}
                and settings.email_junk_filter_evidence
            ):
                try:
                    with settings.email_junk_filter_evidence.open("rb") as evidence_file:
                        raw_junk_evidence = evidence_file.read(2_000_001)
                    if len(raw_junk_evidence) <= 2_000_000:
                        if settings.email_junk_filter_mode in {
                            EmailJunkMode.PILOT,
                            EmailJunkMode.OWNER_FILTER,
                        }:
                            packet_type = (
                                JunkPilot
                                if settings.email_junk_filter_mode == EmailJunkMode.PILOT
                                else JunkStandingAuthorization
                            )
                            candidate = packet_type.model_validate_json(raw_junk_evidence)
                            if candidate.admitted(context.principal, "work", context.clock.now()):
                                junk_pilot = candidate
                        else:
                            junk_calibration = JunkCalibration.model_validate_json(
                                raw_junk_evidence
                            )
                            junk_admitted = junk_calibration.admitted()
                        junk_digest = hashlib.sha256(raw_junk_evidence).hexdigest()
                    else:
                        logger.warning("email_junk_evidence_unavailable")
                except (OSError, ValueError):
                    logger.warning("email_junk_evidence_unavailable")
            junk_filter = EmailJunkFilter(
                judgment_provider,
                settings.email_junk_filter_mode,
                admitted=junk_admitted,
                evidence_digest=junk_digest,
                pilot=junk_pilot,
            )

            return await EmailTaskRunner(
                public_services.email,
                registry,
                semantic_factory=email_semantics,
                junk_filter=junk_filter,
                render_context=render_email_context,
                prepare_servers=mcp_runtime.prepare,
            )(context)

        async def is_email_task(run: Run, run_principal: Principal) -> bool:
            """Identify typed job and email runs that bypass ordinary agent preparation."""
            if run.kind is RunKind.JOB:
                return True
            return await public_services.email.get_task(run_principal, run.id) is not None

        async def resolve_email_archive_approval(
            owner: Principal,
            approval_id: UUID,
            lease: WorkerLease | None,
        ) -> None:
            await archive_approval_service.resolve(
                owner,
                approval_id,
                ApprovalResolutionType.APPROVE_ONCE,
                "Explicit owner archive gesture",
                lease=lease,
            )

        async def resolve_email_send_approval(
            owner: Principal,
            approval_id: UUID,
            lease: WorkerLease | None,
        ) -> None:
            """The owner's one press on Send consents to this exact message (ADR-0279)."""
            await archive_approval_service.resolve(
                owner,
                approval_id,
                ApprovalResolutionType.APPROVE_ONCE,
                "Explicit owner send gesture",
                lease=lease,
            )

        executor = RunExecutor(
            principal=principal,
            release_id=settings.release_id,
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
            recall_elapsed_time=elapsed_time,
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
            task_runner=execute_email_task,
            typed_task=is_email_task,
            validate_browser_binding=_scheduled_browser_binding_validator(browser_provider),
            model_settings=model_settings_catalog,
            prepare_recall=(
                SemanticRecallPreparation(
                    uow_factory,
                    principal,
                    memory_retriever,
                    query_former,
                    inline_output_bytes=inline_output_bytes,
                )
                if semantic_index_pass is not None and semantic_index_pass.queries is not None
                else None
            ),
        )
        dispatcher = (
            InlineRunDispatcher(executor.execute, unit_of_work_open=uow_factory.is_open)
            if storage == "memory"
            else PostgresRunDispatcher()
        )
        local_import_tasks: set[asyncio.Task[None]] = set()
        if people_service is not None:
            if storage == "memory":

                async def resume_import(run_id: UUID, delay: float) -> None:
                    try:
                        await clock.sleep(delay)
                        await dispatch_import(run_id)
                    except Exception as exc:
                        logger.warning(
                            "people_import_dispatch_failed",
                            extra={"error_class": type(exc).__name__},
                        )

                async def dispatch_import(run_id: UUID) -> None:
                    current: UUID | None = run_id
                    while current is not None:
                        await dispatcher.dispatch(current)
                        async with uow_factory() as uow:
                            run = await uow.runs.get(current, principal)
                            source = await uow.sessions.get(run.session_id, principal)
                            job_id = UUID(source.metadata["people_import_job_id"])
                            job = await people_service.imports._get(
                                uow,
                                principal,
                                job_id,
                                Sensitivity.RESTRICTED,
                            )
                            continuation = (
                                await uow.runs.get(job.run_id, principal)
                                if job.run_id is not None and job.run_id != current
                                else None
                            )
                        current = None
                        if job.state == "queued" and continuation is not None:
                            delay = (
                                (continuation.scheduled_for - clock.now()).total_seconds()
                                if continuation.scheduled_for is not None
                                else 0
                            )
                            if delay > 0:
                                task = asyncio.create_task(resume_import(continuation.id, delay))
                                local_import_tasks.add(task)
                                task.add_done_callback(local_import_tasks.discard)
                            else:
                                current = continuation.id

                people_service.imports.dispatch = dispatch_import
            else:
                people_service.imports.dispatch = dispatcher.dispatch
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
            close_session=close_session,
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
            on_parked_cancelled=after_parked_cancel,
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
            close_session=close_session,
            on_session_closed=consolidate_closed_session,
            trajectory_artifacts=trajectory_artifact_store,
            general_artifacts=general_artifact_store,
            model_settings=model_settings_catalog,
        )

        async def sweep_session_deletions() -> int:
            return await public_session_service.purge_pending_artifacts(principal)

        async def sweep_terminal_schedules() -> int:
            async with uow_factory() as uow:
                return await uow.schedules.purge_terminal(
                    principal.tenant_id,
                    before=clock.now() - timedelta(days=schedule_terminal_retention_days),
                    limit=schedule_terminal_purge_batch,
                )

        call_plan_sweep = (
            CallPlanSweep(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                principal=principal,
                wake_scheduler=schedule_notify,
            )
            if settings.call_enabled
            and settings.schedule_api_enabled
            and settings.schedule_worker_enabled
            else None
        )

        async def sweep_device_invocations() -> int:
            async with uow_factory() as uow:
                return await uow.device_invocations.expire_overdue(
                    now=clock.now(),
                    timeout_seconds=device_invocation_timeout_seconds,
                )

        browser_uow_factory = cast(BrowserUnitOfWorkFactory, uow_factory)

        async def continue_verified_browser(owner: Principal, profile_id: UUID) -> None:
            await browser_continuation.resume_profile(owner, profile_id)

        browser_profile_service = BrowserProfileManagementService(
            uow_factory=browser_uow_factory,
            lifecycle=browser_profile_lifecycle,
            authentications=browser_authentications,
            clock=clock,
            ids=ids,
            on_verified=continue_verified_browser,
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
            invocation_timeout_seconds=device_invocation_timeout_seconds,
        )
        notification_inbox = NotificationInboxService(uow_factory=uow_factory, clock=clock)
        surface_management = SurfaceManagementService(
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            code_expiry_seconds=surface_limits.code_expiry_seconds,
            max_code_attempts=surface_limits.code_max_attempts,
        )
        public_run_service = PublicRunService(
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
            offers_websites=isinstance(browser_provider, SessionBoundHostedBrowserProvider),
        )
        browser_continuation = BrowserContinuationService(
            uow_factory=uow_factory,
            clock=clock,
            principal=principal,
            runs=public_run_service,
            profiles=browser_profile_service,
        )

        async def create_surface_session(
            uow: RepositoryUnitOfWork,
            bound_principal: Principal,
            update: SurfaceInboundMessage,
        ) -> Session:
            session_id, catalog = await bootstrap_session(
                uow,
                ids,
                skill_catalogs,
                close_session,
                agent,
                bound_principal,
            )
            created = Session(
                id=session_id,
                tenant_id=bound_principal.tenant_id,
                principal_id=bound_principal.principal_id,
                agent_id=agent.id,
                agent_version=agent.version,
                status=SessionStatus.ACTIVE,
                metadata={
                    "surface": update.provider.value,
                    "surface_id": str(update.surface_id),
                },
                created_at=clock.now(),
                updated_at=clock.now(),
            )
            await uow.sessions.create(created)
            await uow.events.append(
                NewEvent(
                    session_id=created.id,
                    run_id=None,
                    event_type="session.created",
                    payload_schema_version=2,
                    actor_type="surface",
                    actor_id=bound_principal.principal_id,
                    payload={
                        "agent_id": str(agent.id),
                        "title": None,
                        "skill_pins": (
                            []
                            if catalog is None
                            else [pin.model_dump(mode="json") for pin in catalog.pins]
                        ),
                        "dropped_skills": ([] if catalog is None else list(catalog.dropped_names)),
                    },
                )
            )
            return created

        async def ignore_surface_notice(chat_ref: str, reason_code: str) -> None:
            del chat_ref, reason_code

        surface_ingress = SurfaceIngressService(
            uow_factory=uow_factory,
            principals=ConfiguredSchedulePrincipalDirectory(principal),
            run_budget=surface_limits.run_budget,
            self_approval_enabled=ruleset.self_approval_enabled,
            clock=clock,
            ids=ids,
            create_session=create_surface_session,
            submit=public_run_service.submit_surface_in,
            dispatch=public_run_service.dispatch_surface_submission,
            notice=ignore_surface_notice,
            stop_run=executor.cancel_parked_run,
            resume_after_approval=executor.requeue_after_approval,
            session_idle_seconds=surface_limits.session_idle_seconds,
            max_code_attempts=surface_limits.code_max_attempts,
            lockout_seconds=surface_limits.lockout_seconds,
            per_sender_messages_per_minute=surface_limits.per_sender_messages_per_minute,
            inbound_text_max_chars=surface_limits.inbound_text_max_chars,
        )
        device_ingest_service = DeviceMessageIngestService(
            uow_factory=uow_factory,
            clock=clock,
            ids=ids,
            seed_checkpoint=checkpoint_seeder,
            dispatcher=dispatcher,
            deliver_device_message=public_run_service.deliver_device_message,
            default_agent=agent,
            sms_enabled=settings.device_sms_enabled,
            ingest_daily_cap=device_ingest_daily_cap,
        )

        async def forget_email_source(
            owner: Principal,
            account_id: str,
            thread_id: str,
            message_ids: frozenset[str],
        ) -> None:
            service = EmailSemanticFormationService(
                uow_factory,
                clock,
                ids,
                owner,
                provider=resolved_model.provider,
                model=resolved_model.model,
            )
            for message_id in message_ids:
                await service.exclude_source(account_id, thread_id, message_id)

        async def cleanup_email_artifacts() -> None:
            await artifact_writers.sweep_expired()
            await trajectory_service.sweep_once()

        people_erasure.set_cleanup(cleanup_email_artifacts)

        task_grants_enabled = browser_provider is not None and _task_grants_composed(
            settings, browser_provider
        )
        archive_approval_service = PublicApprovalService(
            uow_factory=uow_factory,
            dispatcher=dispatcher,
            resume_waiting_run=executor.requeue_after_approval,
            self_approval_enabled=ruleset.self_approval_enabled,
            task_grants=(TaskGrantResolution(clock=clock) if task_grants_enabled else None),
        )
        folder_proposal_pass: FolderProposalPass | None = None
        if settings.thread_folders_api_enabled and folder_profiles.proposals.enabled:
            thread_grouper: ThreadGrouper = ModelAssistedThreadGrouper(
                router=model_router,
                providers=model_providers,
                clock=clock,
                ids=ids,
                model_policy=folder_profiles.proposals.model_policy,
            )
            if folder_profiles.proposals.judgment_matching_enabled:
                if judgment_provider is None:
                    # The knob needs a composed provider; the pass keeps its
                    # existing grouper and never refuses startup over it.
                    logger.warning(
                        "folder_judgment_matching_unavailable",
                        extra={"selector": "JUDGMENT_PROVIDER"},
                    )
                else:
                    thread_grouper = JudgmentFolderMatcher(
                        judge=judgment_provider,
                        inner=thread_grouper,
                        match_threshold=folder_profiles.proposals.judgment_match_threshold,
                    )
            folder_proposal_pass = FolderProposalPass(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                principal=principal,
                profile=folder_profiles.proposals,
                grouper=thread_grouper,
            )
        # ADR-0155: a non-routed policy keeps first-message titles, so no pass runs.
        title_pass: ConversationTitlePass | None = None
        title_generation = title_profiles.generation
        if (
            title_generation.enabled
            and title_generation.model_policy not in NON_ROUTED_MODEL_POLICIES
        ):
            title_pass = ConversationTitlePass(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                principal=principal,
                profile=title_generation,
                titler=ConversationTitleGenerator(
                    router=model_router,
                    providers=model_providers,
                    clock=clock,
                    ids=ids,
                    model_policy=title_generation.model_policy,
                ),
            )
        public_services = ApplicationServices(
            sessions=public_session_service,
            runs=public_run_service,
            approvals=archive_approval_service,
            artifacts=PublicArtifactService(
                uow_factory=uow_factory,
                artifacts=trajectory_artifact_store,
                general_artifacts=general_artifact_store,
                clock=clock,
                ids=ids,
                inspector=SignatureAttachmentInspector(),
            ),
            browser_profiles=browser_profile_service,
            browser_grants=browser_grant_service,
            schedules=schedule_service,
            schedule_drafts=(
                ScheduleDrafter(
                    uow_factory=uow_factory,
                    clock=clock,
                    ids=ids,
                    agent=agent,
                    limits=schedule_definition_limits,
                    interpreter=ScheduleDraftInterpreter(
                        router=model_router,
                        providers=model_providers,
                        clock=clock,
                        ids=ids,
                        model_policy=title_profiles.generation.model_policy,
                    ),
                )
                if settings.schedule_drafts_enabled
                and title_profiles.generation.model_policy not in NON_ROUTED_MODEL_POLICIES
                else None
            ),
            schedule_runs=ScheduleMaterializer(
                uow_factory=uow_factory,
                principals=ConfiguredSchedulePrincipalDirectory(principal),
                clock=clock,
                ids=ids,
                seed_checkpoint=checkpoint_seeder,
                metrics=schedule_metrics,
                notification_producer=notification_producer,
            ),
            devices=device_service,
            device_ingest=device_ingest_service,
            notifications=notification_inbox,
            surfaces=surface_management,
            reconsolidation=PublicReconsolidationService(uow_factory, clock),
            cockpit=cockpit_service,
            memory=PublicMemoryService(
                clock=clock,
                derived_enabled=settings.memory_reconsolidation_api_enabled,
                uow_factory=uow_factory,
                memory_for=lambda owner: GovernedMemoryService(uow_factory, clock, ids, owner),
            ),
            people=people_service,
            persona=PublicPersonaService(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                # ADR-0226 parts C and D.
                previewer=(
                    PersonaPreviewer(
                        uow_factory=uow_factory,
                        router=model_router,
                        providers=model_providers,
                        clock=clock,
                        ids=ids,
                        model_policy=title_profiles.generation.model_policy,
                    )
                    if title_profiles.generation.model_policy not in NON_ROUTED_MODEL_POLICIES
                    else None
                ),
                boundaries=persona_boundaries(ruleset, settings),
            ),
            model_settings=PublicModelSettingsService(
                uow_factory=uow_factory, clock=clock, ids=ids, catalog=model_settings_catalog
            ),
            folders=PublicFolderService(uow_factory=uow_factory, clock=clock, ids=ids),
            browser_task_grants=(
                PublicBrowserTaskGrantService(uow_factory=uow_factory, clock=clock)
                if task_grants_enabled
                else None
            ),
            integration_secrets=integration_secrets,
            mcp_servers=owner_mcp_servers,
            calls=call_service,
            email=EmailExperienceService(
                uow_factory=uow_factory,
                clock=clock,
                ids=ids,
                account_ids=(
                    settings.email_account_ids or (("default",) if settings.email_enabled else ())
                ),
                agent=agent,
                budget_limits=email_budget_limits,
                dispatch=dispatcher.dispatch,
                seed_checkpoint=checkpoint_seeder,
                catalogs=skill_catalogs,
                activate_session=mcp_runtime.activate_session,
                close_session=close_session,
                release_session_transports=mcp_runtime.release_session_transports,
                forget_source=forget_email_source,
                cleanup_artifacts=cleanup_email_artifacts,
                cancel_parked_run=executor.cancel_parked_run,
                resolve_archive_approval=resolve_email_archive_approval,
                resolve_send_approval=resolve_email_send_approval,
                send_undo_seconds=settings.email_send_undo_seconds,
                current_archive_principal=lambda: (
                    principal
                    if settings.email_mode_enabled
                    else principal.model_copy(update={"scopes": set()})
                ),
                archive_self_approval_enabled=ruleset.self_approval_enabled,
                unsubscribe_enabled=settings.email_unsubscribe_enabled,
                unsubscribe_grace_days=email_budget_limits.unsubscribe_grace_days,
            ),
        )
        if people_service is not None:
            people_service.imports.account_servers = public_services.email.account_servers
        if settings.email_mode_enabled:
            registry.register(EmailContextTool(public_services.email))
            registry.register(EmailFeedbackTool(public_services.email))
        if settings.email_unsubscribe_enabled and one_click_transport is not None:
            registry.register(EmailSubscriptionsTool(public_services.email.subscriptions))
            registry.register(
                EmailUnsubscribeTool(
                    public_services.email.subscriptions,
                    one_click_transport,
                    owner=lambda: principal,
                )
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

        # Run workers hold the hosted browser leases of the runs they executed,
        # so they, not the maintenance role, release and renew them (ADR-0127).
        browser_lease_maintenance = (
            None if browser_provider is None else browser_lease_upkeep(browser_provider)
        )
        # A heartbeat sleeping on a fixed clock would advance it whenever a run
        # awaits real I/O (runtime-loop.md, the heartbeat supervisor).
        heartbeat_wait = heartbeat_wait_for(clock)

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
                    maintain_browser_leases=browser_lease_maintenance,
                    heartbeat_wait=heartbeat_wait,
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
                    maintain_browser_leases=browser_lease_maintenance,
                    heartbeat_wait=heartbeat_wait,
                ),
                maintenance_factory=lambda: MaintenanceWorker(
                    uow_factory=uow_factory,
                    clock=clock,
                    sweep_approvals=approval_service.expire_due,
                    sweep_exports=trajectory_service.sweep_once,
                    sweep_artifacts=artifact_writers.sweep_expired,
                    sweep_email_cache=lambda: public_services.email.expire_cache(principal),
                    sweep_sandboxes=None if storage == "memory" else sandbox_manager.reap,
                    sweep_artifact_orphans=reconcile_artifact_orphans,
                    sweep_memory=sweep_memory,
                    sweep_traces=sweep_traces,
                    sweep_memory_consolidation=sweep_memory_consolidation,
                    sweep_memory_semantic_index=sweep_semantic_index
                    if semantic_index_pass is not None
                    else None,
                    sweep_memory_reconsolidation=recon_pass.run_once
                    if recon_pass is not None
                    else None,
                    sweep_memory_decay=sweep_memory_decay,
                    sweep_session_deletions=sweep_session_deletions,
                    sweep_people_erasures=lambda: people_erasure.resume_pending(principal),
                    sweep_people_duplicates=_duplicate_sweep(people_duplicates, principal),
                    sweep_terminal_schedules=sweep_terminal_schedules,
                    sweep_call_plans=(
                        call_plan_sweep.run_once if call_plan_sweep is not None else None
                    ),
                    **({} if cockpit_maintenance is None else cockpit_maintenance.sweeps()),
                    sweep_folder_proposals=(
                        folder_proposal_pass.run_once if folder_proposal_pass is not None else None
                    ),
                    sweep_conversation_titles=(
                        title_pass.run_once if title_pass is not None else None
                    ),
                    sweep_upload_ingests=UploadAutoIngest(
                        uow_factory=uow_factory, knowledge=knowledge_service, principal=principal
                    ).sweep_once,
                    folder_proposal_interval_seconds=folder_profiles.proposals.interval_seconds,
                    sweep_device_invocations=(
                        sweep_device_invocations if device_flags_enabled else None
                    ),
                    sweep_browser_task_grants=(
                        partial(
                            sweep_expired_task_grants,
                            uow_factory,
                            clock,
                            tenant_id=principal.tenant_id,
                        )
                        if task_grants_enabled
                        else None
                    ),
                    sweep_browser_continuations=browser_continuation.sweep,
                    memory_decay_interval_seconds=(
                        memory_profiles.formation.scheduled_interval_seconds
                    ),
                    terminal_schedule_sweep_interval_seconds=(
                        schedule_terminal_purge_interval_seconds
                    ),
                ),
                schedule_worker_factory=schedule_worker_factory,
                surface_ingress=surface_ingress,
                sandbox=sandbox_manager,
                cockpit_builder=cockpit_builder,
                cockpit_sandbox=cockpit_sandbox,
                cockpit_publication=cockpit_publication,
                job_publication=job_publication,
                job_execution=job_execution,
                job_credentials=job_credentials,
                cockpit_maintenance=cockpit_maintenance,
                cockpit_local_branch=local_branch,
                cockpit_local_outbox=local_outbox,
                mcp=mcp_runtime,
                skill_catalogs=skill_catalogs,
                skill_reviews=skill_reviews,
                tool_pipeline=pipeline,
                memory=memory_service,
                source_learning=source_learning,
                people_erasure=people_erasure,
                memory_retriever=memory_retriever,
                semantic_services=semantic_index_pass,
                recall_composition=recall_composition,
                memory_profiles=memory_profiles,
                folder_profiles=folder_profiles,
                folder_proposals=folder_proposal_pass,
                knowledge=knowledge_service,
                mcp_proxy=mcp_proxy,
                owner_mcp_proxy=owner_mcp_proxy,
                local_import_tasks=local_import_tasks,
                judgment_provider=judgment_provider,
                people_repair=people_repair,
                call_plans=call_plan_sweep,
            ),
            list(effective_providers.values()),
        )
    except BaseException:
        if semantic_index_pass is not None:
            with suppress(BaseException):
                await semantic_index_pass.close()
        if mcp_runtime is not None:
            with suppress(BaseException):
                await mcp_runtime.close()
        if mcp_proxy is not None:
            with suppress(BaseException):
                await mcp_proxy.close()
        if owner_mcp_proxy is not None:
            with suppress(BaseException):
                await owner_mcp_proxy.close()
        with suppress(BaseException):
            await sandbox_manager.close()
        if cockpit_sandbox is not None:
            with suppress(BaseException):
                await cockpit_sandbox.close()
        for provider in effective_providers.values():
            with suppress(BaseException):
                await provider.close()
        raise


def _sdk_mcp_clients(
    http_proxy_url: str | None = None, owner_proxy_url: str | None = None
) -> MCPClientFactory:
    # Every service role starts by importing this module; the MCP SDK loads
    # only in a role that constructs an MCP client.
    from agent_core.adapters.mcp.sdk import SDKMCPClientFactory

    return SDKMCPClientFactory(http_proxy_url=http_proxy_url, owner_proxy_url=owner_proxy_url)


class _ModelProviderDescription:
    """Availability metadata for a configuration capture; never a callable backend."""

    def __init__(self, name: str) -> None:
        self.name = name

    def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        raise ConfigurationError("Metadata-only composition cannot execute model requests")

    async def close(self) -> None:
        return


def _provider_adapters(
    settings: Settings,
    registry: ProviderRegistry,
    attachment_resolver: AttachmentResolver | None = None,
    *,
    describe_only: bool = False,
) -> dict[str, ModelProvider]:
    # A provider SDK loads only in a role that constructs model providers, not
    # in every service that starts by importing this module.
    providers: dict[str, ModelProvider] = {}
    for profile_name, loaded in registry.profiles.items():
        profile = loaded.document
        credential = settings.credentials.get(profile_name)
        if describe_only:
            if profile.adapter not in {"openai", "anthropic", "chat_completions"}:
                raise ConfigurationError(f"adapter {profile.adapter!r} is not constructible")
            if profile.adapter in providers:
                raise ConfigurationError(
                    f"multiple enabled profiles select adapter {profile.adapter!r}"
                )
            providers[profile.adapter] = (
                MissingCredentialProvider(profile.adapter)
                if credential is None and profile.adapter in {"openai", "anthropic"}
                else _ModelProviderDescription(profile.adapter)
            )
            continue
        api_key = None if credential is None else credential.get_secret_value()
        if profile.adapter == "openai":
            from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider

            provider: ModelProvider = (
                MissingCredentialProvider("openai")
                if api_key is None
                else OpenAIResponsesProvider(
                    api_key=api_key,
                    base_url=profile.base_url,
                    attachment_resolver=attachment_resolver,
                )
            )
        elif profile.adapter == "anthropic":
            from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider

            provider = (
                MissingCredentialProvider("anthropic")
                if api_key is None
                else AnthropicMessagesProvider(
                    api_key=api_key,
                    base_url=profile.base_url,
                    attachment_resolver=attachment_resolver,
                )
            )
        elif profile.adapter == "chat_completions":
            tags = profile.in_band_reasoning
            provider = ChatCompletionsProvider(
                base_url=profile.base_url,
                api_key=api_key,
                think_open="<think>" if tags is None else tags.open,
                think_close="</think>" if tags is None else tags.close,
                attachment_resolver=attachment_resolver,
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
    if kind is WebProviderKind.KEENABLE:
        return KeenableWebProvider(credentials=credentials)
    raise ConfigurationError(f"unsupported web provider {kind.value!r}")


def _judgment_provider(
    kind: JudgmentProviderKind,
    credentials: CredentialResolver,
    clock: Clock,
) -> JudgmentProvider | None:
    if kind is JudgmentProviderKind.DISABLED:
        return None
    if kind is JudgmentProviderKind.TYPESAFE:
        return TypeSafeJudgmentProvider(credentials=credentials, clock=clock)
    raise ConfigurationError(f"unsupported judgment provider {kind.value!r}")


def _configured_web_provider_route(
    allocations: tuple[WebProviderAllocation, ...],
    credentials: CredentialResolver,
    cache: dict[WebProviderKind, WebProvider],
) -> WebProvider | WebProviderRouter | None:
    if not allocations:
        return None
    weighted: list[tuple[WebProvider, int]] = []
    for allocation in allocations:
        provider = cache.get(allocation.provider)
        if provider is None:
            provider = _web_provider(allocation.provider, credentials)
            if provider is None:
                raise ConfigurationError("disabled web provider cannot receive traffic")
            cache[allocation.provider] = provider
        weighted.append((provider, allocation.weight))
    if len(weighted) == 1:
        return weighted[0][0]
    return WeightedWebProviderRouter(weighted)


def _browser_provider(
    kind: BrowserProviderKind,
    *,
    principal: Principal,
    allowed_origins: tuple[str, ...],
    profile_id: UUID | None,
    profiles: UnitOfWorkFactory,
    sessions: BrowserSessionControlPlane | None,
    clock: Clock,
) -> BrowserProvider | None:
    now = clock.now
    if kind is BrowserProviderKind.DISABLED:
        return None
    if kind is BrowserProviderKind.PLAYWRIGHT:
        return PlaywrightBrowserProvider(
            tenant_id=principal.tenant_id,
            allowed_origins=allowed_origins,
            # ADR-0129: the runtime checks a grant's expiry on this clock.
            now=now,
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

        async def read_run_state(run_id: UUID) -> BrowserRunState:
            return await browser_run_state(profiles, principal, run_id)

        if profile_id is not None:
            return HostedBrowserProvider(
                principal=principal,
                profile_id=profile_id,
                allowed_origins=allowed_origins,
                profiles=load_profile,
                sessions=sessions,
                now=now,
                run_state=read_run_state,
                acquire_session=BrowserStartupRecovery(clock),
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
            run_state=read_run_state,
            acquire_session=BrowserStartupRecovery(clock),
        )
    raise ConfigurationError(f"unsupported browser provider {kind.value!r}")


def _shipped_evidence_digests() -> tuple[str, str, str]:
    """The corpus, formation corpus, and holdout digests this tree ships.

    An artifact activates only for the corpus this tree ships; a tree without
    its corpora activates no provider policy.
    """

    distillation = shipped_corpus_sha256(MEMORY_DISTILLATION_CORPUS_PATH) or "unavailable"
    formation = shipped_corpus_sha256(MEMORY_FORMATION_CORPUS_PATH) or "unavailable"
    holdout = shipped_corpus_sha256(MEMORY_DISTILLATION_HOLDOUT_PATH) or "unavailable"
    if "unavailable" in (distillation, formation, holdout):
        logger.warning("memory_evaluation_corpus_unavailable")
    return distillation, formation, holdout


def _operator_artifact_is_bound(settings: Settings, evidence_path: Path, build_ref: str) -> bool:
    """An operator file must be built from the running release.

    A bundled artifact is checked by ancestry in the bundle test; an
    operator-supplied file never passes through it, so the runtime holds it to
    the one commit it can name. Outside production, where no release identity
    exists, the file is accepted and the gap is logged.
    """

    if evidence_path != settings.memory_provider_extraction_evidence:
        return True
    if settings.release_id is None:
        if settings.deployment_mode is DeploymentMode.PRODUCTION:
            logger.warning("memory_operator_evidence_unbound_in_production")
            return False
        logger.warning("memory_operator_evidence_unbound")
        return True
    deployed_sha = settings.release_id.rsplit("-", 1)[-1]
    if build_ref.startswith(deployed_sha):
        return True
    logger.warning("memory_operator_evidence_build_mismatch")
    return False


async def _model_settings_catalog(
    *,
    agent: AgentSpec,
    model_router: StaticModelRouter,
    providers: Mapping[str, ModelProvider],
    tenant_id: str,
    memory_default: ModelChoice,
    memory_alternatives: Sequence[MemoryModelOption] = (),
) -> ModelSettingsCatalog:
    """What the owner may choose here, and what applies until they do (ADR-0119).

    Chat offers every selectable policy whose provider holds a credential, and
    always the deployment default. A non-routed development composition
    offers only its own deterministic policy. Memory offers the tuple this
    composition selected and any alternative with matching evidence.
    """

    async def resolved_or_none(policy: str) -> ResolvedModel | None:
        if policy in NON_ROUTED_MODEL_POLICIES:
            return None
        try:
            return await model_router.resolve(policy, tenant_id=tenant_id)
        except ConfigurationError:
            return None

    chat_options: list[ChatModelOption] = []
    if agent.model_policy in NON_ROUTED_MODEL_POLICIES:
        chat_options.append(
            ChatModelOption(
                model_policy=agent.model_policy,
                display_name="Deterministic development model",
                provider="fake",
                model="scripted",
            )
        )
    else:
        selectable = list(model_router.registry.policies.selectable_chat_policies)
        if agent.model_policy not in selectable:
            selectable.insert(0, agent.model_policy)
        for policy in selectable:
            resolved = await resolved_or_none(policy)
            if resolved is None:
                if policy == agent.model_policy:
                    # Runs fail on an unresolvable default anyway; the choice
                    # still names what the deployment uses.
                    chat_options.append(
                        ChatModelOption(
                            model_policy=policy,
                            display_name=policy,
                            provider="unresolved",
                            model=policy,
                        )
                    )
                continue
            provider = providers.get(resolved.provider)
            if policy != agent.model_policy and (
                provider is None or isinstance(provider, MissingCredentialProvider)
            ):
                continue
            chat_options.append(
                ChatModelOption(
                    model_policy=policy,
                    display_name=resolved.display_name or resolved.model,
                    provider=resolved.provider,
                    model=resolved.model,
                    reasoning_efforts=resolved.reasoning_efforts,
                )
            )
    default_option = next(
        option for option in chat_options if option.model_policy == agent.model_policy
    )
    memory_resolved = await resolved_or_none(memory_default.model_policy)
    memory_options = (
        MemoryModelOption(
            model_policy=memory_default.model_policy,
            display_name=(
                memory_default.model_policy
                if memory_resolved is None
                else memory_resolved.display_name or memory_resolved.model
            ),
            provider="fake" if memory_resolved is None else memory_resolved.provider,
            model="scripted" if memory_resolved is None else memory_resolved.model,
            reasoning_effort=memory_default.reasoning_effort,
        ),
        *memory_alternatives,
    )
    return ModelSettingsCatalog(
        chat_default=ModelChoice(
            model_policy=agent.model_policy,
            reasoning_effort=default_chat_effort(default_option.reasoning_efforts),
        ),
        chat_options=tuple(chat_options),
        memory_default=memory_default,
        memory_options=memory_options,
    )


def _effective_model_policy(
    deployment_mode: DeploymentMode,
    requested_policy: str | None,
) -> str:
    """Keep deterministic development while refusing a fake production default."""

    if requested_policy is not None:
        return requested_policy
    if deployment_mode is DeploymentMode.PRODUCTION:
        return PRODUCTION_MODEL_POLICY
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
    mcp_oauth_gateway: MCPOAuthGateway | None = None,
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
    media_provider_override: MediaGenerationProvider | None = None,
    one_click_transport_override: OneClickTransport | None = None,
    judgment_provider_override: JudgmentProvider | None = None,
    browser_provider_override: BrowserProvider | None = None,
    device_channel_override: DeviceChannel | None = None,
    cockpit_builder_override: WidgetBuilder | None = None,
    cockpit_local_branch: bool = False,
    job_runner_override: JobRunner | None = None,
    job_screen_override: JobScreen | None = None,
    trajectory_redactor: TrajectoryRedactor | None = None,
    memory_provider_evaluation_mode: bool = False,
    memory_provider_evaluation_policy: str = PROVIDER_FORMATION_POLICY_VERSION,
    memory_distillation_evaluation_mode: bool = False,
    memory_people_evaluation_mode: bool = False,
    memory_legacy_formation_control: bool = False,
    memory_legacy_retrieval_control: bool = False,
    memory_reasoning_effort: ReasoningEffort | None = None,
    model_metadata_only: bool = False,
    recall_capture: Callable[[SemanticCompositionInputs], None] | None = None,
    service_logging: bool = False,
) -> AsyncIterator[Composition]:
    """Construct and own a Milestone 3 application graph for one process role."""

    # Phase 1: refusal. Loading Settings enforces production sandbox and auth rules.
    effective_settings = settings or load_settings()
    _configure_service_logging(effective_settings, enabled=service_logging)
    validate_settings(effective_settings)
    if recall_capture is not None and not model_metadata_only:
        raise ConfigurationError("Recall capture requires model metadata-only mode")
    if (
        (
            memory_provider_evaluation_mode
            or memory_distillation_evaluation_mode
            or memory_people_evaluation_mode
        )
        and effective_settings.memory_provider_extraction_mode
        is MemoryProviderExtractionMode.REQUIRED
    ):
        raise ConfigurationError(
            "memory extraction evaluation and activation are mutually exclusive"
        )
    if (
        sum(
            (
                memory_provider_evaluation_mode,
                memory_distillation_evaluation_mode,
                memory_people_evaluation_mode,
            )
        )
        > 1
    ):
        raise ConfigurationError("memory extraction evaluation modes are mutually exclusive")
    if (
        memory_provider_evaluation_mode
        or memory_distillation_evaluation_mode
        or memory_people_evaluation_mode
    ) and effective_settings.deployment_mode is DeploymentMode.PRODUCTION:
        raise ConfigurationError(
            "provider memory extraction evaluation mode is unavailable in production"
        )
    if memory_people_evaluation_mode and (
        storage != "memory" or not effective_settings.people_enabled
    ):
        raise ConfigurationError(
            "People evaluation requires isolated memory storage and People enabled"
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
        config for config in mcp_servers if is_email_server_id(config.server_id)
    )
    if supplied_email_rows:
        raise ConfigurationError(
            "first-party Gmail MCP rows are composed only through AGENT_EMAIL_ENABLED"
        )
    composed_email_rows = email_server_configs(
        effective_principal.tenant_id,
        enabled=effective_settings.email_enabled,
        account_ids=effective_settings.email_account_ids,
    )
    if any(config.server_id in {"bland_read", "bland_call"} for config in mcp_servers):
        raise ConfigurationError("first-party Bland rows require AGENT_CALL_ENABLED")
    composed_call_rows = calling_server_configs(
        effective_principal.tenant_id, enabled=effective_settings.call_enabled
    )
    if any(config.server_id == "svp_read" for config in mcp_servers):
        raise ConfigurationError("the first-party Scale VP row requires AGENT_SVP_ENABLED")
    # A role that carries the flag but not the grant's path composes nothing.
    composed_svp_rows = svp_server_configs(
        effective_principal.tenant_id,
        enabled=effective_settings.svp_enabled
        and (credential_resolver is not None or "svp_read" in effective_settings.credentials),
    )
    effective_mcp_servers = (
        *mcp_servers,
        *composed_email_rows,
        *composed_call_rows,
        *composed_svp_rows,
    )
    if composed_svp_rows:
        effective_principal = effective_principal.model_copy(
            update={
                "scopes": {
                    *effective_principal.scopes,
                    *(scope for row in composed_svp_rows for scope in row.required_scopes),
                }
            },
            deep=True,
        )
    if composed_call_rows:
        effective_principal = effective_principal.model_copy(
            update={
                "scopes": {
                    *effective_principal.scopes,
                    *(scope for row in composed_call_rows for scope in row.required_scopes),
                }
            },
            deep=True,
        )
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
    if memory_legacy_retrieval_control:
        memory_profiles = memory_profiles.model_copy(
            update={
                "retrieval": memory_profiles.retrieval.model_copy(
                    update={"candidate_selection": "recent"}
                )
            }
        )
    folder_profiles = FolderProfiles.from_document(
        load_config_document(effective_settings, "folders/profiles.yaml")
    )
    title_profiles = TitleProfiles.from_document(
        load_config_document(effective_settings, "titles/profiles.yaml")
    )
    run_defaults = runtime_config["run_defaults"]
    browser_task_limits = _browser_task_limits(run_defaults, runtime_config["browser_task"])
    email_budget_limits = EmailBudgetLimits.model_validate(runtime_config["email"])
    model_limits = runtime_config["model"]
    queue_config = runtime_config["queue"]
    worker_config = runtime_config["worker"]
    scheduling_config = runtime_config["scheduling"]
    notification_config = runtime_config["notifications"]
    surface_limits = SurfaceLimits.model_validate(runtime_config["surfaces"])
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
    device_config = runtime_config["device"]
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
    web_search_enabled = web_search_provider_override is not None or bool(
        effective_settings.web_search_providers
    )
    web_fetch_enabled = web_fetch_provider_override is not None or bool(
        effective_settings.web_fetch_providers
    )
    browser_enabled = (
        browser_provider_override is not None
        or effective_settings.browser_provider is not BrowserProviderKind.DISABLED
    )
    media_enabled = (
        media_provider_override is not None or "tensorscale" in effective_settings.credentials
    )
    default_enabled_tools = [
        "math.calculate",
        "conversation.ask_user",
        "system.current_time",
        "workspace.read_text",
        "workspace.write_text",
        # workspace.list_files stays registered but gives its slot to the
        # calling tools: the workspace lives for one claim (ADR-0124).
        *(
            []
            if web_search_enabled or web_fetch_enabled or browser_enabled
            else ["demo.external_write"]
        ),
        "sandbox.run_command",
        "artifact.export",
        *(["image.generate", "video.generate"] if media_enabled else []),
        WORKING_STATE_TOOL_NAME,
        SKILL_LOAD_TOOL_NAME,
        TOOL_CALL_TOOL_NAME,
        # ADR-0261: an unindexed deferral; the capture marker advertises it.
        CAPTURE_READER_TOOL_NAME,
        "memory.remember",
        "memory.learn",
        "memory.search",
        "memory.recall_episodes",
        *(
            ["people.search", "people.context", "people.history"]
            if effective_settings.people_enabled
            else []
        ),
        *(["email.context", "email.feedback"] if effective_settings.email_mode_enabled else []),
        *(
            [SUBSCRIPTIONS_TOOL_NAME, UNSUBSCRIBE_TOOL_NAME]
            if effective_settings.email_unsubscribe_enabled
            else []
        ),
        *([] if web_search_enabled and web_fetch_enabled else ["knowledge.ingest"]),
        "knowledge.search",
        *(["web.search"] if web_search_enabled else []),
        *(["web.fetch", "web.fetch_json", "web.inspect_image"] if web_fetch_enabled else []),
        *(
            [SCHEDULE_CREATE_TOOL_NAME, *SCHEDULE_LIFECYCLE_TOOL_NAMES]
            if effective_settings.schedule_api_enabled
            and effective_settings.schedule_worker_enabled
            else []
        ),
        *(["browser.navigate", "browser.observe", "browser.act"] if browser_enabled else []),
        *(
            ["browser.upload"]
            if browser_enabled
            and (
                browser_provider_override is None
                or isinstance(browser_provider_override, BrowserImageUploader)
            )
            else []
        ),
        *(["delegate.run"] if effective_settings.delegation_enabled else []),
        *(COCKPIT_TOOL_NAMES if effective_settings.cockpit_enabled else ()),
        *(JOB_TOOL_NAMES if effective_settings.jobs_enabled else ()),
        *(
            INSTALL_TOOL_NAMES
            if effective_settings.jobs_enabled
            and effective_settings.cockpit_enabled
            and effective_settings.schedule_api_enabled
            and effective_settings.schedule_worker_enabled
            else ()
        ),
        *(
            [
                RecordsListTool.spec.name,
                RecordsQueryTool.spec.name,
                RecordsAppendTool.spec.name,
                RecordsCreateSeriesTool.spec.name,
                RecordsDeleteTool.spec.name,
                RecordsImportTool.spec.name,
            ]
            if effective_settings.records_enabled
            else []
        ),
        *(
            [DEVICE_SMS_SEND_TOOL_NAME]
            if effective_settings.device_channel_enabled
            and effective_settings.device_sms_enabled
            and (
                device_channel_override is not None
                or effective_settings.notification_dispatch_enabled
            )
            else []
        ),
    ]
    # ADR-0123: management tools that change or clean up existing state are
    # never the first step of a request, so they are offered through the
    # deferred tool index and leave their definition slots to other tools.
    default_deferred_tools = [
        name
        for name in (
            "memory.learn",
            "workspace.write_text",
            # A JSON read is a narrower web.fetch, chosen once the model knows
            # the answer is JSON; it waits in the index (ADR-0258).
            "web.fetch_json",
            "web.inspect_image",
            SCHEDULE_UPDATE_TOOL_NAME,
            SCHEDULE_PAUSE_TOOL_NAME,
            SCHEDULE_RESUME_TOOL_NAME,
            SCHEDULE_CANCEL_TOOL_NAME,
            SUBSCRIPTIONS_TOOL_NAME,
            UNSUBSCRIBE_TOOL_NAME,
            # Declaring or deleting a series and the reviewed import are rare and
            # ask the owner first, so they wait in the index; listing, querying and
            # appending stay direct. A refused append names records.import.
            RecordsCreateSeriesTool.spec.name,
            RecordsDeleteTool.spec.name,
            RecordsImportTool.spec.name,
            # The Cockpit is a mode of its own; its tools never take a definition slot.
            *COCKPIT_TOOL_NAMES,
            *JOB_TOOL_NAMES,
            *INSTALL_TOOL_NAMES,
            CAPTURE_READER_TOOL_NAME,
        )
        if name in default_enabled_tools
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
        limits=limits or _run_limits_from_defaults(run_defaults),
        metadata={
            **(
                {DEFERRED_TOOLS_METADATA_KEY: default_deferred_tools}
                if enabled_tools is None and default_deferred_tools
                else {}
            ),
            # ADR-0130: the default agent's version pins the limits a chat
            # bound to a website profile runs under. Compositions that pin
            # their own limits or tools keep exactly what they asked for.
            **(
                {BROWSER_TASK_LIMITS_METADATA_KEY: browser_task_limits.model_dump(mode="json")}
                if browser_enabled and enabled_tools is None and limits is None
                else {}
            ),
        },
    )
    if storage == "postgres":
        agent = agent.model_copy(
            update={"version": _content_addressed_agent_version(agent)}, deep=True
        )
    engine = None
    recall_schema_sha256 = None
    model_providers: list[ModelProvider] = []
    web_providers: list[WebProvider] = []
    media_provider: MediaGenerationProvider | None = None
    web_provider_cache: dict[WebProviderKind, WebProvider] = {}
    one_click_transport: OneClickTransport | None = None
    unsubscribe_proxy: WorkerEgressProxy | None = None
    judgment_provider: JudgmentProvider | None = None
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
            if effective_settings.memory_semantic_index_enabled or model_metadata_only:
                from agent_core.adapters.persistence.recall_schema import capture_recall_schema

                recall_schema_sha256 = configuration_digest(await capture_recall_schema(engine))
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
                        surface_admission_limits=surface_limits,
                        schedule_metrics=schedule_metrics,
                    ),
                    effective_principal.tenant_id,
                ),
            )
        # ADR-0120: every constructed adapter can read the owner's attachments,
        # whether or not the upload flag is on, so existing ones keep rendering.
        attachment_resolver = StoredAttachmentResolver(
            uow_factory=uow_factory,
            store=FilesystemArtifactStore(effective_settings.artifact_root),
            principal=effective_principal,
            clock=effective_clock,
        )
        provider_adapters = (
            _provider_adapters(effective_settings, provider_registry, describe_only=True)
            if model_metadata_only
            else _provider_adapters(effective_settings, provider_registry, attachment_resolver)
        )
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
        if media_enabled:
            media_provider = media_provider_override or TensorScaleMediaProvider(
                credentials=effective_credential_resolver
            )
        web_search_provider = (
            web_search_provider_override
            if web_search_provider_override is not None
            else _configured_web_provider_route(
                effective_settings.web_search_providers,
                effective_credential_resolver,
                web_provider_cache,
            )
        )
        if web_search_provider_override is not None:
            web_providers.append(web_search_provider_override)
        web_fetch_provider = (
            web_fetch_provider_override
            if web_fetch_provider_override is not None
            else _configured_web_provider_route(
                effective_settings.web_fetch_providers,
                effective_credential_resolver,
                web_provider_cache,
            )
        )
        if (
            web_fetch_provider_override is not None
            and web_fetch_provider is not web_search_provider
        ):
            web_providers.append(web_fetch_provider_override)
        web_providers.extend(
            provider
            for provider in web_provider_cache.values()
            if all(provider is not tracked for tracked in web_providers)
        )
        judgment_provider = (
            judgment_provider_override
            if judgment_provider_override is not None
            else _judgment_provider(
                effective_settings.judgment_provider,
                effective_credential_resolver,
                effective_clock,
            )
        )
        if (
            judgment_provider_override is None
            and credential_resolver is None
            and effective_settings.judgment_provider is JudgmentProviderKind.TYPESAFE
            and "typesafe" not in effective_settings.credentials
        ):
            # Degrade, never refuse startup: every call then fails without dialing
            # and each consumer takes its deterministic fallback.
            logger.warning(
                "judgment_credential_missing",
                extra={"selector": "JUDGMENT_PROVIDER", "credential": "TYPESAFE_API_KEY"},
            )
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
                clock=effective_clock,
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
        if effective_settings.email_unsubscribe_enabled:
            # The public-HTTPS transport is constructed for one tool and handed to
            # nothing else; the operator's egress policy cannot select it (ADR-0112).
            if one_click_transport_override is not None:
                one_click_transport = one_click_transport_override
            else:
                unsubscribe_proxy = await start_unsubscribe_egress_proxy(
                    tenant_id=effective_principal.tenant_id
                )
                one_click_transport = HttpOneClickTransport(unsubscribe_proxy.url)
        composition, model_providers = await _compose(
            storage=storage,
            recall_schema_sha256=recall_schema_sha256,
            settings=effective_settings,
            agent=agent,
            email_budget_limits=email_budget_limits,
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
            inline_output_bytes=int(output_config["inline_maximum_bytes"]),
            capture_step_maximum_reads=int(tool_config["capture"]["step_maximum_reads"]),
            capture_run_maximum_reads=int(tool_config["capture"]["run_maximum_reads"]),
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
            schedule_terminal_retention_days=int(scheduling_config["terminal_retention_days"]),
            schedule_terminal_purge_interval_seconds=float(
                scheduling_config["terminal_purge_interval_seconds"]
            ),
            schedule_terminal_purge_batch=int(scheduling_config["terminal_purge_batch"]),
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
            folder_profiles=folder_profiles,
            title_profiles=title_profiles,
            mcp_config=tool_config["mcp"],
            max_compactions_per_step=int(runtime_config["context"]["max_compactions_per_step"]),
            skill_store=skill_store,
            mcp_clients=mcp_client_factory,
            mcp_scripts=mcp_scripts,
            mcp_oauth_gateway=mcp_oauth_gateway,
            credential_resolver=effective_credential_resolver,
            mcp_server_configs=effective_mcp_servers,
            web_search_provider=web_search_provider,
            web_fetch_provider=web_fetch_provider,
            media_provider=media_provider,
            one_click_transport=one_click_transport,
            judgment_provider=judgment_provider,
            memory_provider_evaluation_mode=memory_provider_evaluation_mode,
            memory_provider_evaluation_policy=memory_provider_evaluation_policy,
            memory_distillation_evaluation_mode=memory_distillation_evaluation_mode,
            memory_people_evaluation_mode=memory_people_evaluation_mode,
            memory_legacy_formation_control=memory_legacy_formation_control,
            memory_reasoning_effort=memory_reasoning_effort,
            browser_provider=browser_provider,
            browser_profile_lifecycle=browser_profile_lifecycle,
            browser_authentications=browser_authentications,
            device_channel=device_channel_override,
            device_invocation_timeout_seconds=int(device_config["invocation_timeout_seconds"]),
            device_invocation_poll_seconds=float(device_config["invocation_poll_seconds"]),
            device_ingest_daily_cap=int(device_config["ingest_daily_cap"]),
            surface_limits=surface_limits,
            recall_capture=recall_capture,
            cockpit_builder_override=cockpit_builder_override,
            cockpit_local_branch=cockpit_local_branch,
            job_runner_override=job_runner_override,
            job_screen_override=job_screen_override,
        )
        composition = replace(
            composition,
            attachment_resolver=attachment_resolver,
            reliability_check=(
                None
                if engine is None or not effective_settings.notification_dispatch_enabled
                else partial(
                    check_reliability,
                    create_session_factory(engine),
                    outbox_factory=lambda session: PostgresNotificationOutbox(
                        session, effective_clock
                    ),
                    events_factory=PostgresProcessEventRepository,
                    principal=effective_principal,
                    clock=effective_clock,
                    ids=effective_ids,
                    release_id=effective_settings.release_id,
                )
            ),
            reliability_report=(
                None
                if engine is None
                else partial(
                    reliability_report,
                    create_session_factory(engine),
                    principal=effective_principal,
                )
            ),
            latency_report=(
                None
                if engine is None
                else partial(
                    chat_latency_report,
                    create_session_factory(engine),
                    tenant_id=effective_principal.tenant_id,
                )
            ),
        )
        yield composition
    finally:
        if composition is not None:
            if composition.semantic_services is not None:
                try:
                    await composition.semantic_services.close()
                except Exception as exc:
                    logger.warning(
                        "semantic_close_failed", extra={"error_class": type(exc).__name__}
                    )
            for import_task in tuple(composition.local_import_tasks):
                import_task.cancel()
            await asyncio.gather(*composition.local_import_tasks, return_exceptions=True)
            try:
                await composition.mcp.close()
            except Exception as exc:
                logger.warning("mcp_close_failed", extra={"error_class": type(exc).__name__})
            try:
                await composition.sandbox.close()
            except Exception as exc:
                logger.warning("sandbox_close_failed", extra={"error_class": type(exc).__name__})
            if composition.job_credentials is not None:
                try:
                    await composition.job_credentials.close()
                except Exception as exc:
                    logger.warning(
                        "job_credentials_close_failed",
                        extra={"error_class": type(exc).__name__},
                    )
            if composition.cockpit_maintenance is not None:
                with suppress(Exception):
                    await composition.cockpit_maintenance.close()
            if composition.cockpit_sandbox is not None:
                try:
                    await composition.cockpit_sandbox.close()
                except Exception as exc:
                    logger.warning(
                        "cockpit_sandbox_close_failed",
                        extra={"error_class": type(exc).__name__},
                    )
            for proxy in (composition.mcp_proxy, composition.owner_mcp_proxy):
                if proxy is None:
                    continue
                try:
                    await proxy.close()
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
        for closing in (
            one_click_transport if one_click_transport_override is None else None,
            unsubscribe_proxy,
        ):
            if closing is not None:
                try:
                    await closing.close()
                except Exception as exc:
                    logger.warning(
                        "unsubscribe_transport_close_failed",
                        extra={"error_class": type(exc).__name__},
                    )
        if media_provider is not None:
            try:
                await media_provider.close()
            except Exception as exc:
                logger.warning(
                    "media_provider_close_failed", extra={"error_class": type(exc).__name__}
                )
        for web_provider in web_providers:
            try:
                await web_provider.close()
            except Exception as exc:
                logger.warning(
                    "web_provider_close_failed",
                    extra={"provider": web_provider.name, "error_class": type(exc).__name__},
                )
        if judgment_provider is not None:
            try:
                await judgment_provider.close()
            except Exception as exc:
                logger.warning(
                    "judgment_provider_close_failed",
                    extra={
                        "provider": judgment_provider.name,
                        "error_class": type(exc).__name__,
                    },
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
