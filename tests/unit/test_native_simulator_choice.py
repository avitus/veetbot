"""The native gates' journeys run on a standard iPhone, as `make test-apple-ui-ios` does."""

from __future__ import annotations

from tests.native_simulators import newest_simulator

RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-{}"


def device(name: str, udid: str, type_suffix: str) -> dict[str, object]:
    return {
        "name": name,
        "udid": udid,
        "isAvailable": True,
        "deviceTypeIdentifier": f"com.apple.CoreSimulator.SimDeviceType.{type_suffix}",
    }


def test_the_foldable_iphone_duo_is_never_chosen() -> None:
    # The iOS 27.1 runtime offers only the foldable iPhone Duo, whose wide
    # layout has no compact navigation; the newest standard iPhone wins.
    inventory = {
        "devices": {
            RUNTIME.format("27-1"): [device("iPhone Duo", "duo", "iPhone-Duo")],
            RUNTIME.format("27-0"): [device("iPhone 17", "standard", "iPhone-17")],
        }
    }
    assert newest_simulator(inventory, "iPhone") == "standard"


def test_the_newest_runtime_wins_and_unavailable_devices_are_skipped() -> None:
    inventory = {
        "devices": {
            RUNTIME.format("26-5"): [device("iPad Air", "old", "iPad-Air")],
            RUNTIME.format("27-0"): [
                {**device("iPad Pro", "gone", "iPad-Pro"), "isAvailable": False},
                device("iPad Pro", "new", "iPad-Pro"),
            ],
        }
    }
    assert newest_simulator(inventory, "iPad") == "new"
    assert newest_simulator({"devices": {}}, "iPhone") is None
