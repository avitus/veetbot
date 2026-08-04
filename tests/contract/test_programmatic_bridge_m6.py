"""Programmatic bridge identity, token, cap, and approval-hold contract."""

import asyncio
import hashlib
import json

from agent_core.tools.bridge import ProgrammaticBridgeSession, bridge_call_id


async def test_bridge_counts_ordinals_and_synthesizes_replay_stable_ids() -> None:
    observed: list[str] = []

    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments
        observed.append(call_id)
        return {"status": "succeeded", "result": {"ok": True}, "secret": "dropped"}

    script_hash = hashlib.sha256(b"orchestration source").hexdigest()
    bridge = ProgrammaticBridgeSession(
        script_hash=script_hash,
        token="turn-token",
        dispatch=dispatch,
        maximum_calls=2,
    )
    denied = await bridge.handle(
        json.dumps(
            {"token": "wrong", "call": "workspace.read_text", "arguments": {}, "ordinal": 0}
        ).encode()
    )
    assert json.loads(denied)["reason_code"] == "bridge.unauthorized"
    first = await bridge.handle(
        json.dumps(
            {
                "token": "turn-token",
                "call": "workspace.read_text",
                "arguments": {"path": "notes.md"},
                "ordinal": 0,
            }
        ).encode()
    )
    assert json.loads(first) == {"status": "succeeded", "result": {"ok": True}}
    assert observed == [bridge_call_id(script_hash, 0)]


async def test_bridge_bounds_an_approval_hold() -> None:
    async def blocked(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments, call_id
        await asyncio.Event().wait()
        return {}

    bridge = ProgrammaticBridgeSession(
        script_hash=hashlib.sha256(b"script").hexdigest(),
        token="turn-token",
        dispatch=blocked,
        approval_hold_seconds=0.01,
    )
    response = await bridge.handle(
        json.dumps(
            {"token": "turn-token", "call": "demo.external_write", "arguments": {}, "ordinal": 0}
        ).encode()
    )
    assert json.loads(response)["reason_code"] == "bridge.approval_hold_expired"
