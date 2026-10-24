"""Production builds resolve the builder image through the execution service (Milestone 35)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_core.adapters.execution.service import ExecutionServiceClient
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism
from agent_core.domain.execution import EgressPolicy, EnvironmentSpec, SandboxPurpose
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.ports.cockpit import BuildScreen
from tests.integration.m2_support import database_settings


class _ProvisionedError(Exception):
    pass


async def test_a_production_build_provisions_the_builder_image_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved: list[str] = []
    provisioned: list[EnvironmentSpec] = []

    async def resolve(_client: ExecutionServiceClient, reference: str) -> str:
        resolved.append(reference)
        return "sha256:" + "b" * 64

    async def provision(_client: ExecutionServiceClient, specification: EnvironmentSpec) -> None:
        provisioned.append(specification)
        raise _ProvisionedError

    monkeypatch.setattr(ExecutionServiceClient, "resolve_image_digest", resolve)
    monkeypatch.setattr(ExecutionServiceClient, "provision", provision)
    settings = replace(
        database_settings(),
        artifact_root=tmp_path / "artifacts",
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=SecretStr("synthetic-token"),
        auth_tenant_id="tenant-a",
        auth_principal_id="owner",
        auth_scopes=PLATFORM_SCOPES,
        sandbox=SandboxMechanism.GVISOR,
        execution_service_socket=tmp_path / "execution.sock",
        cockpit_enabled=True,
        cockpit_builder_image="veetbot-cockpit-builder:production",
    )
    async with build(settings=settings, storage="postgres") as composition:
        assert composition.cockpit_builder is not None
        with pytest.raises(_ProvisionedError):
            await composition.cockpit_builder.build(
                composition.principal,
                "weather",
                {"widget.json": "{}"},
                screen=BuildScreen(),
            )

    assert resolved == ["veetbot-cockpit-builder:production"]
    [specification] = provisioned
    assert specification.image_digest == "sha256:" + "b" * 64
    assert specification.purpose is SandboxPurpose.COCKPIT_BUILD
    assert specification.egress == EgressPolicy()
