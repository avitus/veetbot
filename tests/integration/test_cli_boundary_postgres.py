"""The `agent` command line drives the shared services on PostgreSQL.

Guards docs/plan/bootstrap-and-composition.md "The CLI contract" ("The
twelve", "Options" and "Exit codes") where the unit suite cannot: every unit
case routes the command's composition to memory or replaces the submission
itself, so nothing there runs a real command against durable shared state.
Here each invocation builds its own PostgreSQL composition, as the installed
command does, and a separate worker composition executes the queued run, so
the state one command leaves is what the next command and the HTTP API read.

- ``agent session create`` prints only the session identifier, and the API
  serves that session;
- ``agent run`` whose local wait expires exits 5 with the durable run
  identifier and neither cancels nor fails the run; a retried submission with
  the same key names the same run;
- once a worker has executed it, the same submission prints the final message
  on stdout and the six lines of flow on stderr; ``run get`` and ``run
  events`` print the record and the trace the API serves;
- a run suspended on an approval exits 3 with its identifier, ``approval
  list`` shows it, ``approval approve`` resolves it and the run continues;
- an unknown run, an unknown approval or a decision that contradicts the
  first resolution exits 1, a malformed identifier exits 2, and ``run
  cancel`` prints the resulting status.
"""

from __future__ import annotations

import asyncio
import json
import re
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import agent_core.bootstrap as bootstrap
import agent_core.cli.main as cli_main
from agent_core.bootstrap import build
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

runner = CliRunner()
QUESTION = "What is 17 multiplied by 23?"
ANSWER = "17 multiplied by 23 is 391."
UNREACHED = re.compile(r"run did not reach a terminal state: ([0-9a-f-]{36})")


@pytest.fixture(autouse=True)
def cli_reads_the_disposable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every command the scratch database as its loaded configuration."""
    settings = database_settings()
    monkeypatch.setattr(bootstrap, "load_settings", lambda: settings)


def _agent(*arguments: str) -> tuple[int, str, str]:
    result = runner.invoke(cli_main.app, list(arguments))
    return result.exit_code, result.stdout, result.stderr


def _work(script: FakeModelScript, worker_id: str) -> bool:
    """One pass of a worker process whose model answers from ``script``."""

    async def once() -> bool:
        async with build(settings=database_settings(), storage="postgres", script=script) as app:
            return await run_worker_once(app, worker_id)

    return asyncio.run(once())


def _api_get(path: str) -> tuple[int, dict[str, object]]:
    async def read() -> tuple[int, dict[str, object]]:
        async with (
            build(settings=database_settings(), storage="postgres") as app,
            api_client(app) as client,
        ):
            response = await client.get(path)
            return response.status_code, response.json()

    return asyncio.run(read())


def _queued_run(session_id: str, key: str, prompt: str) -> str:
    """Submit with no worker running; the local wait expires and names the run."""
    code, stdout, stderr = _agent(
        "run", "--session", session_id, "--idempotency-key", key, "--wait-timeout", "0.3", prompt
    )
    assert code == 5, (code, stdout, stderr)
    assert stdout == ""
    named = UNREACHED.search(stderr)
    assert named is not None, stderr
    return named.group(1)


def test_a_run_submitted_from_the_command_line_is_executed_and_read_back() -> None:
    """Session, expired wait, worker, final message, record, trace and cancellation."""
    code, stdout, _stderr = _agent("session", "create")
    assert code == 0
    session_id = stdout.strip()
    assert stdout == session_id + "\n" and UUID(session_id)
    status, session = _api_get(f"/v1/sessions/{session_id}")
    assert status == 200 and session["id"] == session_id

    run_id = _queued_run(session_id, "multiply-once", QUESTION)
    assert _queued_run(session_id, "multiply-once", QUESTION) == run_id
    code, stdout, _stderr = _agent("run", "get", run_id)
    assert code == 0
    assert json.loads(stdout)["status"] == RunStatus.QUEUED.value

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "17 * 23"},
                        call_id="cli-multiply",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text=ANSWER, stop_reason=StopReason.END_TURN),
        ]
    )
    assert _work(script, "cli-journey-worker")

    # The retried submission finds the finished run: the answer is the whole
    # of stdout and the flow is on stderr.
    code, stdout, stderr = _agent(
        "run", "--session", session_id, "--idempotency-key", "multiply-once", QUESTION
    )
    assert code == 0, (stdout, stderr)
    assert stdout == ANSWER + "\n"
    assert stderr.splitlines() == [
        "run created",
        "model requests math.calculate",
        "tool executes",
        "tool result returned to model",
        "model produces final response",
        "run completes",
    ]
    code, stdout, _stderr = _agent(
        "run", "--session", session_id, "--idempotency-key", "multiply-once", "--json", QUESTION
    )
    assert code == 0
    assert json.loads(stdout)["id"] == run_id

    code, stdout, _stderr = _agent("run", "get", run_id)
    assert code == 0
    record = json.loads(stdout)
    assert (record["id"], record["status"]) == (run_id, RunStatus.COMPLETED.value)
    assert record["tool_call_count"] == 1
    status, served = _api_get(f"/v1/runs/{run_id}")
    assert status == 200
    assert {key: served[key] for key in ("id", "session_id", "status", "tool_call_count")} == {
        key: record[key] for key in ("id", "session_id", "status", "tool_call_count")
    }

    code, stdout, _stderr = _agent("run", "events", run_id)
    assert code == 0
    trace = json.loads(stdout)
    kinds = [frame["event"] for frame in trace]
    for earlier, later in (
        ("run.queued", "tool.call.started"),
        ("tool.call.started", "tool.call.completed"),
        ("tool.call.completed", "run.completed"),
    ):
        assert kinds.index(earlier) < kinds.index(later), kinds
    [completed] = [frame for frame in trace if frame["event"] == "run.completed"]
    assert completed["data"]["final_message"]["content"] == [{"kind": "text", "text": ANSWER}]
    assert [frame["sequence"] for frame in trace] == sorted(frame["sequence"] for frame in trace)

    # A queued run is cancelled by identifier, and no worker then finds it.
    doomed = _queued_run(session_id, "never-runs", "Summarize the week.")
    code, stdout, _stderr = _agent("run", "cancel", doomed)
    assert code == 0
    assert json.loads(stdout)["status"] == RunStatus.CANCELLED.value
    code, stdout, _stderr = _agent("run", "get", doomed)
    assert json.loads(stdout)["status"] == RunStatus.CANCELLED.value
    assert not _work(FakeModelScript(turns=[ScriptedTurn(text="unused")]), "cli-idle-worker")

    for command in ("get", "events", "cancel"):
        code, stdout, _stderr = _agent("run", command, str(uuid4()))
        assert (code, stdout) == (1, ""), command
    assert _agent("run", "get", "not-a-run-id")[0] == 2


def test_a_run_parked_on_an_approval_is_resolved_from_the_command_line() -> None:
    """Exit 3 names the run, the approval is listed and approved, and the run continues."""
    session_id = _agent("session", "create")[1].strip()
    prompt = "Record an external write."
    run_id = _queued_run(session_id, "write-once", prompt)
    code, stdout, _stderr = _agent("approval", "list")
    assert (code, json.loads(stdout)) == (0, [])

    # Each worker process replays its scripted model from the first turn, so
    # the process that parks the run and the one that resumes it get one each.
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
    assert _work(asks, "cli-approval-parker")

    code, stdout, _stderr = _agent(
        "run", "--session", session_id, "--idempotency-key", "write-once", prompt
    )
    assert (code, stdout) == (3, run_id + "\n")
    code, stdout, _stderr = _agent("approval", "list")
    assert code == 0
    [approval] = json.loads(stdout)
    assert approval["run_id"] == run_id
    assert approval["status"] == "PENDING"
    assert approval["tool_name"] == "demo.external_write"
    assert approval["arguments"]["content"] == "cli-approved"

    code, stdout, _stderr = _agent(
        "approval", "approve", approval["id"], "--reason", "expected write"
    )
    assert code == 0
    resolution = json.loads(stdout)
    assert resolution["decision"] == "approve_once"
    assert resolution["resolved_at"] is not None
    status, served = _api_get(f"/v1/approvals/{approval['id']}")
    assert status == 200 and served["status"] != "PENDING"
    assert _agent("approval", "list")[1].strip() == "[]"
    # The first resolution wins (docs/plan/policy-and-approvals.md,
    # "Resolution is idempotent, and the first one wins"): the same decision
    # again is the stored approval, a different one is refused, and so is an
    # approval that does not exist. A malformed identifier is a usage error.
    code, stdout, _stderr = _agent("approval", "approve", approval["id"])
    assert code == 0
    assert json.loads(stdout)["resolved_at"] == resolution["resolved_at"]
    code, stdout, stderr = _agent("approval", "deny", approval["id"])
    assert (code, stdout) == (1, "") and stderr.strip()
    code, stdout, _stderr = _agent("approval", "approve", str(uuid4()))
    assert (code, stdout) == (1, "")
    assert _agent("approval", "deny", "not-an-approval-id")[0] == 2

    assert _work(answers, "cli-approval-resumer")
    code, stdout, _stderr = _agent("run", "get", run_id)
    record = json.loads(stdout)
    assert (record["status"], record["tool_call_count"]) == (RunStatus.COMPLETED.value, 1)
    code, stdout, _stderr = _agent("run", "events", run_id)
    assert code == 0
    trace = json.loads(stdout)
    kinds = [frame["event"] for frame in trace]
    for earlier, later in (
        ("approval.requested", "approval.resolved"),
        ("approval.resolved", "tool.call.completed"),
        ("tool.call.completed", "run.completed"),
    ):
        assert kinds.index(earlier) < kinds.index(later), kinds
    [completed] = [frame for frame in trace if frame["event"] == "run.completed"]
    assert completed["data"]["final_message"]["content"] == [
        {"kind": "text", "text": "External write recorded."}
    ]
