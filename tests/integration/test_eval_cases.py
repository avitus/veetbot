"""The declarative evaluation corpus is an integration front end."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.evals.cases import EvalCase, load_cases
from agent_core.evals.runner import run_case

ROOT = Path(__file__).resolve().parents[2]
CASES = load_cases(ROOT / "tests" / "eval_cases")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_eval_case(case: EvalCase) -> None:
    await run_case(case, ROOT / "evals" / "fixtures" / "models")
