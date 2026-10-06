"""Command line for the Scale VP read server and its sign-in ceremony."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlsplit

import httpx

from svp_mcp.bootstrap import BootstrapError, bootstrap_credential
from svp_mcp.client import SvpClient
from svp_mcp.constants import (
    CREDENTIAL_VARIABLE,
    DOCUMENT_URL,
    LOOPBACK_REDIRECT_HOST,
    LOOPBACK_REDIRECT_PORT,
    LOOPBACK_REDIRECT_URI,
)
from svp_mcp.credential import CredentialStore, read_state
from svp_mcp.errors import SvpError
from svp_mcp.server import create_server


def _quiet_transport_logging() -> None:
    """Keep request lines, which carry query values, out of the forwarded stderr."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False
    )


def _callback_code(target: str, expected_state: str) -> str | None:
    """Return the code only from the loopback callback that carries this ceremony's state."""
    request = urlsplit(target)
    callback = parse_qs(request.query)
    code = callback.get("code", [""])[0]
    state = callback.get("state", [""])[0]
    if (
        request.path == urlsplit(LOOPBACK_REDIRECT_URI).path
        and code
        and secrets.compare_digest(state.encode(), expected_state.encode())
    ):
        return code
    return None


def _authorize_via_loopback(authorization_url: str) -> str:
    expected_state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            code = _callback_code(self.path, expected_state)
            if code is not None:
                result["code"] = code
                body = b"Sign-in received. You may close this window."
                self.send_response(200)
            else:
                body = b"The sign-in response was rejected."
                self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    try:
        server = HTTPServer((LOOPBACK_REDIRECT_HOST, LOOPBACK_REDIRECT_PORT), Callback)
    except OSError:
        raise BootstrapError("the loopback sign-in port is unavailable") from None
    try:
        if not webbrowser.open(authorization_url, new=1):
            print(f"Open this sign-in URL in a browser:\n{authorization_url}")
        deadline = time.monotonic() + 300
        while "code" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    code = result.get("code")
    if code is None:
        raise BootstrapError("sign-in did not complete")
    return code


def _confirm_disclosure(
    *,
    read_input: Callable[[str], str] | None = None,
    output: TextIO | None = None,
) -> None:
    """Present the data-use notice; anything but the exact word cancels the ceremony."""

    reader = input if read_input is None else read_input
    stream = sys.stdout if output is None else output
    print(
        "\n".join(
            (
                "Veetbot Scale VP data-use disclosure",
                "Access: this grants Veetbot read access to the Scale VP data API through "
                "one sign-in. Whatever that account may read, a Veetbot run may read on "
                "your behalf.",
                "Use and sharing: results are used only for the requests you make. In the "
                "hosted deployment, relevant results are sent to the hosted model provider "
                "(the OpenAI or Anthropic API) solely to produce the reply.",
                "Storage: results may be retained in Veetbot session history until you "
                "delete the session.",
                "Credential: the grant is written to one owner-only file with a single "
                "holder. Move it to the deployment; do not keep a second copy in use.",
                "Control: you may decline now, disable the integration, or delete the file.",
            )
        ),
        file=stream,
        flush=True,
    )
    if reader("Type CONTINUE in capitals to open the Scale VP sign-in page: ") != "CONTINUE":
        raise BootstrapError(
            "sign-in cancelled before anything was sent; the confirmation is the exact "
            "word CONTINUE"
        )


def main(argv: list[str] | None = None) -> None:
    _quiet_transport_logging()
    parser = argparse.ArgumentParser(prog="python -m svp_mcp")
    parser.add_argument("--mode", choices=("read",))
    commands = parser.add_subparsers(dest="command")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--output-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "bootstrap":
        output_file: Path = arguments.output_file
        if (
            arguments.mode is not None
            or not output_file.is_absolute()
            or output_file.exists()
            or output_file.is_symlink()
        ):
            raise SystemExit("bootstrap requires a new absolute private-file path")
        try:
            with _http_client() as http:
                outcome = bootstrap_credential(
                    output_file=output_file,
                    confirm=_confirm_disclosure,
                    authorize=_authorize_via_loopback,
                    http_client=http,
                )
        except BootstrapError as exc:
            raise SystemExit(f"sign-in did not complete: {exc}") from None
        print(f"Saved the private grant to {output_file}")
        print("This file has one holder: move it to the deployment rather than copying it.")
        if outcome.operations is None:
            raise SystemExit(
                "The API accepted the sign-in, but no usable API document was found at "
                f"{DOCUMENT_URL} (HTTP {outcome.document_status}). The grant is saved; the "
                "bridge cannot list operations until that location is corrected."
            )
        print(f"The API answered with {outcome.operations} read operations.")
        return
    if arguments.mode is None:
        parser.error("choose --mode or bootstrap")
    raw_path = os.environ.pop(CREDENTIAL_VARIABLE, "")
    path = Path(raw_path)
    try:
        if not raw_path or not path.is_absolute():
            raise SvpError("svp.credential_invalid")
        read_state(path)
    except SvpError:
        raise SystemExit("svp.configuration_invalid") from None
    create_server(SvpClient(CredentialStore(path))).run(transport="stdio")


if __name__ == "__main__":
    main()
