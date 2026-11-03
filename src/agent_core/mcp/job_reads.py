"""A saved job's read-only MCP calls (ADR-0291).

A job has no model to judge an approval, so this offers only what needs none. It
does not park: a tool that is not plainly a read, or that policy would not simply
allow, is refused.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import MCPUnavailableError, NotFoundError, ToolValidationError
from agent_core.domain.jobs import JobBrokerError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolSource, ToolSpec
from agent_core.mcp.runtime import MCPRuntime, MCPTool
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.policies import PolicyEngine
from agent_core.ports.tools import ToolRegistry
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.validation import validate_and_normalize

_READS = frozenset(
    {SideEffectClass.NONE, SideEffectClass.WORKSPACE_READ, SideEffectClass.NETWORK_READ}
)


def _refused() -> JobBrokerError:
    return JobBrokerError("job.mcp_refused")


def _plainly_reads(
    side_effect: SideEffectClass, risk: RiskLevel, idempotency: IdempotencyClass
) -> bool:
    return (
        side_effect in _READS
        and risk is RiskLevel.LOW
        and idempotency is IdempotencyClass.READ_ONLY
    )


class JobMCPReads:
    def __init__(
        self,
        *,
        runtime: MCPRuntime,
        registry: ToolRegistry,
        policy: PolicyEngine,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._runtime = runtime
        self._registry = registry
        self._policy = policy
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids

    async def read(
        self,
        *,
        principal: Principal,
        run_id: UUID,
        tool: str,
        arguments: dict[str, Any],
        maximum_response_bytes: int,
        call_id: str,
    ) -> dict[str, Any]:
        """Run one tool the broker's grant allows, or raise a content-free failure."""
        del call_id
        server_id = tool.split(".", 2)[1]
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, principal)
            row = await uow.mcp_servers.get(principal.tenant_id, server_id)
        # Checked before preparing, so a refused server is never started.
        if (
            row is None
            or not row.enabled
            or not row.required_scopes <= principal.scopes
            or not _plainly_reads(row.side_effect, row.risk, row.idempotency)
        ):
            raise _refused()
        try:
            await self._runtime.prepare(run.session_id, principal, frozenset({server_id}))
            registered = self._registry.get(
                tool, tenant_id=principal.tenant_id, principal_id=principal.principal_id
            )
        except NotFoundError:
            raise _refused() from None
        except MCPUnavailableError:
            raise JobBrokerError("job.service_unavailable", retryable=True) from None
        implementation = (
            registered.implementation if isinstance(registered, RegisteredTool) else registered
        )
        spec = registered.spec
        if (
            not isinstance(implementation, MCPTool)
            or spec.source is not ToolSource.MCP
            or spec.server_id != server_id
            or spec.deprecated
            or not spec.model_visible
            or not spec.required_scopes <= principal.scopes
            or not _plainly_reads(spec.side_effect, spec.risk, spec.idempotency)
        ):
            raise _refused()
        try:
            normalized, _encoded, digest = validate_and_normalize(arguments, spec.input_schema)
        except ToolValidationError:
            raise JobBrokerError("job.invalid_arguments") from None
        decision = await self._policy.evaluate(
            self._action(run.session_id, run_id, principal, spec, normalized, digest),
            principal,
            run,
        )
        if decision.decision is not PolicyDecisionType.ALLOW:
            raise _refused()
        try:
            result = await self._runtime.read_for_job(
                run.session_id, principal.tenant_id, implementation, normalized
            )
        except MCPUnavailableError:
            raise JobBrokerError("job.service_unavailable", retryable=True) from None
        if not result.ok:
            retryable = result.failure is not None and result.failure.retryable
            raise JobBrokerError("job.mcp_failed", retryable=retryable)
        body = {
            "content": [part.text for part in result.content if isinstance(part, TextPart)],
            "structured": result.structured,
        }
        if len(json.dumps(body, default=str).encode()) > maximum_response_bytes:
            raise JobBrokerError("job.response_limit")
        return body

    def _action(
        self,
        session_id: UUID,
        run_id: UUID,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict[str, Any],
        digest: str,
    ) -> ProposedAction:
        """The action policy sees. The approved code and grant are what asks for this read."""
        return ProposedAction(
            kind=ActionKind.TOOL_CALL,
            action_id=self._ids.new_id(),
            tenant_id=principal.tenant_id,
            session_id=session_id,
            run_id=run_id,
            step_number=0,
            name=spec.name,
            version=spec.version,
            summary=f"A saved job reads {spec.name}.",
            side_effect=spec.side_effect,
            risk=spec.risk,
            idempotency=spec.idempotency,
            required_scopes=set(spec.required_scopes),
            arguments=arguments,
            normalized_arguments_hash=digest,
            origin_trust=TrustLevel.INTERNAL_TOOL,
            target=ExecutionTarget(
                kind=spec.target_kind,
                isolated=False,
                network_enabled=True,
                server_id=spec.server_id,
            ),
            evaluated_at=self._clock.now(),
        )
