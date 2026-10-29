"""Run saved job source through the existing sandbox execution boundary."""

import hashlib
import json
import secrets
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from agent_core.domain.execution import ExecutionCommand
from agent_core.domain.jobs import (
    JOB_INPUT_BYTES,
    JOB_RESULT_BYTES,
    JobExecutionError,
    JobProgram,
    JobResult,
)
from agent_core.execution.manager import SandboxManager
from agent_core.ports.jobs import JobDispatch
from agent_core.tools.bridge import ProgrammaticBridgeSession

_OUTPUT_BYTES = 128 * 1024


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class SandboxJobRunner:
    def __init__(
        self, manager: SandboxManager, *, timeout_seconds: int = 60, maximum_calls: int = 64
    ) -> None:
        if timeout_seconds <= 0 or maximum_calls <= 0:
            raise ValueError("Job time and broker call limits must be positive.")
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._maximum_calls = maximum_calls

    async def run(
        self,
        program: JobProgram,
        inputs: Mapping[str, Any],
        *,
        tenant_id: str,
        run_id: UUID,
        lease_epoch: int,
        dispatch: JobDispatch,
    ) -> JobResult:
        encoded_inputs = _json_bytes(dict(inputs))
        if len(encoded_inputs) > JOB_INPUT_BYTES:
            raise JobExecutionError("Job inputs exceed 64 KiB.")
        request = _json_bytes({"source": program.source, "inputs": dict(inputs)})
        if len(request) > 256 * 1024:
            raise JobExecutionError("Encoded job request exceeds 256 KiB.")

        async def declared_dispatch(
            call: str, arguments: dict[str, Any], call_id: str
        ) -> dict[str, Any]:
            if call not in program.calls:
                return {
                    "status": "denied",
                    "reason_code": "job.undeclared_call",
                    "retryable": False,
                }
            return await dispatch(call, arguments, call_id)

        bridge = ProgrammaticBridgeSession(
            script_hash=hashlib.sha256(program.source.encode()).hexdigest(),
            token=secrets.token_urlsafe(32),
            dispatch=declared_dispatch,
            maximum_calls=self._maximum_calls,
            approval_hold_seconds=self._timeout_seconds,
        )
        command = ExecutionCommand(
            argv=("python", "-m", "agent_core.execution.job_runner"),
            working_directory=PurePosixPath("."),
            timeout_seconds=self._timeout_seconds,
            stdin=request,
            maximum_output_bytes=_OUTPUT_BYTES,
        )
        try:
            result = await self._manager.execute_for(
                tenant_id, run_id, lease_epoch, command, bridge=bridge
            )
        finally:
            await self._manager.release_run(run_id, lease_epoch)
        if (
            result.timed_out
            or result.killed_by is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise JobExecutionError("Job exceeded its execution limits.")
        try:
            report = json.loads(result.stdout)
            if not isinstance(report, dict):
                raise ValueError("Missing job report.")
            if result.exit_code != 0 or report.get("ok") is not True:
                message = report.get("error", "Job did not complete.")
                raise JobExecutionError(str(message)[:2000])
            if "value" not in report or len(_json_bytes(report["value"])) > JOB_RESULT_BYTES:
                raise ValueError("Missing or oversized job result.")
        except JobExecutionError:
            raise
        except (TypeError, ValueError) as exc:
            raise JobExecutionError("Job returned an invalid or oversized JSON result.") from exc
        return JobResult(
            value=report["value"],
            diagnostics=result.stderr.decode("utf-8", errors="replace"),
            broker_calls=bridge.call_count,
        )
