"""Milestone 34 owner records gates."""

from dataclasses import replace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from agent_core.application.records import RecordsService
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.policies import (
    ActionKind,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ExecutionTarget, ToolFailureKind, ToolSpec
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.records import Order
from agent_core.tools.messages import TOOL_MESSAGES
from agent_core.tools.records import (
    RecordsAppendTool,
    RecordsCreateSeriesTool,
    RecordsDeleteTool,
    RecordsListTool,
    RecordsQueryTool,
)
from agent_core.tools.registry import BUILTIN_DOMAINS, validate_registration
from tests.contract.support import (
    NOW,
    RUN_ID,
    SESSION_ID,
    memory_uow_factory,
    principal,
    run,
    tool_context,
)

TOOL_NAMES = (
    "records.list",
    "records.create_series",
    "records.append",
    "records.query",
    "records.delete",
)
REASON_CODES = (
    "series_unknown",
    "series_exists",
    "component_mismatch",
    "value_out_of_range",
    "observed_in_future",
    "series_limit",
    "entry_limit",
    "window_too_large",
    "secret_detected",
    "name_rejected",
)


def _environment(flag: str | None) -> dict[str, str]:
    values = {
        "DATABASE_URL": "postgresql+asyncpg://localhost/records-m34",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "SANDBOX_MECHANISM": "fake",
    }
    if flag is not None:
        values["AGENT_RECORDS_ENABLED"] = flag
    return values


async def _registered(flag: str | None) -> set[str]:
    async with build(
        settings=load_settings(_environment(flag)), script=FakeModelScript(turns=[])
    ) as composition:
        found: set[str] = set()
        for name in TOOL_NAMES:
            try:
                composition.tool_pipeline._registry.get(name)
            except NotFoundError:
                continue
            found.add(name)
        return found


async def test_registration_under_flag() -> None:
    clock, factory = await memory_uow_factory()
    service = RecordsService(factory, clock)
    zone = ZoneInfo("UTC")
    tools = [
        RecordsListTool(service, zone),
        RecordsCreateSeriesTool(service, zone),
        RecordsAppendTool(service, zone),
        RecordsQueryTool(service, zone),
        RecordsDeleteTool(service, zone),
    ]
    assert [tool.spec.name for tool in tools] == list(TOOL_NAMES)
    for tool in tools:
        assert validate_registration(tool.spec) is tool.spec
    assert "records" in BUILTIN_DOMAINS
    assert {"records.read", "records.write"} <= PLATFORM_SCOPES
    for code in REASON_CODES:
        assert f"records.{code}" in TOOL_MESSAGES
    assert await _registered(None) == set()
    assert await _registered("0") == set()
    assert await _registered("1") == set(TOOL_NAMES)


@pytest.mark.parametrize("value", ["1", "0"])
def test_flag_parses(value: str) -> None:
    assert load_settings(_environment(value)).records_enabled is (value == "1")


BP = [{"name": "systolic", "unit": "mmHg"}, {"name": "diastolic", "unit": "mmHg"}]


async def _seeded(origin: TrustLevel) -> tuple[Any, Any, Any]:
    clock, factory = await memory_uow_factory()
    service = RecordsService(factory, clock)
    zone = ZoneInfo("UTC")
    owner = principal().model_copy(update={"scopes": {"records.read", "records.write"}})
    context = replace(tool_context(), principal=owner, origin_trust=origin)
    created = await RecordsCreateSeriesTool(service, zone).execute(
        {"series": "Blood pressure", "components": BP}, context
    )
    assert created.ok, created.failure
    return service, factory, context


async def _entry_and_event_counts(
    service: RecordsService, factory: Any, context: Any
) -> tuple[int, int]:
    result = await service.query(
        context.principal,
        series_name="Blood pressure",
        since=None,
        until=None,
        limit=10,
        order=Order.DESC,
        bucket=None,
        zone=ZoneInfo("UTC"),
    )
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, context.principal)
    appended = [e for e in events if e.event_type == "records.entry.appended"]
    return len(result.entries), len(appended)


async def test_untrusted_turn_append_is_refused_and_writes_nothing() -> None:
    service, factory, context = await _seeded(TrustLevel.EXTERNAL_UNTRUSTED)
    result = await RecordsAppendTool(service, ZoneInfo("UTC")).execute(
        {"series": "Blood pressure", "values": {"systolic": 120, "diastolic": 80}}, context
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "records.untrusted_turn"
    assert result.failure.kind is ToolFailureKind.PERMISSION
    assert result.failure.retryable is False
    assert await _entry_and_event_counts(service, factory, context) == (0, 0)


@pytest.mark.parametrize("origin", [TrustLevel.MEMORY, TrustLevel.USER, TrustLevel.INTERNAL_TOOL])
async def test_memory_turn_append_is_allowed(origin: TrustLevel) -> None:
    service, factory, context = await _seeded(origin)
    result = await RecordsAppendTool(service, ZoneInfo("UTC")).execute(
        {"series": "Blood pressure", "values": {"systolic": 120, "diastolic": 80}}, context
    )
    assert result.ok, result.failure
    assert await _entry_and_event_counts(service, factory, context) == (1, 1)


def _proposed(spec: ToolSpec, origin: TrustLevel) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=uuid4(),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=spec.name,
        version=spec.version,
        summary="Evaluate a records action.",
        side_effect=spec.side_effect,
        risk=spec.risk,
        idempotency=spec.idempotency,
        required_scopes=set(spec.required_scopes),
        arguments={"series": "Blood pressure"},
        normalized_arguments_hash="hash",
        argument_trust={"series": origin},
        origin_trust=origin,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        evaluated_at=NOW,
    )


async def test_create_and_delete_require_approval_by_matrix() -> None:
    engine = DeterministicPolicyEngine(DEFAULT_RULESET)
    assert RecordsCreateSeriesTool.spec.side_effect is SideEffectClass.EXTERNAL_WRITE
    assert RecordsDeleteTool.spec.side_effect is SideEffectClass.EXTERNAL_DELETE
    for spec in (RecordsCreateSeriesTool.spec, RecordsDeleteTool.spec):
        assert spec.risk is RiskLevel.MEDIUM
        assert spec.idempotency is IdempotencyClass.IDEMPOTENT
        assert spec.required_scopes == {"records.write"}
        decision = await engine.evaluate(_proposed(spec, TrustLevel.USER), principal(), run())
        assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
    for origin in (TrustLevel.USER, TrustLevel.MEMORY):
        decision = await engine.evaluate(
            _proposed(RecordsAppendTool.spec, origin), principal(), run()
        )
        assert decision.decision is PolicyDecisionType.ALLOW


async def test_approved_create_series_executes_once() -> None:
    arguments = {"series": "Blood pressure", "components": BP}
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="records.create_series", arguments=arguments)]
            ),
            ScriptedTurn(text="Series created."),
        ]
    )
    async with build(
        settings=load_settings(_environment("1")),
        script=script,
        enabled_tools=["records.create_series"],
    ) as composition:
        run_id = await composition.runs.submit("Track my blood pressure.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        assert approval.tool_name == "records.create_series"
        assert approval.arguments == arguments
        assert "records.write" in approval.required_scopes
        assert await _stored_series(composition) == []
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await composition.runs.wait_terminal(run_id)
        assert completed.status is RunStatus.COMPLETED, completed.failure
        assert len(await _stored_series(composition)) == 1
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(completed.session_id, 0, composition.principal)
        assert [e.event_type for e in events].count("records.series.created") == 1


async def _stored_series(composition: Any) -> list[Any]:
    async with composition.uow_factory() as uow:
        return list(await uow.records.list_series(composition.principal, Sensitivity.RESTRICTED))
