"""Choose the simulators the native gates' iPhone and iPad journeys run on."""

from __future__ import annotations

from typing import Any


def newest_simulator(inventory: dict[str, Any], family: str) -> str | None:
    """The udid of the newest iOS runtime's available `family` device, ties by udid.

    The foldable iPhone Duo is skipped, as in `make test-apple-ui-ios`: it is the
    only iPhone the iOS 27.1 runtime offers, and its wide layout has none of the
    compact navigation the iPhone journeys exercise.
    """
    candidates = [
        (tuple(int(part) for part in runtime.rsplit("iOS-", 1)[1].split("-")), device["udid"])
        for runtime, values in inventory["devices"].items()
        if "iOS-" in runtime
        for device in values
        if device["name"].startswith(family)
        and device["isAvailable"]
        and not device.get("deviceTypeIdentifier", "").rsplit(".", 1)[-1].startswith("iPhone-Duo")
    ]
    return max(candidates)[1] if candidates else None
