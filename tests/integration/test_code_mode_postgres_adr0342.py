"""Code mode end to end: a chat's script runs for real over HTTP and PostgreSQL.

Guards ADR-0342 and docs/plan/tool-system.md "Code mode (ADR-0342)" where the
unit and gate suites stop at an in-memory composition:

- "The script's view", "What a script can reach" and decision 12: a script
  written for the owner's request starts without a card, reaches a pinned tool
  by its own name and by the spelling a provider shows the model, and every
  record of the call carries the tool's own name.
- "Identity, output and trust" and "Budget": only what the script prints
  returns to the model, bridged calls count against the run's tool-call
  count, and the orchestration-only step is given back.
- "Approval" and decision 11, with docs/plan/http-api-and-streaming.md
  "Approvals": the card raised by outside content carries
  `script_request_grant_offer`; `approve_for_request` with
  `request_grant: {"tool_name": "sandbox.run_code"}` needs only
  `approval.resolve`, refuses a missing or different echo and leaves the card
  pending, is safe to retry, and lets the later scripts of the same run start
  under the one approval after a worker restart.

- docs/plan/event-log-and-persistence.md "Projections": a call a script made
  is recorded but adds nothing to the session's history, so a run that
  reloads its conversation after an approval can go on. Production runs
  failed there on 2026-11-24.

The scripts run through the same entry point the sandbox image runs, as a
subprocess whose bridge is a local socket, so each tool call crosses the real
bridge session and the real pipeline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from agent_core.evals.code_mode import ScriptExecutionEnvironment
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

TIME_TOOL = "system.current_time"
TOOLS = [SANDBOX_RUN_CODE_TOOL_NAME, TIME_TOOL]
GRANT_KIND = "script_request_grant"

# The model is shown `system_current_time`, never the dotted name (decision 12).
SHOWN_SPELLING_SCRIPT = """\
shown = tools.system_current_time(timezone="UTC")
own = tools.call("system.current_time", {"timezone": "UTC"})
try:
    tools.system_current(timezone="UTC")
except ToolCallError as refused:
    print(refused.status, refused.reason_code)
print("(UTC" in shown, "(UTC" in own)
"""


def _settings() -> Settings:
    return replace(database_settings(), code_mode_enabled=True)


def _script_turn(call_id: str, code: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ScriptedToolCall(
                name=SANDBOX_RUN_CODE_TOOL_NAME, arguments={"code": code}, call_id=call_id
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _final(text: str) -> ScriptedTurn:
    return ScriptedTurn(text=text, stop_reason=StopReason.END_TURN)


async def _ask(client: httpx.AsyncClient, text: str, key: str) -> UUID:
    created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert created.status_code == 201, created.text
    submitted = await client.post(
        f"/v1/sessions/{created.json()['id']}/messages",
        headers={"Idempotency-Key": key},
        json={"content": [{"type": "text", "text": text}]},
    )
    assert submitted.status_code == 202, submitted.text
    return UUID(submitted.json()["run_id"])


async def _frames(client: httpx.AsyncClient, run_id: UUID) -> list[tuple[str, dict[str, Any]]]:
    """The run's persisted events, as the public stream replays them."""
    stream = await client.get(f"/v1/runs/{run_id}/events", timeout=10.0)
    assert stream.status_code == 200, stream.text
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in stream.text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields and "data" in fields:
            frames.append((fields["event"], json.loads(fields["data"])))
    return frames


async def _pending(client: httpx.AsyncClient, run_id: UUID) -> list[dict[str, Any]]:
    listed = await client.get("/v1/approvals", params={"run_id": str(run_id)})
    assert listed.status_code == 200, listed.text
    items: list[dict[str, Any]] = listed.json()["items"]
    return items


def _build(script: FakeModelScript) -> Any:
    return build(
        settings=_settings(),
        storage="postgres",
        script=script,
        enabled_tools=TOOLS,
        fake_environment_factory=ScriptExecutionEnvironment,
    )


async def test_a_clean_script_reaches_tools_by_shown_name_and_returns_only_what_it_prints() -> None:
    """One script, three bridge requests, no card, one step and one printed answer."""
    model = FakeModelScript(
        turns=[_script_turn("call_script", SHOWN_SPELLING_SCRIPT), _final("It is the same time.")]
    )
    async with _build(model) as composition, api_client(composition) as client:
        run_id = await _ask(client, "Check the time two ways in one script.", "code-mode-clean")
        assert await run_worker_once(composition, "code-mode-clean")

        run = (await client.get(f"/v1/runs/{run_id}")).json()
        assert run["status"] == RunStatus.COMPLETED.value, run["failure"]
        assert await _pending(client, run_id) == []
        frames = await _frames(client, run_id)

    assert not [data for event, data in frames if event == "approval.requested"]
    started = [data["name"] for event, data in frames if event == "tool.call.started"]
    # Both spellings were recorded under the tool's own name. The name that
    # fits no pinned tool reached nothing.
    assert started == [SANDBOX_RUN_CODE_TOOL_NAME, TIME_TOOL, TIME_TOOL]
    completed = {
        data["call_id"]: data["result_item"]["content"][0]["text"]
        for event, data in frames
        if event == "tool.call.completed"
    }
    # The model receives what the script printed and nothing a call returned.
    assert completed.pop("call_script") == "denied policy.matrix.unknown_tool\nTrue True\n"
    assert len(completed) == 2 and all(call.startswith("bridge:") for call in completed)
    # The script and each call that got past the bridge's own refusal are
    # counted, the unknown name included: it was recorded before the pipeline
    # refused it. The run made two model requests, one for the script and one
    # for the answer below, and the orchestration-only step was given back.
    assert run["tool_call_count"] == 4
    assert (run["step_count"], run["model_call_count"]) == (1, 1)
    terminal = next(data for event, data in frames if event == "run.completed")
    assert terminal["final_message"]["content"] == [
        {"kind": "text", "text": "It is the same time."}
    ]


async def _park_on_the_second_script(
    composition: Composition, client: httpx.AsyncClient, key: str
) -> tuple[UUID, dict[str, Any]]:
    """Run a first script, which starts unasked, to the card the second raises."""
    run_id = await _ask(client, "Work it out with scripts.", key)
    assert await run_worker_once(composition, f"{key}-parker")
    parked = await client.get(f"/v1/runs/{run_id}")
    assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
    [card] = await _pending(client, run_id)
    assert card["tool_name"] == SANDBOX_RUN_CODE_TOOL_NAME
    assert card["arguments"]["code"] == "print('second')"
    return run_id, card


async def test_one_approval_over_http_covers_the_later_scripts_after_a_restart() -> None:
    """The owner allows the request once; no later script of the run shows a card."""
    first_claim = FakeModelScript(
        turns=[_script_turn("first", "print('first')"), _script_turn("second", "print('second')")]
    )
    async with _build(first_claim) as composition, api_client(composition) as client:
        run_id, card = await _park_on_the_second_script(composition, client, "code-mode-grant")
        approval_id = card["id"]
        assert card["script_request_grant_offer"] == {
            "tool_name": SANDBOX_RUN_CODE_TOOL_NAME,
            "duration_seconds": 7200,
            "summary": card["script_request_grant_offer"]["summary"],
        }
        assert card["script_request_grant_offer"]["summary"]
        resolve = f"/v1/approvals/{approval_id}/resolve"
        echo = {"tool_name": SANDBOX_RUN_CODE_TOOL_NAME}

        # Validation: the decision needs its echo, the echo belongs to no other
        # decision, and it names exactly one offer and only the script tool.
        for malformed in (
            {"decision": "approve_for_request"},
            {"decision": "approve_once", "request_grant": echo},
            {"decision": "approve_for_request", "request_grant": {**echo, "job_slug": "radar"}},
            {
                "decision": "approve_for_request",
                "request_grant": {"tool_name": "sandbox.run_command"},
            },
        ):
            refused = await client.post(resolve, json=malformed)
            assert refused.status_code == 400, refused.text
            assert refused.json()["error"]["code"] == "malformed_request"
        # An offer this card does not carry is unavailable, and the card stays pending.
        other = await client.post(
            resolve,
            json={"decision": "approve_for_request", "request_grant": {"job_slug": "radar"}},
        )
        assert other.status_code == 409, other.text
        assert other.json()["error"]["details"]["reason"] == "request_grant_unavailable"
        # Authorization: the resolution needs approval.resolve.
        reader = composition.principal.model_copy(
            update={"scopes": composition.principal.scopes - {"approval.resolve"}}
        )
        async with api_client(composition, principal=reader) as unscoped:
            refused = await unscoped.post(
                resolve, json={"decision": "approve_for_request", "request_grant": echo}
            )
        assert refused.status_code == 403, refused.text
        assert [item["id"] for item in await _pending(client, run_id)] == [approval_id]

        allowed = await client.post(
            resolve, json={"decision": "approve_for_request", "request_grant": echo}
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["decision"] == "approve_for_request"
        # A client's retry is safe; a change of mind is not silent.
        retried = await client.post(
            resolve, json={"decision": "approve_for_request", "request_grant": echo}
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["resolved_at"] == allowed.json()["resolved_at"]
        changed = await client.post(resolve, json={"decision": "deny"})
        assert changed.status_code == 409, changed.text
        assert changed.json()["error"]["details"]["reason"] == "approval_already_resolved"

    after_restart = FakeModelScript(
        turns=[
            _script_turn("third", "print('third')"),
            _script_turn("fourth", "print('fourth')"),
            _final("All four scripts ran."),
        ]
    )
    async with _build(after_restart) as composition, api_client(composition) as client:
        assert await run_worker_once(composition, "code-mode-grant-resumer")
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        assert run["status"] == RunStatus.COMPLETED.value, run["failure"]
        assert await _pending(client, run_id) == []
        frames = await _frames(client, run_id)
        events = await composition.runs.events(run_id)

    # One card for four scripts: the first needed none and the grant covered
    # the third and fourth.
    assert [event for event, _ in frames].count("approval.requested") == 1
    printed = [
        data["result_item"]["content"][0]["text"]
        for event, data in frames
        if event == "tool.call.completed"
    ]
    assert printed == ["first\n", "second\n", "third\n", "fourth\n"]
    assert run["tool_call_count"] == 4
    # The approval is the grant, recorded once, and each use names it.
    created = [event for event in events if event.event_type == "script.request_grant.created"]
    assert len(created) == 1
    assert created[0].payload["approval_id"] == approval_id
    uses = [
        event.payload
        for event in events
        if event.event_type == "tool.call.authorized"
        and event.payload.get("authorization_kind") == GRANT_KIND
    ]
    assert [use["authorization_ref"] for use in uses] == [approval_id, approval_id]
    assert [use["name"] for use in uses] == [SANDBOX_RUN_CODE_TOOL_NAME] * 2


async def test_approving_a_script_once_over_http_leaves_the_next_script_asking() -> None:
    """Approve once covers the script on the card and nothing after it."""
    model = FakeModelScript(
        turns=[
            _script_turn("first", "print('first')"),
            _script_turn("second", "print('second')"),
            _script_turn("third", "print('third')"),
            _final("Done."),
        ]
    )
    async with _build(model) as composition, api_client(composition) as client:
        run_id, card = await _park_on_the_second_script(composition, client, "code-mode-once")
        once = await client.post(
            f"/v1/approvals/{card['id']}/resolve", json={"decision": "approve_once"}
        )
        assert once.status_code == 200, once.text
        assert await run_worker_once(composition, "code-mode-once-second")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
        [next_card] = await _pending(client, run_id)
        assert next_card["id"] != card["id"]
        assert next_card["arguments"]["code"] == "print('third')"
        assert next_card["script_request_grant_offer"] is not None

        denied = await client.post(
            f"/v1/approvals/{next_card['id']}/resolve",
            json={"decision": "deny", "reason": "Two scripts are enough."},
        )
        assert denied.status_code == 200, denied.text
        assert await run_worker_once(composition, "code-mode-once-denied")
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        events = await composition.runs.events(run_id)

    # The denied script never ran and the run still reached its answer.
    assert run["status"] == RunStatus.COMPLETED.value, run["failure"]
    assert [
        event.payload["call_id"] for event in events if event.event_type == "tool.call.completed"
    ] == ["first", "second"]
    assert not [event for event in events if event.event_type == "script.request_grant.created"]
    assert not [
        event
        for event in events
        if event.event_type == "tool.call.authorized"
        and event.payload.get("authorization_kind") == GRANT_KIND
    ]


async def test_a_run_goes_on_after_an_approval_that_follows_a_scripts_tool_calls() -> None:
    """A script calls a tool, the next script asks, and the run still answers."""
    model = FakeModelScript(
        turns=[
            _script_turn("first", 'tools.system_current_time(timezone="UTC")\nprint("first")'),
            _script_turn("second", 'tools.system_current_time(timezone="UTC")\nprint("second")'),
            _script_turn("third", "print('third')"),
            _final("All three scripts ran."),
        ]
    )
    async with _build(model) as composition, api_client(composition) as client:
        run_id = await _ask(client, "Work it out with scripts.", "code-mode-reload")
        assert await run_worker_once(composition, "code-mode-reload-parker")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
        [card] = await _pending(client, run_id)
        allowed = await client.post(
            f"/v1/approvals/{card['id']}/resolve",
            json={
                "decision": "approve_for_request",
                "request_grant": {"tool_name": SANDBOX_RUN_CODE_TOOL_NAME},
            },
        )
        assert allowed.status_code == 200, allowed.text
        # The resumed worker reloads the conversation from the session's history.
        assert await run_worker_once(composition, "code-mode-reload-resumer")
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        frames = await _frames(client, run_id)

    assert run["status"] == RunStatus.COMPLETED.value, run["failure"]
    printed = [
        data["result_item"]["content"][0]["text"]
        for event, data in frames
        if event == "tool.call.completed" and data["name"] == SANDBOX_RUN_CODE_TOOL_NAME
    ]
    assert printed == ["first\n", "second\n", "third\n"]
    # Both bridged calls are on the record and in the count; neither is history.
    assert [data["name"] for event, data in frames if event == "tool.call.completed"].count(
        TIME_TOOL
    ) == 2
    assert run["tool_call_count"] == 5
