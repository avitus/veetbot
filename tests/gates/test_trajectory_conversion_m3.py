"""A checked-in trajectory artifact remains convertible into the blocking suite."""

from pathlib import Path

from agent_core.evals.cases import load_cases
from agent_core.evals.trajectory import convert_trajectory

ROOT = Path(__file__).resolve().parents[2]


def test_checked_in_trajectory_case_was_produced_by_the_converter() -> None:
    converted = convert_trajectory(
        (ROOT / "tests/fixtures/trajectory/direct_response.json").read_bytes(),
        case_name="converted_trajectory_response",
        model_fixture="trajectory_direct_response",
    )
    checked = {case.name: case for case in load_cases(ROOT / "tests/eval_cases")}[
        "converted_trajectory_response"
    ]
    assert checked == converted.case
    assert converted.model_script.turns[0].text == checked.expected.final_text
    assert converted.tool_results == {}


def test_suite_has_a_trajectory_source_from_milestone_3() -> None:
    cases = load_cases(ROOT / "tests/eval_cases")
    assert any(case.source == "trajectory" and case.milestone <= 3 for case in cases)
