"""Programmatic bridge identity, token, cap, and approval-hold contract."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from agent_core.tools.bridge import ProgrammaticBridgeSession, UnixToolBridgeServer, bridge_call_id


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
    second = await bridge.handle(
        json.dumps(
            {
                "token": "turn-token",
                "call": "workspace.read_text",
                "arguments": {"path": "other.md"},
                "ordinal": 1,
            }
        ).encode()
    )
    assert json.loads(second)["status"] == "succeeded"
    capped = await bridge.handle(
        json.dumps(
            {"token": "turn-token", "call": "workspace.read_text", "arguments": {}, "ordinal": 2}
        ).encode()
    )
    assert json.loads(capped)["reason_code"] == "bridge.call_limit"
    replay_observed: list[str] = []

    async def replay_dispatch(
        call: str, arguments: dict[str, object], call_id: str
    ) -> dict[str, object]:
        del call, arguments
        replay_observed.append(call_id)
        return {"status": "succeeded", "result": {}}

    replay = ProgrammaticBridgeSession(
        script_hash=script_hash,
        token="turn-token",
        dispatch=replay_dispatch,
        maximum_calls=2,
    )
    await replay.handle(
        json.dumps(
            {
                "token": "turn-token",
                "call": "workspace.read_text",
                "arguments": {"path": "notes.md"},
                "ordinal": 0,
            }
        ).encode()
    )
    assert observed == [bridge_call_id(script_hash, 0), bridge_call_id(script_hash, 1)]
    assert replay_observed == [observed[0]]


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


async def test_unix_bridge_rejects_a_symlinked_socket_directory(tmp_path: Path) -> None:
    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments, call_id
        return {"status": "succeeded", "result": {}}

    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / ".agent").symlink_to(target, target_is_directory=True)
    session = ProgrammaticBridgeSession(
        script_hash=hashlib.sha256(b"script").hexdigest(),
        token="turn-token",
        dispatch=dispatch,
    )
    server = UnixToolBridgeServer(tmp_path / ".agent" / "bridge.sock", session)
    with pytest.raises(OSError):
        await server.start()
