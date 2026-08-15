from __future__ import annotations

import os
from types import MappingProxyType

import pytest

from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal

PRINCIPAL = Principal(
    tenant_id="local",
    principal_id="local-user",
    roles={"user"},
    scopes=set(),
)


def database_settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    return Settings(
        database_url=database_url,
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )


def memory_settings() -> Settings:
    """Return deterministic settings for tests that never open PostgreSQL."""

    return Settings(
        database_url="postgresql+asyncpg://127.0.0.1:1/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )
