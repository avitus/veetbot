import json

from agent_core.domain.messages import UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.evals.general_recall_reader import reader_request
from tests.unit.test_general_recall_corpus import fixture


def test_reader_receives_only_task_rendered_evidence_and_unlabeled_output_contract() -> None:
    case = fixture()
    request = reader_request(
        case.task, "<memory>Step-free routes are required.</memory>", model_policy="balanced"
    )
    content = request.model_dump_json()
    assert "Step-free routes" in content
    assert "recovering" not in content and "expected_values" not in content
    assert case.scenario_lineage not in content
    memory_message = request.conversation[1]
    assert isinstance(memory_message, UserMessage)
    assert memory_message.trust is TrustLevel.MEMORY
    assert request.tools == [] and request.tool_choice == "none"
    assert request.maximum_provider_attempts == 1
    assert request.timeout_seconds == 30 and request.maximum_output_tokens == 1024
    schema = request.response_schema
    assert schema is not None
    values = schema["properties"]["values"]
    assert values["required"] == ["route"]
    assert values["additionalProperties"] is False
    assert "east ramp" not in json.dumps(schema)


def test_changing_gold_cannot_change_reader_request() -> None:
    case = fixture()
    changed = case.model_copy(update={"expected_values": {"route": ("footbridge",)}})
    assert reader_request(case.task, "", model_policy="balanced") == reader_request(
        changed.task, "", model_policy="balanced"
    )
