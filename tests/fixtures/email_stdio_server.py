"""Real Gmail MCP rosters over an entirely synthetic, socket-free mailbox."""

from __future__ import annotations

import base64
import sys

import httpx

from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import GOOGLE_SCOPES
from gmail_mcp.server import create_server


def main() -> None:
    """Serve one account/mode, or exit before discovery when a test requests it."""
    mode, account = sys.argv[1:3]
    if "--unavailable" in sys.argv[3:]:
        return
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
        if request.url.host == "oauth2.googleapis.com" and request.url.path == "/token":
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
