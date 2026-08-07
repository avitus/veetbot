"""Fail-fast validation for a host-native production deployment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from agent_core.config import load_settings


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
    print("OK: production configuration, gVisor, sandbox image, storage, and database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
