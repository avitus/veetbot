"""Hosted Chromium's own requests, measured in the built service image (ADR-0152).

`make test-browser-image` builds the image and runs this module. It starts
`tests/browser_image_probe.py` in a container from the image, under the
production compose limits, on an internal network with no route out.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.browser_image]

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("VEETBOT_BROWSER_IMAGE", "veetbot-browser-profile-service:check")
# In the image the managed policy stops the dictionary download, so the one
# request the browser still makes on its own is the account listing (ADR-0146).
IMAGE_OWN_TARGETS = frozenset({"accounts.google.com:443"})
# The service's own wiring, which the probe replaces: it runs no server, holds
# no secrets, and gets an internal network instead of the egress network.
IGNORED_SERVICE_KEYS = frozenset(
    {"build", "image", "restart", "environment", "ports", "volumes", "networks", "healthcheck"}
)
PROBE_TIMEOUT_SECONDS = 900


def container_options(service: Mapping[str, Any]) -> list[str]:
    """The production service's limits as `docker run` options; no key is dropped silently."""
    options: list[str] = []
    for key, value in service.items():
        match key:
            case "init" | "read_only":
                if value is True:
                    options.append("--init" if key == "init" else "--read-only")
            case "user":
                options += ["--user", str(value)]
            case "pids_limit":
                options += ["--pids-limit", str(value)]
            case "mem_limit":
                options += ["--memory", str(value)]
            case "cpus":
                options += ["--cpus", str(value)]
            case "shm_size":
                options += ["--shm-size", str(value)]
            case "cap_drop" | "security_opt" | "tmpfs":
                flag = {"cap_drop": "--cap-drop", "security_opt": "--security-opt"}.get(
                    key, "--tmpfs"
                )
                for item in value:
                    options += [flag, str(item)]
            case _ if key in IGNORED_SERVICE_KEYS:
                pass
            case _:
                raise AssertionError(
                    f"translate the compose key {key!r} into the measurement or ignore it"
                )
    return options


def _docker(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run one bounded Docker command and retain diagnostics for assertions."""
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False
    )


@contextmanager
def _internal_network() -> Iterator[str]:
    """Create an isolated measurement network and remove it after the probe."""
    name = f"veetbot-browser-traffic-{secrets.token_hex(4)}"
    created = _docker("network", "create", "--internal", name)
    assert created.returncode == 0, created.stderr
    try:
        yield name
    finally:
        _docker("network", "rm", name)


def _measure() -> dict[str, Any]:
    """Run the installed browser in its production limits and read its report."""
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pytest.fail(f"{IMAGE} is not built; run make browser-image", pytrace=False)
    compose = yaml.safe_load(
        (ROOT / "deploy" / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["browser-profile-service"]
    container = f"veetbot-browser-traffic-{secrets.token_hex(4)}"
    with _internal_network() as network:
        try:
            completed = _docker(
                "run",
                "--rm",
                "--name",
                container,
                *container_options(service),
                "--network",
                network,
                "--volume",
                f"{ROOT / 'tests'}:/opt/probe/tests:ro",
                "--workdir",
                "/opt/probe",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                "python",
                IMAGE,
                "-m",
                "tests.browser_image_probe",
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _docker("rm", "--force", container)
            raise
    assert completed.returncode == 0, completed.stderr[-4000:]
    report: dict[str, Any] = json.loads(completed.stdout.splitlines()[-1])
    return report


def test_the_service_image_asks_its_proxy_only_for_what_the_page_loads() -> None:
    """Type into forms, submit, type a note and idle in the shipped image; ADR-0146 holds."""

    report = _measure()

    assert report["page_loads"] == ["GET /", "POST /", "GET /note", "GET /note"], report
    assert report["user_agents"], report
    assert not any("HeadlessChrome" in agent for agent in report["user_agents"]), report
    assert report["tunnelled"] == ["site.test:443"], report
    assert set(report["refused"]) <= IMAGE_OWN_TARGETS, report


def test_every_production_limit_reaches_the_measurement() -> None:
    """Require every production container limit to be translated or rejected."""
    compose = yaml.safe_load(
        (ROOT / "deploy" / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["browser-profile-service"]

    options = container_options(service)

    for option in ("--init", "--read-only", "--pids-limit", "--memory", "--cpus", "--shm-size"):
        assert option in options
    assert options[options.index("--user") + 1] == "65532:65532"
    assert options[options.index("--tmpfs") + 1] == service["tmpfs"][0]
    with pytest.raises(AssertionError, match="ulimits"):
        container_options({**service, "ulimits": {"nofile": 1024}})
