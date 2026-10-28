"""Where the real-browser partition may run (ADR-0236).

Hosted browsers are headed (ADR-0145). On Linux each one draws to a private
Xvfb display; on macOS it draws to the native display, so a browser case run
on the owner's Mac opens Chromium windows on their desktop. The partition
therefore runs on Linux: `make test-browser` and `make test-browser-cases`
send it from a Mac to the sidecar, or with VEETBOT_BROWSER_ROUTE=docker to a
local linux/amd64 container, and a case collected on a
Mac host skips, which the required browser gate counts as unpassed. Setting
VEETBOT_BROWSER_ON_HOST=1 runs it on the host deliberately.
"""

from __future__ import annotations

from collections.abc import Mapping

HOST_OVERRIDE = "VEETBOT_BROWSER_ON_HOST"
ROUTE_SETTING = "VEETBOT_BROWSER_ROUTE"


def host_browser_refusal(platform: str, environment: Mapping[str, str]) -> str | None:
    """Why real Chromium cannot run here, or None when it can."""
    if platform != "darwin" or environment.get(HOST_OVERRIDE) == "1":
        return None
    return (
        "real Chromium would open windows on this Mac's desktop; run browser cases "
        "on the Linux sidecar with `make test-browser` or "
        "`make test-browser-cases CASES=<path>` (add VEETBOT_BROWSER_ROUTE=docker "
        "for a local Linux container), or set "
        f"{HOST_OVERRIDE}=1 to run them here deliberately"
    )
