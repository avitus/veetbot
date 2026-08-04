"""Milestone 9 deterministic cross-arm memory-lift hard gate."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.domain.runs import RunStatus
from agent_core.evals.cases import EvalArm, load_cases
from agent_core.evals.runner import run_case

ROOT = Path(__file__).resolve().parents[2]


async def test_memory_changes_outcome() -> None:
    case = next(
        item
        for item in load_cases(ROOT / "tests/eval_cases")
        if item.name == "memory_changes_outcome"
    )
    result = await run_case(case, ROOT / "evals/fixtures/models")
    before, after = result.arm_results
    assert before.run.status is RunStatus.WAITING_FOR_USER
    assert before.memories
    assert after.run.status is RunStatus.COMPLETED
    assert after.run.final_message == "ORBIT-7"
    assert result.memories == after.memories


def test_unsupported_carry_subject_is_rejected() -> None:
    with pytest.raises(ValidationError, match="carry"):
        EvalArm.model_validate(
            {
                "name": "unsupported_carry",
                "carry": ["skills"],
                "expected": {"terminal_status": "COMPLETED"},
            }
        )
