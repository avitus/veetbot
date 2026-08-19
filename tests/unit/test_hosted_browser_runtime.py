"""Hosted Playwright runtime keeps storage state inside the isolated service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from agent_core.browser_control_plane.runtime import (
    MAXIMUM_INTERACTIVE_FRAME_BYTES,
    HostedPlaywrightSessionRuntime,
)
from agent_core.domain.browser import (
    BrowserAction,
    BrowserAuthenticationStatus,
    BrowserInteractiveEvent,
    BrowserObservation,
)
from agent_core.domain.execution import EgressMode, EgressPolicy


@dataclass
class FakeProxy:
    url: str = "http://127.0.0.1:8123"
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeStatefulRuntime:
    storage: dict[str, object] = field(default_factory=dict)
    started: tuple[str, tuple[str, ...], dict[str, object], bool] | None = None
    closed: bool = False

    async def start(
        self,
        proxy_url: str,
        allowed_origins: tuple[str, ...],
        *,
        storage_state: dict[str, object],
        interactive: bool,
    ) -> None:
        self.started = (proxy_url, allowed_origins, storage_state, interactive)

    async def navigate(self, url: str) -> BrowserObservation:
        return BrowserObservation(url=url, revision="r1")

    async def observe(self) -> BrowserObservation:
        return BrowserObservation(url="https://example.org", revision="r1")

    async def act(self, action: BrowserAction) -> BrowserObservation:
        del action
        return BrowserObservation(url="https://example.org", revision="r2")

    async def storage_state(self) -> dict[str, object]:
        return self.storage

    async def authentication_status(self) -> BrowserAuthenticationStatus:
        return BrowserAuthenticationStatus.NEEDS_USER

    async def interactive_frame(self) -> bytes:
        return b"png"

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None:
        del event

    async def close(self) -> None:
        self.closed = True


async def test_hosted_runtime_loads_and_seals_versioned_storage_state() -> None:
    low_level = FakeStatefulRuntime(storage={"cookies": [], "origins": []})
    proxy = FakeProxy()
    policies: list[EgressPolicy] = []

    async def proxy_factory(policy: EgressPolicy, *, tenant_id: str):  # type: ignore[no-untyped-def]
        assert tenant_id == "tenant-a"
        policies.append(policy)
        return proxy

    runtime = HostedPlaywrightSessionRuntime(
        tenant_id="tenant-a",
        runtime=low_level,
        proxy_factory=proxy_factory,
    )
    material = json.dumps(
        {
            "format_version": 1,
            "storage_state": {"cookies": [{"name": "session"}], "origins": []},
        }
    ).encode()

    await runtime.start(material, ("https://example.org",), interactive=True)
    sealed = json.loads(await runtime.storage_state())
    status = await runtime.authentication_status()
    await runtime.close()

    assert low_level.started is not None
    assert low_level.started[2] == {"cookies": [{"name": "session"}], "origins": []}
    assert low_level.started[3] is True
    assert sealed == {
        "format_version": 1,
        "storage_state": {"cookies": [], "origins": []},
    }
    assert status is BrowserAuthenticationStatus.NEEDS_USER
    assert policies[0].mode is EgressMode.ALLOWLIST
    assert {(item.host, item.ports) for item in policies[0].destinations} == {
        ("example.org", frozenset({443}))
    }
    assert proxy.closed is True
    assert low_level.closed is True


@pytest.mark.parametrize(
    "material",
    [
        b"not-json",
        b'{"format_version":2,"storage_state":{}}',
        b'{"format_version":1,"storage_state":[],"extra":true}',
    ],
)
async def test_hosted_runtime_rejects_invalid_material_before_browser_start(
    material: bytes,
) -> None:
    low_level = FakeStatefulRuntime()
    runtime = HostedPlaywrightSessionRuntime(
        tenant_id="tenant-a",
        runtime=low_level,
        proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError):
        await runtime.start(material, ("https://example.org",), interactive=False)

    assert low_level.started is None


async def test_hosted_runtime_rejects_oversized_interactive_frames() -> None:
    class OversizedFrameRuntime(FakeStatefulRuntime):
        async def interactive_frame(self) -> bytes:
            return b"x" * (MAXIMUM_INTERACTIVE_FRAME_BYTES + 1)

    low_level = OversizedFrameRuntime()
    runtime = HostedPlaywrightSessionRuntime(
        tenant_id="tenant-a",
        runtime=low_level,
        proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="frame exceeds"):
        await runtime.interactive_frame()
