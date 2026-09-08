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
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlsplit

from gmail_mcp.bootstrap import BootstrapError, bootstrap_credentials
from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import GOOGLE_SCOPES, LOOPBACK_REDIRECT_HOST
from gmail_mcp.errors import GmailError
from gmail_mcp.server import create_server

_PRIVACY_POLICY_URL = "https://www.veetbot.com/privacy"
_DATA_BY_MODE = {
    "read": "message metadata, headers, labels, snippets, and plain-text bodies",
    "write": "draft recipients and content, plus thread and label identifiers",
    "send": "the recipient, subject, and plain-text body of the message you approve",
}


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


def _authorize_with_disclosure(
    mode: str,
    authorization_url: str,
    *,
    read_input: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    authorize: Callable[[str, str], str] | None = None,
) -> str:
    reader = input if read_input is None else read_input
    stream = sys.stdout if output is None else output
    open_authorization = _authorize_via_loopback if authorize is None else authorize
    print(
        "\n".join(
            (
                "Veetbot Gmail data-use disclosure",
                f"Scope: {GOOGLE_SCOPES[mode]}",
                f"Data accessed: {_DATA_BY_MODE[mode]}.",
                "Use and sharing: Veetbot uses Gmail data only for the email features "
                "you request. In the hosted deployment, relevant content may be sent "
                "to the OpenAI or Anthropic API solely to produce that user-facing "
                "result; a self-hosted local model keeps that processing on the host.",
                "Prohibited uses: Google data is not sold. It is not used for "
                "advertising, credit decisions, or general-purpose AI/ML training, "
                "and Veetbot does not permit an AI provider to use it for such training.",
                "Provider retention: Standard hosted API controls may retain request "
                "and response content for abuse monitoring for up to 30 days, subject "
                "to limited safety or legal exceptions.",
                "Storage and deletion: Selected Gmail data and derived output may be "
                "retained in Veetbot session history until you delete the session. "
                "Deleted data may remain in encrypted backups for no more than 35 days.",
                "Control: You may decline now, revoke Veetbot in your Google Account, "
                "disable the Gmail integration, and delete associated Veetbot sessions.",
                f"Privacy policy: {_PRIVACY_POLICY_URL}",
            )
        ),
        file=stream,
        flush=True,
    )
    if reader("Type CONTINUE to open Google's consent screen for this scope: ") != "CONTINUE":
        raise BootstrapError("Gmail authorization cancelled before consent")
    return open_authorization(mode, authorization_url)


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
                    authorize=_authorize_with_disclosure,
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
