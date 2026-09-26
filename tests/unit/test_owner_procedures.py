"""Owner-run procedures in the committed documentation do what they say.

Each procedure is extracted from the document the owner reads and run, with
only the host paths and the network replaced, so a check that cannot tell its
outcomes apart, or a command elided to "…", fails here rather than in an
emergency on the production host.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.contract.test_profile_service_http_contract import full_app

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "docs" / "deployment.md"
NGINX = ROOT / "nginx" / "veetbot.conf"
RELEASE_SCRIPT = ROOT / "deploy" / "app" / "release.sh"
PRODUCTION_COMPOSE = ROOT / "deploy" / "docker-compose.production.yml"
ENV_EXAMPLE = ROOT / "deploy" / "veetbot.env.example"
ADR_0128 = ROOT / "docs" / "adr" / "0128-device-sign-in-session-handoff.md"
SWITCH = "BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED"
RELEASE_ID = "20260925-120000-abcdef0"


def _fenced_blocks(text: str) -> list[tuple[str, int]]:
    """Every fenced block's body and the offset just past its closing fence."""

    return [
        (match.group(1), match.end())
        for match in re.finditer(r"^```\w*\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    ]


def _documented_block(needle: str) -> tuple[str, str]:
    """The one deployment block containing ``needle``, and the paragraph after it."""

    text = DEPLOYMENT.read_text(encoding="utf-8")
    matches = [block for block in _fenced_blocks(text) if needle in block[0]]
    assert len(matches) == 1, f"docs/deployment.md gives no single runnable block for {needle!r}"
    body, end = matches[0]
    return body, text[end:].lstrip("\n").split("\n\n", 1)[0]


def _nginx_limit(value: str) -> int:
    units = {"k": 1024, "m": 1024 * 1024}
    return int(value[:-1]) * units[value[-1]] if value[-1] in units else int(value)


# The handoff-location check: 401, 413 and 404 must mean different things.

_CURL_STUB = """#!{python}
import json, os, sys
from pathlib import Path

arguments = sys.argv[1:]
body = b""
for flag in ("-d", "--data", "--data-binary", "--data-raw"):
    if flag in arguments:
        value = arguments[arguments.index(flag) + 1]
        if value == "@-":
            body = sys.stdin.buffer.read()
        elif value.startswith("@") and flag != "--data-raw":
            body = Path(value[1:]).read_bytes()
        else:
            body = value.encode()
Path(os.environ["CURL_BODY"]).write_bytes(body)
Path(os.environ["CURL_ARGUMENTS"]).write_text(json.dumps(arguments))
print("401")
"""


def _run_with_curl_stub(tmp_path: Path, command: str) -> tuple[list[str], bytes]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_CURL_STUB.replace("{python}", sys.executable), encoding="utf-8")
    curl.chmod(0o755)
    body_file, arguments_file = tmp_path / "body", tmp_path / "arguments.json"
    completed = subprocess.run(
        ["bash", "-c", "set -o pipefail\n" + command],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CURL_BODY": str(body_file),
            "CURL_ARGUMENTS": str(arguments_file),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(arguments_file.read_text()), body_file.read_bytes()


async def test_handoff_location_check_tells_a_missing_location_from_a_live_one(
    tmp_path: Path,
) -> None:
    """ADR-0128: only a body between the two Nginx limits separates 401 from 413."""

    command, prose = _documented_block("/handoff")
    arguments, body = _run_with_curl_stub(tmp_path, command)

    nginx = NGINX.read_text(encoding="utf-8")
    browser = nginx.split("live/browser.veetbot.com/fullchain.pem;", 1)[1].split("\nserver {", 1)[0]
    location = re.search(r'location ~ "(\^/authentication/[^"]+)" \{', browser)
    assert location is not None
    handoff = browser.split(location.group(0), 1)[1].split("\n    }", 1)[0]
    vhost_limit = re.search(r"^    client_max_body_size (\w+);", browser, re.MULTILINE)
    handoff_limit = re.search(r"client_max_body_size (\w+);", handoff)
    assert vhost_limit is not None and handoff_limit is not None
    url = next(argument for argument in arguments if argument.startswith("https://"))
    path = url.removeprefix("https://browser.veetbot.com")

    assert re.fullmatch(location.group(1), path), url
    assert _nginx_limit(vhost_limit.group(1)) < len(body) <= _nginx_limit(handoff_limit.group(1)), (
        f"a {len(body)}-byte body passes both locations, so a missing handoff "
        f"location also answers 401; it must exceed the virtual host's {vhost_limit.group(1)}"
    )
    assert arguments[arguments.index("-X") + 1] == "POST"
    assert "Content-Type: application/json" in arguments

    # The service refuses the missing capability before it reads the body,
    # whichever Nginx location forwarded the request.
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        answered = await http.post(path, headers={"Content-Type": "application/json"}, content=body)
    assert answered.status_code == 401
    for outcome in ("`401`", "`413`", "`404`"):
        assert outcome in prose, outcome


# The TLS-floor check: a server that still accepts TLS 1.1 must not pass.


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_tls_server(
    openssl: str, directory: Path, *, tls_1_1_only: bool
) -> tuple[subprocess.Popen[str], int]:
    port = _free_loopback_port()
    protocols = (
        ["-tls1_1", "-cipher", "DEFAULT@SECLEVEL=0"] if tls_1_1_only else ["-no_tls1", "-no_tls1_1"]
    )
    server = subprocess.Popen(
        [
            openssl,
            "s_server",
            "-accept",
            f"127.0.0.1:{port}",
            "-cert",
            str(directory / "cert.pem"),
            "-key",
            str(directory / "key.pem"),
            *protocols,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    ready = threading.Event()

    def watch() -> None:
        assert server.stdout is not None
        for line in server.stdout:
            if line.startswith("ACCEPT"):
                ready.set()

    threading.Thread(target=watch, daemon=True).start()
    if not ready.wait(10):
        server.kill()
        server.wait()
        pytest.skip("this OpenSSL cannot serve TLS 1.1, so the floor check cannot be exercised")
    return server, port


def _run_tls_check(command: str, port: int) -> str:
    completed = subprocess.run(
        ["bash", "-c", command.replace("browser.veetbot.com:443", f"127.0.0.1:{port}")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout + completed.stderr


def test_tls_floor_check_fails_a_server_that_still_accepts_tls_1_1(tmp_path: Path) -> None:
    """ADR-0128 D21: the check must offer TLS 1.1 for the server to refuse it."""

    command, prose = _documented_block("s_client")
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl on PATH")
    version = subprocess.run([openssl, "version"], capture_output=True, text=True, check=True)
    if not version.stdout.startswith("OpenSSL"):
        pytest.skip(f"the documented check needs OpenSSL, not {version.stdout.strip()}")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(tmp_path / "key.pem"),
            "-out",
            str(tmp_path / "cert.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        capture_output=True,
        check=True,
    )
    without_floor_server, without_floor_port = _start_tls_server(
        openssl, tmp_path, tls_1_1_only=True
    )
    try:
        with_floor_server, with_floor_port = _start_tls_server(
            openssl, tmp_path, tls_1_1_only=False
        )
        try:
            without_floor = _run_tls_check(command, without_floor_port)
            with_floor = _run_tls_check(command, with_floor_port)
        finally:
            with_floor_server.kill()
            with_floor_server.wait()
    finally:
        without_floor_server.kill()
        without_floor_server.wait()

    accepted = re.search(r"Cipher is (\S+)", without_floor)
    assert accepted is not None and accepted.group(1) != "(NONE)", (
        "the documented check reports a server that accepts TLS 1.1 as refusing it:\n"
        + without_floor
    )
    assert "alert protocol version" not in without_floor
    assert "alert protocol version" in with_floor, with_floor
    assert "Cipher is (NONE)" in with_floor, with_floor
    assert "`alert protocol version`" in prose


# The device sign-in kill switch: set in the file Compose reads, then recreate.

_DOCKER_STUB = """#!{python}
import json, os, sys
from pathlib import Path

state = Path(os.environ["DOCKER_STATE"])
arguments = sys.argv[1:]
with Path(os.environ["DOCKER_LOG"]).open("a") as log:
    record = {
        "arguments": arguments,
        "cwd": os.getcwd(),
        "image": os.environ.get("BROWSER_PROFILE_SERVICE_IMAGE"),
    }
    log.write(json.dumps(record) + "\\n")
name = "BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED"
if arguments[:1] == ["compose"] and "up" in arguments:
    # Compose interpolates the value only when it creates the container.
    env_file = Path(arguments[arguments.index("--env-file") + 1])
    values = dict(
        line.split("=", 1)
        for line in env_file.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    state.write_text(os.environ.get(name) or values.get(name) or "true")
elif arguments[:1] == ["compose"] and "ps" in arguments:
    print("browser-profile-container")
elif arguments[:1] == ["inspect"]:
    print("PATH=/usr/local/bin")
    print(f"{name}={state.read_text()}")
# restart, compose restart and everything else keep the container's value.
"""


def _kill_switch_host(tmp_path: Path, environment: str) -> dict[str, Path]:
    etc = tmp_path / "etc" / "veetbot"
    release = tmp_path / "opt" / "veetbot" / "releases" / RELEASE_ID
    (release / "deploy").mkdir(parents=True)
    etc.mkdir(parents=True)
    (tmp_path / "opt" / "veetbot" / "shared").mkdir()
    (release / "docker-compose.yml").write_text("services: {}\n")
    (release / "deploy" / "docker-compose.production.yml").write_text("services: {}\n")
    (release / ".release.env").write_text(
        f"VEETBOT_RELEASE_ID={RELEASE_ID}\n"
        "AGENT_EXECUTION_SERVICE_SOCKET=/run/veetbot/execution.sock\n"
    )
    (tmp_path / "opt" / "veetbot" / "current").symlink_to(release)
    environment_file = etc / "veetbot.env"
    environment_file.write_text(environment)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_DOCKER_STUB.replace("{python}", sys.executable), encoding="utf-8")
    (bin_dir / "flock").write_text("#!/usr/bin/env bash\nexit 0\n")
    real_sed = shutil.which("sed")
    assert real_sed is not None
    # The host runs GNU sed; give a local BSD sed the same `-i` meaning.
    (bin_dir / "sed").write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "${{1:-}}" == -i ]] && ! "{real_sed}" --version >/dev/null 2>&1; then\n'
        f'  shift; exec "{real_sed}" -i "" "$@"\n'
        "fi\n"
        f'exec "{real_sed}" "$@"\n'
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    state = tmp_path / "container-value"
    state.write_text("true")
    return {
        "root": tmp_path,
        "bin": bin_dir,
        "environment": environment_file,
        "release": release,
        "state": state,
        "log": tmp_path / "docker.log",
    }


def _run_kill_switch(host: dict[str, Path], value: str) -> list[dict[str, Any]]:
    block, _prose = _documented_block(f"{SWITCH}=")
    assert "value=false" in block
    script = (
        block.replace("/etc/veetbot/veetbot.env", str(host["environment"]))
        .replace("/opt/veetbot", str(host["root"] / "opt" / "veetbot"))
        .replace("value=false", f"value={value}")
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{host['bin']}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(host["root"]),
            "DOCKER_STATE": str(host["state"]),
            "DOCKER_LOG": str(host["log"]),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = [json.loads(line) for line in host["log"].read_text().splitlines()]
    host["log"].unlink()
    return records


@pytest.mark.parametrize(
    ("environment", "project"),
    [
        (f"COMPOSE_PROJECT_NAME=veetbot\n{SWITCH}=true\nBROWSER_PROFILE_PORT=8081\n", "veetbot"),
        ("COMPOSE_PROJECT_NAME=legacy-veetbot\nBROWSER_PROFILE_PORT=8081\n", "legacy-veetbot"),
        ("BROWSER_PROFILE_PORT=8081\n", "veetbot"),
    ],
)
def test_device_sign_in_kill_switch_recreates_the_release_container(
    tmp_path: Path, environment: str, project: str
) -> None:
    """ADR-0128 D11: the documented procedure turns device sign-in off and on."""

    host = _kill_switch_host(tmp_path, environment)

    for value in ("false", "true"):
        records = _run_kill_switch(host, value)

        lines = host["environment"].read_text().splitlines()
        assert [line for line in lines if line.startswith(f"{SWITCH}=")] == [f"{SWITCH}={value}"]
        assert "BROWSER_PROFILE_PORT=8081" in lines
        assert host["state"].read_text() == value
        commands = [record["arguments"] for record in records]
        assert not any("restart" in command for command in commands)
        (up,) = [
            record
            for record, command in zip(records, commands, strict=True)
            if command[:1] == ["compose"] and "up" in command
        ]
        arguments = " ".join(up["arguments"])
        assert f"--env-file {host['environment']} " in arguments
        assert f"--project-name {project} " in arguments
        assert "-f docker-compose.yml -f deploy/docker-compose.production.yml up " in arguments
        assert " --no-build " in arguments
        assert arguments.endswith(" browser-profile-service")
        assert Path(str(up["cwd"])).resolve() == host["release"].resolve()
        assert up["image"] == f"veetbot-browser-profile-service:{RELEASE_ID}"


def test_the_kill_switch_matches_the_release_compose_invocation() -> None:
    """The documented recreate runs the service exactly as a release starts it."""

    release = RELEASE_SCRIPT.read_text(encoding="utf-8")
    compose = yaml.safe_load(PRODUCTION_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["browser-profile-service"]

    assert 'ENV_FILE="${VEETBOT_ENV_FILE:-/etc/veetbot/veetbot.env}"' in release
    assert 'PROFILE_REPOSITORY="veetbot-browser-profile-service"' in release
    assert 'PROFILE_RELEASE_IMAGE="$PROFILE_REPOSITORY:$RELEASE_ID"' in release
    assert 'export BROWSER_PROFILE_SERVICE_IMAGE="$PROFILE_RELEASE_IMAGE"' in release
    assert 'COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-veetbot}"' in release
    assert (
        'docker compose --env-file "$ENV_FILE" \\\n'
        '  --project-name "$COMPOSE_PROJECT_NAME" \\\n'
        "  -f docker-compose.yml -f deploy/docker-compose.production.yml \\\n"
    ) in release
    assert "printf 'VEETBOT_RELEASE_ID=%s" in release
    assert service["image"].startswith("${BROWSER_PROFILE_SERVICE_IMAGE:-")
    assert service["environment"][SWITCH] == "${BROWSER_PROFILE_DEVICE_SIGN_IN_ENABLED:-true}"


def test_every_kill_switch_description_says_the_container_is_recreated() -> None:
    deployment = DEPLOYMENT.read_text(encoding="utf-8")
    paragraph = next(
        paragraph for paragraph in deployment.split("\n\n") if f"`{SWITCH}`" in paragraph
    )
    decision = ADR_0128.read_text(encoding="utf-8").split("\n11. **", 1)[1].split("\n\n", 1)[0]
    comment = ENV_EXAMPLE.read_text(encoding="utf-8").split(f"\n{SWITCH}=", 1)[0]
    comment = comment.rsplit("\n\n", 1)[1]

    assert "/etc/veetbot/veetbot.env" in paragraph
    assert "recreate" in paragraph and "restart" in paragraph
    assert "…" not in paragraph and "release environment file" not in paragraph
    assert "recreat" in decision and "service restart" not in decision
    assert "recreat" in comment and "docs/deployment.md" in comment
    assert "..." not in comment


# Open owner items cite only acceptance steps their ADR defines.


def _cited_labels(item: str) -> list[tuple[str, str]]:
    cited: list[tuple[str, str]] = []
    adr = None
    for token in re.finditer(r"ADR-(\d{4})|\b([A-Z]{1,3})(\d{1,2})\b(?: to \2(\d{1,2})\b)?", item):
        if token.group(1):
            adr = token.group(1)
            continue
        assert adr is not None, f"{token.group(0)!r} cites no ADR: {item}"
        first, last = int(token.group(3)), int(token.group(4) or token.group(3))
        cited += [(adr, f"{token.group(2)}{number}") for number in range(first, last + 1)]
    return cited


def test_open_owner_items_cite_only_steps_their_adr_defines() -> None:
    state = yaml.safe_load(
        (ROOT / "docs" / "status" / "project-state.yaml").read_text(encoding="utf-8")
    )
    page = (ROOT / "docs" / "status" / "milestones.md").read_text(encoding="utf-8")
    outside = page.split("\n## Outside a milestone\n", 1)[1].split("\n## ", 1)[0]
    items = [
        *state["open_items_outside_milestones"],
        *re.findall(r"^- \[ \] (.+)$", outside, re.MULTILINE),
    ]
    adrs = {path.name[:4]: path for path in (ROOT / "docs" / "adr").glob("[0-9]*.md")}

    undefined = [
        f"ADR-{adr} {label}"
        for item in items
        for adr, label in _cited_labels(item)
        if f"**{label}:**" not in adrs[adr].read_text(encoding="utf-8")
    ]
    assert undefined == []
