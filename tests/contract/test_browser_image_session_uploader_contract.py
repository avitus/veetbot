"""Image transfer crosses the authenticated session service once, in sequence."""

import hashlib
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserObservation
from agent_core.domain.browser_upload import BrowserImageFile, BrowserImagePayload
from agent_core.domain.credentials import SecretValue
from agent_core.domain.media import MediaImage
from agent_core.ports.browser_upload import BrowserImageSessionUploader
from tests.contract.support import NOW, principal
from tests.contract.test_hosted_profile_session_service_contract import (
    PROFILE_ID,
    PROVIDER_REF,
    RUN_ID,
    provision,
    services,
)

AUTH = "synthetic-upload-service-auth"
IMAGE = BrowserImageFile.for_artifact(
    "00000000-0000-0000-0000-000000000001", MediaImage("image/png", b"\x89PNG\r\n\x1a\nimage")
)
ACTION = BrowserAction(kind=BrowserActionKind.CLICK, expected_revision="r1", ref="r1:0")


async def test_hosted_image_upload_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, sessions, runtimes, _ = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=10),
    )
    sent = AsyncMock(
        return_value=BrowserObservation(url="https://example.org/compose", revision="r2")
    )
    monkeypatch.setattr(runtimes[0], "upload", sent, raising=False)
    app = create_profile_service_app(lifecycle, SecretValue(AUTH), sessions=sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        client = HostedBrowserSessionControlPlane(
            base_url="https://service.test",
            client=http,
            credentials=MappingCredentialResolver({"browser_profile_control_plane": AUTH}),
        )
        assert isinstance(client, BrowserImageSessionUploader)
        page = await client.upload(lease.lease_ref, ACTION, IMAGE, sequence=1)
        assert page.revision == "r2"
        sent.assert_awaited_once_with(ACTION, IMAGE)
        # Upload and ordinary actions share a sequence; an upload cannot be replayed.
        digest = hashlib.sha256(lease.lease_ref.encode()).hexdigest()[:24]
        replay = await http.post(
            "https://service.test/v1/browser-sessions:upload",
            headers={
                "Authorization": f"Bearer {AUTH}",
                "Idempotency-Key": f"browser-session:{digest}:upload:1",
            },
            json={
                "lease_ref": lease.lease_ref,
                "action": ACTION.model_dump(mode="json"),
                "sequence": 1,
                "image": BrowserImagePayload.encode(IMAGE).model_dump(),
            },
        )
        assert replay.status_code == 409
        sent.assert_awaited_once()


@pytest.mark.parametrize(
    "case", ["unauthenticated", "oversized", "other-route", "invalid-image", "grant", "bad-key"]
)
async def test_upload_http_boundary(tmp_path: Path, case: str) -> None:
    lifecycle, sessions, _, _ = services(tmp_path)
    app = create_profile_service_app(lifecycle, SecretValue(AUTH), sessions=sessions)
    path = "/v1/browser-sessions:upload"
    lease_ref = "l" * 43
    digest = hashlib.sha256(lease_ref.encode()).hexdigest()[:24]
    headers = {
        "Authorization": f"Bearer {AUTH}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"browser-session:{digest}:upload:1",
    }
    body = {
        "lease_ref": lease_ref,
        "action": ACTION.model_dump(mode="json"),
        "sequence": 1,
        "image": BrowserImagePayload.encode(IMAGE).model_dump(),
    }
    expected = 400
    if case == "unauthenticated":
        headers.pop("Authorization")
        headers["Content-Length"] = str(8 * 1024 * 1024)
        expected = 401
    elif case == "oversized":
        headers["Content-Length"] = str(7 * 1024 * 1024 + 1)
        expected = 413
    elif case == "other-route":
        path = "/v1/browser-sessions:act"
        headers["Content-Length"] = str(65537)
        expected = 413
    elif case == "invalid-image":
        body["image"] = {
            "filename": IMAGE.filename,
            "media_type": "image/png",
            "data_base64": "bad",
        }
    elif case == "grant":
        body["constraint"] = {}
    elif case == "bad-key":
        headers["Idempotency-Key"] = "wrong"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://service.test"
    ) as http:
        response = await http.post(path, headers=headers, json=body)
    assert response.status_code == expected
    assert "data_base64" not in response.text
