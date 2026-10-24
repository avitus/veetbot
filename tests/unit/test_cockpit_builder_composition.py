"""The Cockpit builder joins the composition only behind its flag (docs/plan/cockpit.md)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agent_core.adapters.cockpit.builder import SandboxWidgetBuilder
from agent_core.adapters.execution.fake import FakeExecutionEnvironment
from agent_core.bootstrap import build
from agent_core.domain.errors import ExecutionRejected
from agent_core.domain.execution import ExecutionResult, SandboxPurpose
from agent_core.ports.cockpit import BuildScreen
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
