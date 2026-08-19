"""Dedicated process entry point for the isolated browser-profile service."""

from __future__ import annotations

import secrets

import uvicorn

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.adapters.determinism import SystemClock
from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.configuration import load_profile_service_settings
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.execution.proxy import start_worker_egress_proxy


def main() -> None:
    settings = load_profile_service_settings()
    store = FilesystemEncryptedProfileStore(settings.material_root, settings.keyring)
    clock = SystemClock()
    sessions = HostedProfileSessionService(
        store,
        runtime_factory=lambda tenant_id: HostedPlaywrightSessionRuntime(
            tenant_id=tenant_id,
            runtime=PythonPlaywrightRuntime(),
            proxy_factory=start_worker_egress_proxy,
        ),
        now=clock.now,
        process_secret=settings.session_secret.reveal().encode(),
        ceremony_base_url=settings.ceremony_base_url,
    )
    lifecycle = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: secrets.token_urlsafe(32),
        invalidate_profile=sessions.invalidate_profile,
    )
    app = create_profile_service_app(
        lifecycle,
        settings.authorization,
        sessions=sessions,
    )
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        access_log=False,
    )
