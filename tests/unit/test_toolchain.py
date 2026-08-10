"""Repository-foundation smoke tests."""

import importlib
import socket
import subprocess
import tomllib
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from alembic.config import Config
from typer.testing import CliRunner

import agent_core.cli.main as cli_main
import scripts.check_production_deployment as production_check
from agent_core.cli.main import app
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import AssistantMessage, TextPart
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.views import PersistedStreamFrame
from tests.conftest import NETWORK_MODE, _integration_endpoints
from tests.contract.support import run

ROOT = Path(__file__).resolve().parents[2]


def test_static_suite_blocks_network_egress() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
        pytest.raises(RuntimeError, match=r"blocked network.*203\.0\.113\.1"),
    ):
        client.connect(("203.0.113.1", 443))


@pytest.mark.parametrize("entrypoint", ["connect_ex", "sendto", "sendmsg"])
def test_static_suite_blocks_all_socket_egress_entrypoints(entrypoint: str) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
        pytest.raises(RuntimeError, match=r"blocked network.*203\.0\.113\.1"),
    ):
        if entrypoint == "connect_ex":
            client.connect_ex(("203.0.113.1", 443))
        elif entrypoint == "sendto":
            client.sendto(b"probe", ("203.0.113.1", 443))
        else:
            client.sendmsg([b"probe"], [], 0, ("203.0.113.1", 443))


def test_integration_mode_permits_only_configured_database_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://127.0.0.1:9/agent")
    _integration_endpoints.cache_clear()
    token = NETWORK_MODE.set("integration")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.connect_ex(("127.0.0.1", 9))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            assert client.sendto(b"probe", ("127.0.0.1", 9)) == 5
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            assert client.sendmsg([b"probe"], [], 0, ("127.0.0.1", 9)) == 5
    finally:
        NETWORK_MODE.reset(token)
        _integration_endpoints.cache_clear()


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
        "production-check",
        "docs",
    ):
        assert f"{target}:" in text

    assert "docker inspect --format '{{.State.Health.Status}}'" in text
    assert "docker compose ps --status healthy" not in text


def test_production_deployment_assets_preserve_process_boundaries() -> None:
    deploy = ROOT / "deploy"
    environment = (deploy / "veetbot.env.example").read_text(encoding="utf-8")
    assert "DEPLOYMENT_MODE=production" in environment
    assert "AUTH_MODE=token" in environment
    assert "SANDBOX_MECHANISM=gvisor" in environment
    assert "AGENT_ARTIFACT_ROOT=/var/lib/veetbot/artifacts" in environment
    assert "REQUIRED_RANDOM_TOKEN" in environment
    assert "POSTGRES_PORT=REQUIRED_FREE_LOOPBACK_PORT" in environment
    assert environment.count("REQUIRED_FREE_LOOPBACK_PORT") == 2

    production_compose = yaml.safe_load(
        (deploy / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    assert production_compose == {"services": {"postgres": {"restart": "unless-stopped"}}}

    units = deploy / "systemd"
    api = (units / "veetbot-api.service").read_text(encoding="utf-8")
    worker = (units / "veetbot-worker.service").read_text(encoding="utf-8")
    maintenance = (units / "veetbot-maintenance.service").read_text(encoding="utf-8")
    assert "agent api" in api
    assert "SupplementaryGroups=docker" not in api
    assert "agent worker --role worker" in worker
    assert "SupplementaryGroups=docker" in worker
    assert "agent worker --role maintenance" in maintenance
    assert "SupplementaryGroups=docker" not in maintenance
    assert all(
        "EnvironmentFile=/etc/veetbot/veetbot.env" in unit for unit in (api, worker, maintenance)
    )

    caddy = (deploy / "Caddyfile.example").read_text(encoding="utf-8")
    assert "reverse_proxy 127.0.0.1:8000" in caddy
    assert "flush_interval -1" in caddy


def test_production_preflight_normalizes_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", missing)
    result = production_check._run("missing-command")
    assert result.returncode == 127
    assert "missing" in result.stderr


def test_production_preflight_normalizes_command_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(("slow-command",), 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = production_check._run("slow-command", timeout=1)
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_compose_has_one_healthy_postgres_service() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"postgres"}
    postgres = compose["services"]["postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "${POSTGRES_DB:-agent}",
        "POSTGRES_USER": "${POSTGRES_USER:-agent}",
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:-agent}",
    }
    assert postgres["ports"] == ["127.0.0.1:${POSTGRES_PORT:-5432}:5432"]
    assert postgres["volumes"] == ["agent-pgdata:/var/lib/postgresql/data"]
    assert postgres["healthcheck"] == {
        "test": [
            "CMD-SHELL",
            "pg_isready -U ${POSTGRES_USER:-agent} -d ${POSTGRES_DB:-agent}",
        ],
        "interval": "2s",
        "timeout": "2s",
        "retries": 30,
    }
    assert set(compose["volumes"]) == {"agent-pgdata"}


def test_ci_has_the_required_partitions() -> None:
    workflow_directory = ROOT / ".github" / "workflows"
    assert not list(workflow_directory.glob("*.yml"))
    assert not list(workflow_directory.glob("*.yaml"))

    config = yaml.safe_load((ROOT / ".circleci" / "config.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2.1
    jobs = config["jobs"]
    assert set(jobs) == {"static", "contract", "integration", "sandbox", "live"}
    for name, job in jobs.items():
        if name == "sandbox":
            assert job["machine"] == {"image": "ubuntu-2404:current"}
            continue
        assert job["docker"][0]["image"] == "cimg/python:3.12"

    postgres = jobs["integration"]["docker"][1]
    assert postgres == {
        "image": "postgres:16-alpine",
        "environment": {
            "POSTGRES_DB": "agent",
            "POSTGRES_USER": "agent",
            "POSTGRES_PASSWORD": "agent",
        },
    }

    commands = {}
    for name, job in jobs.items():
        commands[name] = [
            step["run"]["command"]
            for step in job["steps"]
            if isinstance(step, dict) and "run" in step
        ]
    assert "make lint typecheck test-static docs-check" in commands["static"]
    assert "make test-contract" in commands["contract"]
    assert "make migrate test-integration" in commands["integration"]
    assert "make test-sandbox" in commands["sandbox"]
    assert "make test-live" in commands["live"]

    workflows = config["workflows"]
    assert set(workflows) == {"verify", "live_manual", "live_nightly"}
    assert workflows["verify"] == {
        "unless": "<< pipeline.parameters.run_live >>",
        "jobs": ["static", "contract", "integration", "sandbox"],
    }
    assert workflows["live_manual"]["when"] == "<< pipeline.parameters.run_live >>"
    nightly = workflows["live_nightly"]
    assert nightly["triggers"] == [
        {
            "schedule": {
                "cron": "17 7 * * *",
                "filters": {"branches": {"only": "main"}},
            }
        }
    ]
    assert config["commands"]["install_uv"]["steps"][0]["restore_cache"]["keys"][0].endswith(
        '{{ checksum "uv.lock" }}'
    )


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


def test_run_reserved_words_and_implicit_submission_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, object]] = []

    async def fake_submit(
        prompt: str,
        session_id: UUID | None,
        idempotency_key: str | None,
        model_policy: str | None,
    ) -> tuple[Run, list[PersistedStreamFrame]]:
        del idempotency_key, model_policy
        seen.append((prompt, session_id))
        completed = run(status=RunStatus.COMPLETED).model_copy(
            update={"final_message": "answer"}, deep=True
        )
        event = PersistedStreamFrame(
            sequence=1,
            event="run.completed",
            data={
                "final_message": AssistantMessage(content=[TextPart(text="answer")]).model_dump(
                    mode="json"
                )
            },
        )
        return completed, [event]

    async def fake_read(run_id: UUID, *, events: bool) -> str:
        seen.append(("events" if events else "get", run_id))
        return "read-result"

    monkeypatch.setattr(cli_main, "_submit", fake_submit)
    monkeypatch.setattr(cli_main, "_ephemeral_read", fake_read)
    runner = CliRunner()

    submitted = runner.invoke(app, ["run", "hello"])
    assert submitted.exit_code == 0
    assert submitted.stdout.strip() == "answer"

    literal_reserved = runner.invoke(app, ["run", "--", "get"])
    assert literal_reserved.exit_code == 0
    assert literal_reserved.stdout.strip() == "answer"

    session_id = "00000000-0000-0000-0000-000000000020"
    json_result = runner.invoke(
        app,
        ["run", "--session", session_id, "--json", "with options"],
    )
    assert json_result.exit_code == 0
    assert '"status":"COMPLETED"' in json_result.stdout

    run_id = "00000000-0000-0000-0000-000000000030"
    fetched = runner.invoke(app, ["run", "get", run_id])
    events = runner.invoke(app, ["run", "events", run_id])
    assert fetched.exit_code == events.exit_code == 0
    assert fetched.stdout.strip() == events.stdout.strip() == "read-result"
    assert [entry[0] for entry in seen] == [
        "hello",
        "get",
        "with options",
        "get",
        "events",
    ]
    assert seen[2][1] == UUID(session_id)

    conflicting_policy = runner.invoke(
        app,
        [
            "run",
            "--session",
            session_id,
            "--model-policy",
            "balanced",
            "with conflict",
        ],
    )
    assert conflicting_policy.exit_code == 2
    assert "--model-policy can only be used for a new session" in conflicting_policy.stderr


def test_run_reports_durable_id_when_wait_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    queued_id = UUID("00000000-0000-0000-0000-000000000040")

    async def timeout_submit(
        prompt: str,
        session_id: UUID | None,
        idempotency_key: str | None,
        model_policy: str | None,
    ) -> tuple[Run, list[EventEnvelope]]:
        del prompt, session_id, idempotency_key, model_policy
        raise cli_main.QueuedRunTimeoutError(queued_id)

    monkeypatch.setattr(cli_main, "_submit", timeout_submit)
    result = CliRunner().invoke(app, ["run", "queued work"])

    assert result.exit_code == 5
    assert "run did not reach a terminal state" in result.stderr
    assert "run queued" not in result.stderr
    assert str(queued_id) in result.stderr


def test_alembic_config_accepts_percent_encoded_database_url() -> None:
    database_url = "postgresql+asyncpg://" + "agent:p%40ss@localhost/agent"
    config = Config()
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    assert config.get_main_option("sqlalchemy.url") == database_url


def test_eval_command_normalizes_lazy_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_import(_name: str) -> object:
        raise ImportError("eval dependency unavailable")

    monkeypatch.setattr(importlib, "import_module", fail_import)
    result = CliRunner().invoke(app, ["eval", "run"])
    assert result.exit_code == 1
    assert "evaluation failed: eval dependency unavailable" in result.stderr
