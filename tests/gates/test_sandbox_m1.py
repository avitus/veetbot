"""Milestone 1 development-sandbox startup refusal."""

from __future__ import annotations

import pytest

from agent_core.config import ConfigurationError, load_settings


@pytest.mark.parametrize(
    "values",
    [
        {
            "DATABASE_URL": "postgresql+asyncpg://database/prod",
            "DEPLOYMENT_MODE": "production",
            "AUTH_MODE": "token",
            "AUTH_TOKEN": "not-a-real-credential",
            "SANDBOX_MECHANISM": "docker",
        },
        {
            "DATABASE_URL": "postgresql+asyncpg://database/dev",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "token",
            "AUTH_TOKEN": "not-a-real-credential",
            "SANDBOX_MECHANISM": "docker",
        },
    ],
)
def test_production_refuses_dev(values: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(values)
    message = str(caught.value)
    assert "DEPLOYMENT_MODE" in message
    assert "AUTH_MODE" in message
    assert "SANDBOX_MECHANISM" in message
