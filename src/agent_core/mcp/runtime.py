"""Session-scoped MCP discovery, registration, availability, and bounded recovery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

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
logger = logging.getLogger(__name__)

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
    client: MCPClient
    discovery: MCPDiscovery
    report: MCPMappingReport
    reauthentication_attempted: bool = False
    unavailable_reason: str | None = None


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
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._clients = clients
        self._credentials = credentials
        self._clock = clock
        self._ids = ids
        self._connect_timeout_seconds = connect_timeout_seconds
        self._sessions: dict[UUID, dict[str, _Connection]] = {}
        self._prepared: set[UUID] = set()
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._session_registrations: dict[UUID, set[_RegistrationKey]] = {}
        self._registration_owners: dict[_RegistrationKey, set[UUID]] = {}
        self._deferred_events: set[UUID] = set()
        self._pending_events: dict[UUID, list[tuple[str, dict[str, Any]]]] = {}

    def _lock(self, session_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

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

    def _unregister_session(self, session_id: UUID) -> None:
        for key in self._session_registrations.pop(session_id, set()):
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

    async def prepare(self, session_id: UUID, principal: Principal) -> None:
        if session_id in self._prepared:
            return
        async with self._lock(session_id):
            if session_id in self._prepared:
                return
            async with self._uow_factory() as uow:
                configs = await uow.mcp_servers.list_enabled(principal.tenant_id)
                try:
                    await uow.sessions.get(session_id, principal)
                except NotFoundError:
                    self._deferred_events.add(session_id)
            connections: dict[str, _Connection] = {}
            try:
                for config in configs:
                    client: MCPClient | None = None
                    entered: MCPClient | None = None
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
                        target = entered or client
                        if target is not None:
                            with suppress(Exception):
                                await target.__aexit__(None, None, None)
                        await self._event(
                            session_id,
                            "mcp.server.disconnected",
                            {"server_id": config.server_id, "reason_code": "tool.auth_failed"},
                        )
                        continue
                    except (MCPTransportError, TimeoutError):
                        target = entered or client
                        if target is not None:
                            with suppress(Exception):
                                await target.__aexit__(None, None, None)
                        await self._event(
                            session_id,
                            "mcp.server.disconnected",
                            {
                                "server_id": config.server_id,
                                "reason_code": "tool.server_unreachable",
                            },
                        )
                        continue
                    except BaseException:
                        target = entered or client
                        if target is not None:
                            with suppress(BaseException):
                                await target.__aexit__(None, None, None)
                        raise
                    try:
                        report = map_discovered_tools(config, discovery.tools)
                    except BaseException:
                        with suppress(BaseException):
                            await entered.__aexit__(None, None, None)
                        raise
                    if discovery.resources:
                        resource_name = f"mcp.{config.server_id}.read_resource"
                        shadowed = tuple(
                            mapped
                            for mapped in report.accepted
                            if mapped.spec.name == resource_name
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
                    connection = _Connection(
                        session_id=session_id,
                        config=config,
                        client=entered,
                        discovery=discovery,
                        report=report,
                    )
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
                    await self._event(
                        session_id,
                        "mcp.server.connected",
                        {
                            "server_id": config.server_id,
                            "catalog_hash": report.catalog_hash,
                            "tool_count": len(report.accepted),
                            "rejected": list(report.rejected),
                            "conflicts": [list(group) for group in report.conflicts],
                        },
                    )
            except BaseException:
                for connection in connections.values():
                    with suppress(BaseException):
                        await connection.client.__aexit__(None, None, None)
                self._unregister_session(session_id)
                raise
            self._sessions[session_id] = connections
            self._prepared.add(session_id)

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
        return await self._invoke(
            connection,
            spec,
            lambda: connection.client.call_tool(remote_name, arguments),
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
            lambda: connection.client.read_resource(uri),
        )

    async def _invoke(
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
            spec.idempotency is IdempotencyClass.NON_IDEMPOTENT
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
            return self._unavailable(connection.unavailable_reason)
        connection.reauthentication_attempted = True
        try:
            credential = await self._credential(connection.config)
            changed = await connection.client.reauthenticate(
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
                                spec.idempotency is IdempotencyClass.READ_ONLY
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

    async def close_session(self, session_id: UUID) -> None:
        connections = self._sessions.pop(session_id, {})
        self._prepared.discard(session_id)
        self._locks.pop(session_id, None)
        self._deferred_events.discard(session_id)
        self._pending_events.pop(session_id, None)
        try:
            for connection in connections.values():
                try:
                    await connection.client.__aexit__(None, None, None)
                except BaseException:
                    logger.exception(
                        "mcp_connection_close_failed",
                        extra={
                            "session_id": str(session_id),
                            "server_id": connection.config.server_id,
                        },
                    )
        finally:
            self._unregister_session(session_id)

    async def close(self) -> None:
        for session_id in list(self._sessions):
            try:
                await self.close_session(session_id)
            except BaseException:
                logger.exception("mcp_session_close_failed", extra={"session_id": str(session_id)})
