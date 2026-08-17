"""Milestone 10A governed skill-authoring hard gates."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.adapters.skills.memory import InMemorySkillRepository
from agent_core.adapters.skills.stores import InMemorySkillPackageStore
from agent_core.application.skill_review import REVIEW_TOOL_ALLOWLIST, SkillBackgroundReview
from agent_core.bootstrap import _memory_uow_repositories, build
from agent_core.config import ConfigurationError, SandboxMechanism, load_settings
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
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
from agent_core.domain.runs import RunCheckpoint, RunKind, RunStatus
from agent_core.domain.skills import (
    AuthoringContext,
    SkillPackage,
    SkillPackageMember,
    SkillRef,
    SkillSource,
)
from agent_core.domain.tools import ToolExecutionContext, ToolResult
from agent_core.evals.cases import load_cases
from agent_core.evals.runner import run_case
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.skills.package import SkillPackageValidator
from agent_core.tools.executor import _turn_origin_trust
from agent_core.tools.skill_manage import SkillManageTool
from tests.contract.support import NOW, memory_stack, principal, run, tool_context

ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://localhost/agent",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "SANDBOX_MECHANISM": "fake",
        "OPENAI_MODEL": "",
    }


def _skill_markdown(name: str, body: str, *, version: str = "1.0.0") -> str:
    metadata = yaml.safe_dump(
        {
            "name": name,
            "version": version,
            "description": f"Procedure for {name}.",
            "required_tools": [],
        },
        sort_keys=False,
    )
    return f"---\n{metadata}---\n{body}"


def _package(name: str, body: str, *, version: str = "1.0.0") -> SkillPackage:
    return SkillPackage(
        directory_name=name,
        members=(
            SkillPackageMember(
                path="SKILL.md",
                data=_skill_markdown(name, body, version=version).encode(),
            ),
        ),
    )


async def _authoring_stack() -> tuple[
    SkillManageTool,
    InMemorySkillRepository,
    InMemorySkillPackageStore,
    MemoryUnitOfWorkFactory,
]:
    clock, sessions, runs, events = await memory_stack()
    store = InMemorySkillPackageStore()
    repository = InMemorySkillRepository(
        store,
        SkillPackageValidator(ConservativeTokenEstimator()),
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(800, 900)),
    )
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            skills=repository,
            clock=clock,
        )
    )
    return SkillManageTool(factory, store), repository, store, factory


def _context(
    *,
    scopes: set[str] | None = None,
    origin_trust: TrustLevel = TrustLevel.USER,
    invocation_id: int = 70,
) -> ToolExecutionContext:
    principal = Principal(
        tenant_id="tenant-a",
        principal_id="principal-a",
        roles={"user"},
        scopes=set(scopes or set()),
    )
    return replace(
        tool_context(),
        invocation_id=UUID(int=invocation_id),
        principal=principal,
        origin_trust=origin_trust,
        idempotency_key=f"authoring-{invocation_id}",
    )


async def test_authoring_trust() -> None:
    corpus = ROOT / "evals/corpora/skill_authoring_trust"
    assert len(tuple(corpus.iterdir())) >= 4
    for index, member in enumerate(sorted(corpus.iterdir()), start=1):
        tool, repository, _store, _factory = await _authoring_stack()
        result = await tool.execute(
            {
                "operation": "create",
                "name": f"blocked-{index}",
                "skill_markdown": _skill_markdown(f"blocked-{index}", member.read_text()),
            },
            _context(
                scopes={"skill.write"},
                origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                invocation_id=70 + index,
            ),
        )
        assert result.ok is False
        assert result.failure is not None
        assert result.failure.reason_code == "policy.skill.origin_untrusted"
        assert repository.revision_count() == 0

    action = ProposedAction(
        kind=ActionKind.SKILL_AUTHORING,
        action_id=UUID(int=99),
        tenant_id="tenant-a",
        session_id=tool_context().session_id,
        run_id=tool_context().run_id,
        step_number=1,
        name="skill.manage",
        version="1.0.0",
        summary="Create an agent skill.",
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        required_scopes={"skill.write"},
        arguments={"operation": "create", "name": "blocked"},
        normalized_arguments_hash="hash",
        argument_trust={"name": TrustLevel.USER},
        origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        evaluated_at=NOW,
    )
    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(action, principal(), run())
    assert decision.decision is PolicyDecisionType.DENY
    assert decision.reason_code == "policy.skill.origin_untrusted"

    settings = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.load",
                        arguments={"name": "untrusted-input"},
                        call_id="load-untrusted",
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.manage",
                        arguments={
                            "operation": "create",
                            "name": "blocked-from-untrusted",
                            "skill_markdown": _skill_markdown(
                                "blocked-from-untrusted", "Do not persist this."
                            ),
                        },
                        call_id="write-after-untrusted",
                    )
                ]
            ),
            ScriptedTurn(text="The untrusted write was denied."),
        ]
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["skill.load", "skill.manage"],
        enabled_skills=["untrusted-input"],
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.skills.install(
                composition.principal.tenant_id,
                _package("untrusted-input", "External instructions."),
                SkillSource.AGENT,
                0,
                AuthoringContext(
                    run_id=UUID(int=160),
                    principal_id=composition.principal.principal_id,
                    invocation_id=UUID(int=161),
                    idempotency_key="trust-gate-fixture",
                ),
            )
        run_id = await composition.runs.submit("Read then propose a skill.")
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(completed.session_id, 0, composition.principal)
            with pytest.raises(NotFoundError):
                await uow.skills.resolve(
                    composition.principal.tenant_id,
                    SkillRef.parse("blocked-from-untrusted"),
                )
    denials = [event for event in events if event.event_type == "tool.call.denied"]
    assert len(denials) == 1
    assert denials[0].payload["reason_code"] == "policy.skill.origin_untrusted"


@pytest.mark.parametrize("scopes", [set(), {"skill"}, {"skill.writex"}])
async def test_authoring_scope(scopes: set[str]) -> None:
    tool, repository, _store, _factory = await _authoring_stack()
    result = await tool.execute(
        {
            "operation": "create",
            "name": "scope-check",
            "skill_markdown": _skill_markdown("scope-check", "Do the scoped procedure."),
        },
        _context(scopes=scopes),
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "policy.scope.missing"
    assert repository.revision_count() == 0

    allowed = await tool.execute(
        {
            "operation": "create",
            "name": "scope-check",
            "skill_markdown": _skill_markdown("scope-check", "Do the scoped procedure."),
        },
        _context(scopes={"skill.write"}, invocation_id=71),
    )
    assert allowed.ok is True
    assert repository.revision_count() == 1

    settings = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.manage",
                        arguments={
                            "operation": "create",
                            "name": "pipeline-scope-check",
                            "skill_markdown": _skill_markdown(
                                "pipeline-scope-check", "Never reaches the repository."
                            ),
                        },
                        call_id="missing-scope",
                    )
                ]
            ),
            ScriptedTurn(text="The scoped operation was denied."),
        ]
    )
    scoped_principal = Principal(
        tenant_id="tenant-a",
        principal_id="principal-a",
        roles={"user"},
        scopes=set(scopes),
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["skill.manage"],
        principal=scoped_principal,
    ) as composition:
        run_id = await composition.runs.submit("Try the scoped write.")
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(completed.session_id, 0, composition.principal)
            with pytest.raises(NotFoundError):
                await uow.skills.resolve("tenant-a", SkillRef.parse("pipeline-scope-check"))
    denials = [event for event in events if event.event_type == "tool.call.denied"]
    assert len(denials) == 1
    assert denials[0].payload["reason_code"] == "policy.scope.missing"


async def test_authoring_is_default_off_and_background_depends_on_it() -> None:
    settings = load_settings(_environment())
    assert settings.skill_authoring_enabled is False
    assert settings.skill_background_review_enabled is False
    with pytest.raises(ConfigurationError, match="requires skill authoring"):
        load_settings({**_environment(), "AGENT_SKILL_BACKGROUND_REVIEW_ENABLED": "1"})

    async with build(settings=settings, fixed_clock_at=NOW, sequential_ids=True) as composition:
        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get("skill.manage")

    enabled = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
    assert enabled.sandbox is SandboxMechanism.FAKE
    async with build(settings=enabled, fixed_clock_at=NOW, sequential_ids=True) as composition:
        assert composition.tool_pipeline._registry.get("skill.manage").spec.name == "skill.manage"


async def test_authoring_approval_contains_a_canonical_diff() -> None:
    settings = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
    markdown = _skill_markdown("approved-skill", "Apply the approved procedure.")
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.manage",
                        arguments={
                            "operation": "create",
                            "name": "approved-skill",
                            "skill_markdown": markdown,
                        },
                        call_id="author-skill",
                    )
                ]
            ),
            ScriptedTurn(text="The skill proposal was approved."),
        ]
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["skill.manage"],
    ) as composition:
        run_id = await composition.runs.submit("Capture this procedure as a skill.")
        waiting = await composition.runs.get(run_id)
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert approval.action_kind is ActionKind.SKILL_AUTHORING
        assert approval.tool_invocation_id is not None
        assert "canonical_diff" in approval.arguments
        assert "skill_markdown" not in approval.arguments
        assert approval.arguments["current_revision"] == 0
        assert approval.arguments["proposed_revision"] == 1
        assert approval.arguments["diff_truncated"] is False
        assert any(
            "Apply the approved procedure." in line for line in approval.arguments["canonical_diff"]
        )
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            revision = await uow.skills.resolve(
                composition.principal.tenant_id, SkillRef.parse("approved-skill")
            )

    assert completed.status is RunStatus.COMPLETED
    assert revision.source is SkillSource.AGENT

    tool, _repository, _store, _factory = await _authoring_stack()
    _summary, bounded = await tool.approval_view(
        {
            "operation": "create",
            "name": "bounded-diff",
            "skill_markdown": _skill_markdown("bounded-diff", "x" * 100_000),
        },
        tenant_id="tenant-a",
    )
    assert bounded["diff_truncated"] is True
    assert len(json.dumps(bounded["canonical_diff"]).encode()) < 40_000


async def test_review_confined() -> None:
    settings = load_settings(
        {
            **_environment(),
            "AGENT_SKILL_AUTHORING_ENABLED": "1",
            "AGENT_SKILL_BACKGROUND_REVIEW_ENABLED": "1",
        }
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "6 * 7"},
                        call_id="parent-work",
                    )
                ]
            ),
            ScriptedTurn(text="The parent is complete."),
            ScriptedTurn(text="No reusable procedure was found."),
        ]
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["math.calculate"],
    ) as composition:
        parent_id = await composition.runs.submit("Do work that requires a tool.")
        parent = await composition.runs.wait_terminal(parent_id)
        async with composition.uow_factory() as uow:
            review = await uow.runs.child_for_parent(
                parent.id, RunKind.SKILL_REVIEW, composition.principal
            )
            assert review is not None
            review_agent = await uow.agents.get_version(review.agent_id, review.agent_version)
            review_checkpoint = await uow.checkpoints.latest(review.id)
            metrics = await uow.process_events.list()

    assert parent.status is RunStatus.COMPLETED
    assert review.status is RunStatus.COMPLETED
    assert review.parent_run_id == parent.id
    assert review.session_id != parent.session_id
    assert tuple(review_agent.enabled_tools) == REVIEW_TOOL_ALLOWLIST
    assert review_checkpoint is not None
    assert set(review_checkpoint.pinned_tool_names) == set(REVIEW_TOOL_ALLOWLIST)
    assert len(review_checkpoint.pinned_tool_names) == len(REVIEW_TOOL_ALLOWLIST)
    assert [
        event.event_type
        for event in metrics
        if event.event_type.startswith("skill.background_review")
    ] == [
        "skill.background_review.enqueued",
        "skill.background_review.completed",
    ]

    checkpoint = RunCheckpoint(
        run_id=UUID(int=901),
        version=1,
        status=RunStatus.RUNNING,
        conversation=[
            UserMessage(content=[TextPart(text="Review the data.")]),
            ToolCallItem(
                call_id="load-current",
                item_index=0,
                name="skill.load",
                arguments={"name": "procedure"},
                raw_arguments='{"name":"procedure"}',
            ),
            ToolResultItem(
                call_id="load-current",
                content=[TextPart(text="untrusted skill body")],
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
            ),
        ],
        created_at=NOW,
    )
    assert _turn_origin_trust(checkpoint) is TrustLevel.EXTERNAL_UNTRUSTED
    assert _turn_origin_trust(checkpoint, RunKind.SKILL_REVIEW.value) is TrustLevel.USER
    duplicate_call_id = checkpoint.model_copy(deep=True)
    duplicate_call_id.conversation.insert(
        2,
        ToolCallItem(
            call_id="load-current",
            item_index=1,
            name="network.fetch",
            arguments={"url": "https://example.invalid"},
            raw_arguments='{"url":"https://example.invalid"}',
        ),
    )
    assert (
        _turn_origin_trust(duplicate_call_id, RunKind.SKILL_REVIEW.value)
        is TrustLevel.EXTERNAL_UNTRUSTED
    )

    tool, repository, _store, _factory = await _authoring_stack()
    created = await tool.execute(
        {
            "operation": "create",
            "name": "review-owned",
            "skill_markdown": _skill_markdown("review-owned", "Original procedure."),
        },
        _context(scopes={"skill.write"}, invocation_id=91),
    )
    assert created.ok is True
    review_context = replace(
        _context(scopes={"skill.write"}, invocation_id=92),
        run_kind=RunKind.SKILL_REVIEW.value,
    )
    unread = await tool.execute(
        {
            "operation": "patch",
            "name": "review-owned",
            "expected_revision": 1,
            "skill_markdown": _skill_markdown(
                "review-owned", "Refined procedure.", version="1.0.1"
            ),
        },
        review_context,
    )
    assert unread.ok is False
    assert unread.failure is not None
    assert unread.failure.reason_code == "policy.skill.review_read_required"
    archived = await tool.execute(
        {"operation": "archive", "name": "review-owned", "expected_revision": 1},
        review_context,
    )
    assert archived.ok is False
    assert archived.failure is not None
    assert archived.failure.reason_code == "policy.skill.review_archive_denied"

    await repository.install(
        "tenant-a",
        _package("operator-owned", "Operator procedure."),
        SkillSource.OPERATOR,
        0,
        None,
    )
    loaded_operator_context = replace(
        review_context,
        invocation_id=UUID(int=93),
        idempotency_key="authoring-93",
        loaded_skills=({"name": "operator-owned", "revision": 1},),
    )
    for operation in ("edit", "patch"):
        denied = await tool.execute(
            {
                "operation": operation,
                "name": "operator-owned",
                "expected_revision": 1,
                "skill_markdown": _skill_markdown(
                    "operator-owned", "Attempted replacement.", version="1.0.1"
                ),
            },
            loaded_operator_context,
        )
        assert denied.ok is False
        assert denied.failure is not None
        assert denied.failure.reason_code == "skill.source_immutable"
    operator_archive = await tool.execute(
        {"operation": "archive", "name": "operator-owned", "expected_revision": 1},
        loaded_operator_context,
    )
    assert operator_archive.ok is False
    assert operator_archive.failure is not None
    assert operator_archive.failure.reason_code == "policy.skill.review_archive_denied"
    assert repository.revision_count() == 2


async def test_review_never_fatal() -> None:
    settings = load_settings(
        {
            **_environment(),
            "AGENT_SKILL_AUTHORING_ENABLED": "1",
            "AGENT_SKILL_BACKGROUND_REVIEW_ENABLED": "1",
        }
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "1 + 1"},
                        call_id="parent-work",
                    )
                ]
            ),
            ScriptedTurn(text="Parent answer remains successful."),
        ]
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["math.calculate"],
    ) as composition:
        parent_id = await composition.runs.submit("Complete this parent run.")
        parent = await composition.runs.wait_terminal(parent_id)
        async with composition.uow_factory() as uow:
            review = await uow.runs.child_for_parent(
                parent.id, RunKind.SKILL_REVIEW, composition.principal
            )
            failures = await uow.process_events.list("skill.background_review.failed")

    assert parent.status is RunStatus.COMPLETED
    assert parent.final_message == "Parent answer remains successful."
    assert review is not None
    assert review.status is RunStatus.FAILED
    assert len(failures) == 1
    assert failures[0].payload["parent_run_id"] == str(parent.id)

    class FailedDispatch:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        async def dispatch(self, run_id: UUID) -> None:
            del run_id
            raise self.failure

        async def resume(self, run_id: UUID) -> None:
            del run_id
            raise self.failure

    for failure in (TimeoutError("review dispatch timed out"), RuntimeError("worker killed")):
        contained_script = FakeModelScript(
            turns=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="math.calculate",
                            arguments={"expression": "2 + 2"},
                            call_id="contained-parent-work",
                        )
                    ]
                ),
                ScriptedTurn(text="Contained parent answer."),
            ]
        )
        contained_settings = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
        async with build(
            settings=contained_settings,
            script=contained_script,
            fixed_clock_at=NOW,
            sequential_ids=True,
            enabled_tools=["math.calculate"],
        ) as composition:
            contained_parent_id = await composition.runs.submit("Complete contained work.")
            contained_parent = await composition.runs.wait_terminal(contained_parent_id)
            async with composition.uow_factory() as uow:
                before = await uow.events.list_after(
                    contained_parent.session_id, 0, composition.principal
                )
            review_service = SkillBackgroundReview(
                uow_factory=composition.uow_factory,
                dispatcher=FailedDispatch(failure),
                catalogs=composition.skill_catalogs,
                principal=composition.principal,
                clock=composition.clock,
                seed_checkpoint=DurableCheckpointSeeder(composition.clock),
                activate_session=composition.mcp.activate_session,
                enabled=True,
            )
            assert await review_service.after_run(contained_parent.id) is None
            async with composition.uow_factory() as uow:
                unchanged_parent = await uow.runs.get(contained_parent.id, composition.principal)
                after = await uow.events.list_after(
                    contained_parent.session_id, 0, composition.principal
                )
                child = await uow.runs.child_for_parent(
                    contained_parent.id, RunKind.SKILL_REVIEW, composition.principal
                )
                dispatch_failures = await uow.process_events.list("skill.background_review.failed")
                assert child is not None
                await uow.runs.transition(child.id, RunStatus.QUEUED, RunStatus.RUNNING)
                terminal_child = await uow.runs.transition(
                    child.id, RunStatus.RUNNING, RunStatus.FAILED
                )
            assert await review_service.after_run(terminal_child.id) == terminal_child.id
            async with composition.uow_factory() as uow:
                all_failures = await uow.process_events.list("skill.background_review.failed")
        assert unchanged_parent.status is RunStatus.COMPLETED
        assert unchanged_parent.final_message == "Contained parent answer."
        assert after == before
        assert child is not None
        assert child.status is RunStatus.QUEUED
        assert len(dispatch_failures) == 1
        assert dispatch_failures[0].payload["review_run_id"] == str(child.id)
        assert len(all_failures) == 2
        assert {event.payload.get("status") for event in all_failures} == {None, "FAILED"}


async def test_review_dispatch_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingDispatch:
        async def dispatch(self, run_id: UUID) -> None:
            del run_id
            await asyncio.Event().wait()

        async def resume(self, run_id: UUID) -> None:
            del run_id
            await asyncio.Event().wait()

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "3 + 3"},
                        call_id="bounded-parent-work",
                    )
                ]
            ),
            ScriptedTurn(text="Bounded parent answer."),
        ]
    )
    settings = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["math.calculate"],
    ) as composition:
        parent_id = await composition.runs.submit("Complete bounded work.")
        parent = await composition.runs.wait_terminal(parent_id)
        review_service = SkillBackgroundReview(
            uow_factory=composition.uow_factory,
            dispatcher=HangingDispatch(),
            catalogs=composition.skill_catalogs,
            principal=composition.principal,
            clock=composition.clock,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
            activate_session=composition.mcp.activate_session,
            enabled=True,
        )
        monkeypatch.setattr(
            "agent_core.application.skill_review.REVIEW_DISPATCH_TIMEOUT_SECONDS", 0.01
        )

        assert await asyncio.wait_for(review_service.after_run(parent.id), timeout=0.2) is None
        async with composition.uow_factory() as uow:
            failures = await uow.process_events.list("skill.background_review.failed")

    assert len(failures) == 1
    assert failures[0].payload["error_class"] == "TimeoutError"


async def test_provenance_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    tool, repository, _store, _factory = await _authoring_stack()
    result = await tool.execute(
        {
            "operation": "create",
            "name": "provenance",
            "skill_markdown": _skill_markdown("provenance", "Keep the authoring trace."),
        },
        _context(scopes={"skill.write"}, invocation_id=77),
    )
    assert result.ok is True
    revision = await repository.resolve("tenant-a", SkillRef.parse("provenance@1"))
    assert revision.source is SkillSource.AGENT
    assert revision.authored_by_run_id == tool_context().run_id
    assert revision.authored_by_principal_id == "principal-a"
    assert revision.authored_by_invocation_id == UUID(int=77)
    assert revision.authoring_idempotency_key == "authoring-77"
    replay = await tool.execute(
        {
            "operation": "create",
            "name": "provenance",
            "skill_markdown": _skill_markdown("provenance", "Keep the authoring trace."),
        },
        _context(scopes={"skill.write"}, invocation_id=77),
    )
    assert replay.ok is True
    assert repository.revision_count() == 1
    conflicting_context = replace(
        _context(scopes={"skill.write"}, invocation_id=77),
        idempotency_key="different-arguments-hash",
    )
    conflict = await tool.execute(
        {
            "operation": "create",
            "name": "different-name",
            "skill_markdown": _skill_markdown("different-name", "Different content."),
        },
        conflicting_context,
    )
    assert conflict.ok is False
    assert conflict.failure is not None
    assert conflict.failure.reason_code == "skill_authoring_idempotency_conflict"
    assert repository.revision_count() == 1
    tenant_revisions = [
        revision
        for (tenant_id, _name), revisions in repository._revisions.items()
        if tenant_id == "tenant-a"
        for revision in revisions
    ]
    assert tenant_revisions
    assert all(
        revision.authored_by_principal_id is not None
        and revision.authored_by_invocation_id is not None
        and revision.authoring_idempotency_key is not None
        for revision in tenant_revisions
        if revision.source is SkillSource.AGENT
    )

    settings = load_settings({**_environment(), "AGENT_SKILL_AUTHORING_ENABLED": "1"})
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.manage",
                        arguments={
                            "operation": "create",
                            "name": "linked-provenance",
                            "skill_markdown": _skill_markdown(
                                "linked-provenance", "Retain the authoring run link."
                            ),
                        },
                        call_id="linked-authoring",
                    )
                ]
            ),
            ScriptedTurn(text="The linked skill was created."),
        ]
    )
    async with build(
        settings=settings,
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["skill.manage"],
    ) as composition:
        run_id = await composition.runs.submit("Create an auditable skill.")
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            linked = await uow.skills.resolve(
                composition.principal.tenant_id, SkillRef.parse("linked-provenance")
            )
            assert linked.authored_by_run_id is not None
            authoring_run = await uow.runs.get(linked.authored_by_run_id, composition.principal)
    assert completed.status is RunStatus.COMPLETED
    assert authoring_run.id == completed.id
    assert linked.authored_by_principal_id == composition.principal.principal_id
    assert linked.authored_by_invocation_id is not None
    assert linked.authoring_idempotency_key is not None
    _assert_agent_provenance_insert_paths_are_complete()
    test_agent_provenance_migration_backfills_by_skill_source(monkeypatch)


def _assert_agent_provenance_insert_paths_are_complete() -> None:
    required = {
        "authored_by_run_id",
        "authored_by_principal_id",
        "authored_by_invocation_id",
        "authoring_idempotency_key",
    }
    memory_source = (ROOT / "src/agent_core/adapters/skills/memory.py").read_text()
    memory_tree = ast.parse(memory_source)
    constructors = [
        node
        for node in ast.walk(memory_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SkillRevision"
    ]
    assert len(constructors) == 1
    assert required <= {keyword.arg for keyword in constructors[0].keywords}

    postgres_source = (ROOT / "src/agent_core/adapters/persistence/skills.py").read_text()
    postgres_tree = ast.parse(postgres_source)
    inserts = [
        node
        for node in ast.walk(postgres_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        and "pg_insert(SkillRevisionRow)"
        in (ast.get_source_segment(postgres_source, node.func.value) or "")
    ]
    assert len(inserts) == 1
    assert required <= {keyword.arg for keyword in inserts[0].keywords}


def test_agent_provenance_migration_backfills_by_skill_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.e1a4b7c9d205_add_skill_authoring_provenance"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "add_column", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "create_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(statement))

    migration.upgrade()

    sql = "\n".join(statements)
    assert "FROM skills AS skills" in sql
    assert "skills.source = 'agent'" in sql
    assert "authored_by_principal_id = COALESCE" in sql
    assert "authored_by_invocation_id = skill_revisions.id" in sql
    assert "authoring_idempotency_key = 'legacy:' || skill_revisions.id::text" in sql
    assert "skills.source <> 'agent'" in sql
    assert "authored_by_run_id = NULL" in sql


async def test_edit_conflict() -> None:
    tool, repository, _store, _factory = await _authoring_stack()
    created = await tool.execute(
        {
            "operation": "create",
            "name": "race",
            "skill_markdown": _skill_markdown("race", "Initial procedure."),
        },
        _context(scopes={"skill.write"}, invocation_id=80),
    )
    assert created.ok is True

    async def patch(body: str, invocation_id: int) -> ToolResult:
        return await tool.execute(
            {
                "operation": "patch",
                "name": "race",
                "expected_revision": 1,
                "skill_markdown": _skill_markdown("race", body, version="1.0.1"),
            },
            _context(scopes={"skill.write"}, invocation_id=invocation_id),
        )

    outcomes = await asyncio.gather(patch("Winner A.", 81), patch("Winner B.", 82))
    assert [result.ok for result in outcomes].count(True) == 1
    loser = next(result for result in outcomes if not result.ok)
    assert loser.failure is not None
    assert loser.failure.reason_code == "skill_revision_conflict"
    assert repository.revision_count() == 2
    latest = await repository.resolve("tenant-a", SkillRef.parse("race"))
    assert latest.revision == 2
    assert latest.body in {"Winner A.", "Winner B."}
    stale_archive = await tool.execute(
        {"operation": "archive", "name": "race", "expected_revision": 1},
        _context(scopes={"skill.write"}, invocation_id=83),
    )
    assert stale_archive.ok is False
    assert stale_archive.failure is not None
    assert stale_archive.failure.reason_code == "skill_revision_conflict"

    archived = await tool.execute(
        {"operation": "archive", "name": "race", "expected_revision": 2},
        _context(scopes={"skill.write"}, invocation_id=84),
    )
    assert archived.ok is True
    assert archived.structured is not None
    assert archived.structured["status"] == "archived"
    replayed_archive = await tool.execute(
        {"operation": "archive", "name": "race", "expected_revision": 2},
        _context(scopes={"skill.write"}, invocation_id=84),
    )
    assert replayed_archive.ok is True
    other = await tool.execute(
        {
            "operation": "create",
            "name": "race-other",
            "skill_markdown": _skill_markdown("race-other", "Other procedure."),
        },
        _context(scopes={"skill.write"}, invocation_id=85),
    )
    assert other.ok is True
    reused_for_other_skill = await tool.execute(
        {"operation": "archive", "name": "race-other", "expected_revision": 1},
        _context(scopes={"skill.write"}, invocation_id=84),
    )
    assert reused_for_other_skill.ok is False
    assert reused_for_other_skill.failure is not None
    assert reused_for_other_skill.failure.reason_code == "skill_authoring_idempotency_conflict"
    changed_replay = await tool.execute(
        {"operation": "archive", "name": "race", "expected_revision": 2},
        replace(
            _context(scopes={"skill.write"}, invocation_id=84),
            idempotency_key="changed-archive-request",
        ),
    )
    assert changed_replay.ok is False
    assert changed_replay.failure is not None
    assert changed_replay.failure.reason_code == "skill_authoring_idempotency_conflict"


async def test_case_27_replays_with_a_self_authored_skill() -> None:
    case = next(
        item
        for item in load_cases(ROOT / "tests/eval_cases")
        if item.name == "skill_changes_outcome"
    )
    assert case.arms[1].skill_source == "agent"
    result = await run_case(case, ROOT / "evals/fixtures/models")
    before, after = result.arm_results
    assert before.run.status is RunStatus.FAILED
    assert after.run.status is RunStatus.COMPLETED
    assert after.run.final_message == "READY_FOR_RELEASE"
