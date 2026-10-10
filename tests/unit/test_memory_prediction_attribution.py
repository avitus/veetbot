"""Anticipation must name independently verifiable remembered claims."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.memory.distillation import _AnticipationResponse


def test_anticipation_rejects_one_prediction_combining_two_memories() -> None:
    # The live provider combined a location and a weekly routine, then used
    # that compound prediction to omit a location restatement. Neither cited
    # memory asserted the compound claim, so coverage could not verify it.
    with pytest.raises(ValidationError, match="attributed_memory_ids"):
        _AnticipationResponse.model_validate(
            {
                "predictions": [
                    {
                        "episode_index": 0,
                        "statement": "User lives in York and checks their calendar on Mondays.",
                        "attributed_memory_ids": [str(UUID(int=1)), str(UUID(int=2))],
                    }
                ]
            }
        )


def test_separate_attributed_claims_and_unattributed_expectations_remain_valid() -> None:
    response = _AnticipationResponse.model_validate(
        {
            "predictions": [
                {
                    "episode_index": 0,
                    "statement": statement,
                    "attributed_memory_ids": memory_ids,
                }
                for statement, memory_ids in [
                    ("User lives in York.", [str(UUID(int=1))]),
                    ("User checks their calendar on Mondays.", [str(UUID(int=2))]),
                    ("User may discuss their plans.", []),
                ]
            ]
        }
    )
    assert len(response.predictions) == 3
