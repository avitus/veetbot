"""Milestone 6 structural gates for the isolated execution boundary."""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

from agent_core.adapters.artifacts.filesystem import artifact_storage_key
from agent_core.domain.execution import (
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionResult,
    FileChange,
)

ROOT = Path(__file__).resolve().parents[2]


def test_no_runtime_in_worker() -> None:
    forbidden_import_roots = {"docker", "firecracker", "kubernetes", "libvirt", "subprocess"}
    forbidden_calls = {
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.popen",
        "os.system",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.run",
    }
    findings: list[str] = []
    for package in ("runtime", "tools"):
        for path in (ROOT / "src" / "agent_core" / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = (
                        [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    if any(module.split(".", 1)[0] in forbidden_import_roots for module in modules):
                        findings.append(f"{path}:{node.lineno}: runtime import")
                if isinstance(node, ast.Call):
                    target = ast.unparse(node.func)
                    if target in forbidden_calls:
                        findings.append(f"{path}:{node.lineno}: runtime process spawn")
    assert findings == []


def test_spec_has_no_host_path() -> None:
    for value_type in (EnvironmentSpec, EnvironmentHandle, ExecutionResult, FileChange):
        annotations = {field.name: str(field.type) for field in fields(value_type)}
        assert not {
            name: annotation
            for name, annotation in annotations.items()
            if "pathlib.Path" in annotation or annotation in {"Path", "<class 'pathlib.Path'>"}
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
