"""A scheduled website read keeps an explicitly selected, owned profile."""

from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.schedules import ScheduleDefinitionPatch
from tests.gates.test_schedule_api_m11 import NOW, _agent, _client, _definition
from tests.unit.test_browser_composition import (
    PROFILE_ID,
    seed_browser_authority,
    session_bound_hosted_settings,
)


def browser_definition() -> dict[str, object]:
    return {
        **_definition(),
        "browser_profile_id": str(PROFILE_ID),
        "requested_scopes": ["browser.profile.read"],
    }


async def test_schedule_api_pins_browser_profile_and_preserves_it_on_chat_patch() -> None:
    async with build(
        settings=replace(session_bound_hosted_settings(), schedule_api_enabled=True),
        fixed_clock_at=NOW,
    ) as composition:
        await seed_browser_authority(composition)
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        async with _client(composition) as client:
            response = await client.post(
                "/v1/schedules",
                json=browser_definition(),
                headers={"Idempotency-Key": "browser-briefing"},
            )
            assert response.status_code == 201, response.text
            record = response.json()
            assert record["revision"]["browser_profile_id"] == str(PROFILE_ID)
            replay = await client.post(
                "/v1/schedules",
                json=browser_definition(),
                headers={"Idempotency-Key": "browser-briefing"},
            )
            assert replay.status_code == 200
            schedule_id = UUID(record["schedule"]["id"])
            patched = await composition.services.schedules.patch(
                composition.principal,
                schedule_id,
                1,
                ScheduleDefinitionPatch(title="Renamed briefing"),
                "rename-briefing",
            )
            assert patched.revision.browser_profile_id == PROFILE_ID
            assert patched.revision.requested_scopes == {"browser.profile.read"}
            old = await composition.services.schedules.get(composition.principal, schedule_id)
            assert old.schedule.current_revision == 2
            changed = await client.post(
                "/v1/schedules",
                json={**browser_definition(), "browser_profile_id": None},
                headers={"Idempotency-Key": "browser-briefing"},
            )
            assert changed.status_code == 409


@pytest.mark.parametrize(
    "problem", ["caller_scope", "delegated_scope", "missing", "foreign", "revoked"]
)
async def test_schedule_api_rejects_unauthorized_browser_binding(problem: str) -> None:
    from agent_core.domain.browser import BrowserProfileStatus

    async with build(
        settings=replace(session_bound_hosted_settings(), schedule_api_enabled=True),
        fixed_clock_at=NOW,
    ) as composition:
        await seed_browser_authority(composition)
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
            profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
            if problem == "foreign":
                # A different owned record must not be accepted by UUID alone.
                await uow.browser_profiles.create(
                    profile.model_copy(
                        update={
                            "id": UUID(int=999),
                            "principal_id": "another-owner",
                        }
                    )
                )
            if problem == "revoked":
                await uow.browser_profiles.transition(
                    PROFILE_ID,
                    composition.principal,
                    expected_generation=profile.generation,
                    status=BrowserProfileStatus.REVOKED,
                    updated_at=NOW,
                )
        principal = composition.principal
        definition = browser_definition()
        if problem == "caller_scope":
            principal = principal.model_copy(
                update={"scopes": principal.scopes - {"browser.profile.read"}}
            )
            definition["requested_scopes"] = []
        if problem == "delegated_scope":
            definition["requested_scopes"] = []
        if problem in {"missing", "foreign"}:
            definition["browser_profile_id"] = str(UUID(int=999))
        async with _client(composition, principal=principal) as client:
            response = await client.post(
                "/v1/schedules",
                json=definition,
                headers={"Idempotency-Key": "invalid-browser-briefing"},
            )
            assert response.status_code == (403 if problem == "caller_scope" else 422), (
                response.text
            )
            listing = await client.get("/v1/schedules")
            assert listing.json()["items"] == []


@pytest.mark.parametrize("provider_mode", ["session", "fixed", "mismatch"])
async def test_materialized_browser_schedule_exposes_browser_tools_with_pinned_budget(
    provider_mode: str,
) -> None:
    from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
    from agent_core.domain.runs import RunStatus
    from agent_core.domain.schedules import ScheduleDefinition
    from agent_core.runtime.checkpoints import DurableCheckpointSeeder
    from agent_core.scheduling.materializer import ScheduleMaterializer
    from tests.unit.test_browser_composition import context_plan_payload, one_text_turn

    settings = session_bound_hosted_settings()
    if provider_mode != "session":
        settings = replace(
            settings,
            browser_profile_id=UUID(int=999) if provider_mode == "mismatch" else PROFILE_ID,
            browser_allowed_origins=("https://example.org",),
        )
    async with build(
        settings=settings,
        fixed_clock_at=NOW,
        script=one_text_turn(),
    ) as composition:
        await seed_browser_authority(composition)
        if provider_mode == "mismatch":
            async with composition.uow_factory() as uow:
                profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
                await uow.browser_profiles.create(profile.model_copy(update={"id": UUID(int=999)}))
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            source = await uow.sessions.get(session_id, composition.principal)
        definition = ScheduleDefinition.model_validate(
            {
                **_definition(),
                "agent_id": str(source.agent_id),
                "agent_version": source.agent_version,
                "requested_scopes": ["browser.profile.read"],
                "browser_profile_id": str(PROFILE_ID),
                "cadence": {"kind": "ONCE", "at": NOW.isoformat()},
            }
        )
        record = await composition.services.schedules.create(
            composition.principal, definition, "bound"
        )
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(composition.principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        occurrence = await materializer.materialize(record.schedule.id)
        assert (
            occurrence is not None
            and occurrence.session_id is not None
            and occurrence.run_id is not None
        )
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(occurrence.session_id, composition.principal)
            assert session.metadata.get("browser_profile_id") == str(PROFILE_ID)
        await composition.executor.execute(occurrence.run_id)
        if provider_mode != "mismatch":
            plan = await context_plan_payload(composition, occurrence.session_id)
            assert {"browser.navigate", "browser.observe"} <= set(
                cast(list[str], plan["tool_names"])
            )
        async with composition.uow_factory() as uow:
            run = await uow.runs.get(occurrence.run_id, composition.principal)
            assert run.limits.max_cost == definition.limits.max_cost
            assert run.principal_scopes == {"browser.profile.read"}
        if provider_mode == "mismatch":
            assert run.status is RunStatus.FAILED
            assert run.failure is not None
            assert run.failure.details["reason_code"] == "tool.browser.profile_unavailable"
            provider = composition.executor._model_provider
            assert isinstance(provider, FakeModelProvider)
            assert provider.requests == []
        else:
            assert run.status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    "unavailable", ["missing", "revoked", "authentication_required", "needs_user", "disabled"]
)
async def test_scheduled_browser_preflight_fails_before_model_call(unavailable: str) -> None:
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.config import BrowserProviderKind
    from agent_core.domain.browser import BrowserProfileStatus
    from agent_core.domain.runs import RunStatus
    from tests.unit.test_browser_composition import one_text_turn

    settings = session_bound_hosted_settings()
    if unavailable == "disabled":
        settings = replace(settings, browser_provider=BrowserProviderKind.DISABLED)
    async with build(settings=settings, script=one_text_turn(), fixed_clock_at=NOW) as composition:
        await seed_browser_authority(composition)
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(session_id, composition.principal)
            session_id = UUID(int=444)
            await uow.sessions.create(
                session.model_copy(
                    update={
                        "id": session_id,
                        "metadata": {
                            "schedule_id": str(UUID(int=333)),
                            "browser_profile_id": str(
                                UUID(int=999) if unavailable == "missing" else PROFILE_ID
                            ),
                        },
                    }
                )
            )
            if unavailable in {"revoked", "authentication_required", "needs_user"}:
                profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
                await uow.browser_profiles.transition(
                    PROFILE_ID,
                    composition.principal,
                    expected_generation=profile.generation,
                    status=BrowserProfileStatus(unavailable),
                    updated_at=NOW,
                )
        from tests.scheduled_run_support import submit_scheduled_seed

        run_id = await submit_scheduled_seed(composition, session_id, "Read my feed.")
        run = await composition.runs.wait_terminal(run_id)
        assert run.status is RunStatus.FAILED
        assert run.failure is not None
        expected = {
            "authentication_required": "tool.browser.authentication_required",
            "needs_user": "tool.browser.needs_user",
            "disabled": "tool.browser.provider_unavailable",
        }.get(unavailable, "tool.browser.profile_unavailable")
        assert run.failure.details["reason_code"] == expected
        provider = composition.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert provider.requests == []


def test_legacy_unbound_schedule_request_hash_is_unchanged() -> None:
    import hashlib
    import json

    from agent_core.application.schedule_service import _definition_hash
    from agent_core.domain.schedules import ScheduleDefinition

    definition = ScheduleDefinition.model_validate(_definition())
    legacy = definition.model_dump(mode="json", exclude={"browser_profile_id"})
    previous_hash = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert _definition_hash(definition) == previous_hash


NATIVE_REVISION_ONLY_FIELDS = (
    "schedule_id",
    "revision",
    "timezone",
    "created_by_principal_id",
    "created_at",
)


def native_website_access_update(record: dict[str, Any], profile_id: str | None) -> dict[str, Any]:
    """The update the Apple client derives from a point read (ADR-0154)."""
    definition = {
        name: value
        for name, value in record["revision"].items()
        if name not in NATIVE_REVISION_ONLY_FIELDS
    }
    scopes = [scope for scope in definition["requested_scopes"] if scope != "browser.profile.read"]
    if profile_id is not None:
        scopes.append("browser.profile.read")
    definition["requested_scopes"] = scopes
    definition["browser_profile_id"] = profile_id
    return {"expected_revision": record["schedule"]["current_revision"], "definition": definition}


async def test_native_echo_of_a_point_read_binds_and_unbinds_the_schedule() -> None:
    """A revision field the definition does not accept would break the app's picker."""
    async with build(
        settings=replace(session_bound_hosted_settings(), schedule_api_enabled=True),
        fixed_clock_at=NOW,
    ) as composition:
        await seed_browser_authority(composition)
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        async with _client(composition) as client:
            created = await client.post(
                "/v1/schedules", json=_definition(), headers={"Idempotency-Key": "native-echo"}
            )
            assert created.status_code == 201, created.text
            schedule_id = created.json()["schedule"]["id"]
            read = (await client.get(f"/v1/schedules/{schedule_id}")).json()

            bound = await client.patch(
                f"/v1/schedules/{schedule_id}",
                json=native_website_access_update(read, str(PROFILE_ID)),
            )

            assert bound.status_code == 200, bound.text
            revision = bound.json()["revision"]
            assert revision["browser_profile_id"] == str(PROFILE_ID)
            assert sorted(revision["requested_scopes"]) == [
                "browser.profile.read",
                "workspace.read",
            ]
            for unchanged in (
                "title",
                "instruction",
                "agent_id",
                "agent_version",
                "policy_profile",
                "limits",
                "run_timeout_seconds",
                "cadence",
                "misfire_grace_seconds",
                "max_consecutive_failures",
            ):
                assert revision[unchanged] == read["revision"][unchanged], unchanged

            reread = (await client.get(f"/v1/schedules/{schedule_id}")).json()
            unbound = await client.patch(
                f"/v1/schedules/{schedule_id}",
                json=native_website_access_update(reread, None),
            )

            assert unbound.status_code == 200, unbound.text
            assert unbound.json()["revision"]["browser_profile_id"] is None
            assert unbound.json()["revision"]["requested_scopes"] == ["workspace.read"]


async def test_api_can_bind_existing_schedule_without_changing_prior_revision() -> None:
    async with build(
        settings=replace(session_bound_hosted_settings(), schedule_api_enabled=True),
        fixed_clock_at=NOW,
    ) as composition:
        await seed_browser_authority(composition)
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        async with _client(composition) as client:
            original = await client.post(
                "/v1/schedules", json=_definition(), headers={"Idempotency-Key": "existing"}
            )
            assert original.status_code == 201
            schedule_id = original.json()["schedule"]["id"]
            bound = await client.patch(
                f"/v1/schedules/{schedule_id}",
                json={
                    "expected_revision": 1,
                    "definition": browser_definition(),
                },
            )
            assert bound.status_code == 200, bound.text
            assert bound.json()["revision"]["browser_profile_id"] == str(PROFILE_ID)
            assert bound.json()["revision"]["limits"] == original.json()["revision"]["limits"]
            async with composition.uow_factory() as uow:
                old = await uow.schedules.get_revision(UUID(schedule_id), 1, composition.principal)
                assert old.browser_profile_id is None
            stale = await client.patch(
                f"/v1/schedules/{schedule_id}",
                json={
                    "expected_revision": 1,
                    "definition": browser_definition(),
                },
            )
            assert stale.status_code == 409
            unbound = await client.patch(
                f"/v1/schedules/{schedule_id}",
                json={
                    "expected_revision": 2,
                    "definition": _definition(),
                },
            )
            assert unbound.status_code == 200
            assert unbound.json()["revision"]["browser_profile_id"] is None
