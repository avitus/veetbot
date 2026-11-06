"""The local Linux test runner admits only explicitly requested service access."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("disposable", [False, True])
@pytest.mark.parametrize("docker_access", [False, True])
def test_local_runner_service_access_is_explicit(
    tmp_path: Path, network: bool, disposable: bool, docker_access: bool
) -> None:
    """Record Docker arguments without starting containers or contacting a database."""
    capture = tmp_path / "docker.jsonl"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['DOCKER_CAPTURE'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'build': sys.stdin.buffer.read()\n"
    )
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_CAPTURE": str(capture),
        "DATABASE_URL": "postgresql+asyncpg://test:test@postgres/test",
        "VEETBOT_TEST_DATABASE_DISPOSABLE": "1" if disposable else "0",
        "VEETBOT_TEST_NETWORK": "synthetic-test-network" if network else "",
        "VEETBOT_TEST_DOCKER": "1" if docker_access else "0",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/run_in_linux_container.sh"), "true"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    run = next(call for call in calls if call[0] == "run")
    assert ("/var/run/docker.sock:/var/run/docker.sock" in run) is docker_access
    assert ("--network" in run) is network
    if network:
        assert run[run.index("--network") + 1] == "synthetic-test-network"
    assert ("DATABASE_URL" in run) is (network and disposable)
    assert ("VEETBOT_TEST_DATABASE_DISPOSABLE" in run) is (network and disposable)
