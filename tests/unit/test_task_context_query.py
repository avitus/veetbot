from agent_core.domain.context import Fact, TaskState, TaskStatus, WorkingState
from agent_core.domain.policies import TrustLevel
from agent_core.domain.semantic_memory import query_digest
from agent_core.memory.retrieval import DeterministicQueryFormer
from tests.contract.support import NOW, principal, run


def state() -> WorkingState:
    return WorkingState(
        objective="Prepare the Lenox visit",
        next_action="Choose dessert for Priya",
        constraints=["Respect dietary restrictions"],
        tasks=[
            TaskState(
                task_id="done",
                description="Book the Oakridge hotel",
                status=TaskStatus.COMPLETED,
                updated_at=NOW,
            ),
            TaskState(
                task_id="next",
                description="Arrange accessible travel",
                status=TaskStatus.IN_PROGRESS,
                updated_at=NOW,
            ),
        ],
        established_facts=[
            Fact(
                statement="Priya coordinates the Lenox visit.",
                source_event_ids=[2],
                trust_level=TrustLevel.USER,
                established_at=NOW,
            )
        ],
    )


def test_admitted_task_query_uses_pending_work_without_turning_hints_into_filters() -> None:
    current = state()
    before = current.model_dump()
    former = DeterministicQueryFormer(principal(), planning="task_context", admitted=lambda: True)
    queries = former.form(run(), current, "Continue", current_scope="current-project")
    assert len(queries) == 1
    query = queries[0]
    assert query.text and "Choose dessert for Priya" in query.text
    assert "Arrange accessible travel" in query.text
    assert "Respect dietary restrictions" in query.text
    assert "Oakridge" not in query.text
    assert query.current_scope == "current-project"
    assert query.as_of is None and query.known_at is None
    assert current.model_dump() == before
    changed = former.form(
        run(), current.model_copy(update={"next_action": "Choose lunch"}), "Continue"
    )
    assert query_digest(query) != query_digest(changed[0])


def test_task_planning_requires_current_admission_and_preserves_frozen_controls() -> None:
    admitted = [True]
    former = DeterministicQueryFormer(
        principal(), planning="task_context", admitted=lambda: admitted[0]
    )
    expected = DeterministicQueryFormer(principal()).form(run(), state(), "Continue")
    assert former.form(run(), state(), "Continue") != expected
    admitted[0] = False
    assert former.form(run(), state(), "Continue") == expected
    assert (
        DeterministicQueryFormer(principal(), planning="task_context").form(
            run(), state(), "Continue"
        )
        == expected
    )
    frozen = DeterministicQueryFormer(principal(), candidate_selection="recent")
    attempted = DeterministicQueryFormer(
        principal(), candidate_selection="recent", planning="task_context", admitted=lambda: True
    )
    assert attempted.form(run(), state(), "Continue") == frozen.form(run(), state(), "Continue")


def test_query_enrichment_is_bounded_and_never_truncates_the_current_request() -> None:
    former = DeterministicQueryFormer(principal(), planning="task_context", admitted=lambda: True)
    huge = state().model_copy(update={"objective": "Large objective " * 200})
    query = former.form(run(), huge, "Continue")[0]
    assert query.text and len(query.text.encode()) <= 512
    assert "Large objective" not in query.text and "Choose dessert" in query.text
    long_message = "Context " * 100 + "The important request is at the end."
    baseline = DeterministicQueryFormer(principal()).form(run(), huge, long_message)
    assert former.form(run(), huge, long_message) == baseline


def test_failed_admission_restores_baseline_without_a_cached_task_plan() -> None:
    def unavailable() -> bool:
        raise RuntimeError("Evidence unavailable")

    former = DeterministicQueryFormer(principal(), planning="task_context", admitted=unavailable)
    assert former.form(run(), state(), "Continue") == DeterministicQueryFormer(principal()).form(
        run(), state(), "Continue"
    )
