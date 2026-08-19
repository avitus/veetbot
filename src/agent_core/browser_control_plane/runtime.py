"""Hosted browser session runtime with isolated storage-state ownership."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Protocol
from urllib.parse import urlsplit

from agent_core.domain.browser import (
    BrowserAction,
    BrowserAuthenticationStatus,
    BrowserInteractiveEvent,
    BrowserObservation,
    normalize_browser_origin,
)
from agent_core.domain.execution import EgressDestination, EgressMode, EgressPolicy

MAXIMUM_PROFILE_MATERIAL_BYTES = 2 * 1024 * 1024
MAXIMUM_INTERACTIVE_FRAME_BYTES = 4 * 1024 * 1024


class StatefulBrowserRuntime(Protocol):
    async def start(
        self,
        proxy_url: str,
        allowed_origins: tuple[str, ...],
        *,
        storage_state: dict[str, object],
        interactive: bool,
    ) -> None: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(self, action: BrowserAction) -> BrowserObservation: ...

    async def storage_state(self) -> dict[str, object]: ...

    async def authentication_status(self) -> BrowserAuthenticationStatus: ...

    async def interactive_frame(self) -> bytes: ...

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None: ...

    async def close(self) -> None: ...


class HostedProxy(Protocol):
    url: str

    async def close(self) -> None: ...


HostedProxyFactory = Callable[..., Awaitable[HostedProxy]]


class HostedPlaywrightSessionRuntime:
    def __init__(
        self,
        *,
        tenant_id: str,
        runtime: StatefulBrowserRuntime,
        proxy_factory: HostedProxyFactory,
    ) -> None:
        if not tenant_id:
            raise ValueError("hosted browser runtime requires a tenant")
        self._tenant_id = tenant_id
        self._runtime = runtime
        self._proxy_factory = proxy_factory
        self._proxy: HostedProxy | None = None
        self._started = False

    async def start(
        self,
        material: bytes,
        allowed_origins: tuple[str, ...],
        *,
        interactive: bool,
    ) -> None:
        if self._started:
            return
        storage_state = _decode_material(material)
        normalized = tuple(normalize_browser_origin(origin) for origin in allowed_origins)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("hosted browser runtime requires unique origins")
        destinations = tuple(
            EgressDestination(
                host=urlsplit(origin).hostname or "",
                ports=frozenset({443}),
            )
            for origin in normalized
        )
        try:
            self._proxy = await self._proxy_factory(
                EgressPolicy(EgressMode.ALLOWLIST, destinations),
                tenant_id=self._tenant_id,
            )
            await self._runtime.start(
                self._proxy.url,
                normalized,
                storage_state=storage_state,
                interactive=interactive,
            )
        except Exception:
            with suppress(Exception):
                await self._runtime.close()
            if self._proxy is not None:
                with suppress(Exception):
                    await self._proxy.close()
                self._proxy = None
            raise
        self._started = True

    async def navigate(self, url: str) -> BrowserObservation:
        return await self._runtime.navigate(url)

    async def observe(self) -> BrowserObservation:
        return await self._runtime.observe()

    async def act(self, action: BrowserAction) -> BrowserObservation:
        return await self._runtime.act(action)

    async def storage_state(self) -> bytes:
        payload = {
            "format_version": 1,
            "storage_state": await self._runtime.storage_state(),
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > MAXIMUM_PROFILE_MATERIAL_BYTES:
            raise ValueError("browser profile material exceeds its bound")
        return encoded

    async def authentication_status(self) -> BrowserAuthenticationStatus:
        return await self._runtime.authentication_status()

    async def interactive_frame(self) -> bytes:
        frame = await self._runtime.interactive_frame()
        if len(frame) > MAXIMUM_INTERACTIVE_FRAME_BYTES:
            raise ValueError("browser authentication frame exceeds its bound")
        return frame

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None:
        await self._runtime.interactive_event(event)

    async def close(self) -> None:
        try:
            await self._runtime.close()
        finally:
            if self._proxy is not None:
                await self._proxy.close()
            self._proxy = None
            self._started = False


def _decode_material(material: bytes) -> dict[str, object]:
    if len(material) > MAXIMUM_PROFILE_MATERIAL_BYTES:
        raise ValueError("browser profile material exceeds its bound")
    try:
        payload: Any = json.loads(material)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("browser profile material is invalid") from exc
    if not isinstance(payload, dict) or set(payload) not in (
        {"format_version"},
        {"format_version", "storage_state"},
    ):
        raise ValueError("browser profile material is invalid")
    if payload.get("format_version") != 1:
        raise ValueError("browser profile material version is unsupported")
    storage_state = payload.get("storage_state", {"cookies": [], "origins": []})
    if not isinstance(storage_state, dict):
        raise ValueError("browser profile storage state is invalid")
    if not {"cookies", "origins"} <= set(storage_state):
        raise ValueError("browser profile storage state is invalid")
    if not isinstance(storage_state["cookies"], list) or not isinstance(
        storage_state["origins"], list
    ):
        raise ValueError("browser profile storage state is invalid")
    return {str(key): value for key, value in storage_state.items()}
