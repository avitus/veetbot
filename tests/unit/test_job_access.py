"""Private grants reject invalid bounds and cannot name platform feeds."""

from typing import Any

import pytest

from agent_core.domain.jobs import JobAccess


@pytest.mark.parametrize(
    "values",
    [
        {"model_cost": "-1"},
        {"model_cost": "abc"},
        {"model_cost": "NaN"},
        {"model_cost": "Infinity"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 601},
        {"maximum_requests": -1},
        {"maximum_requests": 51},
        {"maximum_response_bytes": 65536},
        {"hosts": {"*.github.com": None}},
        {"hosts": {"127.0.0.1": None}},
        {"hosts": {"other.example": "github-reader"}},
        {"feeds": frozenset({"veetbot.runs.activity"})},
        {"inputs": {"oversized": "x" * 65536}},
    ],
)
def test_invalid_access_is_not_a_grant(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        JobAccess(**({"inputs": {}, "hosts": {}} | values))


def test_operator_can_restrict_jobs_to_an_exact_host_superset() -> None:
    from agent_core.config import ConfigurationError, load_settings
    from tests.unit.test_config import base_environment

    settings = load_settings(
        {
            **base_environment(),
            "AGENT_JOBS_ENABLED": "1",
            "AGENT_JOBS_ALLOWED_HOSTS": "api.github.com,example.com",
        }
    )
    assert settings.jobs_allowed_hosts == frozenset({"api.github.com", "example.com"})
    with pytest.raises(ConfigurationError):
        load_settings({**base_environment(), "AGENT_JOBS_ALLOWED_HOSTS": "localhost"})
