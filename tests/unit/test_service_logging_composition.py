"""A long-running service configures structured logging at phase 1; a one-shot does not."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_core import bootstrap
from agent_core.bootstrap import build
from agent_core.cli import main as cli_main
from agent_core.config import DeploymentMode, load_settings
from tests.unit.test_config import base_environment

COMPOSITION_ROOTS = (
    "build",
    "build_schedule_worker",
    "build_notification_worker",
    "build_call_worker",
    "build_surface_worker",
)
SERVICE_ENTRY_POINTS = (
    "_serve_api",
    "_serve_worker",
    "call_worker_command",
    "call_ingress_command",
)


def _functions(module_file: str) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }


def _composition_calls(function: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in COMPOSITION_ROOTS
    ]


def _opts_in(call: ast.Call) -> bool:
    return any(
        keyword.arg == "service_logging"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


async def test_a_service_composition_configures_logging_and_a_one_shot_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[DeploymentMode] = []
    monkeypatch.setattr(bootstrap, "configure_logging", configured.append, raising=False)
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(settings=settings):
        pass
    assert configured == []

    async with build(settings=settings, service_logging=True):
        pass
    assert configured == [DeploymentMode.DEVELOPMENT]


def test_every_composition_root_configures_logging_behind_the_flag() -> None:
    functions = _functions(bootstrap.__file__)
    for name in COMPOSITION_ROOTS:
        root = functions[name]
        assert "service_logging" in {argument.arg for argument in root.args.kwonlyargs}, name
        passed_on = [
            node
            for node in ast.walk(root)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_configure_service_logging"
        ]
        assert len(passed_on) == 1, name


def test_every_service_entry_point_opts_in_and_no_one_shot_command_does() -> None:
    functions = _functions(cli_main.__file__)
    for name, function in functions.items():
        calls = _composition_calls(function)
        if name in SERVICE_ENTRY_POINTS:
            assert calls, name
            assert all(_opts_in(call) for call in calls), name
        else:
            assert not any(_opts_in(call) for call in calls), name
