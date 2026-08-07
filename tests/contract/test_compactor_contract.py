import pytest
from pydantic import ValidationError

from agent_core.context.compactor import StructuredCompactor
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.context import ContextBudget
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.messages import AssistantMessage, TextPart, UserMessage
from agent_core.domain.runs import RunCheckpoint, RunStatus
from tests.contract.support import NOW, RUN_ID


def _budget(history_tokens: int) -> ContextBudget:
    return ContextBudget(
        total_tokens=32768,
        reserve_output_tokens=4096,
        platform_tokens=2000,
        agent_tokens=4000,
        tool_tokens=6000,
        skill_catalog_tokens=1500,
        skill_body_tokens=6000,
        retrieved_context_tokens=3500,
        history_tokens=history_tokens,
        working_state_tokens=1000,
        tool_result_tokens=4000,
        knowledge_tokens=3000,
    )


def test_context_budget_rejects_an_output_reserve_larger_than_total() -> None:
    invalid = _budget(100).model_dump()
    invalid.update({"total_tokens": 100, "reserve_output_tokens": 101})
    with pytest.raises(ValidationError, match="output reserve exceeds"):
        ContextBudget.model_validate(invalid)
    boundary = _budget(100).model_dump()
    boundary.update({"total_tokens": 100, "reserve_output_tokens": 100})
    assert ContextBudget.model_validate(boundary).input_capacity == 0


async def test_compactor_returns_a_provenance_bearing_checkpoint_and_result() -> None:
    estimator = ConservativeTokenEstimator()
    latest = UserMessage(
        content=[TextPart(text="retain this current request")],
        source_event_sequence=3,
    )
    checkpoint = RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[
            UserMessage(
                content=[TextPart(text="original goal")],
                source_event_sequence=1,
            ),
            AssistantMessage(
                content=[TextPart(text="untrusted assistant material")],
                source_event_sequence=2,
            ),
            latest,
        ],
        budget_state={
            "context_model_id": "fake:scripted",
            "context_seed_event_sequence": 3,
        },
        created_at=NOW,
    )
    compactor = StructuredCompactor(estimator)

    updated, result = await compactor.compact(
        checkpoint,
        _budget(0),
        "contract-test",
    )

    assert updated.conversation == [latest]
    assert result.source_event_ids == (1, 2)
    assert result.elided[0].event_id == 2
    assert result.elided[0].item_id == "assistant:event:2"
    assert updated.compacted_summary == result.summary
    assert checkpoint.compacted_summary is None

    provenance_boundary = RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[
            UserMessage(
                content=[TextPart(text="compact this")],
                source_event_sequence=1,
            ),
            UserMessage(content=[TextPart(text="uncommitted active item")]),
        ],
        budget_state={
            "context_model_id": "fake:scripted",
            "context_seed_event_sequence": 2,
        },
        created_at=NOW,
    )
    boundary_updated, boundary_result = await compactor.compact(
        provenance_boundary,
        _budget(0),
        "contract-provenance-boundary",
    )

    assert boundary_result.source_event_ids == (1,)
    assert boundary_updated.conversation == provenance_boundary.conversation[1:]

    with pytest.raises(ContextOverflow, match="depth cap"):
        await compactor.compact(
            RunCheckpoint(
                run_id=RUN_ID,
                version=1,
                status=RunStatus.RUNNING,
                summary_depth=2,
                created_at=NOW,
            ),
            _budget(0),
            "depth-cap",
        )
    with pytest.raises(ContextOverflow, match="no history"):
        await compactor.compact(
            RunCheckpoint(
                run_id=RUN_ID,
                version=1,
                status=RunStatus.RUNNING,
                created_at=NOW,
            ),
            _budget(0),
            "empty",
        )
    active_only = RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text="active")], source_event_sequence=1)],
        budget_state={
            "context_model_id": "fake:scripted",
            "context_seed_event_sequence": 1,
        },
        created_at=NOW,
    )
    with pytest.raises(ContextOverflow, match="no inactive history"):
        await compactor.compact(active_only, _budget(0), "active-only")
    retain_all = active_only.model_copy(
        update={"budget_state": {**active_only.budget_state, "context_seed_event_sequence": 2}},
        deep=True,
    )
    with pytest.raises(ContextOverflow, match="could not identify"):
        await compactor.compact(retain_all, _budget(1_000), "already-fits")
