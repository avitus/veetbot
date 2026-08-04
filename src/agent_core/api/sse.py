"""SSE framing where only durable sequences receive ids."""

from __future__ import annotations

import json

from agent_core.domain.views import PersistedStreamFrame, StreamFrame


def encode_sse(frame: StreamFrame) -> bytes:
    lines: list[str] = []
    if isinstance(frame, PersistedStreamFrame):
        lines.append(f"id: {frame.sequence}")
    lines.append(f"event: {frame.event}")
    lines.append(
        "data: "
        + json.dumps(
            frame.data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def heartbeat() -> bytes:
    return b": heartbeat\n\n"
