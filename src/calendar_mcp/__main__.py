"""Operator consent and stdio entry point for read-only Google Calendar."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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

from calendar_mcp.bootstrap import BootstrapError, bootstrap_credentials
from calendar_mcp.client import SCOPES, CalendarClient, CalendarError
from calendar_mcp.server import create_server

LOOPBACK_REDIRECT_HOST = "127.0.0.1"
_PRIVACY_POLICY_URL = "https://www.veetbot.com/privacy"


def _oauth_client(path: Path) -> tuple[str, str]:
    """Read installed-app client settings from an owner-only regular file."""
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
    """Accept a matching-state callback on localhost within five minutes."""
    query = parse_qs(urlsplit(authorization_url).query)
    redirect = urlsplit(query["redirect_uri"][0])
    expected_state = query["state"][0]
    if redirect.hostname != LOOPBACK_REDIRECT_HOST or redirect.port is None:
        raise BootstrapError("loopback redirect is invalid")
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Reject mismatched state and retain only the accepted authorization code."""
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
            """Suppress callback request logging so authorization codes stay private."""
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
    """Present the scope-specific data-use notice before opening Google OAuth."""

    reader = input if read_input is None else read_input
    stream = sys.stdout if output is None else output
    open_authorization = _authorize_via_loopback if authorize is None else authorize
    print(
        "\n".join(
            (
                "Veetbot Calendar data-use disclosure",
                f"Scope: {SCOPES}",
                "Data accessed: Calendar names, event times, titles, descriptions, "
                "locations and attendees.",
                "Use and sharing: Veetbot uses Calendar data only for the calendar features "
                "you request. In the hosted deployment, relevant content may be sent "
                "to the OpenAI or Anthropic API solely to produce that user-facing "
                "result; a self-hosted local model keeps that processing on the host.",
                "Prohibited uses: Google data is not sold. It is not used for "
                "advertising, credit decisions, or general-purpose AI/ML training, "
                "and Veetbot does not permit an AI provider to use it for such training.",
                "Provider retention: Standard hosted API controls may retain request "
                "and response content for abuse monitoring for up to 30 days, subject "
                "to limited safety or legal exceptions.",
                "Storage and deletion: Selected Calendar data and derived output may be "
                "retained in Veetbot session history until you delete the session. "
                "Deleted data may remain in encrypted backups for no more than 35 days.",
                "Control: You may decline now, revoke Veetbot in your Google Account, "
                "disable the Calendar integration, and delete associated Veetbot sessions.",
                f"Privacy policy: {_PRIVACY_POLICY_URL}",
            )
        ),
        file=stream,
        flush=True,
    )
    if reader("Type CONTINUE to open Google's consent screen for this scope: ") != "CONTINUE":
        raise BootstrapError("Calendar authorization cancelled before consent")
    return open_authorization(mode, authorization_url)


def main(argv: list[str] | None = None) -> None:
    """Run the consent ceremony or serve the broker-provided credential over stdio."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m calendar_mcp")
    commands = parser.add_subparsers(dest="command")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--client-file", type=Path, required=True)
    bootstrap.add_argument("--output-directory", type=Path, required=True)
    bootstrap.add_argument("--calendar-id", action="append", required=True)
    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        try:
            client_id, client_secret = _oauth_client(args.client_file.expanduser().absolute())
            asyncio.run(
                bootstrap_credentials(
                    client_id=client_id,
                    client_secret=client_secret,
                    calendar_ids=args.calendar_id,
                    output_directory=args.output_directory.expanduser().absolute(),
                    authorize=_authorize_with_disclosure,
                )
            )
        except (BootstrapError, OSError):
            raise SystemExit("Calendar consent failed; check configuration and retry.") from None
        return
    try:
        client = CalendarClient(os.environ.get("CALENDAR_MCP_CREDENTIAL", ""))
    except CalendarError as exc:
        raise SystemExit(exc.code) from None
    create_server(client).run(transport="stdio")


if __name__ == "__main__":
    main(sys.argv[1:])
