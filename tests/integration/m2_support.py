from __future__ import annotations

from types import MappingProxyType

from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from tests.integration.disposable_database import disposable_database_url

PRINCIPAL = Principal(
    tenant_id="local",
    principal_id="local-user",
    roles={"user"},
    scopes=set(),
)


def database_settings() -> Settings:
    return Settings(
        database_url=disposable_database_url(),
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        people_enabled=False,  # Legacy suites opt into People in their own boundary cases.
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
        people_enabled=False,  # Legacy suites opt into People in their own boundary cases.
    )
