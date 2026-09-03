"""Command line for the three Gmail MCP modes and bootstrap ceremony."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from gmail_mcp.bootstrap import BootstrapError, bootstrap_credentials
from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import GOOGLE_SCOPES, LOOPBACK_REDIRECT_HOST
from gmail_mcp.errors import GmailError
from gmail_mcp.server import create_server


def _oauth_client(path: Path) -> tuple[str, str]:
    if not path.is_absolute() or path.is_symlink():
        raise BootstrapError("OAuth client file must be an absolute private regular file")
    try:
        metadata = path.stat()
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("OAuth client file is invalid") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise BootstrapError("OAuth client file must be owner-only")
    installed = raw.get("installed") if isinstance(raw, dict) else None
    values = installed if isinstance(installed, dict) else raw
    client_id = values.get("client_id") if isinstance(values, dict) else None
    client_secret = values.get("client_secret") if isinstance(values, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise BootstrapError("OAuth client file is invalid")
    if not isinstance(client_secret, str) or not client_secret:
        raise BootstrapError("OAuth client file is invalid")
    return client_id, client_secret


def _authorize_via_loopback(_mode: str, authorization_url: str) -> str:
    query = parse_qs(urlsplit(authorization_url).query)
    redirect = urlsplit(query["redirect_uri"][0])
    expected_state = query["state"][0]
    if redirect.hostname != LOOPBACK_REDIRECT_HOST or redirect.port is None:
        raise BootstrapError("loopback redirect is invalid")
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            callback = parse_qs(urlsplit(self.path).query)
            code = callback.get("code", [""])[0]
            state = callback.get("state", [""])[0]
            if state == expected_state and code:
                result["code"] = code
                body = b"Authorization received. You may close this window."
                self.send_response(200)
            else:
                body = b"Authorization response was rejected."
                self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer((LOOPBACK_REDIRECT_HOST, redirect.port), Callback)
    try:
        if not webbrowser.open(authorization_url, new=1):
            print(f"Open this authorization URL in a browser:\n{authorization_url}")
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
        raise BootstrapError("authorization did not complete")
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gmail_mcp")
    parser.add_argument("--mode", choices=tuple(GOOGLE_SCOPES))
    parser.add_argument("--account-id")
    subcommands = parser.add_subparsers(dest="command")
    bootstrap = subcommands.add_parser("bootstrap")
    client_file = os.environ.get("GMAIL_OAUTH_CLIENT_FILE")
    bootstrap.add_argument(
        "--client-file",
        type=Path,
        default=Path(client_file) if client_file else None,
    )
    bootstrap.add_argument(
        "--output-directory",
        type=Path,
        default=Path(
            os.environ.get(
                "GMAIL_CREDENTIAL_DIRECTORY",
                str(Path.home() / ".config" / "veetbot" / "gmail"),
            )
        ),
    )
    bootstrap.add_argument("--account-id", dest="bootstrap_account_id")
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    if arguments.command == "bootstrap":
        if arguments.mode is not None:
            raise SystemExit("--mode and bootstrap are mutually exclusive")
        if arguments.client_file is None:
            raise SystemExit("bootstrap requires GMAIL_OAUTH_CLIENT_FILE or --client-file")
        try:
            client_id, client_secret = _oauth_client(arguments.client_file.resolve())
            asyncio.run(
                bootstrap_credentials(
                    client_id=client_id,
                    client_secret=client_secret,
                    account_id=arguments.bootstrap_account_id,
                    output_directory=arguments.output_directory.expanduser().resolve(),
                    authorize=_authorize_via_loopback,
                )
            )
        except BootstrapError as exc:
            raise SystemExit(str(exc)) from None
        return
    if arguments.mode is None:
        _parser().error("one of --mode or bootstrap is required")
    credential_value = os.environ.get("GMAIL_MCP_CREDENTIAL")
    if credential_value is None:
        raise SystemExit("gmail.credential_rejected")
    try:
        credential = GmailCredential.parse(
            credential_value,
            expected_scope=GOOGLE_SCOPES[arguments.mode],
            expected_account_id=arguments.account_id,
        )
    except GmailError as exc:
        raise SystemExit(exc.code) from None
    create_server(arguments.mode, GmailClient(credential)).run(transport="stdio")


if __name__ == "__main__":
    main(sys.argv[1:])
