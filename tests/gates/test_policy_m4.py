from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.adapters.persistence.memory import InMemoryApprovalRepository
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import (
    ApprovalResolutionState,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ToolValidationError,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolCallItem,
)
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    PolicyDecisionRank,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunStatus, Step
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolInvocation,
    ToolInvocationStatus,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.policy.engine import (
    DeterministicPolicyEngine,
    combine_decision_types,
)
from agent_core.policy.hardline import hardline_matches
from agent_core.policy.loader import DEFAULT_RULESET, load_ruleset
from agent_core.policy.revalidation import revalidation_denial_reason
from agent_core.policy.scopes import PLATFORM_SCOPES, missing_scopes
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.executor import ToolPipeline, _approval_argument_view, _idempotency_key
from agent_core.tools.registry import StaticToolRegistry, validate_registration
from agent_core.tools.validation import validate_and_normalize
from tests.contract.support import NOW, RUN_ID, SESSION_ID, ids, principal, run
from tests.contract.test_approval_repository_contract import request

ROOT = Path(__file__).resolve().parents[2]


class _ModifyingPolicy:
    async def evaluate(
        self, proposed: ProposedAction, actor: Principal, active_run: Run
    ) -> PolicyDecision:
        del proposed, actor, active_run
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW_WITH_MODIFICATIONS,
            reason_code="policy.test.modify",
            explanation="Narrow the test value.",
            modified_arguments={"value": "effective"},
            policy_version="test@modified+h00000000",
        )


class _AllowPolicy:
    async def evaluate(
        self, proposed: ProposedAction, actor: Principal, active_run: Run
    ) -> PolicyDecision:
        del proposed, actor, active_run
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            reason_code="policy.test.allow",
            explanation="Allow the scheduling contract fixture.",
            policy_version="test@allow+h00000000",
        )


class _RecordingTool:
    spec = ToolSpec(
        name="demo.modify",
        version="1.0.0",
        description="Record modified arguments for a policy plumbing test.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=1,
        maximum_output_bytes=1024,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self) -> None:
        self.observed: dict[str, object] | None = None
        self.observed_timeout: float | None = None

    async def execute(
        self, arguments: dict[str, object], context: ToolExecutionContext
    ) -> ToolResult:
        self.observed = arguments
        self.observed_timeout = context.timeout_seconds
        return ToolResult(ok=True, content=[TextPart(text="ok")], structured={})


class _ExecutionValidationTool:
    spec = _RecordingTool.spec.model_copy(
        update={
            "name": "demo.execution_validation",
            "output_schema": {
                "type": "object",
                "properties": {"accepted": {"type": "boolean"}},
                "required": ["accepted"],
                "additionalProperties": False,
            },
        }
    )

    def __init__(self, *, reject_arguments: bool) -> None:
        self.reject_arguments = reject_arguments

    async def execute(
        self, arguments: dict[str, object], context: ToolExecutionContext
    ) -> ToolResult:
        del arguments, context
        if self.reject_arguments:
            raise ToolValidationError("semantic arguments rejected")
        return ToolResult(ok=True, content=[TextPart(text="bad output")], structured={})


class _SchedulingTool:
    def __init__(self, *, name: str, side_effect: SideEffectClass, parallel: bool) -> None:
        self.spec = ToolSpec(
            name=name,
            version="1.0.0",
            description="Exercise scheduler overlap boundaries.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            side_effect=side_effect,
            risk=RiskLevel.HIGH if side_effect is SideEffectClass.EXTERNAL_WRITE else RiskLevel.LOW,
            idempotency=(
                IdempotencyClass.NON_IDEMPOTENT
                if side_effect is SideEffectClass.EXTERNAL_WRITE
                else IdempotencyClass.READ_ONLY
            ),
            required_scopes=(
                {"demo.write"} if side_effect is SideEffectClass.EXTERNAL_WRITE else set()
            ),
            timeout_seconds=1,
            maximum_output_bytes=1024,
            allow_parallel=parallel,
            output_trust=TrustLevel.INTERNAL_TOOL,
        )
        self.started = 0
        self.active = 0
        self.peak = 0
        self.release = asyncio.Event()

    async def execute(
        self, arguments: dict[str, object], context: ToolExecutionContext
    ) -> ToolResult:
        del arguments, context
        self.started += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.spec.side_effect is SideEffectClass.NONE:
                if self.started == 2:
                    self.release.set()
                await asyncio.wait_for(self.release.wait(), timeout=0.5)
            else:
                await asyncio.sleep(0.01)
            return ToolResult(ok=True, content=[TextPart(text="ok")], structured={})
        finally:
            self.active -= 1


class _FailingApprovalViewTool(_SchedulingTool):
    def __init__(self, *, name: str, side_effect: SideEffectClass, parallel: bool) -> None:
        super().__init__(name=name, side_effect=side_effect, parallel=parallel)
        self.approval_view_called = False

    async def approval_view(
        self, arguments: dict[str, object], *, tenant_id: str
    ) -> tuple[str, dict[str, object]]:
        del arguments, tenant_id
        self.approval_view_called = True
        raise RuntimeError("approval presentation failed")


def _settings(tmp_path: Path, *, config_dir: Path | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=config_dir,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
    )


def action(
    effect: SideEffectClass = SideEffectClass.EXTERNAL_WRITE,
    arguments: dict[str, object] | None = None,
) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=44),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name="demo.external_write",
        version="1.0.0",
        summary="Evaluate a test action.",
        side_effect=effect,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"demo.write"},
        arguments=arguments or {"content": "hello", "destination": "demo"},
        normalized_arguments_hash="hash",
        argument_trust={"content": TrustLevel.EXTERNAL_UNTRUSTED},
        origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        evaluated_at=NOW,
    )


def test_totality(tmp_path: Path) -> None:
    assert {rule.side_effect for rule in DEFAULT_RULESET.rules} == set(SideEffectClass)
    profile = yaml.safe_load((ROOT / "src/agent_core/policy/default.yaml").read_text())
    del profile["rules"]["none"]
    incomplete = tmp_path / "incomplete.yaml"
    incomplete.write_text(yaml.safe_dump(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="must be total"):
        load_ruleset(incomplete, ROOT / "src/agent_core/policy/hardline.yaml")
    profile["unexpected"] = True
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(yaml.safe_dump(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        load_ruleset(unknown, ROOT / "src/agent_core/policy/hardline.yaml")

    invalid_condition = yaml.safe_load((ROOT / "src/agent_core/policy/default.yaml").read_text())
    invalid_condition["rules"]["workspace_read"]["condition"] = "typo"
    invalid_condition_path = tmp_path / "invalid-condition.yaml"
    invalid_condition_path.write_text(yaml.safe_dump(invalid_condition), encoding="utf-8")
    with pytest.raises(ValueError, match="typo"):
        load_ruleset(invalid_condition_path, ROOT / "src/agent_core/policy/hardline.yaml")

    for value in (True, "3600", 1.5):
        invalid_expiry = yaml.safe_load((ROOT / "src/agent_core/policy/default.yaml").read_text())
        invalid_expiry["approval_expiry_seconds"]["high"] = value
        invalid_expiry_path = tmp_path / f"invalid-expiry-{type(value).__name__}.yaml"
        invalid_expiry_path.write_text(yaml.safe_dump(invalid_expiry), encoding="utf-8")
        with pytest.raises(ValueError, match="must be integers"):
            load_ruleset(invalid_expiry_path, ROOT / "src/agent_core/policy/hardline.yaml")

    unsafe_unknown = yaml.safe_load((ROOT / "src/agent_core/policy/default.yaml").read_text())
    unsafe_unknown["unknown_tool"]["decision"] = "allow"
    unsafe_unknown_path = tmp_path / "unsafe-unknown.yaml"
    unsafe_unknown_path.write_text(yaml.safe_dump(unsafe_unknown), encoding="utf-8")
    with pytest.raises(ValueError, match="fail closed"):
        load_ruleset(unsafe_unknown_path, ROOT / "src/agent_core/policy/hardline.yaml")


async def test_workspace_condition_checks_path_and_origin_trust_overlay() -> None:
    engine = DeterministicPolicyEngine(DEFAULT_RULESET)

    def workspace_action(path: str, *, origin: TrustLevel) -> ProposedAction:
        return action(SideEffectClass.WORKSPACE_WRITE, {"path": path}).model_copy(
            update={
                "argument_trust": {"path": TrustLevel.USER},
                "origin_trust": origin,
            },
            deep=True,
        )

    allowed = await engine.evaluate(
        workspace_action("notes/result.txt", origin=TrustLevel.USER), principal(), run()
    )
    assert allowed.decision is PolicyDecisionType.ALLOW
    for path in ("../outside", "./relative", "nested//empty", "/absolute"):
        denied = await engine.evaluate(
            workspace_action(path, origin=TrustLevel.USER), principal(), run()
        )
        assert denied.decision is PolicyDecisionType.DENY
    escalated = await engine.evaluate(
        workspace_action("notes/result.txt", origin=TrustLevel.EXTERNAL_UNTRUSTED),
        principal(),
        run(),
    )
    assert escalated.decision is PolicyDecisionType.REQUIRE_APPROVAL


async def test_determinism() -> None:
    engine = DeterministicPolicyEngine(DEFAULT_RULESET)
    outputs = {
        (await engine.evaluate(action(), principal(), run())).model_dump_json() for _ in range(1000)
    }
    assert len(outputs) == 1


def test_single_gate() -> None:
    tree = ast.parse((ROOT / "src/agent_core/tools/executor.py").read_text())
    functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "PROPOSED-to-AUTHORIZED" in (ast.get_docstring(node) or "")
    ]
    assert functions == ["authorize_tool_invocation"]


def test_monotonicity() -> None:
    rank = {
        PolicyDecisionType.ALLOW: PolicyDecisionRank.ALLOW,
        PolicyDecisionType.ALLOW_WITH_MODIFICATIONS: PolicyDecisionRank.ALLOW_WITH_MODIFICATIONS,
        PolicyDecisionType.REQUIRE_APPROVAL: PolicyDecisionRank.REQUIRE_APPROVAL,
        PolicyDecisionType.DENY: PolicyDecisionRank.DENY,
    }
    for deterministic in PolicyDecisionType:
        for advisory in (*PolicyDecisionType, None):
            combined = combine_decision_types(deterministic, advisory)
            assert rank[combined] >= rank[deterministic]


async def test_modification_rekeys_before_persistence(tmp_path: Path) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    actor = Principal(tenant_id="local", principal_id="local-user")
    tool = _RecordingTool()
    registry = StaticToolRegistry()
    registry.register(tool)
    async with build(
        settings=_settings(tmp_path),
        script=script,
        fixed_clock_at=datetime(2026, 1, 1, tzinfo=UTC),
    ) as app:
        run_id = await app.runs.submit("prepare a modification test")
        active_run = await app.runs.get(run_id)
        active_run = active_run.model_copy(
            update={"deadline_at": app.clock.now() + timedelta(milliseconds=250)},
            deep=True,
        )
        async with app.uow_factory() as uow:
            active_agent = await uow.agents.get_version(
                active_run.agent_id, active_run.agent_version
            )
            active_checkpoint = await uow.checkpoints.latest(run_id)
        assert active_checkpoint is not None
        active_agent = active_agent.model_copy(
            update={"enabled_tools": [tool.spec.name]}, deep=True
        )
        pipeline = ToolPipeline(
            registry,
            app.uow_factory,
            app.clock,
            ids(),
            policy=_ModifyingPolicy(),
        )
        step = Step(run_id=run_id, step_number=2, started_at=app.clock.now())
        call = ToolCallItem(
            call_id="modify-call",
            item_index=0,
            name=tool.spec.name,
            arguments={"value": "proposed"},
            raw_arguments='{"value":"proposed"}',
        )
        await pipeline.dispatch(
            run=active_run,
            checkpoint=active_checkpoint,
            tool_calls=[call],
            principal=actor,
            step=step,
            agent=active_agent,
            token=RunCancellationToken(app.clock, None),
        )
        async with app.uow_factory() as uow:
            invocation = (await uow.invocations.list_for_run(run_id, actor))[0]

    proposed_hash = validate_and_normalize({"value": "proposed"}, tool.spec.input_schema)[2]
    effective_hash = validate_and_normalize({"value": "effective"}, tool.spec.input_schema)[2]
    assert tool.observed == {"value": "effective"}
    assert tool.observed_timeout is not None
    assert 0 < tool.observed_timeout <= 0.25
    assert invocation.normalized_arguments_hash == proposed_hash
    assert invocation.effective_arguments_hash == effective_hash
    assert invocation.idempotency_key == _idempotency_key(
        active_run, step, call, registry.get(tool.spec.name), effective_hash
    )
    assert invocation.idempotency_key != _idempotency_key(
        active_run, step, call, registry.get(tool.spec.name), proposed_hash
    )


@pytest.mark.parametrize(
    ("reject_arguments", "expected_reason"),
    [(True, "tool.arguments_invalid"), (False, "tool.output_invalid")],
)
async def test_execution_validation_is_distinct_from_output_validation(
    tmp_path: Path,
    reject_arguments: bool,
    expected_reason: str,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    tool = _ExecutionValidationTool(reject_arguments=reject_arguments)
    registry = StaticToolRegistry()
    registry.register(tool)
    async with build(settings=_settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("prepare a validation classification test")
        active_run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            active_agent = await uow.agents.get_version(
                active_run.agent_id, active_run.agent_version
            )
            active_checkpoint = await uow.checkpoints.latest(run_id)
        assert active_checkpoint is not None
        active_agent = active_agent.model_copy(
            update={"enabled_tools": [tool.spec.name]}, deep=True
        )
        pipeline = ToolPipeline(
            registry,
            app.uow_factory,
            app.clock,
            ids(),
            policy=_AllowPolicy(),
        )
        result = await pipeline.dispatch(
            run=active_run,
            checkpoint=active_checkpoint,
            tool_calls=[
                ToolCallItem(
                    call_id="validation-call",
                    item_index=0,
                    name=tool.spec.name,
                    arguments={"value": "candidate"},
                    raw_arguments='{"value":"candidate"}',
                )
            ],
            principal=app.principal,
            step=Step(run_id=run_id, step_number=2, started_at=app.clock.now()),
            agent=active_agent,
            token=RunCancellationToken(app.clock, None),
        )
        async with app.uow_factory() as uow:
            invocation = (await uow.invocations.list_for_run(run_id, app.principal))[0]

    assert result[0].is_error is True
    assert invocation.outcome is not None
    assert invocation.outcome.reason_code == expected_reason


async def test_parallel_reads_overlap_and_external_writes_settle_sequentially(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    actor = Principal(
        tenant_id="local",
        principal_id="local-user",
        scopes=set(PLATFORM_SCOPES),
    )
    parallel_read = _SchedulingTool(
        name="demo.parallel_read",
        side_effect=SideEffectClass.NONE,
        parallel=True,
    )
    external_write = _SchedulingTool(
        name="demo.serial_write",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        parallel=True,
    )
    registry = StaticToolRegistry()
    registry.register(parallel_read)
    registry.register(external_write)
    async with build(settings=_settings(tmp_path), script=script, principal=actor) as app:
        run_id = await app.runs.submit("prepare scheduler tests")
        active_run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            active_agent = await uow.agents.get_version(
                active_run.agent_id, active_run.agent_version
            )
            active_checkpoint = await uow.checkpoints.latest(run_id)
        assert active_checkpoint is not None
        active_agent = active_agent.model_copy(
            update={"enabled_tools": [parallel_read.spec.name, external_write.spec.name]},
            deep=True,
        )
        pipeline = ToolPipeline(
            registry,
            app.uow_factory,
            app.clock,
            ids(),
            policy=_AllowPolicy(),
        )

        def calls(name: str) -> list[ToolCallItem]:
            return [
                ToolCallItem(
                    call_id=f"{name}-{index}",
                    item_index=index,
                    name=name,
                    arguments={},
                    raw_arguments="{}",
                )
                for index in range(2)
            ]

        await pipeline.dispatch(
            run=active_run,
            checkpoint=active_checkpoint,
            tool_calls=calls(parallel_read.spec.name),
            principal=actor,
            step=Step(run_id=run_id, step_number=2, started_at=app.clock.now()),
            agent=active_agent,
            token=RunCancellationToken(app.clock, None),
        )
        await pipeline.dispatch(
            run=active_run,
            checkpoint=active_checkpoint,
            tool_calls=calls(external_write.spec.name),
            principal=actor,
            step=Step(run_id=run_id, step_number=3, started_at=app.clock.now()),
            agent=active_agent,
            token=RunCancellationToken(app.clock, None),
        )

    assert parallel_read.peak == 2
    assert external_write.peak == 1


async def test_approval_compensation_preserves_the_transition_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    tool = _SchedulingTool(
        name="demo.compensation",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        parallel=False,
    )
    registry = StaticToolRegistry()
    registry.register(tool)
    async with build(settings=_settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("prepare approval compensation")
        active_run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            active_agent = await uow.agents.get_version(
                active_run.agent_id, active_run.agent_version
            )
            invocations = uow.invocations
            approvals = uow.approvals
        normalized, _canonical, arguments_hash = validate_and_normalize({}, tool.spec.input_schema)
        invocation = ToolInvocation(
            id=UUID(int=401),
            run_id=run_id,
            session_id=active_run.session_id,
            step_number=2,
            call_id="compensation-call",
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            idempotency_class=tool.spec.idempotency,
            side_effect=tool.spec.side_effect,
            risk=tool.spec.risk,
            status=ToolInvocationStatus.PROPOSED,
            raw_arguments="{}",
            normalized_arguments=normalized,
            normalized_arguments_hash=arguments_hash,
            idempotency_key="compensation-key",
            created_at=app.clock.now(),
            updated_at=app.clock.now(),
        )

        async def fail_transition(*args: object, **kwargs: object) -> ToolInvocation:
            del args, kwargs
            raise ConflictError("primary transition failure")

        original_discard = approvals.discard_pending

        async def discard_then_cancel(approval_id: UUID) -> None:
            await original_discard(approval_id)
            raise asyncio.CancelledError

        monkeypatch.setattr(invocations, "transition", fail_transition)
        monkeypatch.setattr(approvals, "discard_pending", discard_then_cancel)
        pipeline = ToolPipeline(registry, app.uow_factory, app.clock, ids())
        decision = PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason_code="policy.test.approval",
            explanation="Exercise compensation failure handling.",
            policy_version="test@approval+h00000000",
        )
        with pytest.raises(ConflictError, match="primary transition failure"):
            await pipeline._request_approval(
                active_run,
                ToolCallItem(
                    call_id=invocation.call_id,
                    item_index=0,
                    name=tool.spec.name,
                    arguments={},
                    raw_arguments="{}",
                ),
                principal(),
                active_agent,
                tool,
                invocation,
                decision,
                None,
            )

        assert await app.approvals.list_pending(run_id=run_id) == []


async def test_approval_presentation_failure_falls_back_and_still_waits(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    tool = _FailingApprovalViewTool(
        name="demo.approval_fallback",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        parallel=False,
    )
    registry = StaticToolRegistry()
    registry.register(tool)
    async with build(settings=_settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("prepare approval fallback")
        active_run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            active_agent = await uow.agents.get_version(
                active_run.agent_id, active_run.agent_version
            )
            normalized, _canonical, arguments_hash = validate_and_normalize(
                {}, tool.spec.input_schema
            )
            invocation = ToolInvocation(
                id=UUID(int=402),
                run_id=run_id,
                session_id=active_run.session_id,
                step_number=2,
                call_id="approval-fallback-call",
                tool_name=tool.spec.name,
                tool_version=tool.spec.version,
                idempotency_class=tool.spec.idempotency,
                side_effect=tool.spec.side_effect,
                risk=tool.spec.risk,
                status=ToolInvocationStatus.PROPOSED,
                raw_arguments="{}",
                normalized_arguments=normalized,
                normalized_arguments_hash=arguments_hash,
                idempotency_key="approval-fallback-key",
                created_at=app.clock.now(),
                updated_at=app.clock.now(),
            )
            await uow.invocations.create(invocation)

        decision = PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason_code="policy.test.approval",
            explanation="Exercise presentation fallback handling.",
            policy_version="test@approval+h00000000",
        )
        pipeline = ToolPipeline(registry, app.uow_factory, app.clock, ids())
        created = await pipeline._request_approval(
            active_run,
            ToolCallItem(
                call_id=invocation.call_id,
                item_index=0,
                name=tool.spec.name,
                arguments={},
                raw_arguments="{}",
            ),
            principal(),
            active_agent,
            tool,
            invocation,
            decision,
            None,
        )
        async with app.uow_factory() as uow:
            waiting = (await uow.invocations.list_for_run(run_id, app.principal))[0]

    assert created.action_summary == "Run demo.approval_fallback with validated arguments."
    assert created.arguments == {}
    assert waiting.status is ToolInvocationStatus.WAITING_FOR_APPROVAL
    assert tool.approval_view_called


async def test_operator_policy_overlay_is_hashed_audited_and_evaluated(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    policy_overlay = config_dir / "policy" / "default.yaml"
    policy_overlay.parent.mkdir(parents=True)
    policy_overlay.write_text("rules:\n  external_write:\n    decision: deny\n", encoding="utf-8")
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "blocked"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="denied safely", stop_reason=StopReason.END_TURN),
        ]
    )
    actor = Principal(tenant_id="local", principal_id="local-user")
    async with build(settings=_settings(tmp_path, config_dir=config_dir), script=script) as app:
        run_id = await app.runs.submit("apply the operator policy")
        completed = await app.runs.get(run_id)
        assert completed.status is RunStatus.COMPLETED
        assert await app.approvals.list_pending(run_id=run_id) == []
        async with app.uow_factory() as uow:
            invocation = (await uow.invocations.list_for_run(run_id, actor))[0]
            assert invocation.policy_decision is not None
            profile = await uow.policy_profiles.get(invocation.policy_decision.policy_version)

    assert invocation.status is ToolInvocationStatus.DENIED
    assert invocation.outcome is not None
    assert invocation.outcome.reason_code == "policy.matrix.external_write"
    assert profile is not None
    assert profile.policy_version != DEFAULT_RULESET.policy_version


async def test_policy_load_is_process_scoped_event_and_approval_arguments_are_redacted(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={
                            "destination": "demo",
                            "content": "x" * 600,
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(settings=_settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("request a redacted approval")
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        async with app.uow_factory() as uow:
            events = await uow.process_events.list("policy.profile.loaded")

    assert approval.arguments == {
        "destination": "demo",
        "content": f"{'x' * 512}…[TRUNCATED]",
    }
    assert _approval_argument_view({"api_key": "secret", "content": "token=value"}) == {
        "api_key": "[REDACTED]",
        "content": "[REDACTED]",
    }
    assert len(events) == 1
    assert events[0].payload == {
        "policy_version": approval.policy_version,
        "profile_name": "default",
    }


def test_hardline_immutable() -> None:
    with pytest.raises(ValidationError):
        DEFAULT_RULESET.hardline[0].id = "changed"
    targets: dict[str, tuple[SideEffectClass, dict[str, object]]] = {
        "destructive_root_delete": (SideEffectClass.CODE_EXECUTION, {"command": "rm -rf /"}),
        "protected_host_path": (SideEffectClass.WORKSPACE_WRITE, {"path": "/etc/passwd"}),
        "credential_class": (SideEffectClass.CREDENTIAL_ACCESS, {"operation": "read"}),
        "host_access_class": (SideEffectClass.HOST_ACCESS, {"operation": "read"}),
        "privileged_class": (SideEffectClass.PRIVILEGED, {"operation": "mount"}),
        "metadata_egress": (
            SideEffectClass.NETWORK_READ,
            {"url": "http://169.254.169.254/latest/meta-data"},
        ),
        "credential_to_egress": (
            SideEffectClass.EXTERNAL_WRITE,
            {"content": "api_key=secret-value"},
        ),
    }
    near_effects = {
        "credential_class": SideEffectClass.WORKSPACE_READ,
        "host_access_class": SideEffectClass.WORKSPACE_READ,
        "privileged_class": SideEffectClass.CODE_EXECUTION,
    }
    for rule in DEFAULT_RULESET.hardline:
        effect, arguments = targets[rule.id]
        assert hardline_matches(rule, action(effect, arguments))
        assert not hardline_matches(
            rule,
            action(near_effects.get(rule.id, effect), {"content": rule.near_miss}),
        )
    protected = next(rule for rule in DEFAULT_RULESET.hardline if rule.id == "protected_host_path")
    for path in (
        "/etc//passwd",
        "./etc/passwd",
        "workspace/../etc/passwd",
        "\\etc\\passwd",
        ".ssh/id_rsa",
    ):
        assert hardline_matches(
            protected,
            action(SideEffectClass.WORKSPACE_WRITE, {"path": path}),
        )
    assert not hardline_matches(
        protected,
        action(SideEffectClass.WORKSPACE_WRITE, {"path": ".ssh-backup/id_rsa"}),
    )


async def test_network_argument_cannot_authorize_its_own_host() -> None:
    engine = DeterministicPolicyEngine(DEFAULT_RULESET)
    decision = await engine.evaluate(
        action(
            SideEffectClass.NETWORK_READ,
            {"url": "https://example.com/", "host_allowed": True},
        ),
        principal(),
        run(),
    )
    assert decision.decision is PolicyDecisionType.DENY


def test_revalidation() -> None:
    approval = request().model_copy(update={"status": ApprovalStatus.APPROVED}, deep=True)
    assert (
        revalidation_denial_reason(
            approval,
            arguments_hash="hash",
            principal_scopes={"demo.write"},
            agent_version="1.0.0",
            policy_version=approval.policy_version,
            policy_decision=PolicyDecisionType.REQUIRE_APPROVAL,
        )
        is None
    )
    for fresh_decision in (
        PolicyDecisionType.DENY,
        PolicyDecisionType.ALLOW_WITH_MODIFICATIONS,
    ):
        assert (
            revalidation_denial_reason(
                approval,
                arguments_hash="hash",
                principal_scopes={"demo.write"},
                agent_version="1.0.0",
                policy_version=approval.policy_version,
                policy_decision=fresh_decision,
            )
            == "policy.revalidation.escalated"
        )
    for arguments_hash, principal_scopes, agent_version in (
        ("other", {"demo.write"}, "1.0.0"),
        ("hash", set(), "1.0.0"),
        ("hash", {"demo.write"}, "2.0.0"),
    ):
        assert (
            revalidation_denial_reason(
                approval,
                arguments_hash=arguments_hash,
                principal_scopes=principal_scopes,
                agent_version=agent_version,
                policy_version=approval.policy_version,
                policy_decision=PolicyDecisionType.REQUIRE_APPROVAL,
            )
            == "policy.revalidation.changed"
        )
    assert (
        revalidation_denial_reason(
            approval,
            arguments_hash="hash",
            principal_scopes={"demo.write"},
            agent_version="1.0.0",
            policy_version="new@profile+hline",
            policy_decision=PolicyDecisionType.REQUIRE_APPROVAL,
        )
        == "policy.revalidation.escalated"
    )
    assert (
        revalidation_denial_reason(
            approval,
            arguments_hash="hash",
            principal_scopes={"demo.write"},
            agent_version="1.0.0",
            policy_version="new@profile+hline",
            policy_decision=PolicyDecisionType.ALLOW,
        )
        is None
    )


async def test_cross_tenant() -> None:
    repository = InMemoryApprovalRepository(FixedClock(NOW))
    created = await repository.create(request())
    other = Principal(tenant_id="tenant-b", principal_id="principal-a")
    with pytest.raises(NotFoundError):
        await repository.get(created.id, other)
    assert await repository.list_pending(other) == []
    with pytest.raises(NotFoundError):
        await repository.resolve(created.id, other, ApprovalResolutionType.DENY, None)
    tenant_resolver = Principal(tenant_id="tenant-a", principal_id="principal-b")
    assert (await repository.get(created.id, tenant_resolver)).id == created.id
    assert [item.id for item in await repository.list_pending(tenant_resolver)] == [created.id]


async def test_profile_can_require_a_distinct_approval_resolver(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    policy_overlay = config_dir / "policy" / "default.yaml"
    policy_overlay.parent.mkdir(parents=True)
    policy_overlay.write_text(
        "self_approval:\n  enabled: false\napproval_expiry_seconds:\n  high: 7\n",
        encoding="utf-8",
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "review me"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(settings=_settings(tmp_path, config_dir=config_dir), script=script) as app:
        run_id = await app.runs.submit("require another resolver")
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        assert approval.expires_at is not None
        assert (approval.expires_at - approval.created_at).total_seconds() == 7
        with pytest.raises(AuthorizationError, match="distinct resolver"):
            await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await app.approvals.get(approval.id)).status is ApprovalStatus.PENDING


async def test_approval_revalidation_intersects_live_and_run_scopes(tmp_path: Path) -> None:
    actor = Principal(
        tenant_id="local",
        principal_id="local-user",
        scopes=set(PLATFORM_SCOPES),
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "scope check"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="scope revocation honored", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=_settings(tmp_path), script=script, principal=actor) as app:
        run_id = await app.runs.submit("revalidate current authority")
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        actor.scopes.remove("demo.write")
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            invocation = (await uow.invocations.list_for_run(run_id, actor))[0]

    assert completed.status is RunStatus.COMPLETED
    assert invocation.status is ToolInvocationStatus.DENIED
    assert invocation.outcome is not None
    assert invocation.outcome.reason_code == "policy.revalidation.changed"


def test_no_leakage() -> None:
    for reason in (
        "approval.denied",
        "approval.expired",
        "policy.scope.missing",
        "policy.matrix.external_write",
        "policy.revalidation.changed",
        "policy.revalidation.escalated",
    ):
        outcome = ToolOutcome(
            status=ToolOutcomeStatus.DENIED,
            action="demo.external_write",
            reason_code=reason,
            message="Not performed.",
            retryable=False,
            remediation="none",
        )
        assert set(outcome.model_dump(mode="json")) == {
            "status",
            "action",
            "reason_code",
            "message",
            "retryable",
            "remediation",
        }


async def test_idempotent_resolve() -> None:
    repository = InMemoryApprovalRepository(FixedClock(NOW))
    created = await repository.create(request())
    same = await asyncio.gather(
        *(
            repository.resolve(created.id, principal(), ApprovalResolutionType.APPROVE_ONCE, None)
            for _ in range(2)
        )
    )
    assert {item.state for item in same} == {
        ApprovalResolutionState.APPLIED,
        ApprovalResolutionState.ALREADY_RESOLVED_IDENTICALLY,
    }


async def test_prompt_not_authz() -> None:
    members = sorted((ROOT / "evals/corpora/injection").glob("*.txt"))
    assert len(members) >= 40
    engine = DeterministicPolicyEngine(DEFAULT_RULESET)
    for member in members:
        content = member.read_text(encoding="utf-8")
        result = await engine.evaluate(
            action(arguments={"content": content, "destination": "demo"}),
            principal(),
            run(),
        )
        assert result.decision is PolicyDecisionType.REQUIRE_APPROVAL


def test_scope_grammar() -> None:
    assert len(PLATFORM_SCOPES) == 25
    assert {
        "schedule.read",
        "schedule.write",
        "schedule.cancel",
        "device.read",
        "device.write",
        "notification.read",
    } <= PLATFORM_SCOPES
    bad_mcp = ToolSpec(
        name="mcp.files.write",
        version="1.0.0",
        description="bad scope fixture",
        input_schema={"type": "object"},
        output_schema=None,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={"session.write"},
        timeout_seconds=1,
        maximum_output_bytes=100,
        allow_parallel=False,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        source=ToolSource.MCP,
        server_id="files",
    )
    with pytest.raises(ToolValidationError, match="server scope namespace"):
        validate_registration(bad_mcp)
    with pytest.raises(ToolValidationError, match="server id"):
        validate_registration(bad_mcp.model_copy(update={"server_id": None}, deep=True))


def test_scope_match() -> None:
    assert missing_scopes({"run.read", "run.write"}, {"run.write"}) == {"run.read"}
    assert missing_scopes({"run.read", "run.write"}, {"run.read", "run.write"}) == set()
    assert missing_scopes({"run.read"}, {"run.write"}) == {"run.read"}


async def test_scope_stamped() -> None:
    live = Principal(tenant_id="tenant-a", principal_id="principal-a", scopes={"workspace.read"})
    stamped = run(status=RunStatus.RUNNING).model_copy(
        update={"principal_scopes": set(live.scopes)}, deep=True
    )
    resolver = StaticPrincipalResolver(live)
    live.scopes.clear()
    assert (await resolver.for_run(stamped)).scopes == {"workspace.read"}
    next_run = stamped.model_copy(update={"principal_scopes": set(live.scopes)}, deep=True)
    assert (await resolver.for_run(next_run)).scopes == set()
