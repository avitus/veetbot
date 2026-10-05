"""The real Scale VP stdio entrypoint over socket-free transports, with stderr sent to a file."""

from __future__ import annotations

import functools
import logging
import os
import sys
from unittest.mock import patch

import httpx

from svp_mcp import __main__ as svp_entrypoint
from svp_mcp.client import SvpClient
from svp_mcp.constants import DOCUMENT_URL, TOKEN_ENDPOINT
from svp_mcp.credential import CredentialStore

DOCUMENT = {
    "paths": {
        "/companies": {
            "get": {
                "operationId": "list_companies",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            }
        }
    }
}


def main() -> None:
    """Run the production read command line with only the HTTP transports replaced."""
    stderr_path = sys.argv[1]

    def token(request: httpx.Request) -> httpx.Response:
        """Rotate the grant instead of ever using a real network."""
        if request.method != "POST" or str(request.url) != TOKEN_ENDPOINT:
            raise AssertionError("the fixture only supports the token endpoint")
        return httpx.Response(
            200,
            json={
                "access_token": "access-2",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "refresh-2",
            },
        )

    def api(request: httpx.Request) -> httpx.Response:
        logging.getLogger("svp_mcp.fixture").warning("svp child capture probe")
        rotated = request.headers["authorization"].split() == ["Bearer", "access-2"]
        if request.method != "GET" or not rotated:
            raise AssertionError("the fixture only supports reads with the rotated token")
        if str(request.url) == DOCUMENT_URL:
            return httpx.Response(200, json=DOCUMENT)
        return httpx.Response(200, json={"items": ["Acme"]})

    # The grant's path arrives through the environment as in deployment, and the
    # stderr the parent would pass through to its journal goes to a file.
    with open(stderr_path, "a", encoding="utf-8") as stderr:
        os.dup2(stderr.fileno(), 2)
    store = functools.partial(
        CredentialStore, http=httpx.Client(transport=httpx.MockTransport(token))
    )
    client = functools.partial(
        SvpClient, http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))
    )
    with (
        patch.object(svp_entrypoint, "CredentialStore", store),
        patch.object(svp_entrypoint, "SvpClient", client),
    ):
        svp_entrypoint.main(["--mode", "read"])


if __name__ == "__main__":
    main()
