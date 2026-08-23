"""Device-management request identity regressions."""

from __future__ import annotations

import os
import subprocess
import sys


def test_registration_hash_is_stable_across_python_hash_seeds() -> None:
    script = """
from pydantic import SecretStr
from agent_core.application.device_management import _registration_hash
from agent_core.domain.devices import DeviceKind, DeviceRegistration, PushEnvironment, PushProvider
from agent_core.domain.notifications import NotificationKind

registration = DeviceRegistration(
    client_device_id="installation-a",
    name="Owner phone",
    kind=DeviceKind.MOBILE,
    platform="ios",
    app_bundle_id="com.veetbot.apple",
    push_provider=PushProvider.APNS,
    push_token=SecretStr("push-token"),
    push_environment=PushEnvironment.SANDBOX,
    muted_kinds=frozenset(NotificationKind),
)
print(_registration_hash(registration))
"""
    hashes = {
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        ).stdout.strip()
        for seed in range(1, 6)
    }

    assert len(hashes) == 1
