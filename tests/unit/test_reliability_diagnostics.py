"""Failures remain diagnosable without persisting private exception content."""

import asyncio
import json
import logging
from uuid import UUID

import pytest

from agent_core.domain.failures import failure_context, failure_fields, log_context
from tests.gates.test_email_privacy_m26 import (
    DRAFT_CANARY,
    MAIL_CANARY,
    _hostile_mail,
    _internal_api_error,
)


@pytest.mark.parametrize("boundary", ["tool", "model", "api"])
async def test_failure_boundaries_record_safe_diagnostics(
    boundary: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify that failure boundaries record safe diagnostics."""
    caplog.set_level(logging.ERROR)
    if boundary == "api":
        await _internal_api_error()
    else:
        await _hostile_mail(failure=boundary)
    event = {
        "tool": "tool_execution_failed",
        "model": "run_execution_failed",
        "api": "api_request_failed",
    }[boundary]
    records = [record for record in caplog.records if record.getMessage() == event]
    assert records
    for record in records:
        fields = vars(record)
        assert fields.get("diagnostic_id"), "Failures need a support reference."
        UUID(fields["diagnostic_id"])
        assert len(fields["error_fingerprint"]) == 64
        assert fields["error_frames"], "Code locations must survive content suppression."
        assert fields["error_phase"]
        assert record.exc_info is None
        rendered = json.dumps(fields, default=str)
        assert MAIL_CANARY not in rendered and DRAFT_CANARY not in rendered
        assert "/Users/" not in json.dumps(fields["error_frames"])
        if boundary == "tool":
            assert fields["run_id"] and fields["tool_invocation_id"]


def test_fingerprint_does_not_depend_on_exception_text_and_chains_are_bounded() -> None:
    """Verify that fingerprint does not depend on exception text and chains are bounded."""

    class HostileError(Exception):
        def __str__(self) -> str:
            """Fail if diagnostics attempt to render the private exception text."""
            raise AssertionError("Diagnostic formatting must never call str(exception).")

    first = HostileError("Private mail")
    second = HostileError("Different private mail")
    assert failure_fields(first, phase="test") == failure_fields(second, phase="test")
    first.__cause__ = first
    fields = failure_fields(first, phase="test")
    assert fields["error_causes"] == ["HostileError"]
    chain: BaseException = ValueError("Private input")
    for _ in range(20):
        error = RuntimeError("Private output")
        error.__cause__ = chain
        chain = error
    assert len(failure_fields(chain, phase="test")["error_causes"]) == 5


async def test_log_scopes_restore_parent_and_isolate_parallel_tasks() -> None:
    """Verify that log scopes restore parent and isolate parallel tasks."""
    observed: list[str] = []

    async def task(name: str) -> None:
        """Verify nested and concurrent diagnostic scopes restore their parent identifiers."""
        with log_context(run_id=name):
            await asyncio.sleep(0)
            run_id = failure_context()["run_id"]
            assert isinstance(run_id, str)
            observed.append(run_id)
            with log_context(tool_invocation_id=name):
                await asyncio.sleep(0)
                assert failure_context()["run_id"] == name
        assert failure_context()["run_id"] == "parent"

    before = failure_context()
    with log_context(run_id="parent"):
        await asyncio.gather(task("first"), task("second"))
        assert "tool_invocation_id" not in failure_context()
        with pytest.raises(asyncio.CancelledError), log_context(run_id="cancelled"):
            raise asyncio.CancelledError
        assert failure_context()["run_id"] == "parent"
    assert set(observed) == {"first", "second"}
    assert failure_context() == before


def test_run_view_exposes_only_the_safe_diagnostic_reference() -> None:
    """Verify that run view exposes only the safe diagnostic reference."""
    from agent_core.application.public_services import _run_view
    from agent_core.domain.runs import FailureReason, RunFailure, RunStatus
    from tests.contract.support import NOW, run

    reference = UUID(int=42)
    failed = run(status=RunStatus.FAILED).model_copy(
        update={
            "failure": RunFailure(
                reason=FailureReason.INTERNAL_ERROR,
                error_class="RuntimeError",
                message="The task failed.",
                occurred_at=NOW,
                details={"diagnostic_id": str(reference), "private": "PRIVATE_CANARY"},
            )
        }
    )
    view = _run_view(failed)
    assert view.failure is not None
    assert getattr(view.failure, "diagnostic_id", None) == reference
    assert "PRIVATE_CANARY" not in view.model_dump_json()
    assert failed.failure is not None
    failed.failure.details["diagnostic_id"] = "Invalid historical reference"
    assert getattr(_run_view(failed).failure, "diagnostic_id", None) is None
