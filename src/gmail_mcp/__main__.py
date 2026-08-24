"""The stdio entrypoint: ``python -m gmail_mcp --mode read|write|send``.

The mode selects the roster; the credential arrives only through the one
declared environment variable. Startup failures print content-free reasons
to stderr — never the credential value, never argv-borne secrets, because
there are none.
"""

from __future__ import annotations

import argparse
import os
import sys

import anyio
import httpx

from gmail_mcp.credential import CREDENTIAL_VARIABLE, GmailCredential, RefreshingTokenSource
from gmail_mcp.gmail import GmailClient
from gmail_mcp.server import MODES, build_server

GMAIL_BASE_URL = "https://gmail.googleapis.com"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmail_mcp")
    parser.add_argument("--mode", required=True, choices=sorted(MODES))
    options = parser.parse_args(argv)

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
