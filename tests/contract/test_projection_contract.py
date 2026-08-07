from agent_core.adapters.persistence.projections import (
    SESSION_HISTORY_NAME,
    SESSION_HISTORY_VERSION,
    TRAJECTORY_NAME,
    TRAJECTORY_VERSION,
)


def test_projection_declares_stable_identity_and_builder_version() -> None:
    assert SESSION_HISTORY_NAME == "session_history"
    assert SESSION_HISTORY_VERSION == "session-history@2"
    assert TRAJECTORY_NAME == "trajectory_export"
    assert TRAJECTORY_VERSION == "trajectory@1"
