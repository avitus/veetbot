"""The profile service's logs never carry exception detail (ADR-0128 D18, S3).

Starlette re-raises every exception that reaches the generic handler, and
uvicorn logs it on ``uvicorn.error`` with its message, traceback and chained
causes. A pydantic error prints its input, a Playwright error carries URLs and
a ``KeyError`` carries its key, so the service's one log handler strips all of
it. These tests run the real application under an in-process uvicorn server.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from uvicorn.config import LOGGING_CONFIG

from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.handoff import DeviceSessionHandoff
from agent_core.browser_control_plane.log_redaction import (
    ExceptionDetailFilter,
    profile_service_log_config,
)
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue

OPAQUE_AUTH_VALUE = "synthetic-log-redaction-auth-value"
SENTINEL_VALUE = "log-sentinel-cookie-value"
SENTINEL_NAME = "log-sentinel-cookie-name"
CEREMONY_ID = UUID("00000000-0000-4000-8000-0000000000c1")
_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "agent_core")


def _sentinel_failure() -> RuntimeError:
    try:
        raise KeyError(SENTINEL_NAME)
    except KeyError as cause:
        try:
            raise RuntimeError(f"verification failed for {SENTINEL_VALUE}") from cause
        except RuntimeError as failure:
            return failure


class ExplodingSessions:
    """A session service whose every answer is an exception carrying sentinels."""

    async def refresh_authentication(self, ceremony_id: UUID, principal: Principal) -> Any:
        del ceremony_id, principal
        raise _sentinel_failure()

    async def authenticate_surface(
        self, ceremony_id: UUID, capability: str, operation: str = "frame"
    ) -> bool:
        del ceremony_id, capability, operation
        return True

    async def accept_device_session(
        self, ceremony_id: UUID, capability: str, handoff: DeviceSessionHandoff
    ) -> None:
        del ceremony_id, capability, handoff
        raise _sentinel_failure()


class NoLifecycle:
    """The lifecycle routes are not exercised here."""


def exploding_app() -> FastAPI:
    return create_profile_service_app(
        NoLifecycle(),  # type: ignore[arg-type]
        SecretValue(OPAQUE_AUTH_VALUE),
        sessions=ExplodingSessions(),  # type: ignore[arg-type]
    )


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """uvicorn.Config applies its log config globally; put everything back after."""

    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    saved = {
        name: (
            logging.getLogger(name).level,
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in _LOGGERS
    }
    try:
        yield
    finally:
        root.setLevel(saved_root[0])
        root.handlers[:] = saved_root[1]
        for name, (level, handlers, propagate, disabled) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.disabled = disabled


@asynccontextmanager
async def serving(app: FastAPI, log_config: dict[str, Any]) -> AsyncIterator[httpx.AsyncClient]:
    """Run ``app`` under a real uvicorn server on a Unix socket."""

    with tempfile.TemporaryDirectory(prefix="vb-log-") as directory:
        path = str(Path(directory) / "service.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        server = uvicorn.Server(
            uvicorn.Config(app, log_config=log_config, access_log=False, lifespan="off")
        )
        serve = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(500):
                if server.started or serve.done():
                    break
                await asyncio.sleep(0.01)
            assert server.started, "the in-process server did not start"
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=path), base_url="http://service.test"
            ) as client:
                yield client
        finally:
            server.should_exit = True
            await asyncio.wait_for(serve, timeout=10)
            listener.close()


async def _status_request(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/v1/browser-authentications:status",
        headers={
            "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
            "Content-Type": "application/json",
        },
        json={"ceremony_id": str(CEREMONY_ID), "tenant_id": "tenant-a", "principal_id": "p"},
    )


async def _handoff_request(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        f"/authentication/{CEREMONY_ID}/handoff",
        headers={
            "X-Browser-Ceremony-Capability": "c" * 43,
            "Content-Type": "application/json",
        },
        json={"confirmed_url": "https://example.org/learn", "cookies": [], "origins": []},
    )


@pytest.mark.usefixtures("restored_logging")
async def test_uvicorn_error_records_carry_no_exception_detail(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Control: under uvicorn's default configuration the detail does reach
    # stderr, so this test can see a leak.
    async with serving(exploding_app(), LOGGING_CONFIG) as client:
        leaked = await _status_request(client)
    default_stderr = capfd.readouterr().err
    assert leaked.status_code == 500
    assert "Exception in ASGI application" in default_stderr
    assert SENTINEL_VALUE in default_stderr

    async with serving(exploding_app(), profile_service_log_config()) as client:
        failed = await _status_request(client)
        handed_off = await _handoff_request(client)
    service_stderr = capfd.readouterr().err

    assert failed.status_code == 500
    assert handed_off.status_code == 500
    assert handed_off.json() == {
        "error": {"code": "internal_error", "message": "service unavailable"}
    }
    assert "profile service request failed (RuntimeError)" in service_stderr
    assert "device handoff failed (RuntimeError)" in service_stderr
    assert service_stderr.count("Exception in ASGI application") == 0
    assert "Traceback" not in service_stderr
    assert SENTINEL_VALUE not in service_stderr
    assert SENTINEL_NAME not in service_stderr


def _record(**fields: Any) -> logging.LogRecord:
    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "%s", ("x",), None)
    for key, value in fields.items():
        setattr(record, key, value)
    return record


def test_the_filter_strips_every_form_of_exception_detail() -> None:
    failure = _sentinel_failure()
    detail_filter = ExceptionDetailFilter()
    with_info = _record(exc_info=(RuntimeError, failure, failure.__traceback__))
    with_text = _record(exc_text=f"Traceback: {SENTINEL_VALUE}")
    with_stack = _record(stack_info=f"Stack: {SENTINEL_VALUE}")
    typed = _record(msg="device handoff failed", args=None, failure_type="KeyError")

    for record in (with_info, with_text, with_stack, typed):
        assert detail_filter.filter(record) is True

    assert with_info.getMessage() == "profile service request failed (RuntimeError)"
    assert with_text.getMessage() == "profile service request failed (Exception)"
    assert with_stack.getMessage() == "profile service request failed (Exception)"
    for record in (with_info, with_text, with_stack):
        assert (record.exc_info, record.exc_text, record.stack_info, record.args) == (
            None,
            None,
            None,
            None,
        )
    assert typed.getMessage() == "device handoff failed (KeyError)"


def test_the_service_log_config_routes_everything_through_one_filtered_handler() -> None:
    config = profile_service_log_config()

    assert config["disable_existing_loggers"] is False
    assert config["handlers"] == {
        "service": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "service",
            "filters": ["exception_detail"],
        }
    }
    assert config["root"] == {"level": "WARNING", "handlers": ["service"]}
    loggers = config["loggers"]
    assert loggers["uvicorn"] == {"level": "INFO", "handlers": ["service"], "propagate": False}
    assert loggers["uvicorn.error"]["propagate"] is True
    assert loggers["uvicorn.error"].get("handlers", []) == []
    assert loggers["uvicorn.access"].get("handlers", []) == []
    assert loggers["uvicorn.access"]["propagate"] is False
    assert loggers["agent_core"] == {"level": "INFO", "propagate": True}
