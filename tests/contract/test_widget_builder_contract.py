"""Widget builder contract: one fresh, offline build sandbox per package; a verdict or an error.

docs/plan/cockpit.md, Validation, the privacy screen and the build. The sandbox
adapter runs the builder CLI in the builder image with the package and screen on
stdin. Here a scripted execution environment stands in for the image;
tests/security/test_cockpit_builder_runtime_m35.py runs the real one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from agent_core.adapters.cockpit.builder import (
    BUILDER_ARGV,
    SandboxWidgetBuilder,
    cockpit_build_sandbox,
)
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic, TileSize
from agent_core.domain.execution import (
    EgressPolicy,
    EnvironmentHandle,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
    ResourceLimits,
    SandboxPurpose,
)
from agent_core.ports.cockpit import BuildScreen, WidgetBuild, WidgetBuildFailedError

NOW = datetime(2026, 10, 23, tzinfo=UTC)
OWNER = Principal(tenant_id="tenant-a", principal_id="owner")
LIMITS = ResourceLimits(
    cpu_millicores=2000,
    memory_bytes=2 * 1024 * 1024 * 1024,
    pids_max=512,
    workspace_bytes=64 * 1024 * 1024,
    inodes_max=10_000,
    wall_clock_seconds=300,
)
FILES = {
    "widget.json": '{"slug": "weather"}',
    "widget.tsx": "export default function Weather() { return null; }",
    "fixtures/typical.json": "{}",
}
MESSAGE = "cockpit: create weather\n\nCockpit-Widget: weather\nVeetbot-Revision: 0\n"
SCREEN = BuildScreen(
    names=("Example Person",), settings_values=("Example City",), commit_message=MESSAGE
)
BUNDLE = "(()=>{VeetbotRealm.register(function(){return null})})();"
MANIFEST = {"slug": "weather", "kit_api": 1, "sizes": ["md"]}


def _result(
    report: object,
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
    truncated: bool = False,
) -> ExecutionResult:
    stdout = report if isinstance(report, bytes) else (json.dumps(report) + "\n").encode()
    return ExecutionResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=b"",
        stdout_truncated=truncated,
        stderr_truncated=False,
        timed_out=timed_out,
        killed_by=KillReason.TIMEOUT if timed_out else None,
        files_changed=(),
        duration_ms=900,
    )


def _built(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "ok": True,
        "slug": "weather",
        "kit_api": 1,
        "sha256": hashlib.sha256(BUNDLE.encode()).hexdigest(),
        "diagnostics": [],
        "bundle": BUNDLE,
        "manifest": MANIFEST,
    }
    report.update(overrides)
    return report


def _rejected(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "ok": False,
        "slug": "weather",
        "diagnostics": [{"rule": "lint.banned", "file": "widget.tsx", "message": "eval"}],
    }
    report.update(overrides)
    return report


def _builder(
    environment: FakeExecutionEnvironment | None = None,
) -> tuple[SandboxWidgetBuilder, FakeExecutionEnvironment]:
    environment = environment or FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    manager = cockpit_build_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    return SandboxWidgetBuilder(manager, SequenceIdFactory(), timeout_seconds=120), environment


async def test_a_built_package_returns_its_verified_bundle_and_manifest() -> None:
    builder, environment = _builder()
    environment.queue_result(_result(_built()))

    build = await builder.build(OWNER, "weather", FILES, screen=SCREEN)

    assert build == WidgetBuild(
        ok=True,
        diagnostics=(),
        kit_api=1,
        bundle=BUNDLE,
        bundle_sha256=hashlib.sha256(BUNDLE.encode()).hexdigest(),
        manifest=MANIFEST,
    )
    [(_handle, command)] = environment.commands
    assert tuple(command.argv) == (*BUILDER_ARGV, "weather", "--emit-bundle")
    assert command.working_directory == PurePosixPath(".")
    assert command.timeout_seconds == 120
    assert json.loads(command.stdin or b"") == {
        "files": FILES,
        "screen": {
            "names": ["Example Person"],
            "settingsValues": ["Example City"],
            "commitMessage": MESSAGE,
        },
    }


async def test_a_rejected_package_returns_bounded_diagnostics_and_nothing_else() -> None:
    builder, environment = _builder()
    diagnostics = [
        {"rule": "lint.banned", "file": "widget.tsx", "message": "eval is not allowed", "line": 3},
        {"rule": "render.throws", "file": "fixtures/typical.json", "size": "md", "message": "x"},
        {"rule": "package.path", "file": "../" * 40, "message": "m" * 5000},
    ]
    environment.queue_result(_result(_rejected(diagnostics=diagnostics), exit_code=1))

    build = await builder.build(OWNER, "weather", FILES, screen=BuildScreen())

    assert build.ok is False
    assert (build.bundle, build.bundle_sha256, build.kit_api, build.manifest) == (None,) * 4
    assert build.diagnostics[:2] == (
        BuildDiagnostic(
            rule="lint.banned", file="widget.tsx", message="eval is not allowed", line=3
        ),
        BuildDiagnostic(
            rule="render.throws", file="fixtures/typical.json", message="x", size=TileSize.MD
        ),
    )
    clamped = build.diagnostics[2]
    assert (clamped.rule, len(clamped.file), len(clamped.message)) == ("package.path", 64, 2000)


@pytest.mark.parametrize(
    "result",
    [
        _result(b"", exit_code=2),
        _result(_built(), exit_code=None, timed_out=True),
        _result(_built(), truncated=True),
        _result(b"not json\n"),
        _result(_rejected(), exit_code=0),
        _result(_built(), exit_code=1),
        _result(_rejected(diagnostics=[]), exit_code=1),
        _result(_built(sha256="0" * 64)),
        _result(_built(slug="markets")),
        _result(_built(bundle=None)),
        _result(_built(diagnostics=[{"rule": "", "message": "x"}]), exit_code=1),
    ],
    ids=[
        "usage-exit",
        "timed-out",
        "truncated",
        "not-json",
        "exit-disagrees-with-report",
        "rejection-exit-with-built-report",
        "rejection-without-diagnostics",
        "digest-mismatch",
        "other-slug",
        "built-without-bundle",
        "malformed-diagnostic",
    ],
)
async def test_a_build_without_a_trustworthy_verdict_raises(result: ExecutionResult) -> None:
    builder, environment = _builder()
    environment.queue_result(result)

    with pytest.raises(WidgetBuildFailedError):
        await builder.build(OWNER, "weather", FILES, screen=BuildScreen())

    assert environment.live_environment_ids() == frozenset()


async def test_an_oversized_bundle_is_not_a_verdict() -> None:
    builder, environment = _builder()
    bundle = "x" * (256 * 1024 + 1)
    environment.queue_result(
        _result(_built(bundle=bundle, sha256=hashlib.sha256(bundle.encode()).hexdigest()))
    )

    with pytest.raises(WidgetBuildFailedError):
        await builder.build(OWNER, "weather", FILES, screen=BuildScreen())


async def test_each_build_gets_a_fresh_sandbox_that_is_gone_when_it_returns() -> None:
    builder, environment = _builder()
    environment.queue_result(_result(_built()))
    environment.queue_result(_result(_rejected(), exit_code=1))

    await builder.build(OWNER, "weather", FILES, screen=BuildScreen())
    await builder.build(OWNER, "weather", FILES, screen=BuildScreen())

    first, second = environment.specifications
    assert first.run_id != second.run_id
    assert {first.tenant_id, second.tenant_id} == {"tenant-a"}
    assert environment.live_environment_ids() == frozenset()


async def test_the_build_sandbox_is_offline_credential_free_and_held_by_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://db.invalid/agent")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    builder, environment = _builder()
    environment.queue_result(_result(_built()))

    await builder.build(OWNER, "weather", FILES, screen=BuildScreen())

    [specification] = environment.specifications
    assert specification.purpose is SandboxPurpose.COCKPIT_BUILD
    assert specification.egress == EgressPolicy()
    assert dict(specification.environment) == {
        "HOME": "/workspace",
        "PWD": "/workspace",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
    }
    assert specification.limits == LIMITS


@pytest.mark.parametrize("slug", ["--out=/workspace", "Weather", "../weather", "w"])
async def test_a_slug_that_is_not_a_widget_slug_never_reaches_a_sandbox(slug: str) -> None:
    builder, environment = _builder()

    with pytest.raises(ValueError, match="widget slug"):
        await builder.build(OWNER, slug, FILES, screen=BuildScreen())

    assert environment.specifications == []


async def test_a_cancelled_build_releases_its_sandbox() -> None:
    started = asyncio.Event()

    class Hanging(FakeExecutionEnvironment):
        async def execute(
            self, environment: EnvironmentHandle, command: ExecutionCommand
        ) -> ExecutionResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    builder, environment = _builder(Hanging(FixedClock(NOW), SequenceIdFactory()))
    task = asyncio.create_task(builder.build(OWNER, "weather", FILES, screen=BuildScreen()))
    await started.wait()
    assert len(environment.live_environment_ids()) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert environment.live_environment_ids() == frozenset()


@pytest.mark.parametrize(
    "preview",
    [
        None,
        {"fixture": "fixtures/typical.json", "size": "md", "text": "OK", "png": "not-png"},
        {"fixture": "fixtures/empty.json", "size": "md", "text": "OK", "png": ""},
    ],
)
async def test_a_preview_requires_the_selected_fixture_and_bounded_png(preview: object) -> None:
    builder, environment = _builder()
    environment.queue_result(_result(_built(preview=preview)))
    with pytest.raises(WidgetBuildFailedError):
        await builder.build(OWNER, "weather", FILES, screen=SCREEN, preview={})
    assert not environment.live_environment_ids()
