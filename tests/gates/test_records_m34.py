"""Milestone 34 owner records gates."""

import hashlib
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.application.records import RecordsCatalog, RecordsService
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.estimator import ConservativeTokenEstimator, canonical_json_bytes
from agent_core.context.records_catalog import RecordsCatalogSource, render_records_catalog
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.context import REGION_ASSIGNMENTS, ContextClass, ContextPlan, ContextRegion
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import (
    ConversationItem,
    FakeModelScript,
    ModelRequest,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    UserMessage,
)
from agent_core.domain.policies import (
    ActionKind,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.records import RecordCadence, RecordComponent, RecordPrecision
from agent_core.domain.runs import RunCheckpoint, RunStatus
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
    agent,
    memory_uow_factory,
    principal,
    run,
    tool_context,
)
from tests.gates.test_context_m7 import (
    _budget,
    _PlanStore,
    _StaticMemoryRetriever,
    _StaticQueryFormer,
    _working_state,
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


# --- The records catalog in context -------------------------------------------------

CATALOG_OWNER = principal().model_copy(update={"scopes": {"records.read", "records.write"}})
# Distinctive recorded values: neither may ever reach the context body.
CATALOG_VALUES = {"systolic": Decimal("137.25"), "diastolic": Decimal("91.5")}


class _StaticCatalog:
    def __init__(self, catalog: RecordsCatalog | None) -> None:
        self.catalog_value = catalog
        self.calls: list[tuple[Sensitivity, int]] = []

    async def catalog(
        self, principal: Principal, *, ceiling: Sensitivity, max_series: int
    ) -> RecordsCatalog | None:
        del principal
        self.calls.append((ceiling, max_series))
        return None if self.catalog_value is None else self.catalog_value.model_copy(deep=True)


async def _stored_catalog() -> tuple[RecordsService, Any]:
    clock, factory = await memory_uow_factory()
    service = RecordsService(factory, clock)
    await service.create_series(
        CATALOG_OWNER,
        display_name="Blood pressure",
        components=[
            RecordComponent(name="systolic", unit="mmHg"),
            RecordComponent(name="diastolic", unit="mmHg"),
        ],
        sensitivity=None,
        cadence=RecordCadence.DAILY,
        session_id=SESSION_ID,
        run_id=None,
    )
    await service.create_series(
        CATALOG_OWNER,
        display_name="Sleep",
        components=[RecordComponent(name="hours")],
        sensitivity=None,
        cadence=None,
        session_id=SESSION_ID,
        run_id=None,
    )
    await service.append(
        CATALOG_OWNER,
        series_name="Blood pressure",
        values=CATALOG_VALUES,
        observed_at=None,
        precision=RecordPrecision.EXACT,
        note=None,
        idempotency_key="catalog-entry-1",
        session_id=SESSION_ID,
        run_id=None,
        origin_trust=TrustLevel.USER,
    )
    return service, factory


def _catalog_builder(
    source: RecordsCatalogSource | None,
    *,
    total_tokens: int = 32_768,
    records_catalog_tokens: int = 600,
    recall: bool = False,
) -> BudgetedContextBuilder:
    configured_agent = agent()
    estimator = ConservativeTokenEstimator()
    prefix = build_prefix(configured_agent, [])
    plan = ContextPlan(
        session_id=SESSION_ID,
        epoch=1,
        prefix_sha256=hashlib.sha256(prefix_bytes(prefix, [])).hexdigest(),
        prefix_tokens=estimator.estimate(prefix, "fake:scripted"),
        model_id="fake:scripted",
        tool_names=(),
        tool_specs=(),
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        policy_version="milestone-34-test",
        builder_version="context-builder@13",
        budget=_budget().model_copy(
            update={"total_tokens": total_tokens, "records_catalog_tokens": records_catalog_tokens}
        ),
        created_at=NOW,
    )
    return BudgetedContextBuilder(
        _PlanStore(plan),
        estimator,
        FixedClock(NOW),
        _working_state(),
        _StaticMemoryRetriever() if recall else None,
        _StaticQueryFormer() if recall else None,
        catalog_source=source,
    )


def _checkpoint(text: str = "How is my blood pressure tracking?") -> RunCheckpoint:
    return RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text=text)])],
        created_at=NOW,
    )


def _body(request: ModelRequest) -> list[ConversationItem]:
    return request.conversation[int(request.metadata["region_a_items"]) :]


def _texts(items: list[ConversationItem]) -> list[str]:
    return [
        part.text
        for item in items
        for part in getattr(item, "content", [])
        if isinstance(part, TextPart)
    ]


def _body_bytes(request: ModelRequest) -> bytes:
    return canonical_json_bytes([item.model_dump(mode="json") for item in _body(request)])


def test_every_context_class_has_a_region() -> None:
    assert set(REGION_ASSIGNMENTS) == set(ContextClass)
    assert REGION_ASSIGNMENTS[ContextClass.RECORDS_CATALOG] is ContextRegion.BODY


async def test_catalog_region_bytes_and_trust() -> None:
    assert set(REGION_ASSIGNMENTS) == set(ContextClass)
    assert REGION_ASSIGNMENTS[ContextClass.RECORDS_CATALOG] is ContextRegion.BODY
    active = run(status=RunStatus.RUNNING)

    # No series renders no bytes: the body equals one built without a source.
    without = await _catalog_builder(None).build(active, _checkpoint(), agent(), CATALOG_OWNER)
    empty_source = _StaticCatalog(None)
    empty = await _catalog_builder(empty_source).build(
        active, _checkpoint(), agent(), CATALOG_OWNER
    )
    assert _body_bytes(empty) == _body_bytes(without)
    assert empty.metadata["body_sha256"] == without.metadata["body_sha256"]
    assert empty_source.calls == [(Sensitivity.RESTRICTED, 16)]

    service, _factory = await _stored_catalog()
    request = await _catalog_builder(service).build(active, _checkpoint(), agent(), CATALOG_OWNER)
    stored = await service.catalog(CATALOG_OWNER, ceiling=Sensitivity.RESTRICTED, max_series=16)
    assert stored is not None
    rendered = render_records_catalog(stored, zone=ZoneInfo("UTC"), now=NOW)
    body = _body(request)
    catalog_texts = [text for text in _texts(body) if 'source="records.catalog"' in text]

    assert len(catalog_texts) == 1
    assert catalog_texts[0].startswith('<untrusted trust="internal_tool" source="records.catalog"')
    assert f"\n{rendered}\n</untrusted:" in catalog_texts[0]
    assert "Blood pressure (systolic mmHg, diastolic mmHg): 1 entry" in rendered
    assert "Sleep (hours): no entries, logged ad hoc" in rendered
    # The catalog lives in the body, ahead of the runtime metadata row, and
    # never moves the prefix.
    assert request.metadata["prefix_sha256"] == without.metadata["prefix_sha256"]
    assert (
        request.conversation[: len(request.conversation) - len(body)]
        == (without.conversation[: len(without.conversation) - len(_body(without))])
    )
    runtime_index = next(
        index for index, text in enumerate(_texts(body)) if "Runtime metadata" in text
    )
    catalog_index = next(
        index for index, text in enumerate(_texts(body)) if 'source="records.catalog"' in text
    )
    assert catalog_index == runtime_index - 1
    everything = "\n".join(_texts(request.conversation))
    for value in ("137.25", "91.5"):
        assert value not in everything
    # INTERNAL_TOOL content leaves a fresh chat's origin trust at USER.
    assert request.metadata["context_origin_trust"] == TrustLevel.USER.value

    # A principal without records.read sees no catalog at all.
    unscoped = principal().model_copy(update={"scopes": {"records.write"}})
    refused_source = _StaticCatalog(stored)
    refused = await _catalog_builder(refused_source).build(active, _checkpoint(), agent(), unscoped)
    baseline = await _catalog_builder(None).build(active, _checkpoint(), agent(), unscoped)
    assert _body_bytes(refused) == _body_bytes(baseline)
    assert refused_source.calls == []

    # The ceiling: an internal-ceiling catalog omits the two sensitive series
    # and holds nothing above internal; the restricted ceiling includes them.
    await service.create_series(
        CATALOG_OWNER,
        display_name="Steps",
        components=[RecordComponent(name="count")],
        sensitivity=Sensitivity.INTERNAL,
        cadence=None,
        session_id=SESSION_ID,
        run_id=None,
    )
    internal = await service.catalog(CATALOG_OWNER, ceiling=Sensitivity.INTERNAL, max_series=16)
    restricted = await service.catalog(CATALOG_OWNER, ceiling=Sensitivity.RESTRICTED, max_series=16)
    assert internal is not None
    assert restricted is not None
    assert [line.display_name for line in internal.lines] == ["Steps"]
    assert internal.omitted == 0
    assert [line.display_name for line in restricted.lines] == [
        "Blood pressure",
        "Sleep",
        "Steps",
    ]
    internal_text = render_records_catalog(internal, zone=ZoneInfo("UTC"), now=NOW)
    assert "Blood pressure" not in internal_text
    assert "Sleep" not in internal_text


async def test_catalog_is_absent_without_its_configured_class() -> None:
    """An overlay without classes.records_catalog has zero caps: no read, no yield step."""

    source = _StaticCatalog(
        await (await _stored_catalog())[0].catalog(
            CATALOG_OWNER, ceiling=Sensitivity.RESTRICTED, max_series=16
        )
    )
    active = run(status=RunStatus.RUNNING)
    assembled = await _catalog_builder(source, records_catalog_tokens=0).assemble(
        active, _checkpoint(), agent(), CATALOG_OWNER
    )
    bare = await _catalog_builder(None, records_catalog_tokens=0).assemble(
        active, _checkpoint(), agent(), CATALOG_OWNER
    )
    assert source.calls == []
    assert assembled.pressure.yield_steps == ()
    assert _body_bytes(assembled.request) == _body_bytes(bare.request)


def _catalog_envelopes(request: ModelRequest) -> list[str]:
    return [text for text in _texts(_body(request)) if 'source="records.catalog"' in text]


@pytest.mark.parametrize("flag", ["1", "0"])
async def test_composed_builder_carries_the_catalog_under_the_flag(flag: str) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(settings=load_settings(_environment(flag)), script=script) as composition:
        provider = composition.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert "records.read" in composition.principal.scopes
        session_id = await composition.sessions.create()
        records = RecordsService(composition.uow_factory, composition.clock)
        await records.create_series(
            composition.principal,
            display_name="Blood pressure",
            components=[RecordComponent(name="systolic", unit="mmHg")],
            sensitivity=None,
            cadence=RecordCadence.DAILY,
            session_id=session_id,
            run_id=None,
        )
        run_id = await composition.runs.submit("How is my blood pressure?", session_id)
        completed = await composition.runs.wait_terminal(run_id)
        assert completed.status is RunStatus.COMPLETED, completed.failure
        envelopes = _catalog_envelopes(provider.requests[-1])
    if flag == "1":
        assert len(envelopes) == 1
        assert envelopes[0].startswith('<untrusted trust="internal_tool" source="records.catalog"')
        assert "Blood pressure (systolic mmHg): no entries, due daily since " in envelopes[0]
    else:
        assert envelopes == []


async def test_catalog_yields_whole_under_pressure() -> None:
    active = run(status=RunStatus.RUNNING)
    service, _factory = await _stored_catalog()
    roomy = await _catalog_builder(service, recall=True).assemble(
        active, _checkpoint(), agent(), CATALOG_OWNER
    )
    bare = await _catalog_builder(None, recall=True).assemble(
        active, _checkpoint(), agent(), CATALOG_OWNER
    )
    assert roomy.pressure.yield_steps == ()
    catalog_tokens = roomy.pressure.total_tokens - bare.pressure.total_tokens
    assert catalog_tokens > 0

    # A window that holds everything but the catalog: the catalog yields
    # whole, first, and the in-turn recall survives.
    squeezed = await _catalog_builder(
        service, recall=True, total_tokens=bare.pressure.total_tokens + catalog_tokens // 2
    ).assemble(active, _checkpoint(), agent(), CATALOG_OWNER)
    assert squeezed.pressure.yield_steps[0] == "records_catalog"
    assert "recall" not in squeezed.pressure.yield_steps
    assert squeezed.pressure.fits
    squeezed_texts = _texts(_body(squeezed.request))
    assert not any("records.catalog" in text for text in squeezed_texts)
    assert not any("Blood pressure (" in text for text in squeezed_texts)
    assert any("<memory" in text for text in squeezed_texts)
    assert _body_bytes(squeezed.request) == _body_bytes(bare.request)

    # Tighter still: the catalog is gone before the recall trims.
    crushed = await _catalog_builder(
        service, recall=True, total_tokens=bare.pressure.total_tokens - 1
    ).assemble(active, _checkpoint(), agent(), CATALOG_OWNER)
    assert crushed.pressure.yield_steps[:2] == ("records_catalog", "recall")

    # A catalog over its own class cap drops whole, never truncated.
    capped = await _catalog_builder(service, recall=True, records_catalog_tokens=1).assemble(
        active, _checkpoint(), agent(), CATALOG_OWNER
    )
    assert capped.pressure.yield_steps == ("records_catalog",)
    assert _body_bytes(capped.request) == _body_bytes(bare.request)


async def test_catalog_listing_is_byte_stable() -> None:
    service, _factory = await _stored_catalog()
    zone = ZoneInfo("UTC")
    first = await service.catalog(CATALOG_OWNER, ceiling=Sensitivity.RESTRICTED, max_series=16)
    second = await service.catalog(CATALOG_OWNER, ceiling=Sensitivity.RESTRICTED, max_series=16)
    assert first is not None
    assert second is not None
    assert render_records_catalog(first, zone=zone, now=NOW).encode(
        "utf-8"
    ) == render_records_catalog(second, zone=zone, now=NOW).encode("utf-8")

    context = replace(tool_context(), principal=CATALOG_OWNER, origin_trust=TrustLevel.USER)
    listed = [await RecordsListTool(service, zone).execute({}, context) for _ in range(2)]
    assert all(result.ok for result in listed)
    assert canonical_json_bytes(listed[0].model_dump(mode="json")) == canonical_json_bytes(
        listed[1].model_dump(mode="json")
    )

    request_bytes = []
    for _ in range(2):
        request = await _catalog_builder(service).build(
            run(status=RunStatus.RUNNING), _checkpoint(), agent(), CATALOG_OWNER
        )
        request_bytes.append(_body_bytes(request))
    assert request_bytes[0] == request_bytes[1]
