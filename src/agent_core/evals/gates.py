"""Execute and report the canonical hard-gate registry by stable gate id."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

GateOutcome = Literal["pass", "fail", "pending"]
CheckExecutor = Callable[[Path, Sequence[str]], dict[str, tuple[bool, str]]]
GATE_PYTEST_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class GateStatus:
    id: str
    milestone: int
    kind: str
    check: str
    outcome: GateOutcome
    detail: str = ""


def _registry_module(repository_root: Path) -> Any:
    cached = sys.modules.get("_agent_gate_registry")
    if cached is not None:
        return cached
    registry_path = repository_root / "scripts" / "gate_registry.py"
    spec = importlib.util.spec_from_file_location("_agent_gate_registry", registry_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load gate registry implementation: {registry_path}")
    registry = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = registry
    spec.loader.exec_module(registry)
    return registry


def current_milestone(repository_root: Path) -> int:
    path = repository_root / "docs" / "status" / "project-state.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"cannot parse project-state.yaml: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("project"), dict):
        raise ValueError("project-state.yaml has no project mapping")
    value = loaded["project"].get("current_milestone")
    if type(value) is not int:
        raise ValueError("project-state.yaml has no integer current_milestone")
    return value


def maximum_milestone(repository_root: Path) -> int:
    """Return the highest milestone declared by the canonical registry."""

    registry = _registry_module(repository_root)
    entries, errors = registry.load_registry(repository_root)
    if errors:
        raise ValueError("invalid gate registry: " + "; ".join(dict.fromkeys(errors)))
    return int(max(entry.milestone for entry in entries))


def _execute_pytest_checks(
    repository_root: Path, checks: Sequence[str]
) -> dict[str, tuple[bool, str]]:
    with tempfile.TemporaryDirectory(prefix="agent-eval-gates-") as temporary:
        report_path = Path(temporary) / "report.json"
        environment = dict(os.environ)
        environment["AGENT_GATE_REPORT"] = str(report_path)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "agent_core.evals.gate_pytest_plugin",
                    "--tb=short",
                    "-q",
                    *checks,
                ],
                cwd=repository_root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=GATE_PYTEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return dict.fromkeys(checks, (False, "pytest gate execution timed out"))
        if not report_path.is_file():
            detail = (completed.stderr or completed.stdout or "pytest produced no report").strip()
            return dict.fromkeys(checks, (False, detail[-1000:]))
        raw: Any = json.loads(report_path.read_text(encoding="utf-8"))
        reports = raw.get("reports", {}) if isinstance(raw, dict) else {}
        results: dict[str, tuple[bool, str]] = {}
        for check in checks:
            matching = [
                outcome
                for nodeid, outcomes in reports.items()
                if isinstance(nodeid, str)
                and (nodeid == check or nodeid.startswith(f"{check}["))
                and isinstance(outcomes, list)
                for outcome in outcomes
                if isinstance(outcome, str)
            ]
            failed = "failed" in matching
            skipped = "skipped" in matching
            passed = "passed" in matching
            if failed:
                results[check] = (False, "pytest failure")
            elif skipped:
                results[check] = (False, "active gate skipped")
            elif not passed:
                results[check] = (False, "gate check was not collected")
            else:
                results[check] = (True, "")
        return results


def collect_status(
    repository_root: Path,
    *,
    milestone: int,
    area: str | None = None,
    execute: CheckExecutor = _execute_pytest_checks,
) -> list[GateStatus]:
    registry = _registry_module(repository_root)
    entries, load_errors = registry.load_registry(repository_root)
    errors = [*load_errors, *registry.registry_errors(repository_root, milestone)]
    if errors:
        raise ValueError("invalid gate registry: " + "; ".join(dict.fromkeys(errors)))
    selected = [entry for entry in entries if area is None or entry.id.startswith(f"gate.{area}.")]
    if not selected:
        raise ValueError(f"the gate registry has no area {area!r}")
    active_checks = sorted({entry.check for entry in selected if entry.milestone <= milestone})
    executed = execute(repository_root, active_checks) if active_checks else {}
    return [
        GateStatus(
            id=entry.id,
            milestone=entry.milestone,
            kind=entry.kind,
            check=entry.check,
            outcome=(
                "pending"
                if entry.milestone > milestone
                else "pass"
                if executed.get(entry.check, (False, "gate check was not executed"))[0]
                else "fail"
            ),
            detail=(
                ""
                if entry.milestone > milestone
                else executed.get(entry.check, (False, "gate check was not executed"))[1]
            ),
        )
        for entry in sorted(selected, key=lambda item: (item.milestone, item.id))
    ]
