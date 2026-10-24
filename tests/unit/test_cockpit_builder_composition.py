"""The Cockpit builder joins the composition only behind its flag (docs/plan/cockpit.md)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.adapters.cockpit.builder import SandboxWidgetBuilder
from agent_core.adapters.execution.fake import FakeExecutionEnvironment
from agent_core.bootstrap import build
from agent_core.config import ConfigurationError
from agent_core.domain.errors import ExecutionRejected
from agent_core.domain.execution import ExecutionResult, SandboxPurpose
from agent_core.ports.cockpit import BuildScreen
from tests.contract.fake_github import app_private_key
from tests.integration.m2_support import memory_settings

FILES = {"widget.json": "{}", "widget.tsx": "", "fixtures/typical.json": "{}"}
BUNDLE = "(()=>{})();"


def _built() -> ExecutionResult:
    report = {
        "ok": True,
        "slug": "weather",
        "kit_api": 1,
        "sha256": hashlib.sha256(BUNDLE.encode()).hexdigest(),
        "diagnostics": [],
        "bundle": BUNDLE,
        "manifest": {"slug": "weather"},
    }
    return ExecutionResult(0, json.dumps(report).encode(), b"", False, False, False, None, (), 1)


async def test_the_builder_is_absent_while_cockpit_is_off() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:
        assert composition.cockpit_builder is None


async def test_an_enabled_cockpit_builds_in_its_own_sandbox_and_closes_it() -> None:
    settings = replace(memory_settings(), cockpit_enabled=True)
    async with build(settings=settings, storage="memory") as composition:
        builder = composition.cockpit_builder
        assert isinstance(builder, SandboxWidgetBuilder)
        environment = composition.sandbox.adapter
        assert isinstance(environment, FakeExecutionEnvironment)
        environment.queue_result(_built())
        build_result = await builder.build(
            composition.principal, "weather", FILES, screen=BuildScreen()
        )
        assert build_result.ok is True
        [specification] = [
            spec
            for spec in environment.specifications
            if spec.purpose is SandboxPurpose.COCKPIT_BUILD
        ]
        assert specification.tenant_id == composition.principal.tenant_id
    with pytest.raises(ExecutionRejected, match="closing"):
        await builder.build(composition.principal, "weather", FILES, screen=BuildScreen())


async def test_off_means_no_publication_and_no_cockpit_maintenance() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:
        assert composition.cockpit_publication is None
        worker = composition.maintenance_factory()
        assert worker._sweep_cockpit_outbox is None  # type: ignore[attr-defined]
        assert worker._sweep_cockpit_import is None  # type: ignore[attr-defined]


async def test_the_maintenance_role_refuses_cockpit_without_the_github_app() -> None:
    settings = replace(memory_settings(), cockpit_enabled=True)
    async with build(settings=settings, storage="memory") as composition:
        assert composition.cockpit_publication is not None
        with pytest.raises(ConfigurationError, match="GitHub App"):
            composition.maintenance_factory()


async def test_the_maintenance_role_commits_and_imports_with_the_github_app(
    tmp_path: Path,
) -> None:
    key = tmp_path / "cockpit-github-app"
    key.write_bytes(app_private_key())
    key.chmod(0o400)
    settings = replace(
        memory_settings(),
        cockpit_enabled=True,
        cockpit_github_repository="avitus/veetbot",
        cockpit_github_app_id=123456,
        cockpit_github_installation_id=7890,
        cockpit_github_key_file=key,
    )
    async with build(settings=settings, storage="memory") as composition:
        worker = composition.maintenance_factory()
        assert worker._sweep_cockpit_outbox is not None  # type: ignore[attr-defined]
        assert worker._sweep_cockpit_import is not None  # type: ignore[attr-defined]
    key.chmod(0o644)
    async with build(settings=settings, storage="memory") as composition:
        with pytest.raises(ConfigurationError, match="private"):
            composition.maintenance_factory()
