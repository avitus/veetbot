"""Programmatic bridge identity, token, cap, and approval-hold contract."""

import asyncio
import hashlib
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

import pytest

from agent_core.tools.bridge import (
    BridgeProtocolError,
    ProgrammaticBridgeSession,
    UnixToolBridgeServer,
    bridge_call_id,
)

_TURN_TOKEN = secrets.token_urlsafe(16)


async def test_bridge_counts_ordinals_and_synthesizes_replay_stable_ids() -> None:
    observed: list[str] = []

    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments
        observed.append(call_id)
        return {"status": "succeeded", "result": {"ok": True}, "secret": "dropped"}

    script_hash = hashlib.sha256(b"orchestration source").hexdigest()
    bridge = ProgrammaticBridgeSession(
        script_hash=script_hash,
        token=_TURN_TOKEN,
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
                "token": _TURN_TOKEN,
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
                "token": _TURN_TOKEN,
                "call": "workspace.read_text",
                "arguments": {"path": "other.md"},
                "ordinal": 1,
            }
        ).encode()
    )
    assert json.loads(second)["status"] == "succeeded"
    capped = await bridge.handle(
        json.dumps(
            {"token": _TURN_TOKEN, "call": "workspace.read_text", "arguments": {}, "ordinal": 2}
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
        token=_TURN_TOKEN,
        dispatch=replay_dispatch,
        maximum_calls=2,
    )
    await replay.handle(
        json.dumps(
            {
                "token": _TURN_TOKEN,
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
        token=_TURN_TOKEN,
        dispatch=blocked,
        approval_hold_seconds=0.01,
    )
    response = await bridge.handle(
        json.dumps(
            {"token": _TURN_TOKEN, "call": "demo.external_write", "arguments": {}, "ordinal": 0}
        ).encode()
    )
    payload = json.loads(response)
    assert payload["reason_code"] == "bridge.approval_hold_expired"
    assert payload["retryable"] is False


async def test_unix_bridge_rejects_a_symlinked_socket_directory(tmp_path: Path) -> None:
    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments, call_id
        return {"status": "succeeded", "result": {}}

    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / ".agent").symlink_to(target, target_is_directory=True)
    session = ProgrammaticBridgeSession(
        script_hash=hashlib.sha256(b"script").hexdigest(),
        token=_TURN_TOKEN,
        dispatch=dispatch,
    )
    server = UnixToolBridgeServer(tmp_path / ".agent" / "bridge.sock", session)
    with pytest.raises(OSError):
        await server.start()


async def test_unix_bridge_starts_with_a_private_socket() -> None:
    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments, call_id
        return {"status": "succeeded", "result": {}}

    with tempfile.TemporaryDirectory(prefix="bridge-") as directory:
        socket_path = Path(directory) / "bridge.sock"
        session = ProgrammaticBridgeSession(
            script_hash=hashlib.sha256(b"script").hexdigest(),
            token=secrets.token_urlsafe(16),
            dispatch=dispatch,
        )
        server = UnixToolBridgeServer(socket_path, session)
        await server.start()
        try:
            assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o600
        finally:
            await server.close()


def test_bridge_call_id_is_the_documented_derivation() -> None:
    """tool-system.md: "bridge:" + sha256(script_hash NUL ordinal)[:32]."""

    script_hash = hashlib.sha256(b"orchestration source").hexdigest()
    material = script_hash.encode("ascii") + b"\x00" + b"3"

    assert bridge_call_id(script_hash, 3) == "bridge:" + hashlib.sha256(material).hexdigest()[:32]
    assert bridge_call_id(script_hash, 3) != bridge_call_id(script_hash, 30)
    assert bridge_call_id(script_hash, 0) != bridge_call_id(hashlib.sha256(b"x").hexdigest(), 0)


def test_bridge_session_requires_a_sha256_script_hash() -> None:
    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        raise AssertionError("unreachable")

    with pytest.raises(ValueError, match="SHA-256"):
        ProgrammaticBridgeSession(script_hash="abc", token=_TURN_TOKEN, dispatch=dispatch)


def _request(ordinal: object, *, token: str = _TURN_TOKEN, **fields: object) -> bytes:
    payload: dict[str, object] = {
        "token": token,
        "call": "workspace.read_text",
        "arguments": {"path": "notes.md"},
        "ordinal": ordinal,
    }
    payload.update(fields)
    return json.dumps(payload).encode()


async def test_bridge_counts_ordinals_itself_so_a_script_cannot_reuse_one() -> None:
    """A script that resends a consumed ordinal, or skips ahead, cannot inherit or
    forge a recorded call identity; nothing is dispatched and the count holds."""

    dispatched: list[str] = []

    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call, arguments
        dispatched.append(call_id)
        return {"status": "succeeded", "result": {}}

    script_hash = hashlib.sha256(b"script").hexdigest()
    bridge = ProgrammaticBridgeSession(
        script_hash=script_hash, token=_TURN_TOKEN, dispatch=dispatch
    )
    await bridge.handle(_request(0))

    for ordinal in (0, 2, -1, "1", None):
        with pytest.raises(BridgeProtocolError, match="ordinal"):
            await bridge.handle(_request(ordinal))

    assert dispatched == [bridge_call_id(script_hash, 0)]
    assert bridge.call_count == 1


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"x" * (64 * 1024 + 1),
        b"{not json",
        b"\xff\xfe",
        b"[]",
        _request(0, call=7),
        _request(0, arguments=["notes.md"]),
    ],
    ids=["oversized", "invalid_json", "not_utf8", "not_an_object", "call", "arguments"],
)
async def test_bridge_refuses_malformed_requests_before_dispatch(request_bytes: bytes) -> None:
    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        raise AssertionError("a malformed request reached dispatch")

    bridge = ProgrammaticBridgeSession(
        script_hash=hashlib.sha256(b"script").hexdigest(), token=_TURN_TOKEN, dispatch=dispatch
    )

    with pytest.raises(BridgeProtocolError):
        await bridge.handle(request_bytes)
    assert bridge.call_count == 0


async def test_bridge_checks_the_turn_token_before_the_request_shape() -> None:
    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        raise AssertionError("an unauthorized request reached dispatch")

    bridge = ProgrammaticBridgeSession(
        script_hash=hashlib.sha256(b"script").hexdigest(), token=_TURN_TOKEN, dispatch=dispatch
    )

    for request in (_request(9, token="stale"), _request(0, token=None), b'{"call": 1}'):  # type: ignore[arg-type]
        denied = json.loads(await bridge.handle(request))
        assert denied == {
            "status": "denied",
            "reason_code": "bridge.unauthorized",
            "retryable": False,
        }
    assert bridge.call_count == 0


async def test_unix_bridge_speaks_newline_json_and_maps_failures_to_denials() -> None:
    calls: list[str] = []

    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        del call_id
        calls.append(call)
        if call == "demo.explode":
            raise RuntimeError("internal detail that must not reach the script")
        return {"status": "succeeded", "result": {"echo": arguments}}

    with tempfile.TemporaryDirectory(prefix="bridge-") as directory:
        socket_path = Path(directory) / "bridge.sock"
        session = ProgrammaticBridgeSession(
            script_hash=hashlib.sha256(b"script").hexdigest(),
            token=_TURN_TOKEN,
            dispatch=dispatch,
        )
        server = UnixToolBridgeServer(socket_path, session)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(_request(0) + b"\n")
            writer.write(_request(0) + b"\n")
            writer.write(_request(1, call="demo.explode") + b"\n")
            await writer.drain()
            succeeded = json.loads(await reader.readline())
            protocol = json.loads(await reader.readline())
            internal = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()

            oversized_reader, oversized_writer = await asyncio.open_unix_connection(
                str(socket_path)
            )
            oversized_writer.write(b"x" * (64 * 1024 + 2) + b"\n")
            await oversized_writer.drain()
            too_large = json.loads(await oversized_reader.readline())
            assert await oversized_reader.read() == b""
            oversized_writer.close()
        finally:
            await server.close()
        assert not socket_path.exists()

    assert succeeded == {"status": "succeeded", "result": {"echo": {"path": "notes.md"}}}
    assert protocol["status"] == "denied"
    assert protocol["reason_code"] == "bridge.protocol_error"
    assert protocol["retryable"] is False
    assert internal == {
        "status": "denied",
        "reason_code": "bridge.internal_error",
        "retryable": False,
    }
    assert too_large["reason_code"] == "bridge.request_too_large"
    assert calls == ["workspace.read_text", "demo.explode"]
