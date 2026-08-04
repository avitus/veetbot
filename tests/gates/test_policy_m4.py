from __future__ import annotations

import ast
import asyncio
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
from agent_core.domain.errors import NotFoundError, ToolValidationError
from agent_core.domain.messages import (
    FakeModelScript,
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
from agent_core.tools.executor import ToolPipeline, _idempotency_key
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

    async def execute(
        self, arguments: dict[str, object], context: ToolExecutionContext
    ) -> ToolResult:
        del context
        self.observed = arguments
        return ToolResult(ok=True, content=[TextPart(text="ok")], structured={})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
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
    async with build(settings=_settings(tmp_path), script=script) as app:
        run_id = await app.runs.submit("prepare a modification test")
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
    assert invocation.normalized_arguments_hash == proposed_hash
    assert invocation.effective_arguments_hash == effective_hash
    assert invocation.idempotency_key == _idempotency_key(
        active_run, step, call, registry.get(tool.spec.name), effective_hash
    )
    assert invocation.idempotency_key != _idempotency_key(
        active_run, step, call, registry.get(tool.spec.name), proposed_hash
    )


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
    assert len(PLATFORM_SCOPES) == 15
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
