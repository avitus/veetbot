"""`agent run` prints the answer of a run that was approved, on PostgreSQL.

Guards docs/plan/bootstrap-and-composition.md "The CLI contract": `agent run`
writes "the final message" to stdout and the flow to stderr, and exit 3 is
reserved for a run that is waiting for an approval. `--idempotency-key`
deduplicates a retried submission, so a retry of a run that parked on an
approval, was approved and then completed replays an event stream whose
history holds a `run.waiting_for_approval` frame. That frame is history, not
the run's state: the command reads on to the terminal frame and prints the
final message.
"""

from __future__ import annotations

import json

import pytest

from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from tests.integration.test_cli_boundary_postgres import _agent, _queued_run, _work
from tests.integration.test_cli_boundary_postgres import (
    cli_reads_the_disposable_database as cli_reads_the_disposable_database,
)

pytestmark = pytest.mark.integration


def test_a_retried_submission_prints_the_answer_of_a_run_that_was_approved() -> None:
    session_id = _agent("session", "create")[1].strip()
    prompt = "Record an external write."
    run_id = _queued_run(session_id, "write-once", prompt)
    asks = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "cli-approved"},
                        call_id="cli-approval",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    answers = FakeModelScript(
        turns=[ScriptedTurn(text="External write recorded.", stop_reason=StopReason.END_TURN)]
    )
    assert _work(asks, "approval-parker")
    [approval] = json.loads(_agent("approval", "list")[1])
    assert _agent("approval", "approve", approval["id"])[0] == 0
    assert _work(answers, "approval-resumer")
    assert json.loads(_agent("run", "get", run_id)[1])["status"] == "COMPLETED"

    code, stdout, stderr = _agent(
        "run", "--session", session_id, "--idempotency-key", "write-once", prompt
    )
    assert code == 0
    assert stdout == "External write recorded.\n", (stdout, stderr)
    assert stderr.splitlines()[-1] == "run completes", stderr
