"""A checked-in trajectory artifact remains convertible into the blocking suite."""

import json
from pathlib import Path

import pytest

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
    assert any(case.source == "trajectory" and case.milestone == 3 for case in cases)


def test_converter_keeps_assistant_text_with_its_tool_calls() -> None:
    artifact = {
        "schema_version": 1,
        "export_id": "00000000-0000-0000-0000-000000000130",
        "outcome": "COMPLETED",
        "messages": [
            {"kind": "user", "content": [{"kind": "text", "text": "calculate"}]},
            {"kind": "assistant", "content": [{"kind": "text", "text": "checking"}]},
            {
                "kind": "tool_call",
                "call_id": "call-1",
                "name": "math.calculate",
                "arguments": {"expression": "1+1"},
            },
            {
                "kind": "tool_result",
                "call_id": "call-1",
                "content": [{"kind": "text", "text": "2"}],
            },
            {"kind": "assistant", "content": [{"kind": "text", "text": "2"}]},
        ],
    }

    converted = convert_trajectory(
        json.dumps(artifact).encode(),
        case_name="combined_assistant_tool_turn",
        model_fixture="combined_assistant_tool_turn",
    )

    first_turn = converted.model_script.turns[0]
    assert first_turn.text == "checking"
    assert [call.call_id for call in first_turn.tool_calls] == ["call-1"]
    assert converted.model_script.turns[1].text == "2"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("outcome", None, "no outcome"),
        ("outcome", "UNKNOWN", "invalid outcome"),
        ("export_id", None, "no export id"),
    ],
)
def test_converter_names_missing_or_invalid_export_state(
    field: str, value: object, message: str
) -> None:
    artifact = json.loads((ROOT / "tests/fixtures/trajectory/direct_response.json").read_text())
    if value is None:
        artifact.pop(field)
    else:
        artifact[field] = value

    with pytest.raises(ValueError, match=message):
        convert_trajectory(
            json.dumps(artifact).encode(),
            case_name="invalid_trajectory",
            model_fixture="invalid_trajectory",
        )
