"""Milestone 24 SMS ingest: the projector, triage seeding, and the daily cap.

The device channel appends an ingested message as a device-originated user
message the projector must reproduce at `EXTERNAL_UNTRUSTED`, because the
tool pipeline decides a turn's origin trust by walking the conversation for
a `USER`-trusted message and finding none.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.device_channel import FakeDeviceChannel
from agent_core.application.device_ingest import DeviceMessageIngestService, ingest_digest
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.devices import DeviceIngestReceipt, DeviceStatus
from agent_core.domain.errors import ConflictError, DeviceIngestError, NotFoundError
from agent_core.domain.events import EventEnvelope, conversation_items
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import (
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.views import DeviceIngestResult, TextContentBlock
from agent_core.ports.policies import PolicyEngine
from agent_core.tools.device_tools import DEVICE_SMS_SEND_TOOL_NAME
from tests.contract.test_device_registry_contract import device
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-0000-0000-0000000002b0")
RUN_ID = UUID("00000000-0000-0000-0000-0000000002b1")
DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002b2")
FOREIGN_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002b3")
SENDER = "+15555550123"
BODY = "Marzipan needs feeding at six."
OTHER_BODY = "The sitter is running late."


def _user_message_event(payload: dict[str, object]) -> EventEnvelope:
    return EventEnvelope(
        id=1,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        sequence=7,
        event_type="user.message.created",
        payload_schema_version=1,
        actor_type="device",
        actor_id="local-user",
        payload=payload,
        trace_id=None,
        created_at=NOW,
    )


def test_the_projector_honors_an_external_untrusted_seed_payload() -> None:
    event = _user_message_event({"content": "an SMS arrived", "trust": "external_untrusted"})

    assert conversation_items(event) == [
        UserMessage(
            content=[TextPart(text="an SMS arrived")],
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            principal_id="local-user",
            source_event_sequence=7,
        )
    ]


def test_the_projector_leaves_every_other_user_message_at_user_trust() -> None:
    absent = _user_message_event({"content": "hello"})
    unknown = _user_message_event({"content": "hello", "trust": "platform"})

    expected = [
        UserMessage(
            content=[TextPart(text="hello")],
            trust=TrustLevel.USER,
            principal_id="local-user",
            source_event_sequence=7,
        )
    ]
    assert conversation_items(absent) == expected
    assert conversation_items(unknown) == expected


# --- the ingest service -----------------------------------------------------


def _settings(**updates: object) -> Settings:
    return replace(
        memory_settings(),
        device_channel_enabled=True,
        device_sms_enabled=True,
        **updates,  # type: ignore[arg-type]
    )


def _replies(count: int) -> FakeModelScript:
    return FakeModelScript(turns=[ScriptedTurn(text="Triaged.") for _ in range(count)])


async def _seed_device(
    composition: Composition,
    *,
    capabilities: frozenset[str] = frozenset({DEVICE_SMS_SEND_TOOL_NAME}),
    status: DeviceStatus = DeviceStatus.ACTIVE,
) -> None:
    owned = device(device_id=DEVICE_ID, capabilities=capabilities).model_copy(
        update={
            "tenant_id": composition.principal.tenant_id,
            "principal_id": composition.principal.principal_id,
            "status": status,
            "revoked_at": None if status is DeviceStatus.ACTIVE else NOW,
        }
    )
    async with composition.uow_factory() as uow:
        await uow.devices.upsert(owned, composition.principal)


async def _ingest(
    composition: Composition,
    *,
    body: str = BODY,
    sender: str = SENDER,
    channel: str = "sms",
    received_at: datetime = NOW,
) -> DeviceIngestResult:
    return await composition.services.device_ingest.ingest(
        composition.principal,
        DEVICE_ID,
        channel=channel,
        sender=sender,
        body=body,
        received_at=received_at,
    )


async def _session_events(composition: Composition, session_id: UUID) -> list[EventEnvelope]:
    async with composition.uow_factory() as uow:
        return await uow.events.list_after(session_id, 0, composition.principal)


async def test_a_replayed_message_reports_the_duplicate_and_seeds_one_run() -> None:
    async with build(
        settings=_settings(),
        script=_replies(4),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        first = await _ingest(composition)
        second = await _ingest(composition)
        events = await _session_events(composition, first.session_id)

    assert first.duplicate is False
    assert second == first.model_copy(update={"duplicate": True})
    assert len([event for event in events if event.event_type == "run.queued"]) == 1
    assert len([event for event in events if event.event_type == "user.message.created"]) == 1


async def test_the_same_instant_spelled_with_an_offset_and_as_utc_is_one_duplicate() -> None:
    """`ingest_digest` normalizes to UTC before hashing, so a re-spelled offset
    of the same instant is a replay, not a second message.

    Before the fix, `ingest_digest` hashed `received_at.isoformat()` verbatim:
    the same instant spelled `+01:00` and `Z`/UTC produced two digests, two
    receipts, and two triage runs.
    """
    offset_spelling = NOW.astimezone(timezone(timedelta(hours=1)))
    utc_spelling = NOW.astimezone(UTC)

    async with build(
        settings=_settings(),
        script=_replies(4),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        first = await _ingest(composition, received_at=offset_spelling)
        second = await _ingest(composition, received_at=utc_spelling)
        events = await _session_events(composition, first.session_id)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.session_id == first.session_id
    assert second.run_id == first.run_id
    assert len([event for event in events if event.event_type == "run.queued"]) == 1


async def test_an_idle_standing_session_takes_the_next_message_as_a_new_run() -> None:
    async with build(
        settings=_settings(),
        script=_replies(4),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        first = await _ingest(composition)
        second = await _ingest(composition, body=OTHER_BODY)

    assert second.session_id == first.session_id
    assert second.run_id != first.run_id


async def test_a_waiting_standing_run_takes_the_message_as_untrusted_input() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Should I reply to the sitter?"},
                        call_id="ask-owner",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Triaged both."),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        first = await _ingest(composition)
        waiting = await composition.runs.get(first.run_id)
        second = await _ingest(composition, body=OTHER_BODY)
        resumed = await composition.runs.get(first.run_id)
        events = await _session_events(composition, first.session_id)
        async with composition.uow_factory() as uow:
            history = await uow.history.read(first.session_id, events[-1].sequence)

    assert waiting.status is RunStatus.WAITING_FOR_USER
    assert second.run_id == first.run_id
    assert second.session_id == first.session_id
    assert resumed.status is RunStatus.COMPLETED

    # The delivery is the untrusted half of the deliver-input path: both the
    # message it appends and the tool result it completes carry the device's
    # trust, so dropping either would let the resumed turn reach a plain allow.
    delivered = [
        event
        for event in events
        if event.event_type == "user.message.created"
        and event.derivation_key == f"device.ingest:{_digest_of(OTHER_BODY)}"
    ]
    assert [event.payload["trust"] for event in delivered] == ["external_untrusted"]
    assert [event.actor_type for event in delivered] == ["device"]
    completed = [
        event
        for event in events
        if event.event_type == "tool.call.completed"
        and event.derivation_key == f"device.ingest.completed:{_digest_of(OTHER_BODY)}"
    ]
    assert [event.payload["result_item"]["trust"] for event in completed] == ["external_untrusted"]
    assert [
        item.trust
        for item in history.items
        if isinstance(item, UserMessage) and OTHER_BODY in str(item.content)
    ] == [TrustLevel.EXTERNAL_UNTRUSTED]
    assert [
        item.trust
        for item in history.items
        if isinstance(item, ToolResultItem) and OTHER_BODY in str(item.content)
    ] == [TrustLevel.EXTERNAL_UNTRUSTED]


async def test_a_busy_standing_session_rotates_onto_a_fresh_session() -> None:
    async with build(
        settings=_settings(),
        script=_replies(4),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        first = await _ingest(composition)
        async with composition.uow_factory() as uow:
            finished = await uow.runs.get(first.run_id, composition.principal)
            await _park_queued_run(composition, finished)
        second = await _ingest(composition, body=OTHER_BODY)
        async with composition.uow_factory() as uow:
            mapping = await uow.device_ingest.get_triage_mapping(DEVICE_ID, "sms")

    assert second.session_id != first.session_id
    assert mapping is not None
    assert mapping.session_id == second.session_id


async def _park_queued_run(composition: Composition, template: Run) -> Run:
    """Leave one queued run in the standing session so the next message rotates."""

    queued = template.model_copy(
        update={
            "id": composition.ids.new_id(),
            "status": RunStatus.QUEUED,
            "final_message": None,
            "failure": None,
        },
        deep=True,
    )
    async with composition.uow_factory() as uow:
        await uow.runs.create(queued)
    return queued


async def test_the_daily_cap_refuses_further_messages() -> None:
    async with build(
        settings=_settings(),
        script=_replies(4),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        service = cast(DeviceMessageIngestService, composition.services.device_ingest)
        service._ingest_daily_cap = 1

        await _ingest(composition)
        with pytest.raises(DeviceIngestError) as refused:
            await _ingest(composition, body=OTHER_BODY)

    assert refused.value.reason == "ingest_daily_cap"
    assert BODY not in str(refused.value)
    assert OTHER_BODY not in str(refused.value)


async def test_sms_ingest_is_refused_while_the_sms_flag_is_unset() -> None:
    """The channel flag alone opens the route; only the SMS flag admits a message.

    Configuration pairs the two flags, so the half-enabled deployment is
    refused before it composes. The service still states the rule, because the
    route is mounted by the channel flag and the channel is admitted by the
    SMS one.
    """

    async with build(
        settings=_settings(),
        script=_replies(1),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)
        service = cast(DeviceMessageIngestService, composition.services.device_ingest)
        service._sms_enabled = False

        with pytest.raises(DeviceIngestError) as refused:
            await _ingest(composition)

    assert refused.value.reason == "channel_disabled"


async def test_an_unknown_channel_is_refused_by_name() -> None:
    async with build(
        settings=_settings(),
        script=_replies(1),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        with pytest.raises(DeviceIngestError) as refused:
            await _ingest(composition, channel="imessage")

    assert refused.value.reason == "channel_unsupported"


async def test_an_unknown_or_revoked_device_cannot_ingest() -> None:
    async with build(
        settings=_settings(),
        script=_replies(1),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        with pytest.raises(NotFoundError):
            await _ingest(composition)

        await _seed_device(composition, status=DeviceStatus.REVOKED)
        with pytest.raises(ConflictError) as revoked:
            await _ingest(composition)

    assert revoked.value.reason == "device_revoked"


async def test_the_message_body_lands_only_in_the_seed_event_content() -> None:
    async with build(
        settings=_settings(),
        script=_replies(2),
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        await _seed_device(composition)

        result = await _ingest(composition)
        events = await _session_events(composition, result.session_id)
        async with composition.uow_factory() as uow:
            process_events = await uow.process_events.list()
            receipt = await uow.device_ingest.get(DEVICE_ID, "sms", _digest())

    carriers = [event for event in events if BODY in str(event.payload)]
    assert [event.event_type for event in carriers] == ["user.message.created"]
    assert carriers[0].payload["trust"] == "external_untrusted"
    assert carriers[0].payload["origin"] == {
        "kind": "device_ingest",
        "device_id": str(DEVICE_ID),
        "channel": "sms",
        "digest": _digest(),
    }
    assert not [event for event in process_events if BODY in str(event.payload)]
    assert isinstance(receipt, DeviceIngestReceipt)
    assert BODY not in receipt.model_dump_json()


def _digest() -> str:
    return _digest_of(BODY)


def _digest_of(body: str) -> str:
    return ingest_digest(SENDER, body, NOW)


async def test_a_triage_turn_cannot_reach_a_plain_allow() -> None:
    """Gate 10: the seeded turn taints every consequential call it drives."""

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name=DEVICE_SMS_SEND_TOOL_NAME,
                        arguments={"recipient": SENDER, "body": "Feeding at six."},
                        call_id="draft-reply",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Drafted."),
        ]
    )
    channel = FakeDeviceChannel(
        clock=FixedClock(NOW),
        capabilities={DEVICE_ID: frozenset({DEVICE_SMS_SEND_TOOL_NAME})},
    )
    async with build(
        settings=_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        recording: list[PolicyDecisionType] = []
        inner = composition.tool_pipeline._policy

        class _Recording:
            async def evaluate(
                self,
                action: ProposedAction,
                principal: Principal,
                run: Run,
            ) -> PolicyDecision:
                decision = await inner.evaluate(action, principal, run)
                if action.name == DEVICE_SMS_SEND_TOOL_NAME:
                    recording.append(decision.decision)
                return decision

        composition.tool_pipeline._policy = cast(PolicyEngine, _Recording())
        result = await _ingest(composition)
        parked = await composition.runs.get(result.run_id)
        pending = await composition.approvals.list_pending(run_id=result.run_id)

    assert recording == [PolicyDecisionType.REQUIRE_APPROVAL]
    assert parked.status is RunStatus.WAITING_FOR_APPROVAL
    assert [approval.tool_name for approval in pending] == [DEVICE_SMS_SEND_TOOL_NAME]


def _draft_reply_script(*, preamble: list[ScriptedTurn]) -> FakeModelScript:
    """Answer the owner first, then let the next triage turn reach for the send."""

    return FakeModelScript(
        turns=[
            *preamble,
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name=DEVICE_SMS_SEND_TOOL_NAME,
                        arguments={"recipient": SENDER, "body": "Feeding at six."},
                        call_id="draft-reply",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Drafted."),
        ]
    )


def _record_device_decisions(
    composition: Composition,
    recording: list[PolicyDecisionType],
) -> None:
    inner = composition.tool_pipeline._policy

    class _Recording:
        async def evaluate(
            self,
            action: ProposedAction,
            principal: Principal,
            run: Run,
        ) -> PolicyDecision:
            decision = await inner.evaluate(action, principal, run)
            if action.name == DEVICE_SMS_SEND_TOOL_NAME:
                recording.append(decision.decision)
            return decision

    composition.tool_pipeline._policy = cast(PolicyEngine, _Recording())


async def test_an_owner_turn_in_the_triage_session_does_not_launder_the_next_message() -> None:
    """Gate 10 in the steady state: an owner message earlier in the session is not consent.

    The standing triage session accumulates owner turns — every alert answered,
    every question asked. The trust of the turn being executed is the trust of
    the message that opened it, not of the newest owner message anywhere behind
    it.
    """

    channel = FakeDeviceChannel(
        clock=FixedClock(NOW),
        capabilities={DEVICE_ID: frozenset({DEVICE_SMS_SEND_TOOL_NAME})},
    )
    async with build(
        settings=_settings(),
        script=_draft_reply_script(
            preamble=[ScriptedTurn(text="Triaged."), ScriptedTurn(text="Noted.")]
        ),
        fixed_clock_at=NOW,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        recording: list[PolicyDecisionType] = []

        first = await _ingest(composition)
        owner_run = await composition.runs.submit("Thanks, noted.", first.session_id)
        await composition.runs.wait_terminal(owner_run)
        _record_device_decisions(composition, recording)
        second = await _ingest(composition, body=OTHER_BODY)
        parked = await composition.runs.get(second.run_id)
        pending = await composition.approvals.list_pending(run_id=second.run_id)

    assert second.session_id == first.session_id
    assert recording == [PolicyDecisionType.REQUIRE_APPROVAL]
    assert parked.status is RunStatus.WAITING_FOR_APPROVAL
    assert [approval.tool_name for approval in pending] == [DEVICE_SMS_SEND_TOOL_NAME]
    assert channel.invocations == []


async def test_an_owner_answer_to_a_triage_question_does_not_launder_the_next_message() -> None:
    """The same steady state reached through the ask_user path the design names."""

    channel = FakeDeviceChannel(
        clock=FixedClock(NOW),
        capabilities={DEVICE_ID: frozenset({DEVICE_SMS_SEND_TOOL_NAME})},
    )
    async with build(
        settings=_settings(),
        script=_draft_reply_script(
            preamble=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="conversation.ask_user",
                            arguments={"question": "Should I reply to the sitter?"},
                            call_id="ask-owner",
                        )
                    ],
                    stop_reason=StopReason.TOOL_USE,
                ),
                ScriptedTurn(text="Triaged."),
            ]
        ),
        fixed_clock_at=NOW,
        sequential_ids=True,
        device_channel_override=channel,
    ) as composition:
        await _seed_device(composition)
        recording: list[PolicyDecisionType] = []

        first = await _ingest(composition)
        events = await composition.runs.events(first.run_id)
        waiting_event = next(
            event for event in events if event.event_type == "run.waiting_for_user"
        )
        await composition.services.runs.deliver_input(
            composition.principal,
            first.run_id,
            [TextContentBlock(text="Yes, tell them six o'clock.")],
            UUID(str(waiting_event.payload["question_id"])),
        )
        await composition.runs.wait_terminal(first.run_id)
        _record_device_decisions(composition, recording)
        second = await _ingest(composition, body=OTHER_BODY)
        parked = await composition.runs.get(second.run_id)

    assert second.session_id == first.session_id
    assert second.run_id != first.run_id
    assert recording == [PolicyDecisionType.REQUIRE_APPROVAL]
    assert parked.status is RunStatus.WAITING_FOR_APPROVAL
    assert channel.invocations == []
