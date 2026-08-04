"""Milestone 8 deterministic MCP evaluation hard gate."""

from __future__ import annotations

import asyncio
import importlib
import socket
import subprocess
from pathlib import Path
from typing import Any, Never

import pytest

from agent_core.evals.cases import load_cases
from agent_core.evals.fixtures import resolve_mcp_fixture
from agent_core.evals.runner import run_selected

ROOT = Path(__file__).resolve().parents[2]


async def test_mcp_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_cases = [
        case
        for case in load_cases(ROOT / "tests" / "eval_cases")
        if case.milestone == 8 and case.fixtures.mcp_servers
    ]
    assert {case.name for case in mcp_cases} == {
        "mcp_tool_round_trip",
        "mcp_server_disconnects_mid_call",
    }
    for case in mcp_cases:
        for name in case.fixtures.mcp_servers:
            resolved = resolve_mcp_fixture(
                ROOT / "evals" / "fixtures" / "mcp",
                name,
                tenant_id="tenant_eval",
            )
            assert resolved.script.name == name

    # Import the composition graph before sealing I/O so import-time type
    # annotations in third-party SDKs do not observe the test doubles.
    importlib.import_module("agent_core.bootstrap")
    original_socket = socket.socket

    def blocked(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("deterministic MCP evaluation attempted external I/O")

    def guarded_socket(*args: Any, **kwargs: Any) -> socket.socket:
        family = args[0] if args else kwargs.get("family", socket.AF_INET)
        if family in {socket.AF_INET, socket.AF_INET6}:
            blocked()
        return original_socket(*args, **kwargs)

    async def blocked_async(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("deterministic MCP evaluation attempted external I/O")

    monkeypatch.setattr(socket, "socket", guarded_socket)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(asyncio, "open_connection", blocked_async)
    results = await run_selected(ROOT, current_milestone=8, tag="mcp")
    assert len(results) == 2
    assert all(result.run.final_message for result in results)
