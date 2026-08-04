"""Fail-closed sandbox environment construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from agent_core.domain.execution import BridgeEndpoint

TIER_ZERO_NAMES = frozenset(
    {
        "AGENT_DATABASE_URL",
        "AGENT_AUTH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "VEETBOT_OPENAI_KEY",
    }
)
_WORKSPACE_ROOT = PurePosixPath("/workspace")


def build_sandbox_environment(
    parent: Mapping[str, str],
    passthrough_names: Sequence[str] = (),
    *,
    working_directory: PurePosixPath = _WORKSPACE_ROOT,
    bridge: BridgeEndpoint | None = None,
) -> dict[str, str]:
    """Build, never filter, the environment visible to untrusted code."""

    requested = set(passthrough_names)
    forbidden = requested & TIER_ZERO_NAMES
    if forbidden:
        raise ValueError(
            "tier-0 sandbox variables cannot be passed through: " + ", ".join(sorted(forbidden))
        )
    result = {
        "HOME": "/workspace",
        "PWD": str(working_directory),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
    }
    result.update({name: parent[name] for name in requested if name in parent})
    if bridge is not None:
        result["AGENT_TOOL_BRIDGE_SOCKET"] = str(bridge.socket_path)
        result["AGENT_TOOL_BRIDGE_TOKEN"] = bridge.token
    return result
