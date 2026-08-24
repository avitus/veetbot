"""The stdio entrypoint and the bootstrap consent command.

``python -m gmail_mcp --mode read|write|send`` serves one roster; the
credential arrives only through the one declared environment variable.
``python -m gmail_mcp bootstrap`` runs the one-time consent ceremony.
Startup failures print content-free reasons to stderr — never the credential
value, never argv-borne secrets, because there are none.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
import httpx

from gmail_mcp.bootstrap import loopback_grant_source, run_bootstrap
from gmail_mcp.credential import CREDENTIAL_VARIABLE, GmailCredential, RefreshingTokenSource
from gmail_mcp.gmail import GmailClient
from gmail_mcp.server import MODES, build_server

GMAIL_BASE_URL = "https://gmail.googleapis.com"


def _bootstrap_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gmail_mcp bootstrap")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    options = parser.parse_args(argv)
    try:
        client_secret = options.client_secret_file.read_text(encoding="ascii").strip()
    except OSError:
        print("gmail_mcp: the client secret file is unavailable", file=sys.stderr)
        return 2
    if not client_secret:
        print("gmail_mcp: the client secret file is empty", file=sys.stderr)
        return 2
    with httpx.Client() as http:
        return run_bootstrap(
            client_id=options.client_id,
            client_secret=client_secret,
            output_directory=options.output_dir,
            http=http,
            grant_source=loopback_grant_source(),
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:]) if argv is None else list(argv)
    if arguments[:1] == ["bootstrap"]:
        return _bootstrap_main(arguments[1:])

    parser = argparse.ArgumentParser(prog="gmail_mcp")
    parser.add_argument("--mode", required=True, choices=sorted(MODES))
    options = parser.parse_args(arguments)

    raw = os.environ.get(CREDENTIAL_VARIABLE)
    if raw is None:
        print(f"gmail_mcp: {CREDENTIAL_VARIABLE} is not set", file=sys.stderr)
        return 2
    try:
        credential = GmailCredential.parse(raw)
    except ValueError as error:
        print(f"gmail_mcp: {error}", file=sys.stderr)
        return 2

    async def serve() -> None:
        async with (
            httpx.AsyncClient() as token_http,
            httpx.AsyncClient(base_url=GMAIL_BASE_URL) as gmail_http,
        ):
            tokens = RefreshingTokenSource(credential, http=token_http)
            gmail = GmailClient(http=gmail_http, token_source=tokens)
            await build_server(options.mode, gmail).run_stdio_async()

    anyio.run(serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
