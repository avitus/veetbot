"""Milestone 1 evaluation-harness hard gates."""

from __future__ import annotations

from pathlib import Path

from agent_core.config import (
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    SandboxMechanism,
    Settings,
    validate_runtime_identity,
)
from agent_core.evals.cases import load_cases
from agent_core.evals.fixtures import resolve_model_fixture
from agent_core.evals.runner import run_selected
from agent_core.tools.messages import TOOL_MESSAGES
from scripts.gate_registry import load_registry, registry_errors

ROOT = Path(__file__).resolve().parents[2]


def _production_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://database/prod",
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=None,
        sandbox=SandboxMechanism.MICROVM,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
    )


def test_no_eval_in_prod() -> None:
    settings = _production_settings()
    for values in (
        {"tenant_id": "tenant_eval", "principal_id": "user", "policy_profile": "default"},
        {"tenant_id": "tenant", "principal_id": "eval.standard", "policy_profile": "default"},
        {"tenant_id": "tenant", "principal_id": "user", "policy_profile": "eval.default"},
    ):
        try:
            validate_runtime_identity(settings, **values)
        except ConfigurationError:
            pass
        else:
            raise AssertionError(f"production accepted evaluation identity {values}")


def test_case_schema() -> None:
    cases = load_cases(ROOT / "tests" / "eval_cases")
    assert len(cases) == 26
    assert len({case.name for case in cases}) == len(cases)
    for case in cases:
        script = resolve_model_fixture(ROOT / "evals" / "fixtures" / "models", case.model_fixture)
        assert script.turns


async def test_no_egress() -> None:
    results = await run_selected(ROOT, current_milestone=1)
    assert len(results) == 11


def test_reason_code_table() -> None:
    cases = load_cases(ROOT / "tests" / "eval_cases")
    expectations = [
        expected
        for case in cases
        for expected in (
            [case.expected] if case.expected is not None else [arm.expected for arm in case.arms]
        )
    ]
    reason_codes = [code for expected in expectations for code in expected.reason_codes]
    assert reason_codes
    assert all(code in TOOL_MESSAGES for code in reason_codes)
    assert len({code: TOOL_MESSAGES[code] for code in reason_codes}) == len(set(reason_codes))
    assert all("{" not in TOOL_MESSAGES[code] for code in reason_codes)


def test_milestone_order() -> None:
    assert registry_errors(ROOT, current_milestone=1) == []
    entries, errors = load_registry(ROOT)
    assert errors == []
    active = [entry for entry in entries if entry.milestone <= 1]
    assert len(active) == 41
    assert sum(entry.milestone == 1 for entry in entries) == 28
    assert all(entry.check != "tests/gates/pending.py::pending_gate" for entry in active)

    map_text = (ROOT / "docs" / "plan" / "milestone-map.md").read_text(encoding="utf-8")
    for assignment in (
        "context-engine                 7  step 1 M1, steps 2-7 M7",
        "event-log-and-persistence      9  all M2, export step M3",
        "memory-formation               6  all M9",
        "memory-retrieval               7  all M9",
        "knowledge-documents            7  all M9",
        "model-gateway                 14  all M3",
    ):
        assert assignment in map_text
