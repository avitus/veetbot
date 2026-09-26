"""One shared harness for tests that drive a real Chromium (build plan N8).

A test serves its own Starlette site over HTTPS on a synthetic public-shaped
host, such as ``https://site.test``, and the browser reaches it through an
asyncio CONNECT relay passed to the runtime as its proxy. The relay forwards
only the synthetic host, to a Unix socket, so nothing leaves the machine and
the suite's socket guard, which allows Unix sockets only, still holds for the
test process. Chromium, a separate process, connects to the relay's loopback
port.

The browser checks the throwaway certificate as usual; only
``RealBrowserRuntime`` accepts it, through the production runtime's
``_context_options`` seam. The production runtime is otherwise unchanged:
its origin guard, its CDP document guard and its egress routing all run.

Every test that uses the harness calls ``require_real_browser()``. It skips
when Playwright's Chromium is missing, as in CI, and fails instead when
``VEETBOT_REQUIRE_REAL_BROWSER=1``, so a local run can prove the test ran.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright
from starlette.types import ASGIApp

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.execution import EgressPolicy

REQUIRE_REAL_BROWSER = "VEETBOT_REQUIRE_REAL_BROWSER"
SYNTHETIC_HOST = "site.test"


@cache
def chromium_installed() -> bool:
    """Whether Playwright's Chromium is on this machine; safe inside a running loop."""

    async def probe() -> str:
        async with async_playwright() as playwright:
            return playwright.chromium.executable_path

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            return Path(executor.submit(asyncio.run, probe()).result(timeout=60)).exists()
    except Exception:
        return False


def require_real_browser() -> None:
    """Skip without Chromium, unless ``VEETBOT_REQUIRE_REAL_BROWSER=1`` demands it."""

    if chromium_installed():
        return
    if os.environ.get(REQUIRE_REAL_BROWSER) == "1":
        pytest.fail(
            f"{REQUIRE_REAL_BROWSER}=1 but Playwright Chromium is not installed", pytrace=False
        )
    pytest.skip("Playwright Chromium is not installed")


class RealBrowserRuntime(PythonPlaywrightRuntime):
    """The production runtime, trusting the harness's throwaway certificate."""

    def _context_options(self, storage_state: dict[str, object] | None) -> dict[str, Any]:
        return super()._context_options(storage_state) | {"ignore_https_errors": True}


def _write_certificate(hosts: tuple[str, ...], directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2000, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "site.crt"
    key_path = directory / "site.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return certificate_path, key_path


@dataclass
class ConnectRelay:
    """An HTTP CONNECT proxy that tunnels only the listed ``host:port`` targets."""

    targets: dict[str, str]
    tunnelled: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    _server: asyncio.Server | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await self._server.wait_closed()
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            with suppress(BaseException):
                await task

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._tunnel(reader, writer)
        finally:
            self._tasks.discard(task)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        method, _, rest = head.decode("latin-1").partition(" ")
        target = rest.partition(" ")[0]
        upstream = self.targets.get(target) if method == "CONNECT" else None
        if upstream is None:
            self.refused.append(target)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        self.tunnelled.append(target)
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(upstream)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            with suppress(ConnectionError, asyncio.IncompleteReadError):
                while chunk := await source.read(65_536):
                    sink.write(chunk)
                    await sink.drain()
            with suppress(Exception):
                sink.write_eof()

        try:
            await asyncio.gather(pump(reader, upstream_writer), pump(upstream_reader, writer))
        finally:
            upstream_writer.close()
            with suppress(Exception):
                await upstream_writer.wait_closed()


@dataclass(frozen=True)
class _SharedProxy:
    url: str

    async def close(self) -> None:
        """The relay belongs to the site, which closes it."""


@dataclass
class LocalHttpsSite:
    """A local HTTPS site reachable from the real browser as ``origin``."""

    host: str
    relay: ConnectRelay
    egress_policies: list[EgressPolicy] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    @property
    def proxy_url(self) -> str:
        return self.relay.url

    def url(self, path: str) -> str:
        return self.origin + path

    def proxy_factory(self) -> Callable[..., Awaitable[_SharedProxy]]:
        """A hosted-runtime proxy factory that hands out the relay for this site only."""

        async def factory(policy: EgressPolicy, *, tenant_id: str) -> _SharedProxy:
            del tenant_id
            assert {destination.host for destination in policy.destinations} == {self.host}
            self.egress_policies.append(policy)
            return _SharedProxy(self.relay.url)

        return factory


@asynccontextmanager
async def local_https_site(
    app: ASGIApp, *, host: str = SYNTHETIC_HOST
) -> AsyncIterator[LocalHttpsSite]:
    """Serve ``app`` over TLS as ``https://{host}`` behind a CONNECT relay."""

    with tempfile.TemporaryDirectory(prefix="vb-site-") as directory:
        root = Path(directory)
        certificate, key = _write_certificate((host,), root)
        socket_path = str(root / "site.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(socket_path)
        config = uvicorn.Config(
            app,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
            ssl_cert_reqs=ssl.CERT_NONE,
            log_config=None,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        relay = ConnectRelay({f"{host}:443": socket_path})
        try:
            for _ in range(500):
                if server.started or serving.done():
                    break
                await asyncio.sleep(0.01)
            if not server.started:
                serving.result()
                raise RuntimeError("the local HTTPS site did not start")
            await relay.start()
            yield LocalHttpsSite(host=host, relay=relay)
        finally:
            await relay.close()
            server.should_exit = True
            with suppress(BaseException):
                await asyncio.wait_for(serving, timeout=10)
            listener.close()
