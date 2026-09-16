"""Real Gmail MCP rosters over an entirely synthetic, socket-free mailbox."""

from __future__ import annotations

import base64
import functools
import logging
import os
import sys
from unittest.mock import patch

import httpx

from gmail_mcp import __main__ as gmail_entrypoint
from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import GOOGLE_SCOPES
from gmail_mcp.server import create_server


def main() -> None:
    """Serve one account/mode, or exit before discovery when a test requests it."""
    mode, account = sys.argv[1:3]
    if "--unavailable" in sys.argv[3:]:
        return
    stderr_path = next(
        (value.split("=", 1)[1] for value in sys.argv[3:] if value.startswith("--entrypoint=")),
        None,
    )
    entrypoint = stderr_path is not None
    message: dict[str, object] = {
        "id": "shared-message",
        "threadId": "shared-thread",
        "historyId": "100",
        "internalDate": "1789128000000",
        "labelIds": ["INBOX"],
        "snippet": "Please approve the board materials.",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": f"Colleague <colleague-{account}@example.test>"},
                {"name": "To", "value": f"{account}@example.test"},
                {"name": "Subject", "value": f"{account.title()} board materials"},
                {"name": "Date", "value": "Fri, 11 Sep 2026 12:00:00 +0000"},
                {"name": "Message-ID", "value": f"<shared-message-{account}@example.test>"},
            ],
            "body": {
                "data": base64.urlsafe_b64encode(b"Please approve the board materials.").decode(),
            },
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        """Reject unexpected operations instead of ever using a real network."""
        if entrypoint:
            logging.getLogger("gmail_mcp.fixture").warning("gmail child capture probe")
        if (
            request.method == "POST"
            and request.url.host == "oauth2.googleapis.com"
            and request.url.path == "/token"
        ):
            return httpx.Response(200, json={"access_token": "fixture-access", "expires_in": 3600})
        if request.method != "GET" or request.url.host != "gmail.googleapis.com":
            raise AssertionError("the fixture only supports read-only mailbox operations")
        if request.url.path.endswith("/profile"):
            value: dict[str, object] = {
                "emailAddress": f"{account}@example.test",
                "historyId": "100",
                "messagesTotal": 1,
                "threadsTotal": 1,
            }
        elif request.url.path.endswith("/threads"):
            value = {
                "threads": [{"id": "shared-thread"}]
                if "in:inbox" in request.url.params["q"]
                else []
            }
        elif request.url.path.endswith("/threads/shared-thread"):
            value = {"id": "shared-thread", "historyId": "100", "messages": [message]}
        elif request.url.path.endswith("/messages/shared-message"):
            value = message
        elif request.url.path.endswith("/history"):
            value = {"historyId": "100", "history": []}
        else:
            raise AssertionError("the fixture received an unexpected mailbox operation")
        return httpx.Response(200, json=value)

    if stderr_path is not None:
        # The production command line, with only the HTTP transport replaced;
        # the credential arrives through the environment as in deployment, and
        # the stderr the parent would pass through to its journal goes to a file.
        with open(stderr_path, "a", encoding="utf-8") as stderr:
            os.dup2(stderr.fileno(), 2)
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        offline = functools.partial(GmailClient, http_client=http_client)
        with patch.object(gmail_entrypoint, "GmailClient", offline):
            gmail_entrypoint.main(["--mode", mode, "--account-id", account])
        return
    credential = GmailCredential(
        client_id="fixture-client",
        client_secret="unused",
        refresh_token="unused",
        scope=GOOGLE_SCOPES[mode],
        account_id=account,
    )
    client = GmailClient(
        credential, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    create_server(mode, client).run(transport="stdio")


if __name__ == "__main__":
    main()
