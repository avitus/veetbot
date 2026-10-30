"""ADR-0270 secret input never becomes a read surface."""

from pathlib import Path
from uuid import uuid4

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.config import load_settings
from tests.unit.test_config import base_environment

CANARY = "synthetic-integration-canary"


async def test_settings_saves_lists_replaces_and_deletes_without_returning_value(
    tmp_path: Path,
) -> None:
    key = tmp_path / "master.key"
    key.write_bytes(bytes(range(32)))
    key.chmod(0o600)
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "AGENT_INTEGRATION_SECRETS_ENABLED": "1",
            "INTEGRATION_SECRETS_KEY_FILE": str(key),
        }
    )
    async with build(settings=settings, storage="memory") as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client:
            path = f"/v1/integration-secrets/{uuid4()}"
            body = {
                "label": "Example",
                "origin": "https://api.example.com",
                "auth_scheme": "bearer",
                "value": CANARY,
                "expected_revision": 0,
            }
            saved = await client.put(path, json=body)
            assert saved.status_code == 200, saved.text
            assert saved.json()["revision"] == 1
            assert CANARY not in saved.text
            assert (await client.put(path, json=body)).json() == saved.json()
            listed = await client.get("/v1/integration-secrets")
            assert listed.json()["secrets"] == [saved.json()]
            assert "no-store" in listed.headers["cache-control"]
            body.update(value="synthetic-replacement", expected_revision=1)
            assert (await client.put(path, json=body)).json()["revision"] == 2
            assert (await client.delete(path, headers={"If-Match": "1"})).status_code == 409
            assert (await client.delete(path, headers={"If-Match": "2"})).status_code == 204
            assert (await client.delete(path, headers={"If-Match": "2"})).status_code == 204
            assert (await client.get("/v1/integration-secrets")).json()["secrets"] == []
            assert (await client.put(path, json=body)).status_code == 409
