"""Suite routing and the deterministic no-egress boundary."""

from __future__ import annotations

import os
import socket
from collections.abc import Buffer, Generator
from contextvars import ContextVar
from functools import cache
from typing import Any, Literal, cast, overload
from urllib.parse import urlparse

import pytest

NetworkMode = Literal["blocked", "integration", "live"]
type SocketAddress = tuple[Any, ...] | str | Buffer
NETWORK_MODE: ContextVar[NetworkMode] = ContextVar("test_network_mode", default="blocked")


@cache
def _integration_hosts() -> set[str]:
    configured = urlparse(os.environ.get("DATABASE_URL", "")).hostname
    hosts = {"127.0.0.1", "::1", "localhost"}
    if configured is None:
        return hosts
    hosts.add(configured)
    try:
        for _family, _type, _protocol, _canonical, address in socket.getaddrinfo(
            configured, None, type=socket.SOCK_STREAM
        ):
            resolved = address[0]
            if isinstance(resolved, str):
                hosts.add(resolved)
    except socket.gaierror:
        pass
    return hosts


class GuardedSocket(socket.socket):
    """Socket that applies the marker-selected network policy for the current test."""

    def _guard_address(self, address: object) -> None:
        mode = NETWORK_MODE.get()
        if mode == "live" or self.family == socket.AF_UNIX:
            return
        host = address[0] if isinstance(address, tuple) and address else None
        if mode == "integration" and host in _integration_hosts():
            return
        raise RuntimeError(f"test attempted blocked network connection to host {host!r}")

    def connect(self, address: object) -> None:
        self._guard_address(address)
        super().connect(address)  # type: ignore[arg-type]

    def connect_ex(self, address: object) -> int:
        self._guard_address(address)
        return super().connect_ex(address)  # type: ignore[arg-type]

    @overload
    def sendto(self, data: Buffer, address: SocketAddress, /) -> int: ...

    @overload
    def sendto(self, data: Buffer, flags: int, address: SocketAddress, /) -> int: ...

    def sendto(
        self,
        data: Buffer,
        address_or_flags: int | SocketAddress,
        address: SocketAddress | None = None,
        /,
    ) -> int:
        destination = address_or_flags if address is None else address
        self._guard_address(destination)
        if address is None:
            return super().sendto(data, cast(SocketAddress, address_or_flags))
        return super().sendto(data, cast(int, address_or_flags), address)

    def sendmsg(
        self,
        buffers: object,
        ancdata: object = (),
        flags: int = 0,
        address: object | None = None,
    ) -> int:
        if address is not None:
            self._guard_address(address)
            return super().sendmsg(buffers, ancdata, flags, address)  # type: ignore[arg-type]
        return super().sendmsg(buffers, ancdata, flags)  # type: ignore[arg-type]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the directory-to-marker routing declared by the toolchain spec."""

    for item in items:
        path = item.path.as_posix()
        if "/tests/gates/" in path or "/tests/unit/" in path:
            item.add_marker(pytest.mark.static)
        elif any(
            part in path
            for part in ("/tests/integration/", "/tests/resilience/", "/tests/security/")
        ):
            item.add_marker(pytest.mark.integration)
        elif "/tests/live/" in path:
            item.add_marker(pytest.mark.live)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Select the policy before pytest constructs test-scoped fixtures."""

    if item.get_closest_marker("live") is not None:
        NETWORK_MODE.set("live")
    elif item.get_closest_marker("integration") is not None:
        NETWORK_MODE.set("integration")
    else:
        NETWORK_MODE.set("blocked")


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown() -> None:
    """Fail closed between tests, including after fixture teardown failures."""

    NETWORK_MODE.set("blocked")


@pytest.fixture(scope="session", autouse=True)
def block_test_egress() -> Generator[None]:
    """Install one suite-wide socket guard; markers select its per-test policy."""

    original_socket = socket.socket
    socket.socket = GuardedSocket  # type: ignore[misc]
    try:
        yield
    finally:
        socket.socket = original_socket  # type: ignore[misc]
