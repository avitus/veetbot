"""The real Bland stdio entrypoint over a socket-free provider, with stderr sent to a file."""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from bland_mcp import __main__ as bland_entrypoint
from bland_mcp.client import BlandClient


def main() -> None:
    """Run the production read command line with only the HTTP transport replaced."""
    stderr_path = sys.argv[1]
    number = json.loads(os.environ["BLAND_MCP_CREDENTIAL"])["configuration"]["phone_number"]

    def respond(request: httpx.Request) -> httpx.Response:
        """Answer the two provider reads instead of ever using a real network."""
        logging.getLogger("bland_mcp.fixture").warning("bland child capture probe")
        if request.method != "GET" or request.url.host != "api.bland.ai":
            raise AssertionError("the fixture only supports provider reads")
        if request.url.path == "/v1/calls":
            return httpx.Response(200, json={"calls": []})
        call_id = request.url.path.removeprefix("/v1/calls/")
        return httpx.Response(
            200,
            json={
                "call_id": call_id,
                "to": number,
                "from": "+14155550101",
                "inbound": True,
                "completed": True,
                "queue_status": "complete",
                "created_at": datetime.now(UTC).isoformat(),
                "call_length": 1.5,
                "answered_by": "human",
                "summary": "Caller asked for a callback.",
                "concatenated_transcript": "user: Please call me back.",
            },
        )

    # The credential arrives through the environment as in deployment, and the
    # stderr the parent would pass through to its journal goes to a file.
    with open(stderr_path, "a", encoding="utf-8") as stderr:
        os.dup2(stderr.fileno(), 2)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    offline = functools.partial(BlandClient, http_client=http_client)
    with patch.object(bland_entrypoint, "BlandClient", offline):
        bland_entrypoint.main(["--mode", "read"])


if __name__ == "__main__":
    main()
