"""Per-case front end for the declarative evaluation corpus.

The corpus runs against the in-memory composition and needs no database;
this parametrized entry point exists for one-failing-case-at-a-time
diagnostics that the gate-side corpus loops cannot give.
"""

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
