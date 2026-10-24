"""Gate 3 against a real container runtime: the builder image builds offline (Milestone 35).

docs/plan/cockpit.md, the build. The builder image runs the builder CLI with no
network, a read-only root, an empty environment and the standard limits; the
release repeats the reference build under runsc as its canary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

import pytest
import yaml

from agent_core.adapters.cockpit.builder import SandboxWidgetBuilder, cockpit_build_sandbox
from agent_core.adapters.determinism import RandomIdFactory, SystemClock
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.domain.agents import Principal
from agent_core.domain.execution import ExecutionCommand, ResourceLimits
from agent_core.execution.manager import SandboxManager
from agent_core.ports.cockpit import BuildScreen

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]

REPOSITORY = Path(__file__).resolve().parents[2]
WEATHER = REPOSITORY / "cockpit/examples/widgets/weather"
OWNER = Principal(tenant_id="tenant-a", principal_id="owner")


def _standard_limits() -> ResourceLimits:
    document = yaml.safe_load(
        (REPOSITORY / "src/agent_core/sandbox/limits.yaml").read_text(encoding="utf-8")
    )
    return ResourceLimits(**{name: int(value) for name, value in document["resources"].items()})


def _package(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
async def builder_sandbox() -> AsyncIterator[SandboxManager]:
    adapter = DockerExecutionEnvironment(
        SystemClock(), RandomIdFactory(), hard_cap_seconds=300, reaper_grace_seconds=0
    )
    digest = await resolve_local_image_digest("veetbot-cockpit-builder:dev")
    manager = cockpit_build_sandbox(adapter, image_digest=digest, limits=_standard_limits())
    try:
        yield manager
    finally:
        await manager.close()


async def test_the_builder_image_builds_a_reference_widget_offline(
    builder_sandbox: SandboxManager,
) -> None:
    builder = SandboxWidgetBuilder(builder_sandbox, RandomIdFactory())

    build = await builder.build(OWNER, "weather", _package(WEATHER), screen=BuildScreen())

    assert build.ok is True, build.diagnostics
    assert build.kit_api == 1
    assert build.bundle is not None
    assert build.bundle_sha256 == hashlib.sha256(build.bundle.encode()).hexdigest()
    assert build.manifest is not None and build.manifest["slug"] == "weather"


async def test_the_builder_image_rejects_banned_code_and_screened_names(
    builder_sandbox: SandboxManager,
) -> None:
    builder = SandboxWidgetBuilder(builder_sandbox, RandomIdFactory())
    package = _package(WEATHER)
    banned = {**package, "widget.tsx": package["widget.tsx"] + "\nvoid eval('1');\n"}

    rejected = await builder.build(OWNER, "weather", banned, screen=BuildScreen())
    screened = await builder.build(
        OWNER, "weather", package, screen=BuildScreen(names=("Example City",))
    )

    assert rejected.ok is False
    assert "lint.banned" in {diagnostic.rule for diagnostic in rejected.diagnostics}
    assert rejected.bundle is None
    assert screened.ok is False
    assert {diagnostic.rule for diagnostic in screened.diagnostics} == {"screen.name"}
    assert all("Example City" not in diagnostic.message for diagnostic in screened.diagnostics)


async def test_the_build_sandbox_has_no_network_a_read_only_image_and_no_secrets(
    builder_sandbox: SandboxManager,
) -> None:
    probe = (
        "const fs = require('node:fs');"
        "const out = {};"
        "try { fs.writeFileSync('/opt/cockpit/kit/probe', 'x'); out.image = 'writable'; }"
        " catch (error) { out.image = error.code; }"
        "out.env = Object.keys(process.env).sort();"
        "fetch('http://1.1.1.1/').then(() => { out.network = 'reachable'; })"
        ".catch((error) => { out.network = error.cause?.code ?? 'refused'; })"
        ".finally(() => console.log(JSON.stringify(out)));"
    )
    run_id = RandomIdFactory().new_id()
    try:
        result = await builder_sandbox.execute_for(
            "tenant-a",
            run_id,
            0,
            ExecutionCommand(
                argv=("node", "-e", probe),
                working_directory=PurePosixPath("."),
                timeout_seconds=30,
                stdin=None,
                maximum_output_bytes=64 * 1024,
            ),
        )
    finally:
        await builder_sandbox.release_run(run_id)

    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["image"] in {"EROFS", "EACCES"}
    assert report["network"] != "reachable"
    assert set(report["env"]) <= {"HOME", "PWD", "PATH", "TMPDIR", "LANG", "SHLVL", "_"}
