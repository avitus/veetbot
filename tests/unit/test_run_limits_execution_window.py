"""An unset execution window leaves serialized limits, and their digests, unchanged."""

from agent_core.domain.runs import RunLimits


def test_an_unset_window_is_absent_from_serialized_limits() -> None:
    assert "execution_seconds" not in RunLimits().model_dump(mode="json")
    assert "execution_seconds" not in RunLimits().model_dump()


def test_a_set_window_survives_the_round_trip_through_the_runs_row() -> None:
    dumped = RunLimits(execution_seconds=120).model_dump(mode="json")
    assert dumped["execution_seconds"] == 120
    assert RunLimits.model_validate(dumped).execution_seconds == 120


def test_limits_stored_by_an_earlier_release_load_without_a_window() -> None:
    dumped = RunLimits().model_dump(mode="json")
    assert RunLimits.model_validate(dumped).execution_seconds is None
