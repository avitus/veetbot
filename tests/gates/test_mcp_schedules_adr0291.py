"""ADR-0291: a schedule names its MCP servers, and a saved job may read them."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_core.application.job_grants import access_needs_approval, grant_covers_revision
from agent_core.bootstrap import Composition, _validate_schedule_role
from agent_core.config import ConfigurationError, load_schedule_worker_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import ScheduleValidationError
from agent_core.domain.jobs import (
    JOB_CALLS,
    JobAccess,
    JobGrant,
    JobManifest,
    JobRecord,
    JobResult,
    JobRevision,
)
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPServerAccess,
    ScriptedMCPResponse,
)
from agent_core.domain.messages import ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import PolicyDecision, PolicyDecisionType
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import JobTask
from agent_core.domain.sessions import (
    SESSION_SCHEDULE_ID_METADATA_KEY,
    SESSION_SCHEDULE_MCP_SERVERS_METADATA_KEY,
)
from agent_core.mcp.job_reads import JobMCPReads
from agent_core.scheduling.defaults import derived_definition
from agent_core.tools.job_schedules import pin_job
from tests.gates.test_deferred_tools_adr0123 import _production
from tests.gates.test_owner_mcp_servers_adr0255 import (
    ENABLED,
    _add_and_sign_in,
    _magicpath,
    _magicpath_gateway,
    _remote,
)
from tests.gates.test_schedule_worker_m11 import _production_schedule_environment

FIRE_AT = datetime(2030, 1, 7, 15, tzinfo=UTC)
SCOPES = "session.read,schedule.read,schedule.write,schedule.cancel"
ASK = {
    "title": "Morning mail summary",
    "instruction": "Summarize unread mail.",
    "at": FIRE_AT.isoformat(),
}


def test_the_schedule_role_vouches_for_scale_vp_without_its_credential() -> None:
    settings = load_schedule_worker_settings(
        {**_production_schedule_environment(scopes=SCOPES), "AGENT_SVP_ENABLED": "1"}
    )

    assert settings.credentials == {}
    assert "mcp.svp_read.use" in _validate_schedule_role(settings).scopes


def test_the_schedule_role_refuses_the_scale_vp_credential(tmp_path: Path) -> None:
    grant = tmp_path / "svp.json"
    grant.write_text("{}", encoding="utf-8")
    grant.chmod(0o600)
    settings = load_schedule_worker_settings(
        {
            **_production_schedule_environment(scopes=SCOPES),
            "AGENT_SVP_ENABLED": "1",
            "SVP_CREDENTIAL_FILE": str(grant),
        }
    )

    with pytest.raises(ConfigurationError, match="provider keys"):
        _validate_schedule_role(settings)


def test_the_schedule_role_holds_no_scale_vp_scope_when_it_is_off() -> None:
    settings = load_schedule_worker_settings(_production_schedule_environment(scopes=SCOPES))

    assert "mcp.svp_read.use" not in _validate_schedule_role(settings).scopes


def _create_turn(**arguments: Any) -> list[ScriptedTurn]:
    return [
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(
                    name="schedule.create",
                    arguments={**ASK, **arguments},
                    call_id="create",
                )
            ]
        ),
        ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
    ]


async def _create(app: Composition, run_prompt: str = "Schedule it.") -> Any:
    """Approve the one schedule.create the script proposes, returning its approval."""
    run_id = await app.runs.submit(run_prompt)
    [approval] = await app.approvals.list_pending(run_id=run_id)
    await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
    terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    return approval


async def test_a_schedule_names_its_servers_and_asks_for_only_their_scopes(
    tmp_path: Path,
) -> None:
    _factory, context = await _production(
        tmp_path, _create_turn(use_mcp_servers=["svp_read", "gmail_work_read"])
    )
    async with context as app:
        approval = await _create(app)
        page = await app.schedules.list(app.principal, 10, None)

    assert approval.arguments["mcp_servers"] == ["gmail_work_read", "svp_read"]
    assert approval.arguments["requested_scopes"] == [
        "mcp.gmail_work_read.use",
        "mcp.svp_read.use",
    ]
    [record] = page.items
    assert record.revision.mcp_servers == {"svp_read", "gmail_work_read"}
    assert record.revision.requested_scopes == {
        "mcp.svp_read.use",
        "mcp.gmail_work_read.use",
    }


@pytest.mark.parametrize("server", ["bland_read", "bland_call", "no_such_server"])
async def test_a_server_that_cannot_be_named_creates_no_schedule(
    tmp_path: Path, server: str
) -> None:
    _factory, context = await _production(tmp_path, _create_turn(use_mcp_servers=[server]))
    async with context as app:
        await _create(app)
        page = await app.schedules.list(app.principal, 10, None)

    assert page.items == []


async def test_an_owner_added_server_is_named_without_a_scope(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path,
        _create_turn(use_mcp_servers=["magicpath"]),
        extra_environment=ENABLED,
        extra_scripts={"magicpath": _magicpath()},
        mcp_oauth_gateway=_magicpath_gateway(),
    )
    async with context as app:
        await _add_and_sign_in(app, MCPServerAccess.READ_ONLY)
        await _create(app)
        page = await app.schedules.list(app.principal, 10, None)

    [record] = page.items
    assert record.revision.mcp_servers == {"magicpath"}
    assert record.revision.requested_scopes == frozenset()


async def test_the_route_refuses_a_server_without_its_scope(tmp_path: Path) -> None:
    """The HTTP body is the definition, so the service holds the rule, not the tool."""
    _factory, context = await _production(
        tmp_path, [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)]
    )
    async with context as app:
        definition = derived_definition(
            *_pins(app),
            title="Mail",
            instruction="Read mail.",
            cadence=_once(),
            mcp_servers=frozenset({"svp_read"}),
        )
        with pytest.raises(ScheduleValidationError) as refused:
            await app.services.schedules.create(app.principal, definition, "no-scope")

    assert refused.value.reason == "schedule.mcp_scope_required"


async def test_an_update_replaces_the_servers_and_their_scopes(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path, _create_turn(use_mcp_servers=["svp_read", "gmail_work_read"])
    )
    async with context as app:
        await _create(app)
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        patched = await app.services.schedules.patch(
            app.principal,
            record.schedule.id,
            record.schedule.current_revision,
            _patch(mcp_servers=frozenset({"svp_read"})),
            "patch-key",
        )

    assert patched.revision.mcp_servers == {"svp_read"}
    assert patched.revision.requested_scopes == {"mcp.svp_read.use"}


def _patch(**values: Any) -> Any:
    from agent_core.domain.schedules import ScheduleDefinitionPatch

    return ScheduleDefinitionPatch(**values)


def test_an_empty_patch_of_servers_is_a_change_and_clears_them() -> None:
    assert _patch(mcp_servers=frozenset()).mcp_servers == frozenset()


def test_a_child_of_scheduled_work_inherits_its_servers() -> None:
    from agent_core.application.delegations import _inherited_servers
    from tests.contract.support import session

    def parent(**metadata: Any) -> Any:
        return session().model_copy(update={"metadata": metadata})

    key = SESSION_SCHEDULE_MCP_SERVERS_METADATA_KEY
    scheduled = {SESSION_SCHEDULE_ID_METADATA_KEY: "s"}
    assert _inherited_servers(parent()) == {}
    assert _inherited_servers(parent(**scheduled)) == {key: []}
    assert _inherited_servers(parent(**scheduled, **{key: ["docs"]})) == {key: ["docs"]}
    # A grandchild has no schedule id, only the set it was handed.
    assert _inherited_servers(parent(**{key: ["docs"]})) == {key: ["docs"]}


def _pins(app: Composition) -> tuple[Any, Any]:
    """The agent and limits the composed schedule.create derives a definition from."""
    tool = cast(Any, app.tool_pipeline._registry.get("schedule.create")).implementation
    return tool._agent, tool._limits


def _once() -> Any:
    from agent_core.domain.schedules import OnceCadence

    return OnceCadence(at=FIRE_AT)


def test_no_servers_leave_stored_and_hashed_definitions_unchanged() -> None:
    from tests.contract.test_schedule_repository_contract import revision

    assert "mcp_servers" not in revision().model_dump(mode="json")


def test_a_job_schedule_names_no_servers() -> None:
    from tests.contract.test_schedule_repository_contract import revision

    values = revision().model_dump()
    values["mcp_servers"] = frozenset({"svp_read"})
    values["task"] = JobTask(slug="count-items", revision_id=UUID(int=1), grant_id=UUID(int=2))
    from agent_core.domain.schedules import ScheduleRevision

    with pytest.raises(ValueError, match="grant"):
        ScheduleRevision.model_validate(values)


async def _plan(tmp_path: Path, metadata: dict[str, object] | None) -> Any:
    """What a session with this metadata is offered once the owner has added MagicPath."""
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=ENABLED,
        extra_scripts={"magicpath": _magicpath()},
        mcp_oauth_gateway=_magicpath_gateway(),
    )
    async with context as app:
        await _add_and_sign_in(app, MCPServerAccess.READ_ONLY)
        session_id = await app.sessions.create()
        if metadata is not None:
            async with app.uow_factory() as uow:
                session = await uow.sessions.get(session_id, app.principal)
                session_id = uuid4()
                await uow.sessions.create(
                    session.model_copy(update={"id": session_id, "metadata": metadata})
                )
        run_id = await app.runs.submit("What can you do?", session_id)
        terminal = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        plan = await app.executor._context_planner.current(session_id)
    assert terminal.status is RunStatus.COMPLETED, terminal.failure
    assert plan is not None
    return plan


def _offers_owner_tools(plan: Any) -> bool:
    """Owner-added tools never take a definition (ADR-0255), and a withheld one is not skipped."""
    named = (*plan.tool_names, *plan.deferred_tool_names, *plan.skipped_tool_names)
    return any(name.startswith("mcp.magicpath.") for name in named)


async def test_a_schedule_that_did_not_name_an_owner_server_is_not_offered_it(
    tmp_path: Path,
) -> None:
    plan = await _plan(tmp_path, {SESSION_SCHEDULE_ID_METADATA_KEY: str(uuid4())})

    assert not _offers_owner_tools(plan)


async def test_a_schedule_that_named_an_owner_server_is_offered_it(tmp_path: Path) -> None:
    plan = await _plan(
        tmp_path,
        {
            SESSION_SCHEDULE_ID_METADATA_KEY: str(uuid4()),
            SESSION_SCHEDULE_MCP_SERVERS_METADATA_KEY: ["magicpath"],
        },
    )

    assert _offers_owner_tools(plan)


async def test_chat_keeps_its_owner_servers(tmp_path: Path) -> None:
    assert _offers_owner_tools(await _plan(tmp_path, None))


def test_earlier_tool_versions_stay_as_pinned() -> None:
    from agent_core.tools.schedule_create import CockpitScheduleCreateTool, ScheduleCreateTool
    from agent_core.tools.schedule_lifecycle import ScheduleUpdateTool

    for tool in (ScheduleCreateTool, CockpitScheduleCreateTool, ScheduleUpdateTool):
        assert "use_mcp_servers" not in tool.spec.input_schema["properties"]


# --- Saved jobs ---


def test_a_grant_names_only_servers_a_job_may_be_given() -> None:
    assert JobAccess({}, {}, mcp_servers=frozenset({"svp_read"})).mcp_servers == {"svp_read"}
    for bad in ("bland_read", "bland_call", "Not-A-Server", ""):
        with pytest.raises(ValueError, match="server"):
            JobAccess({}, {}, mcp_servers=frozenset({bad}))


def test_a_job_declares_the_read_without_naming_a_server() -> None:
    assert "mcp.read" in JOB_CALLS
    manifest = JobManifest(title="Reader", calls=frozenset({"mcp.read"}))
    assert "svp_read" not in manifest.model_dump_json()


def _saved(access: JobAccess, *, approval: UUID | None = None) -> JobRecord:
    revision = JobRevision(
        id=UUID(int=1),
        manifest=JobManifest(title="Reader", calls=frozenset({"mcp.read"})),
        source="def main(inputs, job): return job.mcp.read('mcp.svp_read.search', {})",
        state="live",
        commit_sha="a" * 40,
        created_at=FIRE_AT,
    )
    grant = JobGrant(id=UUID(int=2), access=access, created_at=FIRE_AT, approval_id=approval)
    return JobRecord(
        tenant_id="local",
        principal_id="local-user",
        slug="reader",
        version=1,
        live_revision_id=revision.id,
        revisions={str(revision.id): revision},
        grants={str(grant.id): grant},
        updated_at=FIRE_AT,
    )


def test_server_access_needs_approval_and_binds_to_its_code() -> None:
    access = JobAccess({}, {}, mcp_servers=frozenset({"svp_read"}))
    saved = _saved(access)

    assert access_needs_approval(access, saved)
    assert not grant_covers_revision(
        saved.grants[str(UUID(int=2))], saved.revisions[str(UUID(int=1))]
    )


async def test_a_job_schedule_derives_each_granted_servers_scope(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=ENABLED,
        extra_scripts={"magicpath": _magicpath()},
        mcp_oauth_gateway=_magicpath_gateway(),
    )
    async with context as app:
        await _add_and_sign_in(app, MCPServerAccess.READ_ONLY)
        access = JobAccess({}, {}, mcp_servers=frozenset({"svp_read", "magicpath"}))
        async with app.uow_factory() as uow:
            await uow.jobs.put(_saved(access), expected_version=None)
        definition = await pin_job(
            derived_definition(
                *_pins(app),
                title="Reader",
                instruction="Read.",
                cadence=_once(),
            ),
            JobTask(slug="reader", revision_id=UUID(int=1), grant_id=UUID(int=2)),
            app.principal,
            app.uow_factory,
        )

    # The owner-added server needs no scope; Scale VP's is held exactly.
    assert definition.requested_scopes == {"jobs.run", "mcp.svp_read.use"}


async def test_switching_to_a_job_drops_the_servers_the_schedule_named(tmp_path: Path) -> None:
    _factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment=ENABLED,
    )
    async with context as app:
        async with app.uow_factory() as uow:
            await uow.jobs.put(_saved(JobAccess({}, {})), expected_version=None)
        definition = await pin_job(
            derived_definition(
                *_pins(app),
                title="Reader",
                instruction="Read.",
                cadence=_once(),
                mcp_servers=frozenset({"svp_read"}),
            ),
            JobTask(slug="reader", revision_id=UUID(int=1), grant_id=UUID(int=2)),
            app.principal,
            app.uow_factory,
        )

    # A job reads what its grant names, and this grant names nothing.
    assert definition.mcp_servers == frozenset()
    assert definition.requested_scopes == {"jobs.run"}


# --- The reader itself ---


READ_TOOL = "search_companies"


async def _reads(
    tmp_path: Path, access: MCPServerAccess = MCPServerAccess.READ_ONLY, **build_kwargs: Any
) -> tuple[Any, Any]:
    """A composed reader over a scripted owner-added server, and its scripted client."""
    server = _magicpath(
        ScriptedMCPResponse(name=READ_TOOL, result=MCPCallResult(content=('{"count": 3}',)))
    ).model_copy(update={"discovery": MCPDiscovery(tools=(_remote(READ_TOOL),))})
    factory, context = await _production(
        tmp_path,
        [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)],
        extra_environment={**ENABLED, "AGENT_JOBS_ENABLED": "1"},
        extra_scripts={"magicpath": server},
        mcp_oauth_gateway=_magicpath_gateway(),
        **build_kwargs,
    )
    return factory, (context, access)


class _Answering:
    """A policy engine with one answer, to test the reader's own check apart from it."""

    def __init__(self, decision: PolicyDecisionType) -> None:
        self.decision = decision

    async def evaluate(self, action: Any, principal: Any, run: Any) -> PolicyDecision:
        del action, principal, run
        return PolicyDecision(
            decision=self.decision, reason_code="test", explanation="test", policy_version="test"
        )


def _reader(app: Composition, policy: Any = None) -> JobMCPReads:
    """The composed reader, optionally with a policy that answers as a test needs."""
    pipeline = app.tool_pipeline
    assert app.mcp is not None
    return JobMCPReads(
        runtime=app.mcp,
        registry=pipeline._registry,
        policy=policy or pipeline._policy,
        uow_factory=app.uow_factory,
        clock=app.clock,
        ids=app.ids,
    )


async def test_the_reader_refuses_a_write_even_if_policy_would_allow_it(tmp_path: Path) -> None:
    _factory, (context, _access) = await _reads(tmp_path, MCPServerAccess.CHANGES)
    async with context as app:
        await _add_and_sign_in(app, MCPServerAccess.CHANGES)
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Ready?", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        with pytest.raises(ValueError, match=r"job\.mcp_refused"):
            await _reader(app, _Answering(PolicyDecisionType.ALLOW)).read(
                principal=app.principal,
                run_id=run_id,
                tool=f"mcp.magicpath.{READ_TOOL}",
                arguments={"id": "design"},
                maximum_response_bytes=48 * 1024,
                call_id="call",
            )


@pytest.mark.parametrize("decision", [PolicyDecisionType.DENY, PolicyDecisionType.REQUIRE_APPROVAL])
async def test_a_read_policy_would_not_simply_allow_is_refused_without_waiting(
    tmp_path: Path, decision: PolicyDecisionType
) -> None:
    _factory, (context, access) = await _reads(tmp_path)
    async with context as app:
        await _add_and_sign_in(app, access)
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Ready?", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        with pytest.raises(ValueError, match=r"job\.mcp_refused"):
            await _reader(app, _Answering(decision)).read(
                principal=app.principal,
                run_id=run_id,
                tool=f"mcp.magicpath.{READ_TOOL}",
                arguments={"id": "design"},
                maximum_response_bytes=48 * 1024,
                call_id="call",
            )


async def _read(app: Composition, tool: str, **kwargs: Any) -> dict[str, Any]:
    session_id = await app.sessions.create()
    run_id = await app.runs.submit("Ready?", session_id)
    await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
    return await _reader(app).read(
        principal=app.principal,
        run_id=run_id,
        tool=tool,
        arguments=kwargs.pop("arguments", {"id": "design"}),
        maximum_response_bytes=kwargs.pop("maximum_response_bytes", 48 * 1024),
        call_id="call",
    )


async def test_a_job_reads_a_read_only_server_through_its_grant(tmp_path: Path) -> None:
    _factory, (context, access) = await _reads(tmp_path)
    async with context as app:
        await _add_and_sign_in(app, access)
        answer = await _read(app, f"mcp.magicpath.{READ_TOOL}")

    assert answer["content"] == ['{"count": 3}']


async def test_a_server_that_changes_things_is_refused_without_a_call(tmp_path: Path) -> None:
    _factory, (context, _access) = await _reads(tmp_path, MCPServerAccess.CHANGES)
    async with context as app:
        await _add_and_sign_in(app, MCPServerAccess.CHANGES)
        with pytest.raises(ValueError, match=r"job\.mcp_refused"):
            await _read(app, f"mcp.magicpath.{READ_TOOL}")


async def test_a_scope_the_run_does_not_hold_is_refused(tmp_path: Path) -> None:
    _factory, (context, _access) = await _reads(tmp_path)
    async with context as app:
        narrow = app.principal.model_copy(update={"scopes": {"session.read"}})
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Ready?", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)
        with pytest.raises(ValueError, match=r"job\.mcp_refused"):
            await _reader(app).read(
                principal=narrow,
                run_id=run_id,
                tool="mcp.svp_read.search_companies",
                arguments={},
                maximum_response_bytes=48 * 1024,
                call_id="call",
            )


async def test_an_unknown_tool_is_refused(tmp_path: Path) -> None:
    _factory, (context, access) = await _reads(tmp_path)
    async with context as app:
        await _add_and_sign_in(app, access)
        with pytest.raises(ValueError, match=r"job\.mcp_refused"):
            await _read(app, "mcp.magicpath.no_such_tool")


async def test_an_answer_past_the_grant_limit_is_refused(tmp_path: Path) -> None:
    _factory, (context, access) = await _reads(tmp_path)
    async with context as app:
        await _add_and_sign_in(app, access)
        with pytest.raises(ValueError, match=r"job\.response_limit"):
            await _read(app, f"mcp.magicpath.{READ_TOOL}", maximum_response_bytes=4)


class _ReadingRunner:
    """Stands in for a sandboxed program that reads one tool through the broker."""

    def __init__(self, tool: str) -> None:
        self.tool = tool

    async def run(self, program: Any, inputs: Any, *, dispatch: Any, **kwargs: Any) -> JobResult:
        del program, inputs, kwargs
        reply = await dispatch(
            "mcp.read", {"tool": self.tool, "arguments": {"id": "design"}}, "read-1"
        )
        return JobResult(reply, "", 1)


async def test_a_composed_job_reads_through_its_grant_end_to_end(tmp_path: Path) -> None:
    _factory, (context, access) = await _reads(
        tmp_path,
        job_runner_override=_ReadingRunner(f"mcp.magicpath.{READ_TOOL}"),
    )
    async with context as app:
        await _add_and_sign_in(app, access)
        assert app.job_execution is not None
        async with app.uow_factory() as uow:
            await uow.jobs.put(
                _saved(JobAccess({}, {}, mcp_servers=frozenset({"magicpath"}))),
                expected_version=None,
            )
        session_id = await app.sessions.create()
        run_id = await app.runs.submit("Ready?", session_id)
        await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=30)

        async def no_model(**kwargs: Any) -> dict[str, Any]:
            pytest.fail("A read makes no model call.")

        result = await app.job_execution.run(
            principal=app.principal,
            slug="reader",
            revision_id=UUID(int=1),
            grant_id=UUID(int=2),
            run_id=run_id,
            lease_epoch=0,
            model=no_model,
        )

    assert result.value["status"] == "succeeded"
    assert result.value["result"]["content"] == ['{"count": 3}']


def test_the_sdk_sends_the_read_it_declares() -> None:
    from agent_core.execution.job_sdk import JobContext

    sent: list[tuple[str, dict[str, Any]]] = []
    context = JobContext()

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        sent.append((name, arguments))
        return {"content": []}

    context.mcp._call = call

    assert context.mcp.read("mcp.svp_read.search", {"q": "x"}) == {"content": []}
    assert sent == [("mcp.read", {"tool": "mcp.svp_read.search", "arguments": {"q": "x"}})]


# --- Feed-keeping schedules, the way MCP data reaches a widget ---


async def test_a_feed_schedule_can_name_a_server_and_its_session_records_it() -> None:
    from agent_core.mcp.configuration import svp_server_configs
    from tests.gates.test_cockpit_feed_refresh_adr0254 import (
        OWNER,
        _advance,
        _composition,
        _create,
        _id,
        _latest,
        _materializer,
        _rows,
    )

    async with _composition() as (composition, _):
        schedule, revision = _rows(_id(1))
        await _create(
            composition, (schedule, revision.model_copy(update={"mcp_servers": frozenset({"svp"})}))
        )
        _advance(composition, timedelta(minutes=15))
        await _materializer(composition).materialize(schedule.id)
        occurrence = await _latest(composition, schedule.id)
        assert occurrence.session_id is not None
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(occurrence.session_id, OWNER)
        tool = cast(Any, composition.tool_pipeline._registry.get("schedule.create"))
        row = svp_server_configs(OWNER.tenant_id, enabled=True)[0].model_copy(
            update={"server_id": "docs", "owner_managed": True, "required_scopes": frozenset()}
        )
        async with composition.uow_factory() as uow:
            await uow.mcp_servers.put(row)
        _summary, view = await tool.approval_view(
            {
                "title": "Pipeline widget",
                "instruction": "Publish the pipeline.",
                "cadence": {"kind": "INTERVAL", "every_minutes": 15, "timezone": "UTC"},
                "publish_cockpit_feed": True,
                "use_mcp_servers": ["docs"],
            },
            tenant_id=OWNER.tenant_id,
        )

    assert session.metadata[SESSION_SCHEDULE_MCP_SERVERS_METADATA_KEY] == ["svp"]
    assert view["mcp_servers"] == ["docs"]
    assert view["requested_scopes"] == ["cockpit.read", "cockpit.write"]


async def test_a_call_plans_report_run_is_not_offered_the_named_servers() -> None:
    """Owner-added servers need no scope, so the report's empty scopes do not withhold them."""
    from agent_core.domain.call_plans import (
        CALL_PLAN_STEP_EVENT,
        CallPlanReportReason,
        CallPlanStep,
        CallPlanStepKind,
        call_plan_step_key,
    )
    from agent_core.domain.events import ProcessEvent
    from agent_core.domain.schedules import CallPlan, OnceCadence
    from tests.gates.test_cockpit_feed_refresh_adr0254 import (
        NOW,
        OWNER,
        _advance,
        _composition,
        _create,
        _finish,
        _id,
        _latest,
        _materializer,
        _rows,
    )

    fire_at = NOW + timedelta(minutes=1)
    report_at = fire_at + timedelta(minutes=30)
    async with _composition() as (composition, _):
        schedule, revision = _rows(
            _id(1), cadence=OnceCadence(at=fire_at), scopes=frozenset({"session.read"})
        )
        await _create(
            composition,
            (
                schedule,
                revision.model_copy(
                    update={
                        "mcp_servers": frozenset({"docs"}),
                        "call_plan": CallPlan(
                            retry_every_minutes=2, retry_until=fire_at + timedelta(minutes=10)
                        ),
                    }
                ),
            ),
        )
        _advance(composition, timedelta(minutes=1))
        await _materializer(composition).materialize(schedule.id)
        finished = await _latest(composition, schedule.id)
        assert finished.run_id is not None
        await _finish(composition, finished.run_id, RunStatus.COMPLETED)
        step = CallPlanStep(
            attempt=2,
            kind=CallPlanStepKind.REPORT,
            fire_at=report_at,
            reason=CallPlanReportReason.NOT_REACHED,
        )
        async with composition.uow_factory() as uow:
            await uow.process_events.append(
                ProcessEvent(
                    id=uuid4(),
                    event_type=CALL_PLAN_STEP_EVENT,
                    actor_type="scheduler",
                    actor_id=OWNER.principal_id,
                    payload={
                        "schedule_id": str(schedule.id),
                        "schedule_revision": 1,
                        "tenant_id": OWNER.tenant_id,
                        "principal_id": OWNER.principal_id,
                        "event_time": NOW.isoformat(),
                        "step": step.model_dump(mode="json"),
                    },
                    derivation_key=call_plan_step_key(schedule.id, 2),
                    created_at=NOW,
                )
            )
            held = await uow.schedules.lock(schedule.id, OWNER)
            await uow.schedules.advance(held, held.model_copy(update={"next_fire_at": report_at}))
        _advance(composition, timedelta(minutes=30))
        await _materializer(composition).materialize(schedule.id)
        async with composition.uow_factory() as uow:
            occurrences = await uow.schedule_occurrences.list(schedule.id, OWNER, limit=10)
            first, report = sorted(occurrences, key=lambda row: row.nominal_fire_at)
            assert first.session_id is not None and report.session_id is not None
            calling = await uow.sessions.get(first.session_id, OWNER)
            reporting = await uow.sessions.get(report.session_id, OWNER)

    assert calling.metadata[SESSION_SCHEDULE_MCP_SERVERS_METADATA_KEY] == ["docs"]
    assert SESSION_SCHEDULE_MCP_SERVERS_METADATA_KEY not in reporting.metadata
    assert SESSION_SCHEDULE_ID_METADATA_KEY in reporting.metadata
