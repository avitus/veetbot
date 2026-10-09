"""Owner follow-ups keep the conversation without inheriting scheduled restrictions."""

from pathlib import Path

from agent_core.domain.browser_act_views import session_allows_task_grant
from tests.unit.test_browser_task_grant_authorizer import PROFILE, owner, run


def test_owner_reply_to_schedule_can_use_task_permission() -> None:
    reply = run(principal_scopes={"run.write", "browser.profile.read"})
    assert session_allows_task_grant(
        reply,
        owner_run_id=reply.id,
        session_tenant_id=owner().tenant_id,
        session_principal_id=owner().principal_id,
        session_metadata={"schedule_id": "weekly", "browser_profile_id": str(PROFILE)},
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
    )


def test_scheduled_occurrence_cannot_use_owner_task_permission() -> None:
    assert not session_allows_task_grant(
        run(),
        session_tenant_id=owner().tenant_id,
        session_principal_id=owner().principal_id,
        session_metadata={"schedule_id": "weekly", "browser_profile_id": str(PROFILE)},
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
    )


async def test_message_can_connect_existing_chat_and_replay_binding() -> None:
    from agent_core.bootstrap import build
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from tests.gates.test_schedule_api_m11 import _client
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Connected")]),
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        async with _client(composition) as client:
            body = {
                "content": [{"type": "text", "text": "Read my website"}],
                "browser_profile_id": str(PROFILE_ID),
            }
            response = await client.post(
                f"/v1/sessions/{chat.id}/messages",
                json=body,
                headers={"Idempotency-Key": "connect"},
            )
            assert response.status_code == 202, response.text
            replay = await client.post(
                f"/v1/sessions/{chat.id}/messages",
                json=body,
                headers={"Idempotency-Key": "connect"},
            )
            assert replay.json()["run_id"] == response.json()["run_id"]
            stored = await composition.services.sessions.get(composition.principal, chat.id)
            assert stored.metadata["browser_profile_id"] == str(PROFILE_ID)


async def test_follow_reply_reuses_matching_owned_website() -> None:
    from agent_core.bootstrap import build
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.views import TextContentBlock
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Ready")]),
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        async with composition.uow_factory() as uow:
            profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
            await uow.browser_profiles.create(
                profile.model_copy(
                    update={
                        "id": PROFILE,
                        "allowed_origins": ("https://x.com",),
                    }
                )
            )
            await uow.events.append(
                NewEvent(
                    session_id=chat.id,
                    run_id=None,
                    event_type="assistant.message.completed",
                    actor_type="runtime",
                    payload={
                        "message": {
                            "kind": "assistant",
                            "content": [
                                {
                                    "kind": "text",
                                    "text": "[Armin](https://x.com/mitsuhiko). Approve or deny?",
                                }
                            ],
                        }
                    },
                )
            )
        await composition.services.runs.submit(
            composition.principal,
            chat.id,
            [TextContentBlock(text="This is perfect. Follow him")],
            "follow",
            None,
        )
        stored = await composition.services.sessions.get(composition.principal, chat.id)
        assert stored.metadata.get("browser_profile_id") == str(PROFILE)


async def test_missing_profile_waits_then_connection_resumes_same_run() -> None:
    import asyncio

    from agent_core.bootstrap import build
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.runs import RunStatus
    from agent_core.domain.views import TextContentBlock
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Continued")]),
    ) as composition:
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        submitted = await composition.services.runs.submit(
            composition.principal,
            chat.id,
            [TextContentBlock(text="Follow @mitsuhiko")],
            "follow-needs-connection",
            None,
        )
        for _ in range(100):
            current = await composition.services.runs.get(composition.principal, submitted.run_id)
            if current.status == RunStatus.WAITING_FOR_USER:
                break
            await asyncio.sleep(0.01)
        assert current.status == RunStatus.WAITING_FOR_USER, current.failure
        await seed_browser_authority(composition)
        async with composition.uow_factory() as uow:
            profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
            await uow.browser_profiles.create(
                profile.model_copy(
                    update={
                        "id": PROFILE,
                        "allowed_origins": ("https://x.com",),
                    }
                )
            )
        resumed = await composition.services.runs.connect_browser(
            composition.principal, chat.id, PROFILE
        )
        assert resumed is not None and resumed.run_id == submitted.run_id
        ended = await composition.runs.wait_terminal(submitted.run_id)
        assert ended.status == RunStatus.COMPLETED, ended.failure
        assert (
            await composition.services.runs.connect_browser(composition.principal, chat.id, PROFILE)
            is None
        )
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(chat.id, 0, composition.principal)
        assert sum(e.event_type == "user.message.created" for e in events) == 1
        assert sum(e.event_type == "browser.connection.resumed" for e in events) == 1


async def test_same_scope_profile_binding_rotates_context_only_at_run_boundary() -> None:
    from agent_core.bootstrap import build
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    async with build(settings=session_bound_hosted_settings()) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(chat.id, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        planner = composition.executor._context_planner
        model = composition.executor._resolved_model
        first = await planner.plan(session, agent, composition.principal, model)
        assert "browser.navigate" not in first.tool_names
        await composition.services.runs.connect_browser(composition.principal, chat.id, PROFILE_ID)
        async with composition.uow_factory() as uow:
            bound = await uow.sessions.get(chat.id, composition.principal)
        unchanged = await planner.plan(bound, agent, composition.principal, model)
        assert unchanged == first
        connected = await planner.plan(
            bound, agent, composition.principal, model, refresh_authorization=True
        )
        assert connected.epoch == first.epoch + 1
        assert "browser.navigate" in connected.tool_names
        assert connected.authority_scope_hashes == first.authority_scope_hashes


async def test_connection_endpoint_enforces_ownership_scope_and_ready_state() -> None:
    from agent_core.bootstrap import build
    from tests.gates.test_schedule_api_m11 import _client
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    async with build(settings=session_bound_hosted_settings()) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        async with _client(composition) as client:
            missing = await client.put(
                f"/v1/sessions/{chat.id}/website",
                json={"browser_profile_id": "00000000-0000-0000-0000-000000001234"},
            )
            assert missing.status_code == 404
            malformed = await client.put(
                f"/v1/sessions/{chat.id}/website", json={"browser_profile_id": "not-a-profile"}
            )
            assert malformed.status_code == 400
            for _ in range(2):
                connected = await client.put(
                    f"/v1/sessions/{chat.id}/website", json={"browser_profile_id": str(PROFILE_ID)}
                )
                assert connected.status_code == 200, connected.text
        restricted = composition.principal.model_copy(update={"scopes": {"session.write"}})
        async with _client(composition, principal=restricted) as client:
            denied = await client.put(
                f"/v1/sessions/{chat.id}/website", json={"browser_profile_id": str(PROFILE_ID)}
            )
            assert denied.status_code == 403


async def test_website_schedule_pins_chat_profile_and_fails_without_it() -> None:
    from dataclasses import replace

    from agent_core.bootstrap import build
    from tests.contract.support import tool_context
    from tests.gates.test_schedule_tool_m19 import ARGUMENTS, NOW, _enabled_settings
    from tests.unit.test_browser_composition import PROFILE_ID, seed_browser_authority

    async with build(settings=_enabled_settings(), fixed_clock_at=NOW) as app:
        await seed_browser_authority(app)
        chat = await app.services.sessions.create(app.principal, "general", {})
        tool = app.tool_pipeline._registry.get("schedule.create")
        context = replace(tool_context(), principal=app.principal, session_id=chat.id)
        missing = await tool.execute({**ARGUMENTS, "use_website": True}, context)
        assert not missing.ok and missing.failure is not None
        assert missing.failure.reason_code == "schedule.browser_profile_required"
        assert not (await app.schedules.list(app.principal, 10, None)).items
        await app.services.runs.connect_browser(app.principal, chat.id, PROFILE_ID)
        created = await tool.execute({**ARGUMENTS, "use_website": True}, context)
        assert created.ok, created.failure
        [record] = (await app.schedules.list(app.principal, 10, None)).items
        assert record.revision.browser_profile_id == PROFILE_ID
        assert record.revision.requested_scopes == {"browser.profile.read"}


async def test_complete_connection_journey_follows_once_without_second_approval() -> None:
    import asyncio

    from agent_core.bootstrap import build
    from agent_core.domain.browser import BrowserElement, BrowserObservation
    from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
    from agent_core.domain.runs import RunStatus
    from agent_core.domain.views import TextContentBlock
    from tests.unit.test_browser_act_approval_view import SnapshotProvider, snapshot
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    class Provider(SnapshotProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return BrowserObservation(
                url="https://x.com/mitsuhiko",
                title="Armin",
                revision="after",
                text="Following",
                elements=(
                    BrowserElement(ref="following", role="button", name="Following @mitsuhiko"),
                ),
            )

    provider = Provider(allowed_origins=("https://x.com",))
    before = snapshot(url="https://x.com/mitsuhiko", name="Follow @mitsuhiko")
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments={
                            "kind": "click",
                            "expected_revision": before.observation.revision,
                            "ref": before.observation.elements[0].ref,
                        },
                        call_id="follow-owner-target",
                    )
                ]
            ),
            ScriptedTurn(text="Following @mitsuhiko."),
        ]
    )
    async with build(
        settings=session_bound_hosted_settings(),
        script=script,
        browser_provider_override=provider,
        enabled_tools=["browser.act", "conversation.ask_user"],
    ) as app:
        chat = await app.services.sessions.create(app.principal, "general", {})
        provider.snapshots[chat.id] = before
        submitted = await app.services.runs.submit(
            app.principal, chat.id, [TextContentBlock(text="Follow @mitsuhiko")], "journey", None
        )
        for _ in range(200):
            current = await app.services.runs.get(app.principal, submitted.run_id)
            if current.status == RunStatus.WAITING_FOR_USER:
                break
            await asyncio.sleep(0.01)
        assert current.status == RunStatus.WAITING_FOR_USER
        assert provider.actions == []
        await seed_browser_authority(app)
        async with app.uow_factory() as uow:
            profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
            await uow.browser_profiles.create(
                profile.model_copy(update={"id": PROFILE, "allowed_origins": ("https://x.com",)})
            )
        await app.services.runs.connect_browser(app.principal, chat.id, PROFILE)
        for _ in range(200):
            current = await app.services.runs.get(app.principal, submitted.run_id)
            if current.status not in {RunStatus.RUNNING, RunStatus.QUEUED}:
                break
            await asyncio.sleep(0.01)
        assert current.status == RunStatus.COMPLETED, current
        assert len(provider.actions) == 1
        assert provider.constraints[0] is not None
        assert provider.constraints[0].grant_kind == "follow"
        assert not await app.approvals.list_pending(run_id=submitted.run_id)


async def test_active_run_cannot_switch_accounts_and_foreign_profiles_are_hidden() -> None:
    from uuid import UUID

    import pytest

    from agent_core.bootstrap import build
    from agent_core.domain.errors import ConflictError, NotFoundError
    from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
    from agent_core.domain.views import TextContentBlock
    from tests.unit.test_browser_composition import (
        PROFILE_ID,
        seed_browser_authority,
        session_bound_hosted_settings,
    )

    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(
            turns=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="conversation.ask_user", arguments={"question": "Continue?"}
                        )
                    ]
                )
            ]
        ),
    ) as app:
        await seed_browser_authority(app)
        chat = await app.services.sessions.create(app.principal, "general", {})
        async with app.uow_factory() as uow:
            profile = await uow.browser_profiles.get(PROFILE_ID, app.principal)
            foreign = profile.model_copy(update={"id": UUID(int=999), "principal_id": "another"})
            await uow.browser_profiles.create(foreign)
        with pytest.raises(NotFoundError):
            await app.services.runs.connect_browser(app.principal, chat.id, foreign.id)
        await app.services.runs.submit(
            app.principal, chat.id, [TextContentBlock(text="Ask me")], None, None
        )
        with pytest.raises(ConflictError):
            await app.services.runs.connect_browser(app.principal, chat.id, PROFILE_ID)
        stored = await app.services.sessions.get(app.principal, chat.id)
        assert "browser_profile_id" not in stored.metadata


async def test_owner_can_answer_missing_access_wait_without_being_forced_to_connect() -> None:
    import asyncio

    from agent_core.bootstrap import build
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.runs import RunStatus
    from agent_core.domain.views import TextContentBlock
    from tests.unit.test_browser_composition import session_bound_hosted_settings

    async with build(
        settings=session_bound_hosted_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Cancelled, I have not followed anyone.")]),
    ) as app:
        chat = await app.services.sessions.create(app.principal, "general", {})
        submitted = await app.services.runs.submit(
            app.principal, chat.id, [TextContentBlock(text="Follow @mitsuhiko")], "follow", None
        )
        for _ in range(100):
            current = await app.services.runs.get(app.principal, submitted.run_id)
            if current.status == RunStatus.WAITING_FOR_USER:
                break
            await asyncio.sleep(0.01)
        assert current.status == RunStatus.WAITING_FOR_USER
        reply = await app.services.runs.submit(
            app.principal,
            chat.id,
            [TextContentBlock(text="Cancel, I changed my mind")],
            "cancel-follow",
            None,
        )
        assert reply.run_id == submitted.run_id
        for _ in range(100):
            current = await app.services.runs.get(app.principal, submitted.run_id)
            if current.status not in {RunStatus.RUNNING, RunStatus.QUEUED}:
                break
            await asyncio.sleep(0.01)
        assert current.status == RunStatus.COMPLETED


def test_existing_schedule_tool_pins_keep_the_original_closed_contract() -> None:
    from agent_core.tools.schedule_create import LegacyScheduleCreateTool, ScheduleCreateTool

    old = LegacyScheduleCreateTool.spec
    assert old.version == "1.1.1"
    assert "use_website" not in old.input_schema["properties"]
    assert "use_website" not in old.description
    assert old.description == (
        "Create one future one-time or recurring schedule. For recurrence, supply a "
        "complete IANA-zone cadence; ask about ambiguous dates or times."
    )
    assert ScheduleCreateTool.spec.version == "1.2.0"


async def test_disabled_browser_never_creates_an_unresolvable_connection_wait() -> None:
    from agent_core.bootstrap import build
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.runs import RunStatus
    from agent_core.domain.views import TextContentBlock
    from tests.integration.m2_support import memory_settings

    async with build(
        settings=memory_settings(),
        script=FakeModelScript(
            turns=[ScriptedTurn(text="Website access is unavailable in this deployment.")]
        ),
    ) as app:
        chat = await app.services.sessions.create(app.principal, "general", {})
        submitted = await app.services.runs.submit(
            app.principal, chat.id, [TextContentBlock(text="Follow @mitsuhiko")], None, None
        )
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(submitted.run_id)
        assert checkpoint is not None
        assert "browser_access_wait" not in checkpoint.working_state
        completed = await app.runs.wait_terminal(submitted.run_id)
        assert completed.status == RunStatus.COMPLETED


def test_scheduled_occurrence_with_run_write_scope_is_not_owner_consent() -> None:
    assert not session_allows_task_grant(
        run(principal_scopes={"run.write", "browser.profile.read"}),
        session_tenant_id=owner().tenant_id,
        session_principal_id=owner().principal_id,
        session_metadata={"schedule_id": "weekly", "browser_profile_id": str(PROFILE)},
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
    )


async def test_owner_reply_can_use_a_task_permission_in_a_schedule_chat(tmp_path: Path) -> None:
    from uuid import UUID

    from agent_core.domain.runs import RunStatus
    from tests.integration.test_browser_task_grant_pipeline import (
        CONTINUE,
        FINAL,
        act,
        allow_turns,
        allowed_task,
        navigate,
        pipeline,
    )

    async with pipeline(
        tmp_path, [*allow_turns(), FINAL, navigate(), act(1, CONTINUE), FINAL]
    ) as h:
        original = await h.grant(await allowed_task(h))
        scheduled = await h.scheduled_session()
        grant_id = UUID(int=0x172)
        async with h.composition.uow_factory() as uow:
            await uow.browser_task_grants.create(
                original.model_copy(
                    update={
                        "id": grant_id,
                        "approval_id": UUID(int=0x173),
                        "session_id": scheduled,
                    }
                )
            )
        run_id = await h.submit("Continue my lesson in this chat.", scheduled)
        ended = await h.settled(run_id)
        assert ended.status == RunStatus.COMPLETED, ended.failure
        assert (await h.grant(grant_id)).actions_used == 1
        assert not await h.composition.approvals.list_pending(run_id=run_id)
