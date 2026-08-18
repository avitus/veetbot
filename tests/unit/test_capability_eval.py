import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.persistence.memory import (
    InMemoryCapabilityEvaluationRepository,
    InMemoryProcessEventRepository,
)
from agent_core.domain.runs import FailureReason, RunStatus
from agent_core.evals.capability import CapabilityExecution, run_suite
from agent_core.ports.persistence import UnitOfWorkFactory

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


class EvalUnitOfWork:
    def __init__(
        self,
        repository: InMemoryCapabilityEvaluationRepository,
        process_events: InMemoryProcessEventRepository,
    ) -> None:
        self.evaluations = repository
        self.process_events = process_events

    async def __aenter__(self) -> "EvalUnitOfWork":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class EvalUnitOfWorkFactory:
    def __init__(self) -> None:
        self.repository = InMemoryCapabilityEvaluationRepository()
        self.process_events = InMemoryProcessEventRepository()

    def __call__(self) -> EvalUnitOfWork:
        return EvalUnitOfWork(self.repository, self.process_events)

    def is_open(self) -> bool:
        return False


def _fixture(root: Path) -> None:
    capability = root / "evals" / "capability"
    (capability / "scenarios").mkdir(parents=True)
    (capability / "rubrics").mkdir()
    judge_root = capability / "judges" / "judge.v1"
    judge_root.mkdir(parents=True)
    trajectory_root = capability / "fixtures" / "trajectories"
    trajectory_root.mkdir(parents=True)
    prompt = "Judge the candidate against every criterion.\n"
    (judge_root / "prompt.md").write_text(prompt, encoding="utf-8")
    (judge_root / "judge.yaml").write_text(
        "\n".join(
            [
                "id: judge.v1",
                "provider: anthropic",
                "model: claude-opus-5",
                "model_policy: flagship",
                "prompt: prompt.md",
                f"prompt_sha256: {hashlib.sha256(prompt.encode()).hexdigest()}",
                "rubric_schema_version: 1",
                "max_model_calls: 2",
                'max_cost_usd: "0.20"',
            ]
        ),
        encoding="utf-8",
    )
    (capability / "config.yaml").write_text(
        """schema_version: 1
daily_cost_usd: "2.00"
suites:
  research:
    subject_model_policy: balanced
    cost_usd: "1.00"
""",
        encoding="utf-8",
    )
    (capability / "rubrics" / "quality.yaml").write_text(
        """schema_version: 1
floor: "0.60"
criteria:
  - id: correctness
    description: The result is correct.
    minimum: "0"
    maximum: "4"
    weight: "3"
  - id: clarity
    description: The result is clear.
    minimum: "0"
    maximum: "4"
    weight: "1"
""",
        encoding="utf-8",
    )
    export_id = UUID(int=10)
    run_id = UUID(int=11)
    (trajectory_root / "failed.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "export_id": str(export_id),
                "run_id": str(run_id),
                "outcome": "FAILED",
                "redaction": {"ruleset_version": "secrets@1"},
            }
        ),
        encoding="utf-8",
    )
    (capability / "scenarios" / "research.yaml").write_text(
        f"""id: cap-research-0001
suite: research
milestone: 3
task: Summarize the supplied evidence.
attachments: []
tools: [web.search]
rubric: rubrics/quality.yaml
judge: judge.v1
repeats: 2
ceiling:
  model_calls: 4
  tool_calls: 3
  cost_usd: "0.50"
  wall_seconds: 60
source:
  trajectory: fixtures/trajectories/failed.json
  export_id: {export_id}
  run_id: {run_id}
  outcome: FAILED
  diagnosis: The run returned an unsupported claim.
""",
        encoding="utf-8",
    )


async def test_capability_suite_repeats_scores_and_withholds_weights(tmp_path: Path) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()
    judge_prompts: list[str] = []
    next_run = 100

    async def execute(
        model_policy: str, tools: Any, budget: Any, prompt: str
    ) -> CapabilityExecution:
        nonlocal next_run
        del budget
        next_run += 1
        if model_policy == "balanced":
            assert list(tools) == ["web.search"]
            output = "The evidence supports the answer."
            provider, model = "openai", "gpt-5.6-sol"
        else:
            assert list(tools) == []
            judge_prompts.append(prompt)
            output = json.dumps(
                {
                    "criteria": [
                        {
                            "criterion": "correctness",
                            "observation": "Supported.",
                            "value": 4,
                        },
                        {"criterion": "clarity", "observation": "Clear.", "value": 3},
                    ]
                }
            )
            provider, model = "anthropic", "claude-opus-5"
        return CapabilityExecution(
            run_id=UUID(int=next_run),
            status=RunStatus.COMPLETED,
            output=output,
            provider=provider,
            model=model,
            model_calls=1,
            tool_calls=0,
            cost_usd=Decimal("0.05"),
            policy_failures=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
        )

    result = await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(UUID(int=value) for value in range(1000, 1010)),
    )
    assert len(result.runs) == 2
    assert result.mean == Decimal("0.9375")
    assert result.floor == Decimal("0.9375")
    assert result.variance == Decimal("0.0")
    assert not result.release_blocked
    assert len(judge_prompts) == 2
    assert all('"weight"' not in prompt for prompt in judge_prompts)
    assert all("weights intentionally withheld" in prompt for prompt in judge_prompts)


async def test_capability_ceiling_hit_is_excluded_from_distribution(tmp_path: Path) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()

    async def execute(
        _model_policy: str, _tools: Any, budget: Any, _prompt: str
    ) -> CapabilityExecution:
        return CapabilityExecution(
            run_id=UUID(int=400),
            status=RunStatus.FAILED,
            output=None,
            provider="openai",
            model="gpt-5.6-sol",
            model_calls=budget.model_calls,
            tool_calls=0,
            cost_usd=Decimal("0.10"),
            policy_failures=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            failure_reason=FailureReason.BUDGET_EXCEEDED,
        )

    result = await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(UUID(int=value) for value in range(2000, 2010)),
    )
    assert result.mean is None
    assert result.floor is None
    assert result.ceiling_hits == 2
    assert result.release_blocked
    assert all(row.run.score is None for row in result.runs)
    assert all(row.run.ceiling_hit == "model_calls" for row in result.runs)


async def test_capability_variance_preserves_decimal_precision(tmp_path: Path) -> None:
    _fixture(tmp_path)
    factory = EvalUnitOfWorkFactory()
    judge_values = iter((Decimal("0.4"), Decimal("0.8")))
    next_run = 600

    async def execute(
        model_policy: str, _tools: Any, _budget: Any, _prompt: str
    ) -> CapabilityExecution:
        nonlocal next_run
        next_run += 1
        output = "Subject output."
        provider, model = "openai", "gpt-5.6-sol"
        if model_policy != "balanced":
            value = next(judge_values)
            output = json.dumps(
                {
                    "criteria": [
                        {
                            "criterion": "correctness",
                            "observation": "Measured.",
                            "value": str(value),
                        },
                        {
                            "criterion": "clarity",
                            "observation": "Measured.",
                            "value": str(value),
                        },
                    ]
                }
            )
            provider, model = "anthropic", "claude-opus-5"
        return CapabilityExecution(
            run_id=UUID(int=next_run),
            status=RunStatus.COMPLETED,
            output=output,
            provider=provider,
            model=model,
            model_calls=1,
            tool_calls=0,
            cost_usd=Decimal("0.05"),
            policy_failures=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
        )

    result = await run_suite(
        tmp_path,
        suite="research",
        build_ref="abc123",
        uow_factory=cast(UnitOfWorkFactory, factory),
        execute=execute,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(UUID(int=value) for value in range(2100, 2110)),
    )

    assert result.variance == Decimal("0.0025")
