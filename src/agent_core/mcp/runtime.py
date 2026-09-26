"""Session-scoped MCP discovery, registration, availability, and bounded recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite
from typing import Any
from uuid import UUID
from weakref import WeakValueDictionary

from agent_core.domain.agents import Principal
from agent_core.domain.credentials import CredentialRef, SecretValue
from agent_core.domain.errors import (
    ConflictError,
    MCPTransportError,
    MCPUnauthorizedError,
    MCPUnavailableError,
    NotFoundError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPServerConfig,
    MCPToolCatalogRecord,
    MCPTransport,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, SideEffectClass, TrustLevel
from agent_core.domain.sessions import SessionStatus
from agent_core.domain.skills import CatalogEntry, SkillManifest, SkillSource
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.mcp.configuration import build_stdio_environment
from agent_core.mcp.mapping import MCPMappingReport, map_discovered_tools
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.mcp import MCPClient, MCPClientFactory
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.tools import ToolRegistry

_SKILL_CHARACTERS = re.compile(r"[^a-z0-9-]")
_SKILL_HYPHENS = re.compile(r"-+")
type _RegistrationKey = tuple[str, str, str]
type CallInterceptor = Callable[
    [
        ToolExecutionContext,
        str,
        str,
        dict[str, Any],
        Callable[[dict[str, Any]], Awaitable[MCPCallResult]],
    ],
    Awaitable[MCPCallResult],
]
logger = logging.getLogger(__name__)
_MAXIMUM_PARALLEL_PREPARATIONS = 8
_MAXIMUM_PARALLEL_STDIO_PREPARATIONS = 2

_GMAIL_FAILURE_CODES = frozenset(
    {
        "gmail.credential_rejected",
        "gmail.rate_limited",
        "gmail.provider_unavailable",
        "gmail.provider_rejected",
        "gmail.provider_output_invalid",
        "gmail.outcome_unknown",
        "gmail.arguments_invalid",
    }
)


@dataclass(slots=True)
class _Connection:
    session_id: UUID
    config: MCPServerConfig
    client: MCPClient | None
    discovery: MCPDiscovery
    report: MCPMappingReport
    last_used_at: datetime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    withdrawn_tools: frozenset[str] = frozenset()
    available_resources: frozenset[str] | None = None
    closed: bool = False
    reauthentication_attempted: bool = False
    unavailable_reason: str | None = None
    # A pin taken from remembered discovery starts its server on first use (ADR-0131).
    reused: bool = False
    handshake_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _DisconnectedServer:
    config: MCPServerConfig
    reason_code: str


@dataclass(frozen=True, slots=True)
class _Discovered:
    client: MCPClient | None
    discovery: MCPDiscovery
    report: MCPMappingReport
    handshake_ms: int


@dataclass(frozen=True, slots=True)
class _RememberedDiscovery:
    discovery: MCPDiscovery
    report: MCPMappingReport
    discovered_at: datetime


def _discovery_key(config: MCPServerConfig) -> str:
    """Key remembered discovery by the whole server configuration (ADR-0131)."""
    payload = config.model_dump(mode="json")
    payload["required_scopes"] = sorted(payload["required_scopes"])
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _elapsed_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, round((finished_at - started_at).total_seconds() * 1000))


class MCPTool:
    def __init__(self, runtime: MCPRuntime, spec: ToolSpec, remote_name: str) -> None:
        self._runtime = runtime
        self.spec = spec
        self._remote_name = remote_name

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        return await self._runtime.call_tool(
            context,
            self.spec,
            self._remote_name,
            arguments,
        )


class MCPResourceTool:
    def __init__(self, runtime: MCPRuntime, spec: ToolSpec) -> None:
        self._runtime = runtime
        self.spec = spec

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        uri = arguments.get("uri")
        return await self._runtime.read_resource(
            context,
            self.spec,
            uri if isinstance(uri, str) and uri else None,
        )


class MCPRuntime:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        registry: ToolRegistry,
        clients: MCPClientFactory,
        credentials: CredentialResolver,
        clock: Clock,
        ids: IdFactory,
        *,
        connect_timeout_seconds: float = 10,
        idle_timeout_seconds: float = 900,
        discovery_reuse_seconds: float = 3600,
        call_interceptor: CallInterceptor | None = None,
    ) -> None:
        """Share bounded preparation capacity across this runtime's sessions."""
        if not isfinite(idle_timeout_seconds) or idle_timeout_seconds <= 0:
            raise ValueError("MCP idle timeout must be finite and positive")
        if not isfinite(discovery_reuse_seconds) or discovery_reuse_seconds <= 0:
            raise ValueError("MCP discovery reuse must be finite and positive")
        self._idle_timeout_seconds = idle_timeout_seconds
        self._discovery_reuse_seconds = discovery_reuse_seconds
        # The last live discovery of each configured server in this process (ADR-0131).
        self._discoveries: dict[str, _RememberedDiscovery] = {}
        self._warmup_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._principals: dict[UUID, Principal] = {}
        self._closing = False
        self._uow_factory = uow_factory
        self._registry = registry
        self._clients = clients
        self._credentials = credentials
        self._clock = clock
        self._ids = ids
        self._connect_timeout_seconds = connect_timeout_seconds
        self._preparation_slots = asyncio.Semaphore(_MAXIMUM_PARALLEL_PREPARATIONS)
        self._stdio_preparation_slots = asyncio.Semaphore(_MAXIMUM_PARALLEL_STDIO_PREPARATIONS)
        self._call_interceptor = call_interceptor
        self._sessions: dict[UUID, dict[str, _Connection]] = {}
        # Sessions whose every enabled server was attempted, and the servers
        # attempted for each session, including scoped typed-task preparation.
        self._prepared: set[UUID] = set()
        self._attempted: dict[UUID, set[str]] = {}
        self._locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()
        self._session_registrations: dict[UUID, set[_RegistrationKey]] = {}
        self._registration_owners: dict[_RegistrationKey, set[UUID]] = {}
        self._deferred_events: set[UUID] = set()
        self._pending_events: dict[UUID, list[tuple[str, dict[str, Any]]]] = {}
        self._preparation_cleanup_tasks: dict[asyncio.Task[bool | None], MCPClient] = {}

    def _lock(self, session_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _register(
        self,
        session_id: UUID,
        tenant_id: str,
        tool: MCPTool | MCPResourceTool,
    ) -> None:
        key = (tenant_id, tool.spec.name, tool.spec.version)
        owners = self._registration_owners.get(key)
        if owners is None:
            self._registry.register_dynamic(tool, tenant_id=tenant_id)
            owners = set()
            self._registration_owners[key] = owners
        owners.add(session_id)
        self._session_registrations.setdefault(session_id, set()).add(key)

    def _unregister_session(
        self, session_id: UUID, keep: frozenset[_RegistrationKey] = frozenset()
    ) -> None:
        """Drop a session's registrations, except those ``keep`` names."""
        registrations = self._session_registrations.pop(session_id, set())
        if retained := registrations & keep:
            self._session_registrations[session_id] = retained
        for key in registrations - keep:
            owners = self._registration_owners[key]
            owners.discard(session_id)
            if owners:
                continue
            tenant_id, name, version = key
            self._registry.unregister_dynamic(name, version, tenant_id=tenant_id)
            self._registration_owners.pop(key, None)

    async def _credential(self, config: MCPServerConfig) -> SecretValue | None:
        if config.credential_ref is None:
            return None
        return await self._credentials.resolve(CredentialRef(config.credential_ref))

    @staticmethod
    def _environment(
        config: MCPServerConfig,
        credential: SecretValue | None,
    ) -> dict[str, str]:
        if config.transport is MCPTransport.STDIO:
            return build_stdio_environment(config, credential)
        return {}

    def _preparation_cleanup_finished(self, task: asyncio.Task[bool | None]) -> None:
        """Release the retained client only after its exit task has settled."""
        self._preparation_cleanup_tasks.pop(task, None)
        with suppress(BaseException):
            task.result()

    async def _close_preparation_client(self, client: MCPClient) -> None:
        cleanup = asyncio.create_task(client.__aexit__(None, None, None))
        try:
            async with asyncio.timeout(self._connect_timeout_seconds):
                await asyncio.shield(cleanup)
        except BaseException:
            self._preparation_cleanup_tasks[cleanup] = client
            cleanup.add_done_callback(self._preparation_cleanup_finished)
            raise

    async def _drain_preparation_cleanups(self) -> None:
        """Bound graceful draining, then stop transports before cancelling exit wrappers."""
        tasks = set(self._preparation_cleanup_tasks)
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=self._connect_timeout_seconds)
        for task in done:
            self._preparation_cleanup_finished(task)
        targets = [
            (task, client)
            for task in pending
            if (client := self._preparation_cleanup_tasks.get(task)) is not None
        ]
        results = await asyncio.gather(
            *(client.force_close() for _, client in targets), return_exceptions=True
        )
        errors: list[BaseException] = []
        stopped: set[asyncio.Task[bool | None]] = set()
        for (task, _client), result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(result)
                continue
            task.cancel()
            stopped.add(task)
        if stopped:
            done, pending = await asyncio.wait(stopped, timeout=self._connect_timeout_seconds)
            for task in done:
                self._preparation_cleanup_finished(task)
            if pending:
                errors.append(TimeoutError("MCP exit wrappers did not settle after forced closure"))
        if errors:
            # Keep failed clients/tasks owned so another shutdown attempt can retry.
            raise MCPTransportError from BaseExceptionGroup("MCP shutdown cleanup failures", errors)

    async def _prepare_server(
        self,
        session_id: UUID,
        config: MCPServerConfig,
    ) -> _Connection | _DisconnectedServer:
        """Start and discover one server for a session, remembering what it advertised."""
        discovered = await self._discover_server(config)
        if isinstance(discovered, _DisconnectedServer):
            return discovered
        return _Connection(
            session_id=session_id,
            config=config,
            client=discovered.client,
            discovery=discovered.discovery,
            report=discovered.report,
            last_used_at=self._clock.now(),
            handshake_ms=discovered.handshake_ms,
        )

    async def _pin_server(
        self,
        session_id: UUID,
        config: MCPServerConfig,
        *,
        reuse: bool,
    ) -> _Connection | _DisconnectedServer:
        """Pin remembered discovery without a transport, or discover live (ADR-0131)."""
        remembered = self._remembered(config) if reuse else None
        if remembered is None:
            return await self._prepare_server(session_id, config)
        return _Connection(
            session_id=session_id,
            config=config,
            client=None,
            discovery=remembered.discovery,
            report=remembered.report,
            last_used_at=self._clock.now(),
            reused=True,
        )

    def _remembered(self, config: MCPServerConfig) -> _RememberedDiscovery | None:
        """Reuse stdio discovery for the process; a tenant HTTP catalog only briefly."""
        remembered = self._discoveries.get(_discovery_key(config))
        if remembered is None or config.transport is MCPTransport.STDIO:
            return remembered
        age = (self._clock.now() - remembered.discovered_at).total_seconds()
        return remembered if 0 <= age < self._discovery_reuse_seconds else None

    def _forget(self, config: MCPServerConfig) -> None:
        self._discoveries.pop(_discovery_key(config), None)

    async def _discover_server(self, config: MCPServerConfig) -> _Discovered | _DisconnectedServer:
        """Admit bounded startup work before beginning the handshake deadline."""
        async with AsyncExitStack() as capacity:
            if config.transport is MCPTransport.STDIO:
                await capacity.enter_async_context(self._stdio_preparation_slots)
            await capacity.enter_async_context(self._preparation_slots)
            client: MCPClient | None = None
            entered: MCPClient | None = None
            started_at = self._clock.now()
            try:
                credential = await self._credential(config)
                client = self._clients(
                    config,
                    credential,
                    self._environment(config, credential),
                )
                async with asyncio.timeout(self._connect_timeout_seconds):
                    entered = await client.__aenter__()
                    discovery = await entered.discover()
            except (MCPUnauthorizedError, PermissionError):
                self._forget(config)
                target = entered or client
                if target is not None:
                    with suppress(Exception):
                        await self._close_preparation_client(target)
                return _DisconnectedServer(config, "tool.auth_failed")
            except (MCPTransportError, TimeoutError):
                self._forget(config)
                target = entered or client
                if target is not None:
                    with suppress(Exception):
                        await self._close_preparation_client(target)
                return _DisconnectedServer(config, "tool.server_unreachable")
            except BaseException as exc:
                if isinstance(exc, Exception):
                    self._forget(config)
                target = entered or client
                if target is not None:
                    with suppress(BaseException):
                        await self._close_preparation_client(target)
                raise
            try:
                report = map_discovered_tools(config, discovery.tools)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    self._forget(config)
                with suppress(BaseException):
                    await self._close_preparation_client(entered)
                raise
            if discovery.resources:
                resource_name = f"mcp.{config.server_id}.read_resource"
                shadowed = tuple(
                    mapped for mapped in report.accepted if mapped.spec.name == resource_name
                )
                if shadowed:
                    report = replace(
                        report,
                        accepted=tuple(
                            mapped
                            for mapped in report.accepted
                            if mapped.spec.name != resource_name
                        ),
                        rejected=tuple(
                            sorted(
                                {
                                    *report.rejected,
                                    *(mapped.remote_name for mapped in shadowed),
                                }
                            )
                        ),
                    )
            finished_at = self._clock.now()
            self._discoveries[_discovery_key(config)] = _RememberedDiscovery(
                discovery=discovery, report=report, discovered_at=finished_at
            )
            return _Discovered(
                client=entered,
                discovery=discovery,
                report=report,
                handshake_ms=_elapsed_ms(started_at, finished_at),
            )

    async def _close_prepared_connections(
        self,
        prepared: list[_Connection | _DisconnectedServer],
    ) -> None:
        for result in prepared:
            if not isinstance(result, _Connection):
                continue
            with suppress(BaseException):
                await self._release_connection(result)

    async def prepare(
        self,
        session_id: UUID,
        principal: Principal,
        server_ids: frozenset[str] | None = None,
    ) -> None:
        """Discover servers concurrently, then register results in configured order.

        ``server_ids`` limits preparation to the servers a typed task calls
        (ADR-0104). A server already attempted for the session is not started
        again, so a later full preparation adds only the remaining servers.
        """
        if self._closing:
            raise MCPUnavailableError("tool.server_unreachable")
        if self._ready(session_id, server_ids):
            return
        async with self._lock(session_id):
            if self._closing:
                raise MCPUnavailableError("tool.server_unreachable")
            if self._ready(session_id, server_ids):
                return
            attempted = self._attempted.get(session_id, set())
            earlier = frozenset(self._session_registrations.get(session_id, ()))
            async with self._uow_factory() as uow:
                configs = [
                    config
                    for config in await uow.mcp_servers.list_enabled(principal.tenant_id)
                    if config.server_id not in attempted
                    and (server_ids is None or config.server_id in server_ids)
                ]
                try:
                    await uow.sessions.get(session_id, principal)
                except NotFoundError:
                    self._deferred_events.add(session_id)
            # Full preparation may pin remembered discovery; named servers start now (ADR-0131).
            reuse = server_ids is None
            tasks = [
                asyncio.create_task(self._pin_server(session_id, config, reuse=reuse))
                for config in configs
            ]
            try:
                prepared = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                completed: list[_Connection | _DisconnectedServer] = []
                for task in tasks:
                    if task.cancelled():
                        continue
                    try:
                        completed.append(task.result())
                    except BaseException:
                        continue
                await self._close_prepared_connections(completed)
                self._unregister_session(session_id, keep=earlier)
                raise
            connections: dict[str, _Connection] = {}
            try:
                if self._closing:
                    raise MCPUnavailableError("tool.server_unreachable")
                for result in prepared:
                    if isinstance(result, _DisconnectedServer):
                        await self._event(
                            session_id,
                            "mcp.server.disconnected",
                            {
                                "server_id": result.config.server_id,
                                "reason_code": result.reason_code,
                            },
                        )
                        continue
                    connection = result
                    config = connection.config
                    discovery = connection.discovery
                    report = connection.report
                    connections[config.server_id] = connection
                    await self._record_catalog(connection)
                    for mapped in report.accepted:
                        self._register(
                            session_id,
                            principal.tenant_id,
                            MCPTool(self, mapped.spec, mapped.remote_name),
                        )
                    for group in report.conflicts:
                        await self._event(
                            session_id,
                            "mcp.catalog.conflict",
                            {"server_id": config.server_id, "remote_names": list(group)},
                        )
                    for remote_name in report.rejected:
                        await self._event(
                            session_id,
                            "mcp.tool.rejected",
                            {"server_id": config.server_id, "remote_name": remote_name},
                        )
                    if discovery.resources:
                        spec = self._resource_spec(config, report.catalog_hash)
                        self._register(
                            session_id,
                            principal.tenant_id,
                            MCPResourceTool(self, spec),
                        )
                    if connection.reused:
                        await self._event(
                            session_id, "mcp.server.pinned", self._catalog_payload(connection)
                        )
                    else:
                        await self._event(
                            session_id, "mcp.server.connected", self._connected_payload(connection)
                        )
                if self._closing:
                    raise MCPUnavailableError("tool.server_unreachable")
            except BaseException:
                await self._close_prepared_connections(prepared)
                self._unregister_session(session_id, keep=earlier)
                raise
            self._sessions.setdefault(session_id, {}).update(connections)
            self._attempted.setdefault(session_id, set()).update(
                result.config.server_id for result in prepared
            )
            if server_ids is None:
                self._prepared.add(session_id)
            self._principals[session_id] = principal
            if self._maintenance_task is None:
                self._maintenance_task = asyncio.create_task(self._maintain_sessions())

    @staticmethod
    def _catalog_payload(connection: _Connection) -> dict[str, Any]:
        report = connection.report
        return {
            "server_id": connection.config.server_id,
            "transport": connection.config.transport.value,
            "catalog_hash": report.catalog_hash,
            "tool_count": len(report.accepted),
            "rejected": list(report.rejected),
            "conflicts": [list(group) for group in report.conflicts],
        }

    @classmethod
    def _connected_payload(cls, connection: _Connection) -> dict[str, Any]:
        """Describe one completed handshake, including its duration (ADR-0131)."""
        return {**cls._catalog_payload(connection), "duration_ms": connection.handshake_ms}

    def _ready(self, session_id: UUID, server_ids: frozenset[str] | None) -> bool:
        if session_id in self._prepared:
            return True
        return server_ids is not None and server_ids <= self._attempted.get(session_id, set())

    async def _record_catalog(self, connection: _Connection) -> None:
        config = connection.config
        records = tuple(
            MCPToolCatalogRecord(
                id=self._ids.new_id(),
                tenant_id=config.tenant_id,
                server_id=config.server_id,
                catalog_hash=connection.report.catalog_hash,
                remote_name=mapped.remote_name,
                registry_name=mapped.spec.name,
                input_schema=mapped.spec.input_schema,
                discovered_at=self._clock.now(),
            )
            for mapped in connection.report.accepted
        )
        async with self._uow_factory() as uow:
            await uow.mcp_servers.record_catalog(
                config.tenant_id,
                config.server_id,
                connection.report.catalog_hash,
                records,
            )

    @staticmethod
    def _resource_spec(config: MCPServerConfig, catalog_hash: str) -> ToolSpec:
        return ToolSpec(
            name=f"mcp.{config.server_id}.read_resource",
            version=catalog_hash,
            description="List advertised resources or read one advertised URI.",
            input_schema={
                "type": "object",
                "properties": {"uri": {"type": "string", "maxLength": 4096}},
                "additionalProperties": False,
            },
            output_schema=None,
            side_effect=SideEffectClass.NETWORK_READ,
            risk=config.risk,
            idempotency=IdempotencyClass.READ_ONLY,
            required_scopes=set(config.required_scopes),
            timeout_seconds=config.timeout_seconds,
            maximum_output_bytes=config.maximum_output_bytes,
            allow_parallel=False,
            target_kind="mcp",
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            source=ToolSource.MCP,
            server_id=config.server_id,
        )

    def _connection(self, session_id: UUID, server_id: str) -> _Connection:
        try:
            return self._sessions[session_id][server_id]
        except KeyError as exc:
            raise MCPUnavailableError("tool.server_unreachable") from exc

    async def call_tool(
        self,
        context: ToolExecutionContext,
        spec: ToolSpec,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if spec.server_id is None:
            raise ConflictError("MCP tool has no server id")
        connection = self._connection(context.session_id, spec.server_id)
        if connection.config.tenant_id != context.tenant_id:
            raise MCPUnavailableError("tool.server_unreachable")

        async def operation() -> MCPCallResult:
            if spec.server_id in {"bland_read", "bland_call"}:
                if self._call_interceptor is None:
                    return MCPCallResult(
                        content=("bland.platform_required",),
                        is_error=True,
                        structured={"effect_status": "not_applied"},
                    )
                return await self._call_interceptor(
                    context,
                    spec.server_id,
                    remote_name,
                    arguments,
                    lambda values: self._connected_client(connection).call_tool(
                        remote_name, values
                    ),
                )
            return await self._connected_client(connection).call_tool(remote_name, arguments)

        return await self._invoke(
            connection,
            spec,
            operation,
            remote_name=remote_name,
        )

    async def read_resource(
        self,
        context: ToolExecutionContext,
        spec: ToolSpec,
        uri: str | None,
    ) -> ToolResult:
        if spec.server_id is None:
            raise ConflictError("MCP resource tool has no server id")
        connection = self._connection(context.session_id, spec.server_id)
        if connection.config.tenant_id != context.tenant_id:
            raise MCPUnavailableError("tool.server_unreachable")
        advertised = {resource.uri for resource in connection.discovery.resources}
        if uri is not None and uri not in advertised:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail="resource URI was not advertised by the server",
                    retryable=False,
                ),
            )
        return await self._invoke(
            connection,
            spec,
            lambda: self._connected_client(connection).read_resource(uri),
            resource_uri=uri,
        )

    @staticmethod
    def _connected_client(connection: _Connection) -> MCPClient:
        if connection.client is None:
            raise MCPTransportError
        return connection.client

    async def _reconnect(self, connection: _Connection) -> None:
        """Refresh transport availability while retaining the original advertisement."""
        replacement = await self._prepare_server(connection.session_id, connection.config)
        if isinstance(replacement, _DisconnectedServer):
            connection.unavailable_reason = replacement.reason_code
            return
        current = {tool.remote_name: tool.spec for tool in replacement.report.accepted}
        connection.withdrawn_tools = frozenset(
            tool.remote_name
            for tool in connection.report.accepted
            if tool.remote_name not in current
            or tool.spec.model_dump(exclude={"version"})
            != current[tool.remote_name].model_dump(exclude={"version"})
        )
        connection.available_resources = frozenset(
            resource.uri for resource in replacement.discovery.resources
        )
        connection.client = replacement.client
        # Every handshake is recorded with its duration, including this one (ADR-0131).
        await self._event(
            connection.session_id, "mcp.server.connected", self._connected_payload(replacement)
        )
        if connection.report.catalog_hash != replacement.report.catalog_hash:
            await self._event(
                connection.session_id,
                "mcp.catalog.changed",
                {
                    "server_id": connection.config.server_id,
                    "old_catalog_hash": connection.report.catalog_hash,
                    "new_catalog_hash": replacement.report.catalog_hash,
                },
            )

    async def _invoke(
        self,
        connection: _Connection,
        spec: ToolSpec,
        operation: Callable[[], Awaitable[MCPCallResult]],
        *,
        remote_name: str | None = None,
        resource_uri: str | None = None,
    ) -> ToolResult:
        """Hold a transport lease through connection, invocation and auth recovery."""
        async with connection.lock:
            if connection.closed or self._closing:
                return self._unavailable("tool.server_unreachable")
            if connection.unavailable_reason is not None:
                return self._unavailable(connection.unavailable_reason)
            try:
                if connection.client is None:
                    await self._reconnect(connection)
                if connection.unavailable_reason is not None:
                    return self._unavailable(connection.unavailable_reason)
                if remote_name in connection.withdrawn_tools or (
                    resource_uri is not None
                    and connection.available_resources is not None
                    and resource_uri not in connection.available_resources
                ):
                    return self._unavailable("tool.withdrawn")
                return await self._invoke_connected(connection, spec, operation)
            finally:
                connection.last_used_at = self._clock.now()
                if connection.unavailable_reason is not None:
                    await self._release_connection(connection)

    async def _invoke_connected(
        self,
        connection: _Connection,
        spec: ToolSpec,
        operation: Callable[[], Awaitable[MCPCallResult]],
    ) -> ToolResult:
        if connection.unavailable_reason is not None:
            return self._unavailable(connection.unavailable_reason)
        try:
            result = await operation()
        except MCPUnauthorizedError:
            return await self._recover_unauthorized(connection, spec, operation)
        except MCPTransportError:
            connection.unavailable_reason = "tool.server_unreachable"
            await self._event(
                connection.session_id,
                "mcp.server.disconnected",
                {
                    "server_id": connection.config.server_id,
                    "reason_code": connection.unavailable_reason,
                },
            )
            if self._non_idempotent_effect(spec):
                return self._outcome_unknown("MCP transport failed after dispatch")
            return self._unavailable(connection.unavailable_reason, retryable=True)
        gmail_code = self._gmail_failure_code(result)
        if gmail_code == "gmail.credential_rejected":
            effect_status = (
                result.structured.get("effect_status") if result.structured is not None else None
            )
            return await self._recover_unauthorized(
                connection,
                spec,
                operation,
                safe_to_retry=effect_status == "not_applied",
            )
        return self._result(result, spec)

    @staticmethod
    def _non_idempotent_effect(spec: ToolSpec) -> bool:
        return (
            spec.idempotency not in {IdempotencyClass.READ_ONLY, IdempotencyClass.IDEMPOTENT}
            and spec.side_effect is not SideEffectClass.NONE
        )

    @staticmethod
    def _outcome_unknown(detail: str) -> ToolResult:
        return ToolResult(
            ok=False,
            content=[],
            failure=ToolFailure(
                kind=ToolFailureKind.OUTCOME_UNKNOWN,
                reason_code="tool.outcome_unknown",
                detail=detail,
                retryable=False,
            ),
        )

    @staticmethod
    def _gmail_failure_code(result: MCPCallResult) -> str | None:
        if (
            result.is_error
            and len(result.content) == 1
            and result.content[0] in _GMAIL_FAILURE_CODES
        ):
            return result.content[0]
        return None

    async def _recover_unauthorized(
        self,
        connection: _Connection,
        spec: ToolSpec,
        operation: Callable[[], Awaitable[MCPCallResult]],
        *,
        safe_to_retry: bool = False,
    ) -> ToolResult:
        if connection.reauthentication_attempted:
            connection.unavailable_reason = "tool.server_unauthorized"
            if self._non_idempotent_effect(spec) and not safe_to_retry:
                return self._outcome_unknown("MCP authorization failed after dispatch")
            return self._unavailable(connection.unavailable_reason)
        connection.reauthentication_attempted = True
        try:
            credential = await self._credential(connection.config)
            changed = await self._connected_client(connection).reauthenticate(
                credential,
                self._environment(connection.config, credential),
            )
        except (MCPTransportError, MCPUnauthorizedError, PermissionError):
            connection.unavailable_reason = "tool.server_unauthorized"
            await self._event(
                connection.session_id,
                "mcp.server.reauthenticated",
                {
                    "server_id": connection.config.server_id,
                    "scheme": connection.config.auth_scheme.value,
                    "outcome": "failed",
                },
            )
            if self._non_idempotent_effect(spec) and not safe_to_retry:
                return self._outcome_unknown("MCP authorization failed after dispatch")
            return self._unavailable(connection.unavailable_reason)
        if not changed:
            connection.unavailable_reason = "tool.server_unauthorized"
            await self._event(
                connection.session_id,
                "mcp.server.reauthenticated",
                {
                    "server_id": connection.config.server_id,
                    "scheme": connection.config.auth_scheme.value,
                    "outcome": "credential_unchanged",
                },
            )
            if self._non_idempotent_effect(spec) and not safe_to_retry:
                return self._outcome_unknown("MCP authorization failed after dispatch")
            return self._unavailable(connection.unavailable_reason)
        await self._event(
            connection.session_id,
            "mcp.server.reauthenticated",
            {
                "server_id": connection.config.server_id,
                "scheme": connection.config.auth_scheme.value,
                "outcome": "credential_changed",
            },
        )
        if (
            spec.idempotency not in {IdempotencyClass.READ_ONLY, IdempotencyClass.IDEMPOTENT}
            and spec.side_effect is not SideEffectClass.NONE
            and not safe_to_retry
        ):
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.OUTCOME_UNKNOWN,
                    reason_code="tool.outcome_unknown",
                    detail="MCP authorization failed after the effect watermark",
                    retryable=False,
                ),
            )
        try:
            result = await operation()
            if self._gmail_failure_code(result) == "gmail.credential_rejected":
                connection.unavailable_reason = "tool.server_unauthorized"
                effect_status = (
                    result.structured.get("effect_status")
                    if result.structured is not None
                    else None
                )
                if self._non_idempotent_effect(spec) and effect_status != "not_applied":
                    return self._outcome_unknown("MCP authorization failed after retry dispatch")
                return self._unavailable(connection.unavailable_reason)
            return self._result(result, spec)
        except MCPUnauthorizedError:
            connection.unavailable_reason = "tool.server_unauthorized"
            if self._non_idempotent_effect(spec):
                return self._outcome_unknown("MCP authorization failed after retry dispatch")
            return self._unavailable(connection.unavailable_reason)
        except MCPTransportError:
            connection.unavailable_reason = "tool.server_unreachable"
            await self._event(
                connection.session_id,
                "mcp.server.disconnected",
                {
                    "server_id": connection.config.server_id,
                    "reason_code": connection.unavailable_reason,
                },
            )
            if self._non_idempotent_effect(spec):
                return self._outcome_unknown("MCP transport failed after retry dispatch")
            return self._unavailable(connection.unavailable_reason, retryable=True)

    @staticmethod
    def _result(result: MCPCallResult, spec: ToolSpec) -> ToolResult:
        if result.is_error:
            effect_status = (
                result.structured.get("effect_status") if result.structured is not None else None
            )
            if MCPRuntime._non_idempotent_effect(spec) and effect_status != "not_applied":
                return MCPRuntime._outcome_unknown("MCP non-idempotent call failed after dispatch")
            gmail_code = MCPRuntime._gmail_failure_code(result)
            if gmail_code == "gmail.outcome_unknown":
                return MCPRuntime._outcome_unknown("MCP server could not determine the outcome")
            if gmail_code == "gmail.arguments_invalid":
                return ToolResult(
                    ok=False,
                    content=[],
                    failure=ToolFailure(
                        kind=ToolFailureKind.INVALID_ARGUMENTS,
                        reason_code="tool.arguments_invalid",
                        detail="MCP server rejected the arguments before dispatch",
                        retryable=False,
                    ),
                )
            if gmail_code == "gmail.provider_output_invalid":
                return ToolResult(
                    ok=False,
                    content=[],
                    failure=ToolFailure(
                        kind=ToolFailureKind.OUTPUT_INVALID,
                        reason_code="tool.output_invalid",
                        detail="MCP server rejected invalid provider output",
                        retryable=False,
                    ),
                )
            if gmail_code in {
                "gmail.rate_limited",
                "gmail.provider_unavailable",
                "gmail.provider_rejected",
            }:
                return ToolResult(
                    ok=False,
                    content=[],
                    failure=ToolFailure(
                        kind=ToolFailureKind.UPSTREAM_ERROR,
                        reason_code="tool.server_error",
                        detail="MCP server reported a normalized provider failure",
                        retryable=(
                            (
                                spec.idempotency
                                in {IdempotencyClass.READ_ONLY, IdempotencyClass.IDEMPOTENT}
                                or effect_status == "not_applied"
                            )
                            and gmail_code in {"gmail.rate_limited", "gmail.provider_unavailable"}
                        ),
                    ),
                )
            external = "\n".join(result.content)
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.UPSTREAM_ERROR,
                    reason_code="tool.server_error",
                    detail="MCP server returned a tool error",
                    retryable=False,
                    external_text=external or None,
                ),
            )
        return ToolResult(
            ok=True,
            content=[TextPart(text=text) for text in result.content],
            structured=result.structured,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )

    @staticmethod
    def _unavailable(reason_code: str, *, retryable: bool = False) -> ToolResult:
        return ToolResult(
            ok=False,
            content=[],
            failure=ToolFailure(
                kind=ToolFailureKind.TRANSPORT,
                reason_code=reason_code,
                detail="MCP server unavailable",
                retryable=retryable,
            ),
        )

    @staticmethod
    def _prompt_name(server_id: str, remote_name: str) -> str:
        candidate = _SKILL_HYPHENS.sub(
            "-",
            _SKILL_CHARACTERS.sub("-", remote_name.lower()),
        ).strip("-")
        candidate = candidate or "prompt"
        base = f"mcp-{server_id.replace('_', '-')}-{candidate}"
        if len(base) <= 64:
            return base
        suffix = hashlib.sha256(remote_name.encode()).hexdigest()[:7]
        return f"{base[:56].rstrip('-')}-{suffix}"

    async def prompt_entries(
        self,
        session_id: UUID,
        principal: Principal,
    ) -> list[CatalogEntry]:
        await self.prepare(session_id, principal)
        candidates: dict[str, list[CatalogEntry]] = {}
        for server_id, connection in sorted(self._sessions.get(session_id, {}).items()):
            for prompt in connection.discovery.prompts:
                name = self._prompt_name(server_id, prompt.name)
                digest = hashlib.sha256(prompt.body.encode("utf-8")).hexdigest()
                candidates.setdefault(name, []).append(
                    CatalogEntry(
                        manifest=SkillManifest(
                            name=name,
                            version="0.0.0+mcp",
                            description=prompt.description or f"MCP prompt {prompt.name}",
                        ),
                        revision=0,
                        content_sha256=digest,
                        trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        source=SkillSource.MCP,
                        ephemeral_body=prompt.body,
                    )
                )
        return [entries[0] for name, entries in sorted(candidates.items()) if len(entries) == 1]

    async def _event(
        self,
        session_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if session_id in self._deferred_events:
            self._pending_events.setdefault(session_id, []).append((event_type, dict(payload)))
            return
        await self._persist_event(session_id, event_type, payload)

    async def _persist_event(
        self,
        session_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type=event_type,
                    actor_type="runtime",
                    payload=payload,
                )
            )

    async def activate_session(self, session_id: UUID) -> None:
        self._deferred_events.discard(session_id)
        for event_type, payload in self._pending_events.pop(session_id, []):
            await self._persist_event(session_id, event_type, payload)

    async def _release_connection(self, connection: _Connection) -> None:
        """Drop credential-bearing clients through the SDK's owned cleanup path."""
        client = connection.client
        if client is not None:
            try:
                await self._close_preparation_client(client)
            except Exception:
                logger.exception(
                    "mcp_connection_close_failed",
                    extra={
                        "session_id": str(connection.session_id),
                        "server_id": connection.config.server_id,
                    },
                )
            finally:
                connection.client = None

    async def release_session_transports(self, session_id: UUID) -> None:
        """Release idle execution resources without forgetting session pins or auth state."""
        async with self._lock(session_id):
            for connection in self._sessions.get(session_id, {}).values():
                async with connection.lock:
                    await self._release_connection(connection)

    async def sweep_idle(self) -> None:
        """Reconcile remote session closure and close unused, unleased transports."""
        for session_id, principal in list(self._principals.items()):
            if session_id in self._deferred_events or self._lock(session_id).locked():
                continue
            async with self._uow_factory() as uow:
                try:
                    session = await uow.sessions.get(session_id, principal)
                except NotFoundError:
                    session = None
            if session is None or session.status is not SessionStatus.ACTIVE:
                await self.close_session(session_id)
                continue
            for connection in list(self._sessions.get(session_id, {}).values()):
                if connection.lock.locked():
                    continue
                async with connection.lock:
                    age = (self._clock.now() - connection.last_used_at).total_seconds()
                    if age >= self._idle_timeout_seconds:
                        await self._release_connection(connection)

    async def _maintain_sessions(self) -> None:
        """Own periodic cleanup in every API and worker process, including quiet workers."""
        while True:
            # Use real scheduling; advancing a deterministic Clock must not spin a task.
            await asyncio.sleep(min(30, self._idle_timeout_seconds))
            try:
                await self.sweep_idle()
            except Exception:
                logger.exception("mcp_idle_cleanup_failed")

    def start_discovery_warmup(self, tenant_ids: Iterable[str]) -> asyncio.Task[None]:
        """Discover each enabled server once in the background, recording nothing (ADR-0131)."""
        if self._warmup_task is None or self._warmup_task.done():
            self._warmup_task = asyncio.create_task(self._warm_in_background(tuple(tenant_ids)))
        return self._warmup_task

    async def _warm_in_background(self, tenant_ids: tuple[str, ...]) -> None:
        try:
            await self.warm_discovery(tenant_ids)
        except Exception as exc:
            # The next session open discovers live; nothing waits on the warm-up.
            logger.warning("mcp_discovery_warmup_failed", extra={"error_class": type(exc).__name__})

    async def warm_discovery(self, tenant_ids: tuple[str, ...]) -> None:
        """Remember every enabled server's catalog so new sessions start none of them."""
        for tenant_id in tenant_ids:
            if self._closing:
                return
            async with self._uow_factory() as uow:
                configs = await uow.mcp_servers.list_enabled(tenant_id)
            pending = [config for config in configs if self._remembered(config) is None]
            results = await asyncio.gather(
                *(self._warm_server(config) for config in pending), return_exceptions=True
            )
            for config, result in zip(pending, results, strict=True):
                if isinstance(result, Exception):
                    logger.warning(
                        "mcp_discovery_warmup_failed",
                        extra={"server_id": config.server_id, "error_class": type(result).__name__},
                    )
                elif isinstance(result, BaseException):
                    raise result

    async def _warm_server(self, config: MCPServerConfig) -> None:
        discovered = await self._discover_server(config)
        if isinstance(discovered, _DisconnectedServer):
            logger.warning(
                "mcp_discovery_warmup_failed",
                extra={"server_id": config.server_id, "reason_code": discovered.reason_code},
            )
            return
        if discovered.client is not None:
            await self._close_preparation_client(discovered.client)

    async def close_session(self, session_id: UUID) -> None:
        """Forget a closed session only after its active calls release their leases."""
        async with self._lock(session_id):
            connections = self._sessions.get(session_id, {})
            for connection in connections.values():
                connection.closed = True
            for connection in connections.values():
                async with connection.lock:
                    await self._release_connection(connection)
            # Cancellation while draining must leave ownership available for another
            # sweep or shutdown attempt, including clients waiting on active calls.
            self._sessions.pop(session_id, None)
            self._prepared.discard(session_id)
            self._attempted.pop(session_id, None)
            self._principals.pop(session_id, None)
            self._deferred_events.discard(session_id)
            self._pending_events.pop(session_id, None)
            self._unregister_session(session_id)

    async def close(self) -> None:
        """Stop the sweeper before draining this process's connections."""
        self._closing = True
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
            self._warmup_task = None
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None
        # Include preparation locks so shutdown waits for unpublished clients to unwind.
        for session_id in set(self._sessions) | set(self._locks):
            await self.close_session(session_id)
        await self._drain_preparation_cleanups()
