"""Run code.test's closed check in a fresh offline sandbox (ADR-0281).

Each check provisions its own sandbox from the release's code image and releases
it before returning, also when cancelled. The report comes from code the model
wrote, so a pass counts only when the exit code and every report field agree.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.code import CodeCheck, CodeCheckResult
from agent_core.domain.errors import ExecutionRejected, ExecutionUnavailable
from agent_core.domain.execution import (
    EgressPolicy,
    ExecutionCommand,
    ExecutionResult,
    ResourceLimits,
    SandboxPurpose,
)
from agent_core.execution.manager import SandboxManager, _WorkspaceEnvironment
from agent_core.ports.code import CodeCheckUnavailableError
from agent_core.ports.determinism import IdFactory

# Isolated mode: the entry point resolves nothing from the tree it is about to run.
CODE_CHECK_ARGV = ("/opt/code-sandbox/venv/bin/python", "-I", "/opt/code-sandbox/code_checks.py")
CODE_CHECK_TIMEOUT_SECONDS = 300
_WORKSPACE_ROOT = PurePosixPath(".")
_MAXIMUM_OUTPUT_BYTES = 128 * 1024
_MAXIMUM_OUTPUT_CHARACTERS = 6000


def code_check_sandbox(
    environment: _WorkspaceEnvironment,
    *,
    limits: ResourceLimits,
    image_digest: str | None = None,
    resolve_image_digest: Callable[[], Awaitable[str]] | None = None,
) -> SandboxManager:
    """The code image's own manager: no network, no inherited environment.

    A check keys its sandbox by a fresh id, because a manager refuses a lease it
    has released and a run may test twice. So no run lease holds it, and
    `purpose=code_check` tells the reaper to hold it to its own deadline.
    """

    return SandboxManager(
        environment,
        image_digest=image_digest,
        resolve_image_digest=resolve_image_digest,
        limits=replace(
            limits, wall_clock_seconds=max(limits.wall_clock_seconds, CODE_CHECK_TIMEOUT_SECONDS)
        ),
        egress=EgressPolicy(),
        parent_environment={},
        purpose=SandboxPurpose.CODE_CHECK,
    )


def _verdict(result: ExecutionResult) -> CodeCheckResult:
    if (
        result.timed_out
        or result.killed_by is not None
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise CodeCheckUnavailableError("The check did not finish within its limits.")
    if result.exit_code not in (0, 1):
        raise CodeCheckUnavailableError("The check exited with an unexpected status.")
    try:
        report = CodeCheckResult.model_validate(json.loads(result.stdout), strict=True)
    except (ValueError, ValidationError) as error:
        raise CodeCheckUnavailableError("The check returned a malformed report.") from error
    if report.passed is not (result.exit_code == 0) or (
        report.passed and (report.exit_code != 0 or report.timed_out)
    ):
        raise CodeCheckUnavailableError("The check's exit code and report disagree.")
    return report.model_copy(update={"output": report.output[-_MAXIMUM_OUTPUT_CHARACTERS:]})


class SandboxCodeChecker:
    """Implements CodeChecker. Bind it to `code_check_sandbox`."""

    def __init__(
        self,
        manager: SandboxManager,
        ids: IdFactory,
        *,
        timeout_seconds: int = CODE_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("the check timeout must be positive")
        self._manager = manager
        self._ids = ids
        self._timeout_seconds = timeout_seconds

    async def run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        files: Mapping[str, str | None],
        check: CodeCheck,
    ) -> CodeCheckResult:
        # The calling run neither holds nor shares the sandbox; see code_check_sandbox.
        del run_id, lease_epoch
        request = {
            "files": dict(files),
            "check": {
                "kind": check.kind.value,
                "targets": list(check.targets),
                "keyword": check.keyword,
            },
        }
        command = ExecutionCommand(
            argv=CODE_CHECK_ARGV,
            working_directory=_WORKSPACE_ROOT,
            timeout_seconds=self._timeout_seconds,
            stdin=json.dumps(request, separators=(",", ":")).encode(),
            maximum_output_bytes=_MAXIMUM_OUTPUT_BYTES,
        )
        check_id = self._ids.new_id()
        try:
            result = await self._manager.execute_for(tenant_id, check_id, 0, command)
        except (ExecutionRejected, ExecutionUnavailable) as error:
            raise CodeCheckUnavailableError("The check sandbox is unavailable.") from error
        finally:
            await self._manager.release_run(check_id)
        return _verdict(result)
