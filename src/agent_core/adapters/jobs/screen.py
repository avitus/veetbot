"""Run the existing widget privacy screen over a job in the builder sandbox."""

import json
from collections.abc import Mapping
from pathlib import PurePosixPath

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic
from agent_core.domain.execution import ExecutionCommand
from agent_core.domain.jobs import JobBrokerError
from agent_core.execution.manager import SandboxManager
from agent_core.ports.cockpit import BuildScreen
from agent_core.ports.determinism import IdFactory


class SandboxJobScreen:
    def __init__(self, manager: SandboxManager, ids: IdFactory) -> None:
        self._manager = manager
        self._ids = ids

    async def check(
        self, principal: Principal, files: Mapping[str, str], *, screen: BuildScreen
    ) -> tuple[BuildDiagnostic, ...]:
        """Screen public package files in the builder sandbox and always release that sandbox."""
        build_id = self._ids.new_id()
        request = {
            "files": dict(files),
            "screen": {
                "names": screen.names,
                "settingsValues": screen.settings_values,
                "commitMessage": screen.commit_message,
            },
        }
        command = ExecutionCommand(
            argv=("node", "/opt/cockpit/builder/job-screen.mjs"),
            working_directory=PurePosixPath("."),
            timeout_seconds=30,
            stdin=json.dumps(request).encode(),
            maximum_output_bytes=64 * 1024,
        )
        try:
            result = await self._manager.execute_for(principal.tenant_id, build_id, 0, command)
        finally:
            await self._manager.release_run(build_id)
        if (
            result.timed_out
            or result.killed_by
            or result.stdout_truncated
            or result.exit_code not in (0, 1)
        ):
            raise JobBrokerError("job.screen_unavailable", retryable=True)
        try:
            report = json.loads(result.stdout)
            diagnostics = tuple(
                BuildDiagnostic.model_validate(item) for item in report["diagnostics"]
            )
            if report["ok"] is not (result.exit_code == 0) or bool(diagnostics) == report["ok"]:
                raise ValueError("Screen verdict disagrees with diagnostics.")
        except (ValueError, KeyError, TypeError):
            raise JobBrokerError("job.screen_unavailable", retryable=True) from None
        return diagnostics
