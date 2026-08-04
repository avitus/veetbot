"""Fail-closed sandbox environment construction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from urllib.parse import urlsplit

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
TIER_ONE_PASSTHROUGH_NAMES = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "LANG", "LC_ALL", "TZ"}
)
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|DATABASE_URL|AUTH)(?:_|$)",
    re.I,
)


def build_sandbox_environment(
    parent: Mapping[str, str],
    passthrough_names: Sequence[str] = (),
    *,
    working_directory: PurePosixPath = _WORKSPACE_ROOT,
    bridge: BridgeEndpoint | None = None,
) -> dict[str, str]:
    """Build, never filter, the environment visible to untrusted code."""

    requested = set(passthrough_names)
    forbidden = {name for name in requested if name in TIER_ZERO_NAMES or _SECRET_NAME.search(name)}
    if forbidden:
        raise ValueError(
            "tier-0 sandbox variables cannot be passed through: " + ", ".join(sorted(forbidden))
        )
    unsupported = requested - TIER_ONE_PASSTHROUGH_NAMES
    if unsupported:
        raise ValueError(
            "sandbox passthrough variables are not in the tier-1 allowlist: "
            + ", ".join(sorted(unsupported))
        )
    credentialed_proxies = set()
    for name in requested & {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
        value = parent.get(name)
        if value is None:
            continue
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if parsed.username is not None or parsed.password is not None:
            credentialed_proxies.add(name)
    if credentialed_proxies:
        raise ValueError(
            "credential-bearing proxy URLs cannot be passed through: "
            + ", ".join(sorted(credentialed_proxies))
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
