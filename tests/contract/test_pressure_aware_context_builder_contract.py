import hashlib

from agent_core.adapters.determinism import FixedClock
from agent_core.context.builder import BudgetedContextBuilder
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.context import ContextBudget, ContextPlan
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.runs import RunCheckpoint, RunStatus
from tests.contract.support import NOW, agent, principal, run, session


class _PinnedPlanner:
    def __init__(self, plan: ContextPlan) -> None:
        self._plan = plan

    async def current(self, _session_id: object) -> ContextPlan:
        return self._plan.model_copy(deep=True)


async def test_pressure_aware_builder_measures_before_building() -> None:
    configured_agent = agent()
    prefix = build_prefix(configured_agent, [])
    budget = ContextBudget(
        total_tokens=32768,
        reserve_output_tokens=4096,
        platform_tokens=2000,
        agent_tokens=4000,
        tool_tokens=6000,
        skill_catalog_tokens=1500,
        skill_body_tokens=6000,
        retrieved_context_tokens=3500,
        history_tokens=18000,
        working_state_tokens=1000,
        tool_result_tokens=4000,
        knowledge_tokens=3000,
    )
    plan = ContextPlan(
        session_id=session().id,
        epoch=1,
        prefix_sha256=hashlib.sha256(prefix_bytes(prefix, [])).hexdigest(),
        prefix_tokens=ConservativeTokenEstimator().estimate(prefix, "fake:scripted"),
        model_id="fake:scripted",
        tool_names=(),
        tool_specs=(),
        tool_schema_sha256=hashlib.sha256(b"[]").hexdigest(),
        policy_version="contract-policy@1",
        builder_version="context-builder@2",
        budget=budget,
        created_at=NOW,
    )
    builder = BudgetedContextBuilder(
        _PinnedPlanner(plan),  # type: ignore[arg-type]
        ConservativeTokenEstimator(),
        FixedClock(NOW),
        WorkingStateManager(
            FixedClock(NOW),
            {
                "max_constraints": 20,
                "max_open_tasks": 30,
                "max_established_facts": 40,
                "max_open_questions": 20,
                "block_ceiling_tokens": 1000,
            },
        ),
    )
    checkpoint = RunCheckpoint(
        run_id=run().id,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text="hello")])],
        created_at=NOW,
    )

    active_run = run(status=RunStatus.RUNNING)
    pressure = await builder.measure(active_run, checkpoint, configured_agent, principal())
    request = await builder.build(active_run, checkpoint, configured_agent, principal())

    assert pressure.fits is True
    assert request.metadata["context_total_tokens"] == str(pressure.total_tokens)
    assert request.metadata["prefix_sha256"] == plan.prefix_sha256
