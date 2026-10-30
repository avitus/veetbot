"""Stored keys widen neither the grant nor the destination boundary."""

import base64
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.jobs.http import JobHTTP
from agent_core.adapters.jobs.stored_credentials import StoredJobCredentials
from agent_core.domain.credentials import SecretValue
from agent_core.domain.jobs import JobAccess, JobBrokerError
from tests.unit.test_integration_secrets_security import CANARY, enabled
from tests.unit.test_job_http import Credentials


def test_stored_reference_can_be_proposed_in_private_job_access() -> None:
    reference = f"stored:{UUID(int=70)}"
    access = JobAccess({}, {"api.example.com": reference})
    assert access.hosts == {"api.example.com": reference}


async def public(host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


async def test_bound_job_observes_rotation_and_revocation(tmp_path: Path) -> None:
    async with enabled(tmp_path) as app:
        service = app.services.integration_secrets
        assert service is not None
        key_id = UUID(int=70)
        reference = f"stored:{key_id}"
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="x-api-key",
            value=SecretValue(CANARY),
            expected_revision=0,
        )
        access = JobAccess({}, {"api.example.com": reference})
        resolver = StoredJobCredentials(service, app.principal, access)
        sent: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"result": "safe"})

        http = JobHTTP(
            Credentials(),
            resolver=public,
            transport=httpx.MockTransport(respond),
            stored_credentials=resolver.resolve,
            operator_hosts=frozenset(access.hosts),
        )

        async def request() -> dict[str, object]:
            return await http.request(
                method="GET",
                url="https://api.example.com/data",
                json=None,
                credential=reference,
                maximum_response_bytes=1024,
                call_id="test",
            )

        assert (await request())["json"] == {"result": "safe"}
        assert sent[-1].headers["x-api-key"] == CANARY
        assert "authorization" not in sent[-1].headers
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="x-api-key",
            value=SecretValue("replacement"),
            expected_revision=1,
        )
        await request()
        assert sent[-1].headers["x-api-key"] == "replacement"
        for bad in [
            StoredJobCredentials(service, app.principal, JobAccess({}, {})),
            StoredJobCredentials(
                service, app.principal.model_copy(update={"principal_id": "other"}), access
            ),
        ]:
            with pytest.raises(JobBrokerError):
                await bad.resolve(reference, "api.example.com")
        with pytest.raises(JobBrokerError):
            await resolver.resolve(reference, "other.example.com")
        await service.delete(app.principal, key_id, 2)
        with pytest.raises(JobBrokerError, match="credential_unavailable"):
            await request()
        assert len(sent) == 2


@pytest.mark.parametrize(
    "encoded",
    [
        CANARY,
        quote(CANARY, safe=""),
        quote(CANARY, safe="").replace("%2F", "%2f"),
        base64.b64encode(CANARY.encode()).decode(),
        base64.urlsafe_b64encode(CANARY.encode()).decode().rstrip("="),
    ],
)
@pytest.mark.parametrize("status", [200, 400])
async def test_echoes_in_keys_and_nested_values_never_leave_broker(
    encoded: str, status: int
) -> None:
    async def resolve(reference: str, host: str) -> tuple[str, SecretValue]:
        return "bearer", SecretValue(CANARY)

    for document in [{encoded: "safe"}, {"nested": [encoded]}]:
        http = JobHTTP(
            Credentials(),
            resolver=public,
            stored_credentials=resolve,
            transport=httpx.MockTransport(
                lambda request, doc=document: httpx.Response(status, json=doc)
            ),
        )
        with pytest.raises(JobBrokerError, match="credential_echo_refused") as failure:
            await http.request(
                method="GET",
                url="https://api.example.com",
                json=None,
                credential=f"stored:{UUID(int=70)}",
                maximum_response_bytes=1024,
                call_id="test",
            )
        assert CANARY not in str(failure.value) and encoded not in str(failure.value)


@pytest.mark.parametrize(
    "method,body,host",
    [
        ("POST", None, "api.example.com"),
        ("GET", {}, "api.example.com"),
        ("GET", None, "elsewhere.example.com"),
    ],
)
async def test_method_body_and_operator_restrictions_precede_resolution(
    method: str, body: object, host: str
) -> None:
    async def forbidden(reference: str, host: str) -> tuple[str, SecretValue]:
        pytest.fail("A refused request tried to decrypt a key.")

    http = JobHTTP(
        Credentials(),
        resolver=public,
        stored_credentials=forbidden,
        operator_hosts=frozenset({"api.example.com"}),
    )
    with pytest.raises(JobBrokerError):
        await http.request(
            method=method,
            url=f"https://{host}",
            json=body,
            credential=f"stored:{UUID(int=70)}",
            maximum_response_bytes=1024,
            call_id="test",
        )


async def test_composed_job_tool_checks_binding_and_approval_shows_metadata(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_core.bootstrap import build
    from agent_core.config import load_settings
    from agent_core.domain.errors import NotFoundError
    from agent_core.tools.registry import RegisteredTool
    from tests.contract.support import run, tool_context
    from tests.contract.test_job_store_contract import record
    from tests.unit.test_integration_secrets_security import environment
    from tests.unit.test_job_execution import Runner

    async with build(
        settings=replace(load_settings(environment(tmp_path)), jobs_enabled=True),
        storage="memory",
        job_runner_override=Runner(),
    ) as app:
        service = app.services.integration_secrets
        assert service is not None
        key_id = UUID(int=270)
        reference = f"stored:{key_id}"
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="bearer",
            value=SecretValue(CANARY),
            expected_revision=0,
        )
        saved = record(app.principal)
        async with app.uow_factory() as uow:
            await uow.jobs.put(saved, expected_version=None)
        registry = app.tool_pipeline._registry
        tool = registry.get("job.run")
        assert isinstance(tool, RegisteredTool)
        assert tool.spec.version == "1.1.0"
        assert registry.get("job.run", "1.0.0").spec.version == "1.0.0"
        arguments = {
            "slug": saved.slug,
            "revision_id": str(UUID(int=1)),
            "access": {"inputs": {"items": [1, 2]}, "hosts": {"api.example.com": reference}},
        }
        view = await tool.approval_view_in_session(
            arguments, principal=app.principal, run=run(), turn=None, not_covered=None
        )
        assert view.arguments["integration_keys"][0]["label"] == "Example"
        assert view.arguments["integration_keys"][0]["reference"] == reference
        assert CANARY not in str(view)
        other = app.principal.model_copy(update={"principal_id": "other"})
        with pytest.raises(NotFoundError):
            await tool.approval_view_in_session(
                arguments, principal=other, run=run(), turn=None, not_covered=None
            )
        # Tool execution cannot create a stored grant through an old pinned tool.
        context = replace(
            tool_context(), principal=app.principal, tenant_id=app.principal.tenant_id
        )
        result = await registry.get("job.run", "1.0.0").execute(arguments, context)
        assert not result.ok
        await service.delete(app.principal, key_id, 1)
        result = await tool.execute(arguments, context)
        assert not result.ok and CANARY not in str(result)
        async with app.uow_factory() as uow:
            assert len((await uow.jobs.get(app.principal, saved.slug)).grants) == 1


async def test_composed_execution_keeps_saved_key_out_of_job_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from typing import Any

    import agent_core.bootstrap as bootstrap
    from agent_core.config import load_settings
    from agent_core.domain.jobs import JobResult
    from tests.contract.support import run
    from tests.contract.test_job_store_contract import record
    from tests.unit.test_integration_secrets_security import environment

    observed: list[object] = []
    sent: list[httpx.Request] = []

    class Reader:
        async def run(self, program: Any, inputs: Any, **kwargs: Any) -> JobResult:
            observed.extend([program, inputs])
            reply = await kwargs["dispatch"](
                "http.request", {"url": "https://api.example.com/data"}, "read"
            )
            observed.append(reply)
            return JobResult(reply, "", 1)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"count": 2})

    original = JobHTTP

    def http(*args: Any, **kwargs: Any) -> JobHTTP:
        return original(*args, **kwargs, resolver=public, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(bootstrap, "JobHTTP", http)
    async with bootstrap.build(
        settings=replace(load_settings(environment(tmp_path)), jobs_enabled=True),
        storage="memory",
        job_runner_override=Reader(),
    ) as app:
        service = app.services.integration_secrets
        assert service is not None
        key_id = UUID(int=271)
        reference = f"stored:{key_id}"
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="bearer",
            value=SecretValue(CANARY),
            expected_revision=0,
        )
        saved = record(app.principal)
        grant = saved.grants[str(UUID(int=2))]
        saved = saved.model_copy(
            update={
                "grants": {
                    str(grant.id): grant.model_copy(
                        update={
                            "access": replace(grant.access, hosts={"api.example.com": reference})
                        }
                    )
                }
            }
        )
        session_id = await app.sessions.create()
        current = run().model_copy(
            update={"session_id": session_id, "tenant_id": app.principal.tenant_id}
        )
        async with app.uow_factory() as uow:
            await uow.jobs.put(saved, expected_version=None)
            await uow.runs.create(current)
        execution = app.job_execution
        assert execution is not None

        async def no_model(**kwargs: Any) -> dict[str, Any]:
            pytest.fail("A stored HTTP read invoked a model.")

        result = await execution.run(
            principal=app.principal,
            slug=saved.slug,
            revision_id=UUID(int=1),
            grant_id=UUID(int=2),
            run_id=current.id,
            lease_epoch=1,
            model=no_model,
        )
        assert result.value["status"] == "succeeded"
        assert sent[0].headers["authorization"] == f"Bearer {CANARY}"
        assert CANARY not in str(observed) and CANARY not in str(result)
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, app.principal)
        assert CANARY not in str(events) and reference not in str(events)


async def test_numeric_json_echo_is_also_refused() -> None:
    async def resolve(reference: str, host: str) -> tuple[str, SecretValue]:
        return "bearer", SecretValue("123456789")

    http = JobHTTP(
        Credentials(),
        resolver=public,
        stored_credentials=resolve,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"key": 123456789})),
    )
    with pytest.raises(JobBrokerError, match="credential_echo_refused"):
        await http.request(
            method="GET",
            url="https://api.example.com",
            json=None,
            credential=f"stored:{UUID(int=70)}",
            maximum_response_bytes=1024,
            call_id="test",
        )
