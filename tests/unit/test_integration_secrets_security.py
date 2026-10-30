"""Synthetic secrets stay behind authenticated, owner-bound Settings writes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_core.adapters.sealing import AESGCMSecretSealer
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import ConfigurationError, load_schedule_worker_settings, load_settings
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import ConflictError, NotFoundError
from tests.unit.test_config import base_environment

CANARY = "synthetic/+integration-canary=="


def environment(tmp_path: Path) -> dict[str, str]:
    key = tmp_path / "master.key"
    key.write_bytes(bytes(range(32)))
    key.chmod(0o600)
    return {
        **base_environment(),
        "SANDBOX_MECHANISM": "fake",
        "AGENT_INTEGRATION_SECRETS_ENABLED": "1",
        "INTEGRATION_SECRETS_KEY_FILE": str(key),
    }


@asynccontextmanager
async def enabled(tmp_path: Path) -> AsyncIterator[Composition]:
    async with build(settings=load_settings(environment(tmp_path)), storage="memory") as app:
        yield app


def body(**changes: object) -> dict[str, object]:
    return {
        "label": "Example",
        "origin": "https://api.example.com",
        "auth_scheme": "bearer",
        "value": CANARY,
        "expected_revision": 0,
        **changes,
    }


async def test_scope_validation_limits_and_disabled_routes(tmp_path: Path) -> None:
    async with enabled(tmp_path) as app:
        for scopes, expected in [(set(), 403), ({"settings.read", "settings.write"}, 400)]:
            principal = app.principal.model_copy(update={"scopes": scopes})
            api = create_app(
                app.services, app.settings, principal, app.new_request_id, app.readiness_probe
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
            ) as client:
                path = f"/v1/integration-secrets/{uuid4()}"
                response = await client.put(path, json=body(**{CANARY: CANARY}))
                assert response.status_code == expected
                assert CANARY not in response.text
                assert "no-store" in response.headers["cache-control"]
                if scopes:
                    for payload in [b'{"value": "' + CANARY.encode(), b"x" * 8193]:
                        response = await client.put(path, content=payload)
                        assert response.status_code in {400, 413}
                        assert CANARY not in response.text
                    response = await client.put(f"/v1/integration-secrets/{CANARY}", json=body())
                    assert CANARY not in response.text
                    response = await client.delete(path, headers={"If-Match": CANARY})
                    assert response.status_code == 400 and CANARY not in response.text
        api = create_app(
            app.services,
            replace(app.settings, integration_secrets_enabled=False),
            app.principal,
            app.new_request_id,
            app.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
        ) as client:
            assert (await client.get("/v1/integration-secrets")).status_code == 404


async def test_owner_binding_revision_and_ciphertext_integrity(tmp_path: Path) -> None:
    async with enabled(tmp_path) as app:
        service = app.services.integration_secrets
        assert service is not None
        key_id = uuid4()
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="bearer",
            value=SecretValue(CANARY),
            expected_revision=0,
        )
        reference = f"stored:{key_id}"
        record = await service.binding(app.principal, reference, "api.example.com")
        assert CANARY not in repr(record) and CANARY not in str(record.metadata())
        assert record.sealed_value is not None and CANARY not in record.sealed_value
        for owner in [
            app.principal.model_copy(update={"principal_id": "other"}),
            app.principal.model_copy(update={"tenant_id": "other"}),
        ]:
            assert await service.list(owner) == []
            with pytest.raises(NotFoundError):
                await service.resolve(owner, reference, "api.example.com")
            with pytest.raises(NotFoundError):
                await service.delete(owner, key_id, 1)
        with pytest.raises(NotFoundError):
            await service.resolve(app.principal, reference, "other.example.com")
        for modified in [
            replace(record, id=uuid4()),
            replace(record, principal_id="other"),
            replace(record, tenant_id="other"),
            replace(record, revision=2),
            replace(record, origin="https://other.example.com"),
            replace(record, auth_scheme="x-api-key"),
        ]:
            with pytest.raises(PermissionError):
                service.sealer.open(record.sealed_value, modified.context())
        for sealer in [
            AESGCMSecretSealer(bytes(32), purpose="veetbot.integration-secret"),
            AESGCMSecretSealer(bytes(range(32))),
        ]:
            with pytest.raises(PermissionError):
                sealer.open(record.sealed_value, record.context())
        with pytest.raises(PermissionError):
            service.sealer.open(record.sealed_value[:-8] + "AAAAAAAA", record.context())
        with pytest.raises(ConflictError):
            await service.put(
                app.principal,
                key_id,
                label="Changed",
                origin="https://api.example.com",
                auth_scheme="bearer",
                value=SecretValue(CANARY),
                expected_revision=1,
            )


@pytest.mark.parametrize(
    "origin",
    [
        "http://api.example.com",
        "https://api.example.com/path",
        "https://" + "user:synthetic@api.example.com",
        "https://127.0.0.1",
        "https://localhost",
        "https://*.example.com",
        "https://api.example.com:444",
        "https://api.example.com?key=x",
        "https://api.example.com.",
        "https://café.example.com",
    ],
)
async def test_invalid_origins_are_never_saved(tmp_path: Path, origin: str) -> None:
    async with enabled(tmp_path) as app:
        service = app.services.integration_secrets
        assert service is not None
        with pytest.raises(ValueError):
            await service.put(
                app.principal,
                uuid4(),
                label="Example",
                origin=origin,
                auth_scheme="bearer",
                value=SecretValue(CANARY),
                expected_revision=0,
            )
        assert await service.list(app.principal) == []


@pytest.mark.parametrize("mode,size", [(0o644, 32), (0o600, 31), (0o600, 33)])
def test_invalid_master_key_file_refuses_startup(tmp_path: Path, mode: int, size: int) -> None:
    env = environment(tmp_path)
    key = Path(env["INTEGRATION_SECRETS_KEY_FILE"])
    key.write_bytes(bytes(size))
    key.chmod(mode)
    with pytest.raises(ConfigurationError, match="private owner-held 32-byte key"):
        load_settings(env)


def test_missing_symlink_and_untrusted_role_key_refuse_startup(tmp_path: Path) -> None:
    env = environment(tmp_path)
    key = Path(env["INTEGRATION_SECRETS_KEY_FILE"])
    link = tmp_path / "symlink"
    link.symlink_to(key)
    with pytest.raises(ConfigurationError):
        load_settings({**env, "INTEGRATION_SECRETS_KEY_FILE": str(link)})
    with pytest.raises(ConfigurationError):
        load_schedule_worker_settings(env)
    key.unlink()
    with pytest.raises(ConfigurationError):
        load_settings(env)
    assert (
        load_settings({**env, "AGENT_INTEGRATION_SECRETS_ENABLED": "0"}).integration_secrets_key
        is None
    )


async def test_wrong_master_key_cannot_overwrite_existing_ciphertext(tmp_path: Path) -> None:
    async with enabled(tmp_path) as app:
        service = app.services.integration_secrets
        assert service is not None
        key_id = uuid4()
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="bearer",
            value=SecretValue(CANARY),
            expected_revision=0,
        )
        original = service.sealer
        service.sealer = AESGCMSecretSealer(bytes(32), purpose="veetbot.integration-secret")
        with pytest.raises(PermissionError):
            await service.put(
                app.principal,
                key_id,
                label="Example",
                origin="https://api.example.com",
                auth_scheme="bearer",
                value=SecretValue("replacement"),
                expected_revision=1,
            )
        service.sealer = original
        _, value = await service.resolve(app.principal, f"stored:{key_id}", "api.example.com")
        assert value.reveal() == CANARY


async def test_wrong_key_api_failure_is_content_free_and_unavailable(tmp_path: Path) -> None:
    async with enabled(tmp_path) as app:
        service = app.services.integration_secrets
        assert service is not None
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
        ) as client:
            path = f"/v1/integration-secrets/{uuid4()}"
            assert (await client.put(path, json=body())).status_code == 200
            service.sealer = AESGCMSecretSealer(bytes(32), purpose="veetbot.integration-secret")
            response = await client.put(path, json=body(expected_revision=1))
            assert response.status_code == 503
            assert CANARY not in response.text
            assert "no-store" in response.headers["cache-control"]


async def test_unexpected_failure_does_not_cache_or_log_secret_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with enabled(tmp_path) as app:
        service = app.services.integration_secrets
        assert service is not None

        async def unavailable(*args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError(CANARY)

        monkeypatch.setattr(service, "put", unavailable)
        api = create_app(
            app.services, app.settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
            base_url="http://agent.test",
        ) as client:
            response = await client.put(f"/v1/integration-secrets/{uuid4()}", json=body())
            assert response.status_code == 500
            assert CANARY not in response.text and CANARY not in caplog.text
            assert "no-store" in response.headers.get("cache-control", "")


async def test_secret_ingestion_requires_authentication_before_parsing(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from agent_core.config import AuthMode

    async with enabled(tmp_path) as app:
        settings = replace(
            app.settings, auth_mode=AuthMode.TOKEN, auth_token=SecretStr("synthetic-owner-token")
        )
        api = create_app(
            app.services, settings, app.principal, app.new_request_id, app.readiness_probe
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://agent.test"
        ) as client:
            response = await client.put(f"/v1/integration-secrets/{uuid4()}", content=CANARY)
            assert response.status_code == 401
            assert CANARY not in response.text and "no-store" in response.headers["cache-control"]
