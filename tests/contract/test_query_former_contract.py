"""Structured recall-query formation contract."""

from agent_core.domain.context import WorkingState
from agent_core.memory.retrieval import DeterministicQueryFormer
from tests.contract.support import principal, run


def test_query_former_uses_working_state_and_current_message() -> None:
    queries = DeterministicQueryFormer(principal()).form(
        run(),
        WorkingState(objective="Prepare the Atlas release"),
        "Keep the response concise",
    )
    assert len(queries) == 1
    assert "Atlas" in queries[0].subjects
    assert "concise" in (queries[0].text or "")


def test_query_former_does_not_fire_without_retrievable_signal() -> None:
    former = DeterministicQueryFormer(principal())
    assert former.form(run(), WorkingState(), None) == []
    assert former.form(run(), WorkingState(), "   ") == []


def test_query_former_reads_open_questions_as_task_signal() -> None:
    queries = DeterministicQueryFormer(principal()).form(
        run(),
        WorkingState(open_questions=["Which region hosts Grafana?"]),
        None,
    )
    assert len(queries) == 1
    assert "Grafana" in queries[0].subjects
    assert "region" in (queries[0].text or "")
