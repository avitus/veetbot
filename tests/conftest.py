"""Suite routing and the deterministic no-egress boundary."""

from __future__ import annotations

import socket
from collections.abc import Generator
from contextvars import ContextVar
from typing import Literal

import pytest

NetworkMode = Literal["blocked", "integration", "live"]
NETWORK_MODE: ContextVar[NetworkMode] = ContextVar("test_network_mode", default="blocked")


class GuardedSocket(socket.socket):
    """Socket that applies the marker-selected network policy for the current test."""

    def connect(self, address: object) -> None:
        mode = NETWORK_MODE.get()
        if mode == "live" or self.family == socket.AF_UNIX:
            super().connect(address)  # type: ignore[arg-type]
            return
        host = address[0] if isinstance(address, tuple) and address else None
        if mode == "integration" and host in {"127.0.0.1", "::1", "localhost"}:
            super().connect(address)  # type: ignore[arg-type]
            return
        raise RuntimeError(f"test attempted blocked network connection to host {host!r}")


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
