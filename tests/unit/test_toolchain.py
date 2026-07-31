"""Repository-foundation smoke tests."""

import socket
import tomllib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agent_core.cli.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_static_suite_blocks_network_egress() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
        pytest.raises(RuntimeError, match=r"blocked network.*203\.0\.113\.1"),
    ):
        client.connect(("203.0.113.1", 443))


def test_required_make_targets_exist() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in (
        "install",
        "format",
        "lint",
        "typecheck",
        "test",
        "check",
        "db-up",
        "migrate",
        "test-static",
        "test-contract",
        "test-fast",
        "test-integration",
        "test-live",
        "docs",
    ):
        assert f"{target}:" in text

    assert "docker inspect --format '{{.State.Health.Status}}'" in text
    assert "docker compose ps --status healthy" not in text


def test_compose_has_one_healthy_postgres_service() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"postgres"}
    postgres = compose["services"]["postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "agent",
        "POSTGRES_USER": "agent",
        "POSTGRES_PASSWORD": "agent",
    }
    assert postgres["ports"] == ["5432:5432"]
    assert postgres["volumes"] == ["agent-pgdata:/var/lib/postgresql/data"]
    assert postgres["healthcheck"] == {
        "test": ["CMD-SHELL", "pg_isready -U agent -d agent"],
        "interval": "2s",
        "timeout": "2s",
        "retries": 30,
    }
    assert set(compose["volumes"]) == {"agent-pgdata"}


def test_ci_has_the_four_required_partitions() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    assert set(jobs) == {"static", "contract", "integration", "live"}
    assert jobs["integration"]["services"]["postgres"]["image"] == "postgres:16-alpine"
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch", "schedule"}
    assert triggers["push"]["branches"] == ["main"]
    assert jobs["static"]["if"] == "github.event_name != 'schedule'"
    assert jobs["contract"]["if"] == "github.event_name != 'schedule'"
    assert jobs["integration"]["if"] == "github.event_name != 'schedule'"
    assert jobs["live"]["if"] == (
        "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    )

    commands = {
        name: [step["run"] for step in job["steps"] if "run" in step] for name, job in jobs.items()
    }
    assert "make lint typecheck test-static docs-check" in commands["static"]
    assert "make test-contract" in commands["contract"]
    assert "make migrate test-integration" in commands["integration"]
    assert "make test-live" in commands["live"]

    for job in jobs.values():
        python_steps = [
            step for step in job["steps"] if step.get("uses") == "actions/setup-python@v5"
        ]
        assert python_steps == [
            {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}}
        ]


def test_project_metadata_and_test_layout_match_the_toolchain_spec() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.12"
    assert project["project"]["scripts"]["agent"] == "agent_core.cli.main:app"
    assert set(project["dependency-groups"]) == {"dev", "test", "docs"}
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/agent_core"]
    assert project["tool"]["pytest"]["ini_options"]["addopts"] == (
        "--strict-markers --strict-config"
    )
    for directory in (
        "unit",
        "gates",
        "contract",
        "integration",
        "resilience",
        "security",
        "live",
        "eval_cases",
    ):
        assert (ROOT / "tests" / directory).is_dir()


def test_cli_entry_point_resolves() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0.dev0"
