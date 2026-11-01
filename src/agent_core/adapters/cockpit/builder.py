"""The sandboxed widget builder (docs/plan/cockpit.md, the build).

Every build provisions a fresh sandbox from the builder image, sends the
package and the privacy screen on stdin, reads one JSON report from stdout, and
releases the sandbox before it returns, also when it is cancelled. The report
comes from code that ran over agent-written input, so nothing in it is taken on
trust beyond what is checked here: the exit code and the report agree, the
bundle hashes to its digest and fits the cap, and a rejection says why.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import WIDGET_BUNDLE_MAX_BYTES, BuildDiagnostic, is_widget_slug
from agent_core.domain.execution import (
    EgressPolicy,
    ExecutionCommand,
    ExecutionResult,
    ResourceLimits,
    SandboxPurpose,
)
from agent_core.execution.manager import SandboxManager, _WorkspaceEnvironment
from agent_core.ports.cockpit import BuildScreen, WidgetBuild, WidgetBuildFailedError, WidgetPreview
from agent_core.ports.determinism import IdFactory

BUILDER_ARGV = ("node", "/opt/cockpit/builder/cli.mjs", "build", "--stdin")
# Workspace-relative, as every execution command is: "." is /workspace.
_WORKSPACE_ROOT = PurePosixPath(".")
# A bundle at the cap, JSON-escaped, plus its manifest and diagnostics.
_MAXIMUM_OUTPUT_BYTES = 4 * 1024 * 1024
_FIELD_LIMITS = {"rule": 64, "file": 64, "message": 2000, "size": 8}


def cockpit_build_sandbox(
    environment: _WorkspaceEnvironment,
    *,
    limits: ResourceLimits,
    image_digest: str | None = None,
    resolve_image_digest: Callable[[], Awaitable[str]] | None = None,
) -> SandboxManager:
    """The builder image's own manager: no network, no inherited environment.

    Its sandboxes carry `purpose=cockpit_build`, so the reaper holds each one
    to its own wall-clock deadline rather than to a run lease.
    """

    return SandboxManager(
        environment,
        image_digest=image_digest,
        resolve_image_digest=resolve_image_digest,
        limits=limits,
        egress=EgressPolicy(),
        parent_environment={},
        purpose=SandboxPurpose.COCKPIT_BUILD,
    )


def _diagnostic(raw: object) -> BuildDiagnostic:
    if not isinstance(raw, dict):
        raise WidgetBuildFailedError("a builder diagnostic is not an object")
    fields: dict[str, Any] = {}
    for name, limit in _FIELD_LIMITS.items():
        value = raw.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise WidgetBuildFailedError(f"a builder diagnostic {name} is not text")
            fields[name] = value[:limit]
    line = raw.get("line")
    if isinstance(line, int) and not isinstance(line, bool) and line >= 1:
        fields["line"] = line
    try:
        return BuildDiagnostic.model_validate(fields)
    except ValidationError as error:
        raise WidgetBuildFailedError("a builder diagnostic is malformed") from error


def _preview(report: dict[str, Any], selection: Mapping[str, str]) -> WidgetPreview:
    value = report.get("preview")
    sizes = report["manifest"].get("sizes")
    if not isinstance(value, dict) or not isinstance(sizes, list) or not sizes:
        raise WidgetBuildFailedError("The preview response is missing.")
    fixture, size, text, encoded = (value.get(k) for k in ("fixture", "size", "text", "png"))
    if (
        fixture
        != ("live" if "live" in selection else selection.get("fixture", "fixtures/typical.json"))
        or size != selection.get("size", sizes[0])
        or not isinstance(size, str)
        or size not in {"sm", "md", "lg", "tall", "wide"}
        or not isinstance(text, str)
        or len(text) > 1000
        or not isinstance(encoded, str)
        or len(encoded) > 699052
    ):
        raise WidgetBuildFailedError("The preview does not match the requested fixture and size.")
    try:
        png = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise WidgetBuildFailedError("Invalid preview encoding.") from exc
    if (
        len(png) > 524288
        or not png.startswith(bytes.fromhex("89504e470d0a1a0a0000000d49484452000003c00000021c"))
        or not png.endswith(bytes.fromhex("0000000049454e44ae426082"))
    ):
        raise WidgetBuildFailedError("Invalid preview PNG or dimensions.")
    return WidgetPreview(fixture=fixture, size=size, text=text, png=png)


def _verdict(
    slug: str, result: ExecutionResult, preview: Mapping[str, str] | None = None
) -> WidgetBuild:
    if result.timed_out or result.killed_by is not None or result.stdout_truncated:
        raise WidgetBuildFailedError("the build did not finish within its limits")
    if result.exit_code not in (0, 1):
        raise WidgetBuildFailedError(f"the builder exited with {result.exit_code}")
    try:
        report = json.loads(result.stdout)
    except (UnicodeDecodeError, ValueError) as error:
        raise WidgetBuildFailedError("the builder report is not JSON") from error
    if not isinstance(report, dict) or report.get("slug") != slug:
        raise WidgetBuildFailedError("the builder report is for another package")
    ok = report.get("ok")
    if ok is not (result.exit_code == 0):
        raise WidgetBuildFailedError("the builder exit code and report disagree")
    raw_diagnostics = report.get("diagnostics")
    if not isinstance(raw_diagnostics, list):
        raise WidgetBuildFailedError("the builder report has no diagnostics list")
    diagnostics = tuple(_diagnostic(raw) for raw in raw_diagnostics)
    if not ok:
        if not diagnostics:
            raise WidgetBuildFailedError("the builder rejected the package without a reason")
        return WidgetBuild(ok=False, diagnostics=diagnostics)
    bundle, digest, kit_api, manifest = (
        report.get(name) for name in ("bundle", "sha256", "kit_api", "manifest")
    )
    if diagnostics or not isinstance(bundle, str) or not isinstance(manifest, dict):
        raise WidgetBuildFailedError("the builder accepted the package without its bundle")
    encoded = bundle.encode("utf-8")
    if len(encoded) > WIDGET_BUNDLE_MAX_BYTES:
        raise WidgetBuildFailedError("the bundle exceeds the size cap")
    if digest != hashlib.sha256(encoded).hexdigest():
        raise WidgetBuildFailedError("the bundle does not match its digest")
    if not isinstance(kit_api, int) or isinstance(kit_api, bool) or kit_api < 1:
        raise WidgetBuildFailedError("the builder report names no kit_api")
    return WidgetBuild(
        ok=True,
        diagnostics=(),
        kit_api=kit_api,
        bundle=bundle,
        bundle_sha256=digest,
        manifest=manifest,
        preview=None if preview is None else _preview(report, preview),
    )


class SandboxWidgetBuilder:
    """Run the builder CLI in a fresh builder sandbox for each package."""

    def __init__(
        self, manager: SandboxManager, ids: IdFactory, *, timeout_seconds: int = 120
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("the build timeout must be positive")
        self._manager = manager
        self._ids = ids
        self._timeout_seconds = timeout_seconds

    async def build(
        self,
        principal: Principal,
        slug: str,
        files: Mapping[str, str],
        *,
        screen: BuildScreen,
        preview: Mapping[str, str] | None = None,
    ) -> WidgetBuild:
        # The slug becomes an argument; anything else could read as an option.
        if not is_widget_slug(slug):
            raise ValueError("the package slug is not a widget slug")
        screened: dict[str, object] = {
            "names": list(screen.names),
            "settingsValues": list(screen.settings_values),
        }
        if screen.commit_message is not None:
            screened["commitMessage"] = screen.commit_message
        request = {"files": dict(files), "screen": screened}
        if preview is not None:
            request["preview"] = dict(preview)
        command = ExecutionCommand(
            argv=(*BUILDER_ARGV, slug, "--emit-bundle"),
            working_directory=_WORKSPACE_ROOT,
            timeout_seconds=self._timeout_seconds,
            stdin=json.dumps(request, separators=(",", ":")).encode("utf-8"),
            maximum_output_bytes=_MAXIMUM_OUTPUT_BYTES,
        )
        build_id = self._ids.new_id()
        try:
            result = await self._manager.execute_for(principal.tenant_id, build_id, 0, command)
        finally:
            await self._manager.release_run(build_id)
        return _verdict(slug, result, preview)
