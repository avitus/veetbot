"""Operator-owned stdio MCP adapter to one locally or SSH-forwarded Unix socket."""

from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import WithJsonSchema

from final_cut.plan import Change, Handle, Shot
from final_cut.protocol import (
    Cut,
    Excerpt,
    Handoff,
    Inventory,
    Recommend,
    Request,
    Revision,
    Select,
)

# Veetbot rejects remote JSON references. Inline this closed, two-level schema
# when advertising the tools; Pydantic still validates the original types.
_shot_schema = Shot.model_json_schema()
_change_schema = Change.model_json_schema()
_change_schema.pop("$defs")
_change_schema["properties"]["shot"]["anyOf"][0] = _shot_schema
Shots = Annotated[
    list[Shot],
    WithJsonSchema({"type": "array", "items": _shot_schema, "minItems": 1, "maxItems": 64}),
]
Changes = Annotated[
    list[Change],
    WithJsonSchema({"type": "array", "items": _change_schema, "minItems": 1, "maxItems": 64}),
]


def error(message: str, *, uncertain: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content={"effect_status": "unknown" if uncertain else "not_applied"},
        is_error=True,
    )


def create_server(endpoint: Path) -> MCPServer:
    server = MCPServer("final_cut")

    def call(request: Request) -> CallToolResult:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(300)
            try:
                sock.connect(str(endpoint))
            except OSError:
                return error("Mac offline. Start its editor and reconnect the socket.")
            try:
                sock.sendall(request.model_dump_json().encode() + b"\n")
                with sock.makefile("rb") as stream:
                    line = stream.readline(131072)
                    result = json.loads(line)
                    if not isinstance(result, dict) or not line.endswith(b"\n"):
                        raise ValueError("Incomplete reply.")
            except (OSError, ValueError):
                message = (
                    "Inventory reply lost. Reconnect the Mac and inspect a smaller folder."
                    if isinstance(request, Inventory)
                    else "Mac acknowledgement lost. Check the version before retrying any handoff."
                )
                return error(message, uncertain=True)
        if "error" in result:
            return error(str(result["error"]), uncertain=isinstance(request, Handoff))
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))], structured_content=result
        )

    @server.prompt()
    def editing() -> str:
        """Edit and revise a highlight reel on the owner's Final Cut Mac."""
        return (
            "Inspect the current selection or ask the owner to choose one. Excerpt candidate "
            "ranges with analyze=true. Compare action, delivery, pauses and repeated speech. "
            "For local-only footage use recommend with the excerpt evidence IDs. Never "
            "claim to have inspected unseen media. Use source-relative integer frames and "
            "exclusive ends. Preserve natural audio. Return the local playable preview link. "
            "Use revise on the named parent with only the requested shot edits. Deliver the "
            "approved version and link its movie and editable XML. Report movie verification "
            "and Final Cut import separately."
        )

    @server.tool()
    def select_folder() -> CallToolResult:
        """Show a read-only folder picker and separate notes-sharing choice on the editing Mac."""
        return call(Select(operation="select"))

    @server.tool()
    def inventory() -> CallToolResult:
        """List the active Mac grant, stable clip IDs, frame rates, durations, and source starts."""
        return call(Inventory(operation="inventory"))

    @server.tool()
    def excerpt(
        grant: Handle, clip: Handle, first: int, end: int, analyze: bool = True
    ) -> CallToolResult:
        """Inspect up to 15 seconds from source-relative first through exclusive end frames.
        Returns local range playback with sound, labeled contact sheet and approved local analysis.
        """
        return call(
            Excerpt(
                operation="excerpt",
                grant=grant,
                shot=Shot(clip=clip, first=first, end=end),
                analyze=analyze,
            )
        )

    @server.tool()
    def recommend(
        grant: Handle, evidence: list[Handle], seconds: int, brief: str = ""
    ) -> CallToolResult:
        """Rank analyzed excerpts on the Mac for a highlight reel. Returns only chosen ranges.
        Pass the owner's editorial brief. If more frames are needed, analyze additional ranges.
        """
        return call(
            Recommend(
                operation="recommend", grant=grant, evidence=evidence, seconds=seconds, brief=brief
            )
        )

    @server.tool()
    def rough_cut(grant: Handle, shots: Shots) -> CallToolResult:
        """Render a playable preview and editable timeline from an ordered integer-frame plan."""
        return call(Cut(operation="cut", grant=grant, shots=shots))

    @server.tool()
    def revise(grant: Handle, version: Handle, changes: Changes) -> CallToolResult:
        """Revise only listed zero-based shots in order. Retain the named parent and other shots."""
        return call(Revision(operation="revise", grant=grant, version=version, changes=changes))

    @server.tool()
    def deliver(grant: Handle, version: Handle) -> CallToolResult:
        """Deliver the approved movie and request Final Cut import once for this cut version."""
        return call(Handoff(operation="deliver", grant=grant, version=version))

    @server.tool()
    def reconcile(grant: Handle, version: Handle) -> CallToolResult:
        """Choose re-exported XML on the Mac to verify the project, online media and frame cuts."""
        return call(Handoff(operation="reconcile", grant=grant, version=version))

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("socket", type=Path)
    create_server(parser.parse_args().socket).run(transport="stdio")
