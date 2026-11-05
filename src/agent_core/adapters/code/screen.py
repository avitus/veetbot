"""Screen text bound for the public repository, in the builder sandbox (ADR-0281).

Anything short of a clean, self-consistent verdict raises, so a proposal cannot
pass because the screen failed to run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.code import CodeFinding
from agent_core.domain.errors import ExecutionRejected, ExecutionUnavailable
from agent_core.domain.execution import ExecutionCommand, ExecutionResult
from agent_core.execution.manager import SandboxManager
from agent_core.ports.code import CodeScreenUnavailableError
from agent_core.ports.determinism import IdFactory

CODE_SCREEN_ARGV = ("node", "/opt/cockpit/builder/code-screen.mjs")
_TIMEOUT_SECONDS = 60
_MAXIMUM_OUTPUT_BYTES = 256 * 1024


def _findings(result: ExecutionResult) -> tuple[CodeFinding, ...]:
    """Parse the screen's report; anything short of a clean verdict fails closed."""
    if (
        result.timed_out
        or result.killed_by is not None
        or result.stdout_truncated
        or result.exit_code not in (0, 1)
    ):
        raise CodeScreenUnavailableError("The screen gave no verdict.")
    try:
        report = json.loads(result.stdout)
        findings = tuple(CodeFinding.model_validate(item) for item in report["findings"])
        verdict = report["ok"]
    except (ValueError, KeyError, TypeError, ValidationError) as error:
        raise CodeScreenUnavailableError("The screen returned a malformed report.") from error
    if verdict is not (result.exit_code == 0) or bool(findings) == verdict:
        raise CodeScreenUnavailableError("The screen's verdict disagrees with its findings.")
    return findings


class SandboxCodeScreen:
    """Implements CodeScreen. Bind it to the Cockpit builder's manager, which holds the program."""

    def __init__(self, manager: SandboxManager, ids: IdFactory) -> None:
        self._manager = manager
        self._ids = ids

    async def screen(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        added_lines: Mapping[str, str],
        commit_message: str,
        names: tuple[str, ...],
        settings_values: tuple[str, ...],
    ) -> tuple[CodeFinding, ...]:
        """Screen a draft's added lines and commit message in the offline builder sandbox."""
        # As the builder's: keyed by a fresh id and held to its own deadline, so
        # one run lease may screen more than once.
        del run_id, lease_epoch
        request = {
            "files": dict(added_lines),
            "screen": {
                "names": list(names),
                "settingsValues": list(settings_values),
                "commitMessage": commit_message,
            },
        }
        command = ExecutionCommand(
            argv=CODE_SCREEN_ARGV,
            working_directory=PurePosixPath("."),
            timeout_seconds=_TIMEOUT_SECONDS,
            stdin=json.dumps(request, separators=(",", ":")).encode(),
            maximum_output_bytes=_MAXIMUM_OUTPUT_BYTES,
        )
        screen_id = self._ids.new_id()
        try:
            result = await self._manager.execute_for(tenant_id, screen_id, 0, command)
        except (ExecutionRejected, ExecutionUnavailable) as error:
            raise CodeScreenUnavailableError("The screen sandbox is unavailable.") from error
        finally:
            await self._manager.release_run(screen_id)
        return _findings(result)
