"""Official-SDK stdio fixture that returns exactly its process environment."""

from __future__ import annotations

import json
import os

from mcp.server import MCPServer

server = MCPServer("environment-fixture")


@server.tool()
def echo_environment() -> str:
    return json.dumps(dict(os.environ), sort_keys=True)


if __name__ == "__main__":
    server.run(transport="stdio")
