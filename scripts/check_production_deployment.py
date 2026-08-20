"""Fail-fast validation for a host-native production deployment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from agent_core.config import Settings, load_config_document, load_settings


def _run(*command: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        return subprocess.CompletedProcess(command, 124, stdout or "", f"timed out: {exc}")


def _same_credential_file(credential: str, service_auth: str) -> bool:
    try:
        # samefile compares inode identity, catching hard links whose
        # resolved paths differ.
        return Path(credential).samefile(service_auth)
    except OSError:
        # Files that do not exist yet still conflict through `..` aliases
        # and symlinked parents.
        return Path(credential).resolve() == Path(service_auth).resolve()


def _browser_credential_failures(environment: Mapping[str, str]) -> list[str]:
    """Reject the shared-path browser credential misconfiguration.

    The profile service requires its auth file to be owned by uid 65532 while
    the agent units read the control-plane credential as the service account;
    both readers require mode 0600, so one file can never serve both.
    """

    credential = environment.get("BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE", "").strip()
    service_auth = environment.get("BROWSER_PROFILE_SERVICE_AUTH_FILE", "").strip()
    if credential and service_auth and _same_credential_file(credential, service_auth):
        return [
            "BROWSER_PROFILE_CONTROL_PLANE_CREDENTIAL_FILE and "
            "BROWSER_PROFILE_SERVICE_AUTH_FILE must name different files: the "
            "containerized profile service owns its auth file as uid 65532 while "
            "the agent units read the credential as the service account, and a "
            "0600 file cannot serve both readers"
        ]
    return []


def _model_policy_failures(settings: Settings) -> list[str]:
    policies = load_config_document(settings, "models/policies.yaml")
    model_policies = policies.get("model_policies")
    if not isinstance(model_policies, dict):
        return ["models/policies.yaml does not declare model_policies"]
    balanced = model_policies.get("balanced")
    if not isinstance(balanced, dict) or not isinstance(balanced.get("provider"), str):
        return ["production default model policy 'balanced' is not declared"]
    provider = balanced["provider"]
    if provider in {"openai", "anthropic"} and provider not in settings.credentials:
        return [f"production model provider credential is missing: {provider}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-database", action="store_true")
    args = parser.parse_args()
    failures: list[str] = []

    try:
        settings = load_settings(os.environ)
    except (OSError, ValueError) as exc:
        failures.append(f"configuration: {exc}")
        settings = None

    if settings is not None:
        if settings.deployment_mode.value != "production":
            failures.append("DEPLOYMENT_MODE must be production")
        if settings.auth_mode.value != "token":
            failures.append("AUTH_MODE must be token")
        if settings.sandbox.value != "gvisor":
            failures.append("SANDBOX_MECHANISM must be gvisor for this deployment layout")
        if not settings.artifact_root.is_absolute():
            failures.append("AGENT_ARTIFACT_ROOT must be an absolute path")
        elif not settings.artifact_root.is_dir():
            failures.append(f"artifact directory does not exist: {settings.artifact_root}")
        if settings.release_id is None:
            failures.append("VEETBOT_RELEASE_ID must identify the staged release")
        failures.extend(_model_policy_failures(settings))
    failures.extend(_browser_credential_failures(os.environ))

    available: dict[str, bool] = {}
    for executable in ("docker", "runsc", "uv"):
        available[executable] = shutil.which(executable) is not None
        if not available[executable]:
            failures.append(f"required executable is missing: {executable}")

    if available["docker"]:
        docker_info = _run("docker", "info", "--format", "{{json .Runtimes}}")
        if docker_info.returncode != 0:
            failures.append("Docker daemon is unavailable")
        else:
            try:
                runtimes = json.loads(docker_info.stdout)
            except json.JSONDecodeError:
                failures.append("Docker returned an unreadable runtime inventory")
            else:
                if "runsc" not in runtimes:
                    failures.append("Docker runtime 'runsc' is not installed")

    if settings is not None and available["docker"]:
        image = _run("docker", "image", "inspect", settings.sandbox_image)
        if image.returncode != 0:
            failures.append(f"sandbox image is unavailable: {settings.sandbox_image}")

    if not args.skip_database and available["uv"]:
        migration = _run("uv", "run", "alembic", "current", "--check-heads")
        if migration.returncode != 0:
            failures.append("database is unavailable or not at the expected migration head")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(
        "OK: production configuration, release identity, model credential, gVisor, "
        "sandbox image, storage, and database"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
