"""Milestone 24: capability-derived registration and the device.sms.send tool.

Covers the registration lifecycle a declared capability drives, the terminal
statuses the compose sheet can produce, the offline outcome an unreachable
phone surfaces, the execution target the policy input carries, and the
default-off refusal that keeps the whole surface dark behind its two flags.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.device_channel import FakeDeviceChannel
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import Composition, build
from agent_core.config import ConfigurationError, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.devices import DeviceCapability, DeviceInvocation, DeviceInvocationStatus
from agent_core.domain.errors import (
    DeviceChannelUnavailable,
    NotFoundError,
    ToolValidationError,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
)
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailureKind,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.ports.policies import PolicyEngine
from agent_core.tools.device_tools import (
    DEVICE_SMS_SEND_TOOL_NAME,
    DeviceSmsSendTool,
    DeviceToolRuntime,
    device_sms_send_spec,
)
from agent_core.tools.registry import StaticToolRegistry, validate_registration
from tests.contract.support import (
    NOW,
    RUN_ID,
    SESSION_ID,
    SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    agent,
    memory_uow_factory,
    principal,
    tool_context,
)
from tests.contract.support import run as contract_run
from tests.contract.test_device_channel_contract import DEVICE_ID
from tests.contract.test_device_registry_contract import device
from tests.integration.m2_support import memory_settings

SECOND_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002b0")
OTHER_SESSION_ID = UUID("00000000-0000-0000-0000-0000000002b2")
INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002b1")
TOOL_NAME = DeviceCapability.SMS_SEND.value
ARGUMENTS: dict[str, Any] = {"recipient": "+15555550123", "body": "Feeding Marzipan at six."}
CREDENTIAL_BODY = "api_key=sk-live-9f2c4a"  # noqa: S105 - hardline near-miss fixture


def _device_principal() -> Principal:
    return principal().model_copy(update={"scopes": {"device.write"}}, deep=True)


def _clock() -> FixedClock:
    return FixedClock(NOW)


async def _runtime_stack(
    *,
    capabilities: frozenset[str] = frozenset({TOOL_NAME}),
    channel: FakeDeviceChannel | None = None,
) -> tuple[MemoryUnitOfWorkFactory, StaticToolRegistry, DeviceToolRuntime]:
    _fixed, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.devices.upsert(
            device(device_id=DEVICE_ID, capabilities=capabilities),
            _device_principal(),
        )
    registry = StaticToolRegistry()
    clock = _clock()
    runtime = DeviceToolRuntime(
        factory,
        registry,
        channel or FakeDeviceChannel(clock=clock, capabilities={DEVICE_ID: capabilities}),
        clock,
        SequenceIdFactory(),
        invocation_timeout_seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    )
    return factory, registry, runtime


def _advertised(registry: StaticToolRegistry) -> list[ToolSpec]:
    return registry.specs_for_session(
        agent(),
        _device_principal(),
        profile="default",
        environment="runtime",
    )


# --- (a) capability-derived registration honours the device lifecycle -------


async def test_a_declared_capability_registers_the_device_tool_for_the_session() -> None:
    _factory, registry, runtime = await _runtime_stack()

    await runtime.prepare(SESSION_ID, _device_principal())

    [spec] = _advertised(registry)
    assert spec.name == DEVICE_SMS_SEND_TOOL_NAME
    assert spec.source is ToolSource.DEVICE
    assert spec.target_kind == "device"
    assert spec.device_id == str(DEVICE_ID)
    assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert spec.required_scopes == {"device.write"}
    assert spec.timeout_seconds == SHIPPED_INVOCATION_TIMEOUT_SECONDS + 15


async def test_revoking_the_declaring_device_withdraws_the_tool_at_the_next_attach() -> None:
    factory, registry, runtime = await _runtime_stack()
    await runtime.prepare(SESSION_ID, _device_principal())
    assert _advertised(registry)

    async with factory() as uow:
        await uow.devices.revoke(DEVICE_ID, _device_principal(), NOW)
    await runtime.prepare(SESSION_ID, _device_principal())

    assert _advertised(registry) == []


async def test_a_device_without_the_capability_registers_nothing() -> None:
    _factory, registry, runtime = await _runtime_stack(capabilities=frozenset())

    await runtime.prepare(SESSION_ID, _device_principal())

    assert _advertised(registry) == []


async def test_closing_the_last_owning_session_unregisters_the_device_tool() -> None:
    _factory, registry, runtime = await _runtime_stack()
    await runtime.prepare(SESSION_ID, _device_principal())

    await runtime.close_session(SESSION_ID)

    assert _advertised(registry) == []


async def test_another_principals_attach_leaves_the_owners_registration_standing() -> None:
    """Registrations are principal-scoped: a co-tenant's empty read withdraws nothing.

    The device read is scoped to one principal, so a tenant-scoped registration
    key would let any other principal in the tenant reconcile the owner's tool
    out of the registry.
    """

    _factory, registry, runtime = await _runtime_stack()
    await runtime.prepare(SESSION_ID, _device_principal())
    other = _device_principal().model_copy(update={"principal_id": "principal-b"}, deep=True)

    await runtime.prepare(OTHER_SESSION_ID, other)

    assert [spec.device_id for spec in _advertised(registry)] == [str(DEVICE_ID)]
    assert registry.get(
        DEVICE_SMS_SEND_TOOL_NAME,
        tenant_id=_device_principal().tenant_id,
    ).spec.device_id == str(DEVICE_ID)


async def test_a_second_declaring_device_is_refused_with_a_process_event() -> None:
    factory, registry, runtime = await _runtime_stack()
    async with factory() as uow:
        await uow.devices.upsert(
            device(
                device_id=SECOND_DEVICE_ID,
                client_device_id="client-device-b",
                capabilities=frozenset({TOOL_NAME}),
            ),
            _device_principal(),
        )

    await runtime.prepare(SESSION_ID, _device_principal())

    [spec] = _advertised(registry)
    assert spec.device_id in {str(DEVICE_ID), str(SECOND_DEVICE_ID)}
    async with factory() as uow:
        events = await uow.process_events.list()
    assert [event.event_type for event in events] == ["device.tool.registration_conflict"]
    assert "recipient" not in str(events[0].payload)


# --- registration validation ------------------------------------------------


async def test_device_discovery_may_register_dynamically_but_other_sources_may_not() -> None:
    registry = StaticToolRegistry()
    tool = DeviceSmsSendTool(
        FakeDeviceChannel(clock=_clock(), capabilities={DEVICE_ID: frozenset({TOOL_NAME})}),
        DEVICE_ID,
        SequenceIdFactory(),
        invocation_timeout_seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    )

    registry.register_dynamic(tool, tenant_id=principal().tenant_id)

    sandboxed = tool.spec.model_copy(update={"source": ToolSource.SANDBOX, "name": "device.other"})
    with pytest.raises(ToolValidationError):
        registry.register_dynamic(_Stub(sandboxed), tenant_id=principal().tenant_id)


async def test_a_device_tool_requires_a_device_target_and_a_device_identifier() -> None:
    spec = device_sms_send_spec(
        DEVICE_ID,
        invocation_timeout_seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    )

    with pytest.raises(ToolValidationError):
        validate_registration(spec.model_copy(update={"device_id": None}))
    with pytest.raises(ToolValidationError):
        validate_registration(spec.model_copy(update={"target_kind": "in_process"}))


class _Stub:
    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:  # pragma: no cover - registration is refused before execution
        raise NotImplementedError


# --- terminal statuses ------------------------------------------------------


def _tool(*, status: DeviceInvocationStatus) -> tuple[DeviceSmsSendTool, FakeDeviceChannel]:
    channel = FakeDeviceChannel(
        clock=_clock(),
        capabilities={DEVICE_ID: frozenset({TOOL_NAME})},
        default_status=status,
    )
    return (
        DeviceSmsSendTool(
            channel,
            DEVICE_ID,
            SequenceIdFactory(),
            invocation_timeout_seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS,
        ),
        channel,
    )


async def test_a_sent_message_returns_a_content_free_confirmation() -> None:
    tool, channel = _tool(status=DeviceInvocationStatus.SENT)

    result = await tool.execute(dict(ARGUMENTS), tool_context())

    assert result.ok is True
    assert result.structured is not None
    assert result.structured["status"] == "sent"
    assert result.structured["invocation_id"] == str(channel.invocations[0].id)
    rendered = result.model_dump_json()
    assert ARGUMENTS["recipient"] not in rendered
    assert ARGUMENTS["body"] not in rendered


async def test_a_cancelled_message_is_the_owners_choice_not_an_error() -> None:
    tool, _channel = _tool(status=DeviceInvocationStatus.CANCELLED)

    result = await tool.execute(dict(ARGUMENTS), tool_context())

    assert result.ok is True
    assert result.structured == {"status": "cancelled"}


async def test_a_failed_send_is_an_execution_failure() -> None:
    tool, _channel = _tool(status=DeviceInvocationStatus.FAILED)

    result = await tool.execute(dict(ARGUMENTS), tool_context())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.UPSTREAM_ERROR


# --- (d) the offline outcome ------------------------------------------------


async def test_an_expired_invocation_reports_the_device_offline() -> None:
    tool, _channel = _tool(status=DeviceInvocationStatus.EXPIRED)

    result = await tool.execute(dict(ARGUMENTS), tool_context())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.TRANSPORT
    assert result.failure.reason_code == "tool.device_offline"


@pytest.mark.parametrize(
    "reason",
    [
        "device.not_found",
        "device.revoked",
        "device.capability_absent",
        "device.invocation_not_owned",
    ],
)
async def test_every_unavailable_reason_reports_the_device_offline(reason: str) -> None:
    tool = DeviceSmsSendTool(
        _RaisingChannel(DeviceChannelUnavailable(reason, "unavailable")),
        DEVICE_ID,
        SequenceIdFactory(),
        invocation_timeout_seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    )

    result = await tool.execute(dict(ARGUMENTS), tool_context())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.TRANSPORT
    assert result.failure.reason_code == "tool.device_offline"


async def test_an_invocation_row_that_vanishes_reports_the_device_offline() -> None:
    tool = DeviceSmsSendTool(
        _RaisingChannel(NotFoundError("device invocation not found")),
        DEVICE_ID,
        SequenceIdFactory(),
        invocation_timeout_seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    )

    result = await tool.execute(dict(ARGUMENTS), tool_context())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.TRANSPORT
    assert result.failure.reason_code == "tool.device_offline"


class _RaisingChannel:
    """A channel whose every invocation raises the error under test."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def invoke(
        self,
        *,
        device_id: UUID,
        run_id: UUID,
        invocation_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        principal: Principal,
    ) -> DeviceInvocation:
        del device_id, run_id, invocation_id, tool_name, arguments, principal
        raise self._error


# --- (b), (c), (e) through the composed pipeline ----------------------------


def _device_settings() -> Settings:
    return replace(
        memory_settings(),
        device_channel_enabled=True,
        device_sms_enabled=True,
    )


class _RecordingPolicy:
    """Capture the policy input the pipeline builds, then defer to the real engine."""

    def __init__(self, inner: PolicyEngine) -> None:
        self._inner = inner
        self.actions: list[ProposedAction] = []

    async def evaluate(
        self, action: ProposedAction, principal_: Principal, run: Run
    ) -> PolicyDecision:
        self.actions.append(action)
        return await self._inner.evaluate(action, principal_, run)


async def _seed_device(composition: Composition) -> None:
    async with composition.uow_factory() as uow:
        await uow.devices.upsert(
            device(device_id=DEVICE_ID, capabilities=frozenset({TOOL_NAME})).model_copy(
                update={
                    "tenant_id": composition.principal.tenant_id,
                    "principal_id": composition.principal.principal_id,
                }
            ),
            composition.principal,
        )


def _script(body: str) -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name=DEVICE_SMS_SEND_TOOL_NAME,
                        arguments={"recipient": ARGUMENTS["recipient"], "body": body},
                        call_id="send-sms",
                    )
                ]
            ),
            ScriptedTurn(text="Done."),
        ]
    )


async def test_the_execution_target_names_the_declaring_device() -> None:
    channel = FakeDeviceChannel(
        clock=_clock(),
        capabilities={DEVICE_ID: frozenset({TOOL_NAME})},
    )
    async with build(
        settings=_device_settings(),
        script=_script(str(ARGUMENTS["body"])),
        fixed_clock_at=NOW,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        recording = _RecordingPolicy(composition.tool_pipeline._policy)
        composition.tool_pipeline._policy = recording
        run_id = await composition.runs.submit("Text the sitter.")
        completed = await composition.runs.wait_terminal(run_id)

    assert completed.final_message == "Done."
    [action] = [item for item in recording.actions if item.name == DEVICE_SMS_SEND_TOOL_NAME]
    assert action.target.kind == "device"
    assert action.target.device_id == str(DEVICE_ID)
    assert len(channel.invocations) == 1


async def test_a_credential_shaped_body_is_denied_before_the_tool_rule_is_consulted() -> None:
    """The hardline runs first, so the owner-confirmed entry cannot carry a secret out.

    The denial lands in the pipeline before any execution, so no invocation row
    and no push ever exist for the refused call.
    """

    engine = DeterministicPolicyEngine(DEFAULT_RULESET)

    denied = await engine.evaluate(
        _device_action(body=CREDENTIAL_BODY), principal(), contract_run()
    )
    allowed = await engine.evaluate(
        _device_action(body=str(ARGUMENTS["body"])), principal(), contract_run()
    )

    assert denied.decision is PolicyDecisionType.DENY
    assert denied.reason_code == "policy.hardline.secret_exfiltration"
    assert allowed.decision is PolicyDecisionType.ALLOW


async def test_the_device_tool_is_absent_while_either_flag_is_unset() -> None:
    # Milestone 24 pairs the two flags at configuration time, so a half-enabled
    # deployment never composes at all; the tool cannot exist in a graph that
    # was refused before it was built.
    for half_enabled in (
        replace(memory_settings(), device_channel_enabled=True),
        replace(memory_settings(), device_sms_enabled=True),
    ):
        with pytest.raises(ConfigurationError, match="device channel and SMS"):
            async with build(
                settings=half_enabled,
                script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
                fixed_clock_at=NOW,
                sequential_ids=True,
            ):
                pass

    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        run_id = await composition.runs.submit("ready?")
        await composition.runs.wait_terminal(run_id)

        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get(
                DEVICE_SMS_SEND_TOOL_NAME,
                tenant_id=composition.principal.tenant_id,
            )


async def test_an_unreachable_device_surfaces_the_offline_outcome_to_the_model() -> None:
    channel = FakeDeviceChannel(
        clock=_clock(),
        capabilities={DEVICE_ID: frozenset({TOOL_NAME})},
        default_status=DeviceInvocationStatus.EXPIRED,
    )
    async with build(
        settings=_device_settings(),
        script=_script(str(ARGUMENTS["body"])),
        fixed_clock_at=NOW,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        run_id = await composition.runs.submit("Text the sitter.")
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(completed.id, composition.principal)

    [invocation] = [item for item in invocations if item.tool_name == DEVICE_SMS_SEND_TOOL_NAME]
    assert invocation.outcome is not None
    assert invocation.outcome.reason_code == "tool.device_offline"
    assert invocation.outcome.status.value == "unavailable"


# --- the policy stance ------------------------------------------------------


def _device_action(
    *,
    body: str,
    origin_trust: TrustLevel = TrustLevel.USER,
) -> ProposedAction:
    """Build the policy input the pipeline constructs for one device send."""

    arguments = {"recipient": ARGUMENTS["recipient"], "body": body}
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=INVOCATION_ID,
        tenant_id=principal().tenant_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=DEVICE_SMS_SEND_TOOL_NAME,
        version="1.0.0",
        summary="Compose a text on the paired device.",
        side_effect=SideEffectClass.EXTERNAL_MESSAGE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        arguments=arguments,
        normalized_arguments_hash="hash",
        # Model-authored arguments, exactly as the pipeline classifies them.
        argument_trust=dict.fromkeys(arguments, TrustLevel.EXTERNAL_UNTRUSTED),
        origin_trust=origin_trust,
        target=ExecutionTarget(
            kind="device",
            isolated=False,
            network_enabled=False,
            device_id=str(DEVICE_ID),
        ),
        evaluated_at=NOW,
    )


async def test_the_owner_confirmed_device_send_is_allowed_without_a_second_approval() -> None:
    channel = FakeDeviceChannel(
        clock=_clock(),
        capabilities={DEVICE_ID: frozenset({TOOL_NAME})},
    )
    async with build(
        settings=_device_settings(),
        script=_script(str(ARGUMENTS["body"])),
        fixed_clock_at=NOW,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        recording = _RecordingPolicy(composition.tool_pipeline._policy)
        composition.tool_pipeline._policy = recording
        run_id = await composition.runs.submit("Text the sitter.")
        await composition.runs.wait_terminal(run_id)
        pending = await composition.approvals.list_pending(run_id=run_id)
        decision = await composition.tool_pipeline._policy.evaluate(
            next(item for item in recording.actions if item.name == DEVICE_SMS_SEND_TOOL_NAME),
            composition.principal,
            await composition.runs.get(run_id),
        )

    assert pending == []
    assert decision.decision is PolicyDecisionType.ALLOW


def test_the_external_message_matrix_row_still_requires_approval() -> None:
    [rule] = [
        item
        for item in DEFAULT_RULESET.rules
        if item.side_effect is SideEffectClass.EXTERNAL_MESSAGE
    ]

    assert rule.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert rule.condition is None


async def test_another_external_message_tool_still_requires_approval() -> None:
    action = _device_action(body=str(ARGUMENTS["body"])).model_copy(
        update={"name": "demo.external_write"},
        deep=True,
    )

    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        action,
        principal(),
        contract_run(),
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert decision.reason_code == "policy.matrix.external_message"


def test_only_the_device_send_carries_an_owner_confirmed_tool_rule() -> None:
    assert [rule.tool_name for rule in DEFAULT_RULESET.tool_rules] == [DEVICE_SMS_SEND_TOOL_NAME]
    assert DEFAULT_RULESET.tool_rules[0].decision is PolicyDecisionType.ALLOW
    assert DEFAULT_RULESET.tool_rules[0].human_confirms_arguments is True


@pytest.mark.parametrize(
    "origin",
    [TrustLevel.EXTERNAL_UNTRUSTED, TrustLevel.MEMORY, TrustLevel.KNOWLEDGE],
)
async def test_a_non_authorizing_origin_still_requires_approval_for_the_device_send(
    origin: TrustLevel,
) -> None:
    """Follow the trust table's May-authorize column, not one label's identity."""

    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        _device_action(body=str(ARGUMENTS["body"]), origin_trust=origin),
        principal(),
        contract_run(),
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


async def test_an_untrusted_turn_still_requires_approval_for_the_device_send() -> None:
    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
        _device_action(
            body=str(ARGUMENTS["body"]),
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        ),
        principal(),
        contract_run(),
    )

    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
