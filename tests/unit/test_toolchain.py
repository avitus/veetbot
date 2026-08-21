"""Repository-foundation smoke tests."""

import asyncio
import importlib
import json
import os
import re
import socket
import subprocess
import tomllib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import yaml
from alembic.config import Config
from typer.testing import CliRunner

import agent_core.cli.main as cli_main
import scripts.check_production_deployment as production_check
from agent_core.bootstrap import build
from agent_core.cli.main import app
from agent_core.config import load_settings
from agent_core.domain.errors import ExportConsentError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import ConsolidationRun, MemoryEdit
from agent_core.domain.messages import AssistantMessage, TextPart
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.views import PersistedStreamFrame
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.conftest import NETWORK_MODE, _integration_endpoints
from tests.contract.memory_fixtures import memory, trace
from tests.contract.support import NOW, SESSION_ID, principal, run
from tests.integration.m2_support import memory_settings

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
        "test-apple",
        "test-apple-ui",
        "test-deploy",
        "production-check",
        "client-build",
        "docs",
    ):
        assert f"{target}:" in text

    assert "docker inspect --format '{{.State.Health.Status}}'" in text
    assert "docker compose ps --status healthy" not in text


def test_apple_target_declares_phone_and_tablet_orientations() -> None:
    project = (ROOT / "clients" / "apple" / "Veetbot.xcodeproj" / "project.pbxproj").read_text(
        encoding="utf-8"
    )
    phone = (
        "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone = "
        '"UIInterfaceOrientationPortrait UIInterfaceOrientationLandscapeLeft '
        'UIInterfaceOrientationLandscapeRight";'
    )
    tablet = (
        "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPad = "
        '"UIInterfaceOrientationPortrait UIInterfaceOrientationPortraitUpsideDown '
        'UIInterfaceOrientationLandscapeLeft UIInterfaceOrientationLandscapeRight";'
    )

    assert project.count(phone) == 2
    assert project.count(tablet) == 2


def test_production_environment_preserves_process_boundaries() -> None:
    deploy = ROOT / "deploy"
    environment = (deploy / "veetbot.env.example").read_text(encoding="utf-8")
    assert "DEPLOYMENT_MODE=production" in environment
    assert "AUTH_MODE=token" in environment
    assert "SANDBOX_MECHANISM=gvisor" in environment
    assert "AGENT_ARTIFACT_ROOT=/var/lib/veetbot/artifacts" in environment
    assert "REQUIRED_RANDOM_TOKEN" in environment
    assert "POSTGRES_PORT=REQUIRED_FREE_LOOPBACK_PORT" in environment
    assert environment.count("REQUIRED_FREE_LOOPBACK_PORT") == 2
    assert "COMPOSE_PROJECT_NAME=veetbot" in environment
    assert "PGSSLMODE=disable" in environment
    assert "WEB_SEARCH_PROVIDER=disabled" in environment
    assert "WEB_FETCH_PROVIDER=disabled" in environment
    assert "BROWSER_PROVIDER=disabled" in environment
    assert "BROWSER_PROFILE_SERVICE_URL=https://browser.veetbot.com" in environment
    assert "BROWSER_PROFILE_CEREMONY_BASE_URL=https://browser.veetbot.com" in environment
    assert "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE=" not in environment
    assert "AGENT_EXECUTION_SERVICE_SOCKET=/run/veetbot/execution.sock" in environment
    example_values = dict(
        line.split("=", 1)
        for line in environment.splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE" not in example_values
    assert example_values["BROWSER_PROFILE_SERVICE_AUTH_FILE"].endswith(
        "browser-profile-service-auth"
    )
    template_lines = environment.splitlines()
    assert "TAVILY_API_KEY=" in template_lines
    assert "FIRECRAWL_API_KEY=" in template_lines
    configured_scopes = next(
        line.removeprefix("AUTH_SCOPES=").split(",")
        for line in environment.splitlines()
        if line.startswith("AUTH_SCOPES=")
    )
    assert set(configured_scopes) <= PLATFORM_SCOPES


def test_deployment_validation_rejects_shared_browser_credential_path() -> None:
    shared_path = "/etc/veetbot/secrets/browser-profile-service-auth"
    shared = {
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": shared_path,
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": shared_path,
    }
    failures = production_check._browser_credential_failures(shared)
    assert failures
    assert "different files" in failures[0]

    distinct = {
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": (
            "/etc/veetbot/secrets/browser-control-plane-credential"
        ),
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": shared_path,
    }
    assert production_check._browser_credential_failures(distinct) == []
    assert production_check._browser_credential_failures({}) == []


def test_deployment_validation_rejects_aliased_browser_credential_paths(
    tmp_path: Path,
) -> None:
    # A `..` alias of the same file must be rejected like the literal path.
    dotted = {
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": (
            "/etc/veetbot/secrets/../secrets/browser-profile-service-auth"
        ),
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": "/etc/veetbot/secrets/browser-profile-service-auth",
    }
    assert production_check._browser_credential_failures(dotted)

    # A symlinked parent directory reaching the same file must be rejected.
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth").write_text("credential\n", encoding="utf-8")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    symlinked = {
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": str(tmp_path / "alias" / "auth"),
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": str(real / "auth"),
    }
    assert production_check._browser_credential_failures(symlinked)


def test_deployment_validation_rejects_hard_linked_browser_credential_paths(
    tmp_path: Path,
) -> None:
    # Hard links share an inode but resolve to distinct paths, so identity
    # must be compared with samefile when both files exist.
    original = tmp_path / "browser-profile-service-auth"
    original.write_text("credential\n", encoding="utf-8")
    linked = tmp_path / "browser-control-plane-credential"
    os.link(original, linked)
    hard_linked = {
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE": str(linked),
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": str(original),
    }
    assert production_check._browser_credential_failures(hard_linked)


def test_production_compose_preserves_browser_profile_isolation() -> None:
    deploy = ROOT / "deploy"
    production_compose = yaml.safe_load(
        (deploy / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    profile_service = production_compose["services"]["browser-profile-service"]
    assert profile_service["user"] == "65532:65532"
    assert profile_service["read_only"] is True
    assert profile_service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in profile_service["security_opt"]
    assert profile_service["networks"] == [
        "browser-profile-private",
        "browser-profile-egress",
    ]
    assert profile_service["ports"] == ["127.0.0.1:${BROWSER_PROFILE_PORT:-8081}:8080"]
    assert profile_service["pids_limit"] == 256
    assert profile_service["mem_limit"] == "1g"
    assert profile_service["cpus"] == 2.0
    assert profile_service["shm_size"] == "256m"
    assert profile_service["environment"] == {
        "BROWSER_PROFILE_SERVICE_AUTH_FILE": "/run/secrets/browser-profile-service-auth",
        "BROWSER_PROFILE_SESSION_SECRET_FILE": "/run/secrets/browser-profile-session-secret",
        "BROWSER_PROFILE_KEY_DIR": "/run/secrets/browser-profile-keys",
        "BROWSER_PROFILE_MATERIAL_ROOT": "/var/lib/veetbot/browser-profiles",
        "BROWSER_PROFILE_BIND_HOST": "0.0.0.0",  # noqa: S104 - boundary fixture
        "BROWSER_PROFILE_BIND_PORT": "8080",
        "BROWSER_PROFILE_CEREMONY_BASE_URL": "${BROWSER_PROFILE_CEREMONY_BASE_URL}",
    }
    mounts = profile_service["volumes"]
    assert mounts == [
        "${BROWSER_PROFILE_SERVICE_AUTH_FILE}:/run/secrets/browser-profile-service-auth:ro",
        "${BROWSER_PROFILE_SESSION_SECRET_FILE}:/run/secrets/browser-profile-session-secret:ro",
        "${BROWSER_PROFILE_KEY_DIR}:/run/secrets/browser-profile-keys:ro",
        "browser-profile-material:/var/lib/veetbot/browser-profiles",
    ]
    assert not any("docker.sock" in mount for mount in mounts)
    assert "postgres" not in profile_service.get("depends_on", {})
    assert production_compose["networks"]["browser-profile-private"]["internal"] is True
    assert production_compose["networks"]["browser-profile-egress"]["internal"] is False
    assert "browser-profile-material" in production_compose["volumes"]


def test_browser_profile_dockerfile_preserves_process_isolation() -> None:
    deploy = ROOT / "deploy"
    profile_dockerfile = (deploy / "browser-profile-service.Dockerfile").read_text(encoding="utf-8")
    assert "USER 65532:65532" in profile_dockerfile
    assert 'ENTRYPOINT ["browser-profile-service"]' in profile_dockerfile
    assert "execution/sandbox.Dockerfile" not in profile_dockerfile
    assert "uv sync --frozen --no-dev" in profile_dockerfile
    assert "pip install --no-cache-dir /opt/veetbot" not in profile_dockerfile
    assert "chmod 0700 /var/lib/veetbot/browser-profiles" in profile_dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in profile_dockerfile
    assert "playwright install --with-deps chromium" in profile_dockerfile


def test_systemd_units_preserve_role_boundaries() -> None:
    deploy = ROOT / "deploy"
    units = deploy / "systemd"
    api = (units / "veetbot-api.service").read_text(encoding="utf-8")
    worker = (units / "veetbot-worker.service").read_text(encoding="utf-8")
    async_worker = (units / "veetbot-async-worker.service").read_text(encoding="utf-8")
    execution = (units / "veetbot-execution.service").read_text(encoding="utf-8")
    maintenance = (units / "veetbot-maintenance.service").read_text(encoding="utf-8")
    scheduler = (units / "veetbot-schedule.service").read_text(encoding="utf-8")
    schedule_environment = (deploy / "veetbot-schedule.env.example").read_text(encoding="utf-8")
    assert "agent api" in api
    assert cli_main.API_BIND_HOST == "127.0.0.1"
    assert "SupplementaryGroups=docker" not in api
    assert "agent worker --role interactive" in worker
    assert "SupplementaryGroups=docker" not in worker
    assert "agent worker --role async" in async_worker
    assert "SupplementaryGroups=docker" not in async_worker
    assert "agent execution-service" in execution
    assert "--runtime" not in execution
    assert "User=veetbot-exec" in execution
    assert "SupplementaryGroups=docker" in execution
    assert "/run/docker.sock" in execution
    assert "/var/run/docker.sock" not in execution
    for unit in (api, worker, async_worker, maintenance, scheduler):
        assert "/run/docker.sock" not in unit
        assert "/var/run/docker.sock" not in unit
    assert "EnvironmentFile=" not in execution
    assert "veetbot-execution.service" in worker
    assert "veetbot-execution.service" in async_worker
    assert "agent worker --role maintenance" in maintenance
    assert "SupplementaryGroups=docker" not in maintenance
    assert "agent worker --role schedule" in scheduler
    assert "SupplementaryGroups=docker" not in scheduler
    assert "SANDBOX_MECHANISM=" not in schedule_environment
    assert "AGENT_EXECUTION_SERVICE_SOCKET=" not in schedule_environment
    assert "ReadWritePaths=" not in scheduler
    assert "EnvironmentFile=/etc/veetbot/veetbot-schedule.env" in scheduler
    assert "EnvironmentFile=/etc/veetbot/veetbot.env" not in scheduler
    assert "UnsetEnvironment=" not in scheduler
    assert "DATABASE_URL=" in schedule_environment
    assert "AGENT_SCHEDULE_WORKER_ENABLED=1" in schedule_environment
    assert not {
        "AUTH_TOKEN",
        "VEETBOT_OPENAI_KEY",
        "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE",
    } & {line.partition("=")[0] for line in schedule_environment.splitlines()}
    assert all(
        "EnvironmentFile=/etc/veetbot/veetbot.env" in unit
        for unit in (api, worker, async_worker, maintenance)
    )
    assert "EnvironmentFile=-/opt/veetbot/current/.release.env" in api
    credential_source = (
        "LoadCredential=browser-control-plane:/etc/veetbot/secrets/browser-control-plane-credential"
    )
    credential_environment = (
        "Environment=BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE=%d/browser-control-plane"
    )
    assert all(
        credential_source in unit and credential_environment in unit
        for unit in (api, worker, async_worker, maintenance)
    )
    assert credential_source not in execution
    assert credential_environment not in execution


def test_deployment_accounts_and_documentation_are_separated() -> None:
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    normalized = " ".join(deployment.split())

    assert "useradd --system --create-home --shell /bin/bash veetbot-deploy" in deployment
    assert "useradd --system --no-create-home --shell /usr/sbin/nologin veetbot" in deployment
    assert "useradd --system --no-create-home --shell /usr/sbin/nologin veetbot-exec" in deployment
    assert "usermod -aG docker veetbot-deploy" in deployment
    assert "usermod -aG docker veetbot-exec" in deployment
    assert "usermod -aG docker veetbot\n" not in deployment
    assert "chown -R veetbot-deploy:veetbot /opt/veetbot" in deployment
    assert "application services have no Docker-socket access" in normalized
    assert (
        "replace `/run/veetbot/execution.sock` with an authenticated network "
        "transport while preserving the `ExecutionEnvironment` port"
    ) in normalized
    assert "without changing the port" not in deployment
    assert "public key for the `veetbot-deploy` account" in deployment


def test_nginx_deployment_test_exercises_declared_gnu_toolchain() -> None:
    harness = (ROOT / "deploy" / "nginx" / "deploy.test.sh").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    for command in ("MV", "SHA256SUM", "TAR", "FIND", "FLOCK"):
        assert f"GNU_{command}=" in harness
        assert f"VEETBOT_TEST_GNU_{command}" in harness
    assert "brew install coreutils gnu-tar findutils util-linux" in deployment


def test_documentation_verification_reads_the_resolved_release_identity() -> None:
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert 'docs_release="$(readlink -f /opt/veetbot/docs/current)"' in deployment
    assert 'test "$(cat "$docs_release/release.txt")" = "$VEETBOT_RELEASE_ID"' in deployment


def test_manual_rollback_keeps_documentation_and_application_releases_aligned() -> None:
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    manual = deployment.partition("## Manual rollback")[2].partition("## Accepted limitations")[0]
    app_precondition = 'grep -Fqx "VEETBOT_RELEASE_ID=$target_id" "$target/.release.env"'
    docs_precondition = 'test "$(cat "$docs_target/release.txt")" = "$target_id"'
    image_validation = 'docker image inspect "agent-core-sandbox:$target_id"'
    image_tag = 'docker tag "agent-core-sandbox:$target_id" agent-core-sandbox:production'
    readiness_identity = 'tolower($1) == "x-veetbot-release"'
    unit_process_validation = 'test "$process_cwd" = "$target"'
    app_switch = 'mv -Tf "$app_next" /opt/veetbot/current'
    docs_switch = 'mv -Tf "$docs_next" /opt/veetbot/docs/current'
    required_units = (
        "veetbot-execution",
        "veetbot-maintenance",
        "veetbot-worker",
        "veetbot-async-worker",
        "veetbot-api",
    )

    assert 'docs_target="/opt/veetbot/docs/releases/$target_id"' in manual
    assert 'test -d "$docs_target"' in manual
    assert 'test -f "$docs_target/release.txt"' in manual
    assert app_precondition in manual
    assert docs_precondition in manual
    assert image_validation in manual
    assert image_tag in manual
    assert readiness_identity in manual
    assert "IGNORECASE" not in manual
    assert unit_process_validation in manual
    assert 'ln -s "$docs_target" "$docs_next"' in manual
    assert docs_switch in manual
    unit_validation_start = manual.index("for unit in \\")
    unit_validation = manual[unit_validation_start : manual.index("done", unit_validation_start)]
    for unit in required_units:
        assert f"  {unit}" in unit_validation
    assert manual.index(image_validation) < manual.index(app_switch)
    assert manual.index(image_validation) < manual.index(docs_switch)
    assert manual.index(image_tag) < manual.index(app_switch)
    assert manual.index(image_tag) < manual.index(docs_switch)
    assert manual.index(app_precondition) < manual.index(app_switch)
    assert manual.index(app_precondition) < manual.index(docs_switch)
    assert manual.index(docs_precondition) < manual.index(app_switch)
    assert manual.index(docs_precondition) < manual.index(docs_switch)
    restart_position = manual.rindex("sudo systemctl restart")
    validation_position = manual.index(unit_process_validation)
    assert manual.index(app_switch) < restart_position < validation_position
    assert manual.index(docs_switch) < restart_position < validation_position
    assert manual.index(unit_process_validation) < manual.rindex("rollback_pending=0")


def test_manual_rollback_has_failure_injection_coverage() -> None:
    harness = (ROOT / "deploy" / "app" / "rollback.test.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "VEETBOT_TEST_FAIL_DOCS_SWITCH_ONCE" in harness
    assert "VEETBOT_TEST_READY_RELEASE" in harness
    assert "rollback with a mismatched readiness identity unexpectedly succeeded" in harness
    assert "application pointer was not restored" in harness
    assert "documentation pointer was not restored" in harness
    assert "production image tag was not restored" in harness
    assert "deploy/app/rollback.test.sh" in makefile


def test_release_script_preserves_release_boundaries() -> None:
    deploy = ROOT / "deploy"
    release = (deploy / "app" / "release.sh").read_text(encoding="utf-8")
    assert "flock -w" in release
    assert '"$STAGE/.venv/bin/alembic" upgrade head' in release
    assert "X-Veetbot-Release" not in release
    assert "IGNORECASE" not in release
    assert "VEETBOT_RELEASE_ID" in release
    assert "systemctl enable --now" in release
    assert "VEETBOT_KEEP_RELEASES:-5" in release
    assert '--project-name "$COMPOSE_PROJECT_NAME"' in release
    assert "browser-profile-service.Dockerfile" in release
    assert 'up -d --wait --wait-timeout "$HEALTH_TIMEOUT_SECS"' in release
    assert '[[ -f "$BROWSER_PROFILE_SERVICE_AUTH_FILE" ]]' in release
    assert '[[ -f "$BROWSER_PROFILE_SESSION_SECRET_FILE" ]]' in release
    assert '[[ -d "$BROWSER_PROFILE_KEY_DIR" ]]' in release
    assert '[[ -f "$BROWSER_CONTROL_CREDENTIAL_FILE" ]]' in release
    assert release.count("VEETBOT_BROWSER_CONTROL_PLANE_CREDENTIAL_FILE must") == 3
    assert release.count(": $BROWSER_CONTROL_CREDENTIAL_FILE") == 3
    assert "BROWSER_PROFILE_CEREMONY_BASE_URL must be one HTTPS origin" in release
    assert "AGENT_SCHEDULE_WORKER_ENABLED" in release
    assert "veetbot-async-worker" in release
    assert "veetbot-schedule" in release


def test_nginx_configuration_preserves_public_process_boundaries() -> None:
    deploy = ROOT / "deploy"
    nginx = (ROOT / "nginx" / "veetbot.conf").read_text(encoding="utf-8")
    assert "server_name api.veetbot.com" in nginx
    assert "server_name docs.veetbot.com" in nginx
    assert "proxy_pass http://127.0.0.1:8000" in nginx
    assert "server_name browser.veetbot.com" in nginx
    assert "proxy_pass http://127.0.0.1:8081" in nginx
    assert "/etc/letsencrypt/live/browser.veetbot.com/fullchain.pem" in nginx
    assert "proxy_buffering off" in nginx
    assert "root /opt/veetbot/docs/current" in nginx
    assert "try_files $uri $uri/ =404" in nginx
    nginx_deploy = (deploy / "nginx" / "deploy.sh").read_text(encoding="utf-8")
    assert "nginx -t" in nginx_deploy
    assert "rollback" in nginx_deploy
    assert "flock -w" in nginx_deploy
    assert "VEETBOT_EXPECTED_RELEASE_ID" in nginx_deploy
    assert "VEETBOT_DOCS_ROOT" in nginx_deploy
    assert 'sha256sum "$DOCS_ARCHIVE"' in nginx_deploy
    assert 'mv -Tf "$NEXT_DOCS_CURRENT" "$DOCS_ROOT/current"' in nginx_deploy


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


def test_production_preflight_requires_the_balanced_provider_credential(tmp_path: Path) -> None:
    environment = {
        "DATABASE_URL": "postgresql+asyncpg://" + "agent:agent@localhost:5432/agent",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "SANDBOX_MECHANISM": "docker",
    }
    assert production_check._model_policy_failures(load_settings(environment)) == [
        "production model provider credential is missing: openai"
    ]
    configured = load_settings({**environment, "VEETBOT_OPENAI_KEY": "synthetic-key"})
    assert production_check._model_policy_failures(configured) == []

    overlay = tmp_path / "models" / "policies.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("model_policies:\n  balanced:\n    provider: ollama\n", encoding="utf-8")
    local = load_settings({**environment, "AGENT_CONFIG_DIR": str(tmp_path)})
    assert production_check._model_policy_failures(local) == []


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
    assert set(jobs) == {
        "static",
        "contract",
        "integration",
        "sandbox",
        "apple",
        "live",
        "package-release",
        "deploy-app",
        "deploy-nginx",
    }
    for name, job in jobs.items():
        if name == "sandbox":
            assert job["machine"] == {"image": "ubuntu-2404:current"}
            continue
        if name == "apple":
            assert job["macos"] == {"xcode": "26.6.0"}
            assert job["resource_class"] == "m4pro.medium"
            continue
        expected_image = (
            "cimg/base:stable" if name in {"deploy-app", "deploy-nginx"} else "cimg/python:3.12"
        )
        assert job["docker"][0]["image"] == expected_image

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
    assert "make lint typecheck test-static test-deploy docs-check" in commands["static"]
    assert "make client-build" in commands["static"]
    assert any(
        "python -m scripts.check_reading_lane" in command and "READING_LANE_BASE" in command
        for command in commands["static"]
    )
    assert "make test-contract" in commands["contract"]
    assert "make migrate test-integration" in commands["integration"]
    assert "make test-sandbox" in commands["sandbox"]
    assert "make test-apple" in commands["apple"]
    assert "make test-apple-ui" in commands["apple"]
    assert "make test-live" in commands["live"]
    assert any("git archive --format=tar.gz" in command for command in commands["package-release"])
    assert any(
        "uv run --frozen --only-group docs mkdocs build --strict" in command
        and "veetbot-docs.tar.gz" in command
        for command in commands["package-release"]
    )
    package_workspace = next(
        step["persist_to_workspace"]
        for step in jobs["package-release"]["steps"]
        if isinstance(step, dict) and "persist_to_workspace" in step
    )
    assert "release-id" in package_workspace["paths"]
    assert "veetbot-docs.tar.gz" in package_workspace["paths"]
    assert "veetbot-docs.tar.gz.sha256" in package_workspace["paths"]
    assert any("deploy/app/release.sh" in command for command in commands["deploy-app"])
    assert any(
        "X-Veetbot-Release" not in command and "x-veetbot-release" in command
        for command in commands["deploy-app"]
    )
    assert any(
        "while (( attempt < max_attempts ))" in command
        and "production did not report release" in command
        for command in commands["deploy-app"]
    )
    assert any("deploy/nginx/deploy.sh" in command for command in commands["deploy-nginx"])
    assert any(
        "veetbot-docs.tar.gz" in command and "veetbot-docs.tar.gz.sha256" in command
        for command in commands["deploy-nginx"]
    )
    assert any(
        "VEETBOT_EXPECTED_RELEASE_ID" in command
        and '[[ "$expected_release_id" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}$ ]]' in command
        for command in commands["deploy-nginx"]
    )
    assert any(
        "https://docs.veetbot.com" in command and "documentation did not report release" in command
        for command in commands["deploy-nginx"]
    )
    assert any(
        "nginx-deployment-outcome" in command
        and '[[ "$deployment_status" == 3 ]]' in command
        and "printf 'stale\\n'" in command
        for command in commands["deploy-nginx"]
    )
    assert any(
        "nginx-deployment-outcome" in command
        and "Skipping documentation identity probe for stale release" in command
        for command in commands["deploy-nginx"]
    )
    assert "EE3+mp97" not in (ROOT / ".circleci" / "config.yml").read_text(encoding="utf-8")
    deployment_key_step = {
        "add_ssh_keys": {"fingerprints": ["SHA256:vt3iKfD3dv6dxtjS+Tre6B1EH6408yvMHFrMpp64sao"]}
    }
    for name in ("deploy-app", "deploy-nginx"):
        assert deployment_key_step in jobs[name]["steps"]

    workflows = config["workflows"]
    assert set(workflows) == {"verify", "live_manual", "live_nightly"}
    verify = workflows["verify"]
    assert verify["unless"] == "<< pipeline.parameters.run_live >>"
    assert verify["jobs"][:5] == ["static", "contract", "integration", "sandbox", "apple"]
    delivery_jobs = {
        next(iter(job)): next(iter(job.values()))
        for job in verify["jobs"][5:]
        if isinstance(job, dict)
    }
    assert set(delivery_jobs) == {"package-release", "deploy-app", "deploy-nginx"}
    assert delivery_jobs["package-release"]["requires"] == [
        "static",
        "contract",
        "integration",
        "sandbox",
        "apple",
    ]
    assert delivery_jobs["deploy-app"]["requires"] == ["package-release"]
    assert delivery_jobs["deploy-app"]["context"] == "veetbot-production"
    assert delivery_jobs["deploy-app"]["serial-group"] == (
        "<< pipeline.project.slug >>/veetbot-production"
    )
    assert delivery_jobs["deploy-nginx"]["requires"] == ["deploy-app"]
    assert delivery_jobs["deploy-nginx"]["context"] == "veetbot-production"
    assert delivery_jobs["deploy-nginx"]["serial-group"] == (
        "<< pipeline.project.slug >>/veetbot-production"
    )
    for name, job in delivery_jobs.items():
        assert job["filters"] == {"branches": {"only": "main"}}, name
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


def test_mkdocs_site_has_its_public_origin() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))

    assert config["site_url"] == "https://docs.veetbot.com/"
    assert config["theme"]["font"] is False


def test_repository_contract_requires_red_green_tdd_evidence() -> None:
    contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "## Test-driven development" in contract
    assert "Run the new or changed test first and record the expected failure" in contract
    assert "Do not weaken, delete, skip, or rewrite a failing test" in contract
    assert "Red test command and expected failure" in contract


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
        *,
        wait_timeout_seconds: float,
    ) -> tuple[Run, list[PersistedStreamFrame]]:
        del idempotency_key, model_policy, wait_timeout_seconds
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


def test_run_export_prints_json_and_reports_consent_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id = UUID("00000000-0000-0000-0000-000000000041")
    exported_run_id = UUID("00000000-0000-0000-0000-000000000042")

    async def fake_export(run_id: UUID) -> object:
        assert run_id == exported_run_id
        return SimpleNamespace(
            id=artifact_id,
            model_dump_json=lambda: json.dumps({"id": str(artifact_id)}),
        )

    monkeypatch.setattr(cli_main, "_export_run", fake_export)
    runner = CliRunner()

    exported = runner.invoke(
        app,
        ["run", "export", str(exported_run_id), "--json"],
    )

    assert exported.exit_code == 0
    assert json.loads(exported.stdout) == {"id": str(artifact_id)}

    async def deny_export(_run_id: UUID) -> object:
        raise ExportConsentError("trajectory export requires active consent")

    monkeypatch.setattr(cli_main, "_export_run", deny_export)
    denied = runner.invoke(app, ["run", "export", str(exported_run_id)])

    assert denied.exit_code == 1
    assert "requires active consent" in denied.stderr


@pytest.mark.parametrize(
    ("command", "limit"),
    [
        ("list", "0"),
        ("list", "201"),
        ("formations", "0"),
        ("formations", "201"),
    ],
)
def test_memory_cli_rejects_limits_outside_the_governed_bounds(
    command: str,
    limit: str,
) -> None:
    result = CliRunner().invoke(app, ["memory", command, "--limit", limit])

    assert result.exit_code == 2
    assert "Invalid value" in result.stderr


def test_memory_management_and_diagnostics_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    belief = memory()
    recall_trace = trace()
    formation = ConsolidationRun(
        id=belief.formation_run_id,
        tenant_id=belief.tenant_id,
        principal_id=belief.principal_id,
        trigger="session_idle",
        scope=belief.scope,
        session_id=belief.source_session_id,
        watermark_before=0,
        watermark_after=1,
        model="deterministic-formation-v2",
        policy_version="formation@2",
        candidates_proposed=1,
        committed=1,
        reinforced=0,
        superseded=0,
        rejected=0,
        started_at=NOW,
        finished_at=NOW,
    )
    seen: list[tuple[str, object]] = []

    async def fake_list(
        include_inactive: bool, session_id: UUID | None, limit: int
    ) -> list[object]:
        seen.append(("list", (include_inactive, session_id, limit)))
        return [belief]

    async def fake_get(belief_id: UUID) -> object:
        seen.append(("get", belief_id))
        return belief

    async def fake_edit(belief_id: UUID, edit: MemoryEdit) -> object:
        seen.append(("edit", (belief_id, edit.statement)))
        return belief.model_copy(update={"statement": edit.statement})

    async def fake_delete(belief_id: UUID) -> None:
        seen.append(("delete", belief_id))

    async def fake_formations(session_id: UUID | None, limit: int) -> list[object]:
        seen.append(("formations", (session_id, limit)))
        return [formation]

    async def fake_trace(trace_id: UUID) -> object:
        seen.append(("trace", trace_id))
        return recall_trace

    monkeypatch.setattr(cli_main, "_memory_list", fake_list)
    monkeypatch.setattr(cli_main, "_memory_get", fake_get)
    monkeypatch.setattr(cli_main, "_memory_edit", fake_edit)
    monkeypatch.setattr(cli_main, "_memory_delete", fake_delete)
    monkeypatch.setattr(cli_main, "_memory_formations", fake_formations)
    monkeypatch.setattr(cli_main, "_memory_trace", fake_trace)
    runner = CliRunner()

    listed = runner.invoke(
        app,
        [
            "memory",
            "list",
            "--include-inactive",
            "--session",
            str(SESSION_ID),
            "--limit",
            "7",
        ],
    )
    fetched = runner.invoke(app, ["memory", "get", str(belief.id)])
    edited = runner.invoke(
        app,
        ["memory", "edit", str(belief.id), "--statement", "User prefers direct answers"],
    )
    formations = runner.invoke(
        app,
        ["memory", "formations", "--session", str(SESSION_ID), "--limit", "9"],
    )
    traced = runner.invoke(app, ["memory", "trace", str(recall_trace.id)])
    deleted = runner.invoke(app, ["memory", "delete", str(belief.id)])

    assert all(
        result.exit_code == 0 for result in (listed, fetched, edited, formations, traced, deleted)
    )
    assert json.loads(listed.stdout)[0]["formation_run_id"] == str(formation.id)
    assert json.loads(fetched.stdout)["source_session_id"] == str(SESSION_ID)
    assert json.loads(edited.stdout)["statement"] == "User prefers direct answers"
    assert json.loads(formations.stdout)[0]["committed"] == 1
    assert json.loads(traced.stdout)["query"]["text"] == "concise answers"
    assert json.loads(deleted.stdout) == {"id": str(belief.id)}
    assert seen == [
        ("list", (True, SESSION_ID, 7)),
        ("get", belief.id),
        ("edit", (belief.id, "User prefers direct answers")),
        ("formations", (SESSION_ID, 9)),
        ("trace", recall_trace.id),
        ("delete", belief.id),
    ]


async def test_in_memory_composition_round_trips_a_recall_trace_through_the_public_service(
    tmp_path: Path,
) -> None:
    value = trace()
    settings = replace(memory_settings(), artifact_root=tmp_path / "artifacts")

    async with build(settings=settings, storage="memory", principal=principal()) as composition:
        async with composition.uow_factory() as uow:
            await uow.traces.record(value)

        assert await composition.memory.get_recall_trace(value.id) == value


def test_run_reports_durable_id_when_wait_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    queued_id = UUID("00000000-0000-0000-0000-000000000040")

    async def timeout_submit(
        prompt: str,
        session_id: UUID | None,
        idempotency_key: str | None,
        model_policy: str | None,
        *,
        wait_timeout_seconds: float,
    ) -> tuple[Run, list[EventEnvelope]]:
        del prompt, session_id, idempotency_key, model_policy, wait_timeout_seconds
        raise cli_main.QueuedRunTimeoutError(queued_id)

    monkeypatch.setattr(cli_main, "_submit", timeout_submit)
    result = CliRunner().invoke(app, ["run", "queued work"])

    assert result.exit_code == 5
    assert "run did not reach a terminal state" in result.stderr
    assert "run queued" not in result.stderr
    assert str(queued_id) in result.stderr


def test_run_wait_timeout_is_configurable_and_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    async def fake_submit(
        prompt: str,
        session_id: UUID | None,
        idempotency_key: str | None,
        model_policy: str | None,
        *,
        wait_timeout_seconds: float,
    ) -> tuple[Run, list[PersistedStreamFrame]]:
        del prompt, session_id, idempotency_key, model_policy
        seen.append(wait_timeout_seconds)
        completed = run(status=RunStatus.COMPLETED)
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

    monkeypatch.setattr(cli_main, "_submit", fake_submit)
    runner = CliRunner()

    configured = runner.invoke(
        app,
        ["run", "--wait-timeout", "90.5", "timed work"],
    )
    defaulted = runner.invoke(app, ["run", "default wait"])
    invalid = runner.invoke(app, ["run", "--wait-timeout", "0", "invalid wait"])

    assert configured.exit_code == defaulted.exit_code == 0
    assert invalid.exit_code == 2
    assert "--wait-timeout must be greater than zero" in invalid.stderr
    assert seen == [90.5, 300.0]


async def _async_value(value: Any) -> Any:
    return value


async def test_submit_uses_one_total_timeout_for_status_and_event_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000041")
    observed_timeouts: list[float] = []

    class RecordingTimeout:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    def timeout(seconds: float) -> RecordingTimeout:
        observed_timeouts.append(seconds)
        return RecordingTimeout()

    async def stream() -> AsyncIterator[PersistedStreamFrame]:
        yield PersistedStreamFrame(sequence=1, event="run.completed", data={})

    services = SimpleNamespace(
        runs=SimpleNamespace(
            submit=lambda *_args: _async_value(SimpleNamespace(run_id=run_id)),
            get=lambda *_args: _async_value(SimpleNamespace(id=run_id, status=RunStatus.COMPLETED)),
            stream=lambda *_args: stream(),
        )
    )
    composition = SimpleNamespace(
        services=services,
        principal=object(),
        runs=SimpleNamespace(interrupt=lambda: None),
    )

    class BuildContext:
        async def __aenter__(self) -> Any:
            return composition

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_build(**_kwargs: object) -> BuildContext:
        return BuildContext()

    monkeypatch.setattr(cli_main, "build", fake_build)
    monkeypatch.setattr(asyncio, "timeout", timeout)

    completed, events = await cli_main._submit(
        "one total budget",
        UUID("00000000-0000-0000-0000-000000000020"),
        None,
        None,
        wait_timeout_seconds=12.5,
    )

    assert completed.status is RunStatus.COMPLETED
    assert [event.event for event in events] == ["run.completed"]
    assert observed_timeouts == [12.5]


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


def test_required_files_include_the_status_split_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    check_docs = importlib.import_module("check_docs")

    monkeypatch.setattr(check_docs, "errors", [])
    check_docs.check_required_files()
    assert [
        error
        for error in check_docs.errors
        if "verification-history" in error or "corpus-audit-log" in error
    ] == []

    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    monkeypatch.setattr(check_docs, "errors", [])
    check_docs.check_required_files()
    assert "required file missing: docs/status/verification-history.yaml" in check_docs.errors
    assert "required file missing: docs/status/corpus-audit-log.md" in check_docs.errors


def test_docs_checks_admit_the_roadmap_milestones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Milestones 12 through 15 are authorized; project state and plan checks follow."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    check_docs = importlib.import_module("check_docs")

    status = tmp_path / "docs" / "status"
    status.mkdir(parents=True)
    milestones = {str(n): {"title": f"milestone {n}", "status": "planned"} for n in range(16)}
    (status / "project-state.yaml").write_text(
        yaml.safe_dump({"project": {"current_milestone": 11}, "milestones": milestones}),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    monkeypatch.setattr(check_docs, "errors", [])
    check_docs.check_project_state()
    assert check_docs.errors == []
    assert check_docs.project_current_milestone() == 11

    plan = tmp_path / "docs" / "plan" / "engineering-plan.md"
    plan.parent.mkdir(parents=True)
    sections = "\n".join(f"## {n}. Section" for n in range(1, 26))
    headings = "\n".join(f"### Milestone {n}: title" for n in range(12))
    plan.write_text(
        "---\ncanonical: true\n---\n# Plan\n"
        f"{sections}\n## 26. First assignment for the coding agent\n{headings}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_docs, "PLAN", plan)
    monkeypatch.setattr(check_docs, "errors", [])
    check_docs.check_plan()
    assert "engineering-plan.md missing 'Milestone 12' section" in check_docs.errors
    assert "engineering-plan.md missing 'Milestone 15' section" in check_docs.errors


_SUDOERS_RULE = re.compile(r"^(\S+) (\S+)=\((\S+)\) NOPASSWD: (/\S+)( .+)?$")
_SUDOERS_INCLUDE = re.compile(r"^[#@]include(dir)?\b")


def _sudoers_rules(lines: list[str]) -> list[str]:
    """The non-comment, non-blank lines of a sudoers file, unfiltered of directives."""

    return [
        line.strip()
        for line in lines
        if line.strip()
        and (_SUDOERS_INCLUDE.match(line.strip()) or not line.strip().startswith("#"))
    ]


def _sudoers_rule_violations(lines: list[str]) -> list[str]:
    """Reject any sudoers line wider than the deploy contract permits.

    Operates on raw file lines: true comments are skipped, but #include and
    #includedir (and their modern @ forms) are active sudoers directives that
    pull in additional grants, so they are rejected rather than filtered.
    """

    violations = []
    for rule in _sudoers_rules(lines):
        if _SUDOERS_INCLUDE.match(rule):
            violations.append(f"include directives are forbidden in the contract: {rule}")
            continue
        match = _SUDOERS_RULE.match(rule)
        if match is None:
            violations.append(f"not a NOPASSWD rule with an absolute command path: {rule}")
            continue
        subject, host, runas, _path, arguments = match.groups()
        if subject != "deploy":
            violations.append(f"subject must be the documented deploy placeholder: {rule}")
        if host != "ALL":
            violations.append(f"host must be ALL: {rule}")
        if runas != "root":
            violations.append(f"run-as target must be root only: {rule}")
        if arguments is None:
            violations.append(f"command must constrain its arguments: {rule}")
        if "," in rule:
            violations.append(f"one rule per command specification: {rule}")
    return violations


def test_sudoers_contract_rejects_include_directives() -> None:
    # In sudoers, #include and #includedir are active directives, not
    # comments; an included file could grant extra root commands invisibly.
    for directive in (
        "#include /etc/sudoers.local",
        "#includedir /etc/sudoers.d",
        "@include /etc/sudoers.local",
        "@includedir /etc/sudoers.d",
    ):
        assert _sudoers_rule_violations(["# an ordinary comment", directive]), directive
    assert _sudoers_rule_violations(["# an ordinary comment", ""]) == []


def test_sudoers_contract_rejects_widened_rules() -> None:
    for widened in (
        "ALL ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload",
        "%admin ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload",
        "deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload",
        "deploy ALL=(root) NOPASSWD: /usr/bin/systemctl",
        "deploy ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload, /bin/sh",
        "deploy ALL=(root) NOPASSWD: systemctl daemon-reload",
        "deploy ALL=(root) PASSWD: /usr/bin/systemctl daemon-reload",
        "deploy ALL=(root) NOPASSWD: ALL",
    ):
        assert _sudoers_rule_violations([widened]), widened


def test_deploy_sudoers_contract_covers_every_sudo_command() -> None:
    contract = ROOT / "deploy" / "sudoers" / "veetbot-deploy"
    assert contract.is_file(), "deploy/sudoers/veetbot-deploy is the deploy account's sudo contract"

    raw_lines = contract.read_text(encoding="utf-8").splitlines()
    assert _sudoers_rule_violations(raw_lines) == []
    rules = _sudoers_rules(raw_lines)
    assert rules
    specs = set()
    for rule in rules:
        match = _SUDOERS_RULE.match(rule)
        assert match is not None
        specs.add(f"{match.group(4)}{match.group(5) or ''}")

    # systemctl argv is deterministic, so its rules are exact — never globbed.
    assert all("*" not in spec for spec in specs if spec.startswith("/usr/bin/systemctl")), (
        "systemctl rules must name exact units and arguments"
    )

    # The exact unit lists come from release.sh itself, so a UNITS change
    # fails this test until the contract names the new argv.
    release_text = (ROOT / "deploy" / "app" / "release.sh").read_text(encoding="utf-8")
    units_match = re.search(r"^UNITS=\(([^)]*)\)", release_text, re.MULTILINE)
    assert units_match is not None
    units = units_match.group(1).split()
    scheduled_units = ["veetbot-schedule", *units]
    for argv in (units, scheduled_units):
        assert f"/usr/bin/systemctl enable --now {' '.join(argv)}" in specs
        assert f"/usr/bin/systemctl restart {' '.join(argv)}" in specs
    for unit in scheduled_units:
        assert f"/usr/bin/systemctl is-active --quiet {unit}" in specs
        assert f"/usr/bin/systemctl show --property MainPID --value {unit}" in specs
    assert "/usr/bin/systemctl daemon-reload" in specs
    assert "/usr/bin/systemctl disable --now veetbot-schedule" in specs

    used = set()
    for script in ("deploy/app/release.sh", "deploy/nginx/deploy.sh"):
        used.update(
            re.findall(
                r"\bsudo\s+([a-z][a-z0-9_.-]*)",
                (ROOT / script).read_text(encoding="utf-8"),
            )
        )
    assert used, "the contract exists because the deploy scripts invoke sudo"
    rule_binaries = {Path(spec.split()[0]).name for spec in specs}
    assert used <= rule_binaries, (
        f"sudo commands without a contract rule: {sorted(used - rule_binaries)}"
    )
