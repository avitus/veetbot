from __future__ import annotations

import os
from types import MappingProxyType

from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal

PRINCIPAL = Principal(
    tenant_id="local",
    principal_id="local-user",
    roles={"user"},
    scopes=set(),
)


def database_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )
