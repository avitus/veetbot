"""Milestone 6 structural gates for the isolated execution boundary."""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args, get_type_hints
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import artifact_storage_key
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.docker import DockerExecutionEnvironment
from agent_core.domain.execution import (
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionResult,
    FileChange,
)
from agent_core.execution.egress_core import address_is_public
from agent_core.execution.environment import build_sandbox_environment

ROOT = Path(__file__).resolve().parents[2]


def test_no_runtime_in_worker() -> None:
    forbidden_import_roots = {"docker", "firecracker", "kubernetes", "libvirt", "subprocess"}
    forbidden_call_prefixes = (
        "asyncio.create_subprocess_",
        "os.exec",
        "os.fork",
        "os.popen",
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "subprocess.",
    )
    findings: list[str] = []
    for package in ("runtime", "tools"):
        for path in (ROOT / "src" / "agent_core" / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases: dict[str, set[str]] = {}
            for statement in ast.walk(tree):
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        aliases.setdefault(alias.asname or alias.name.split(".", 1)[0], set()).add(
                            alias.name
                        )
                elif isinstance(statement, ast.ImportFrom):
                    for alias in statement.names:
                        aliases.setdefault(alias.asname or alias.name, set()).add(
                            f"{statement.module or ''}.{alias.name}"
                        )

            def qualified(node: ast.expr, current_aliases: dict[str, set[str]]) -> set[str]:
                if isinstance(node, ast.Name):
                    return current_aliases.get(node.id, {node.id})
                if isinstance(node, ast.Attribute):
                    return {
                        f"{prefix}.{node.attr}" for prefix in qualified(node.value, current_aliases)
                    }
                return {ast.unparse(node)}

            for candidate in ast.walk(tree):
                if isinstance(candidate, (ast.Import, ast.ImportFrom)):
                    modules = (
                        [alias.name for alias in candidate.names]
                        if isinstance(candidate, ast.Import)
                        else [candidate.module or ""]
                    )
                    if any(module.split(".", 1)[0] in forbidden_import_roots for module in modules):
                        findings.append(f"{path}:{candidate.lineno}: runtime import")
                if isinstance(candidate, ast.Call):
                    targets = qualified(candidate.func, aliases)
                    if any(target.startswith(forbidden_call_prefixes) for target in targets):
                        findings.append(f"{path}:{candidate.lineno}: runtime process spawn")
    assert findings == []


def test_spec_has_no_host_path() -> None:
    def contains_host_path(annotation: object) -> bool:
        if annotation is Path:
            return True
        return any(contains_host_path(item) for item in get_args(annotation))

    for value_type in (EnvironmentSpec, EnvironmentHandle, ExecutionResult, FileChange):
        hints = get_type_hints(value_type)
        annotations = {field.name: hints[field.name] for field in fields(value_type)}
        assert not {
            name: annotation
            for name, annotation in annotations.items()
            if contains_host_path(annotation)
        }
        assert all("host_path" not in name and "container_id" not in name for name in annotations)


def test_artifact_key_opaque() -> None:
    assert tuple(inspect.signature(artifact_storage_key).parameters) == (
        "tenant_id",
        "artifact_id",
    )
    tree = ast.parse(inspect.getsource(artifact_storage_key))
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "filename" not in referenced
    assert "media_type" not in referenced
    assert "metadata" not in referenced


def test_secret_like_passthrough_names_fail_closed() -> None:
    with pytest.raises(ValueError, match="tier-0"):
        build_sandbox_environment({"PRIVATE_SERVICE_TOKEN": "secret"}, ("PRIVATE_SERVICE_TOKEN",))
    with pytest.raises(ValueError, match="tier-1"):
        build_sandbox_environment({"UNREVIEWED_VALUE": "value"}, ("UNREVIEWED_VALUE",))
    with pytest.raises(ValueError, match="credential-bearing"):
        build_sandbox_environment(
            {"HTTPS_PROXY": "http://operator@proxy.example:3128"},
            ("HTTPS_PROXY",),
        )
    assert build_sandbox_environment({"TZ": "UTC"}, ("TZ",))["TZ"] == "UTC"


@pytest.mark.parametrize(
    "address",
    ("198.18.0.1", "224.0.0.1", "255.255.255.255", "::", "ff02::1", "::ffff:127.0.0.1"),
)
def test_special_and_multicast_addresses_are_not_public(address: str) -> None:
    assert address_is_public(address) is False


async def test_reaper_grace_protects_a_new_lease_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FixedClock(now)
    run_id = UUID(int=91)
    line = (
        f"container\tenvironment\t{run_id}\t1\t"
        f"{int((now + timedelta(minutes=5)).timestamp())}\t{int(now.timestamp())}\n"
    ).encode()
    operations: list[tuple[str, ...]] = []

    async def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
        del stdin
        operations.append(arguments)
        return line if arguments[0] == "ps" else b""

    monkeypatch.setattr("agent_core.adapters.execution.docker._docker", docker)
    adapter = DockerExecutionEnvironment(clock, SequenceIdFactory(), reaper_grace_seconds=60)
    assert await adapter.reap(frozenset()) == 0
    assert [operation[0] for operation in operations] == ["ps"]
    clock.advance(timedelta(seconds=61))
    assert await adapter.reap(frozenset()) == 1
    assert "rm" in [operation[0] for operation in operations]


async def test_reaper_cleans_legacy_containers_without_creation_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    line = (
        f"container\tenvironment\t{UUID(int=92)}\t1\t"
        f"{int((now + timedelta(minutes=5)).timestamp())}\t\n"
    ).encode()
    operations: list[tuple[str, ...]] = []

    async def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
        del stdin
        operations.append(arguments)
        return line if arguments[0] == "ps" else b""

    monkeypatch.setattr("agent_core.adapters.execution.docker._docker", docker)
    adapter = DockerExecutionEnvironment(
        FixedClock(now), SequenceIdFactory(), reaper_grace_seconds=60
    )
    assert await adapter.reap(frozenset()) == 1
    assert "rm" in [operation[0] for operation in operations]
