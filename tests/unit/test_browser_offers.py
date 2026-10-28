"""ADR-0251: an unconnected chat offers to connect a website the owner names."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.application.browser_offers import OwnedWebsite, match_websites
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings, load_settings
from agent_core.domain.agents import run_limits_for_session
from agent_core.domain.browser import BrowserProfileStatus
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    UserMessage,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import TextContentBlock
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_schedule_api_m11 import _client
from tests.unit.test_browser_composition import (
    PROFILE_ID,
    context_plan_payload,
    one_text_turn,
    runtime_metadata_rows,
    seed_browser_authority,
    session_bound_hosted_settings,
)
from tests.unit.test_config import base_environment

PORTAL = UUID("00000000-0000-0000-0000-0000000001a1")
SHOP = UUID("00000000-0000-0000-0000-0000000001a2")
SOCIAL = UUID("00000000-0000-0000-0000-0000000001a3")
CAR = UUID("00000000-0000-0000-0000-0000000001a4")
WWW = UUID("00000000-0000-0000-0000-0000000001a5")

OWNED = (
    OwnedWebsite(PORTAL, ("https://mychart.example-health.org",)),
    OwnedWebsite(SHOP, ("https://www.amazon.com",)),
    OwnedWebsite(SOCIAL, ("https://x.com",)),
    OwnedWebsite(CAR, ("https://www.bmw.com",)),
)


def test_a_host_or_registrable_domain_in_the_text_matches() -> None:
    assert match_websites("open mychart.example-health.org please", OWNED) == (PORTAL,)
    assert match_websites("Check example-health.org for my results.", OWNED) == (PORTAL,)
    assert match_websites("post this on x.com", OWNED) == (SOCIAL,)
    assert match_websites("look at bmw.com", OWNED) == (CAR,)


def test_words_that_join_to_the_domain_label_match() -> None:
    assert match_websites("Login to Example Health and get my readings", OWNED) == (PORTAL,)
    assert match_websites("what did examplehealth send?", OWNED) == (PORTAL,)
    assert match_websites("Order it on Amazon", OWNED) == (SHOP,)


def test_a_long_first_host_label_matches_its_joined_words() -> None:
    assert match_websites("Open My Chart and read the latest note", OWNED) == (PORTAL,)
    assert match_websites("open mychart", OWNED) == (PORTAL,)


def test_a_five_letter_word_that_begins_the_domain_label_matches() -> None:
    assert match_websites("Sign in to Example and get my readings", OWNED) == (PORTAL,)
    assert match_websites("Check Amazo", OWNED) == (SHOP,)
    assert match_websites("Check Amaz", OWNED) == ()
    assert match_websites("an exam is due", OWNED) == ()


def test_a_country_second_level_domain_keeps_its_name_label() -> None:
    owned = (OwnedWebsite(PORTAL, ("https://portal.example-clinic.co.uk",)),)

    assert match_websites("check example-clinic.co.uk", owned) == (PORTAL,)
    assert match_websites("Log in to Example Clinic", owned) == (PORTAL,)
    assert match_websites("the co is busy", owned) == ()


def test_a_short_label_matches_only_by_host_or_domain() -> None:
    assert match_websites("x marks the spot", OWNED) == ()
    assert match_websites("my BMW stalls", OWNED) == ()
    assert (
        match_websites("the www is big", (OwnedWebsite(WWW, ("https://www.example.net",)),)) == ()
    )


def test_unrelated_text_matches_nothing() -> None:
    assert match_websites("What is the weather tomorrow?", OWNED) == ()
    assert match_websites("", OWNED) == ()


def test_longest_matched_text_comes_first_and_at_most_three_are_offered() -> None:
    many = (
        OwnedWebsite(SHOP, ("https://www.amazon.com",)),
        OwnedWebsite(PORTAL, ("https://mychart.example-health.org",)),
        OwnedWebsite(SOCIAL, ("https://x.com",)),
        OwnedWebsite(CAR, ("https://www.bmw.com",)),
    )
    text = "amazon, x.com, bmw.com and example-health.org"

    # example-health.org (18) > bmw.com (7) > amazon (6) > x.com (5)
    assert match_websites(text, many) == (PORTAL, CAR, SHOP)


def test_each_profile_is_offered_once() -> None:
    text = "Example Health at example-health.org, also known as MyChart"

    assert match_websites(text, OWNED) == (PORTAL,)


# Admission (ADR-0251 decisions 1 and 2) ------------------------------------

OTHER = UUID("00000000-0000-0000-0000-0000000001b1")


async def _owned_x_profile(composition: Composition) -> None:
    async with composition.uow_factory() as uow:
        profile = await uow.browser_profiles.get(PROFILE_ID, composition.principal)
        await uow.browser_profiles.create(
            profile.model_copy(update={"id": OTHER, "allowed_origins": ("https://x.com",)})
        )


async def _say(composition: Composition, chat_id: UUID, text: str, key: str) -> UUID:
    submitted = await composition.services.runs.submit(
        composition.principal, chat_id, [TextContentBlock(text=text)], key, None
    )
    ended = await composition.runs.wait_terminal(submitted.run_id)
    assert ended.status is RunStatus.COMPLETED, ended.failure
    return submitted.run_id


async def _events(composition: Composition, chat_id: UUID) -> list[EventEnvelope]:
    async with composition.uow_factory() as uow:
        return await uow.events.list_after(chat_id, 0, composition.principal)


async def test_naming_an_owned_website_in_an_unconnected_chat_records_an_offer() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        run_id = await _say(composition, chat.id, "Sign in to Example and get my readings", "a")
        events = await _events(composition, chat.id)
        stored = await composition.services.sessions.get(composition.principal, chat.id)
        plan = await context_plan_payload(composition, chat.id)

    [source] = [event for event in events if event.event_type == "user.message.created"]
    [offered] = [event for event in events if event.event_type == "session.browser.offered"]
    assert offered.run_id == run_id
    assert offered.actor_type == "application"
    assert offered.payload["source_sequence"] == source.sequence
    assert offered.payload["profiles"] == [
        {"profile_id": str(PROFILE_ID), "origins": ["https://example.org"]}
    ]
    assert offered.payload["connected_origin"] is None
    UUID(str(offered.payload["offer_id"]))
    # A match never connects the chat or changes its tool surface.
    assert "browser_profile_id" not in stored.metadata
    assert not {"browser.navigate", "browser.observe"} & set(cast(list[str], plan["tool_names"]))


async def test_text_that_names_no_owned_website_records_no_offer() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        await _say(composition, chat.id, "What is the weather tomorrow?", "a")
        events = await _events(composition, chat.id)

    assert not [event for event in events if event.event_type == "session.browser.offered"]


async def test_the_website_the_chat_already_uses_is_never_offered() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        await _say(composition, chat.id, "Sign in to Example and get my readings", "a")
        events = await _events(composition, chat.id)

    assert not [event for event in events if event.event_type == "session.browser.offered"]


async def test_a_chat_on_another_website_is_offered_a_switch() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        await _owned_x_profile(composition)
        chat = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=OTHER
        )
        await _say(composition, chat.id, "Sign in to Example and get my readings", "a")
        events = await _events(composition, chat.id)
        stored = await composition.services.sessions.get(composition.principal, chat.id)

    [offered] = [event for event in events if event.event_type == "session.browser.offered"]
    assert offered.payload["connected_origin"] == "https://x.com"
    assert [item["profile_id"] for item in offered.payload["profiles"]] == [str(PROFILE_ID)]
    assert stored.metadata["browser_profile_id"] == str(OTHER)


async def test_a_composition_without_per_chat_websites_never_offers() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    async with build(settings=settings, script=one_text_turn()) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        await _say(composition, chat.id, "Sign in to Example and get my readings", "a")
        events = await _events(composition, chat.id)

    assert not [event for event in events if event.event_type == "session.browser.offered"]


# The model learns the remedy, not the profile (ADR-0251 decision 3) --------


async def _row(composition: Composition, chat_id: UUID, text: str) -> str:
    await _say(composition, chat_id, text, "row")
    [row] = runtime_metadata_rows(composition)
    return row


async def test_the_source_run_is_told_the_named_website_awaits_a_connect_button() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        row = await _row(composition, chat.id, "Sign in to Example and get my readings")

    assert "; website_offer=https://example.org (" in row
    assert "not connected to this chat" in row
    assert "Connect button" in row
    assert "website=none" not in row
    assert str(PROFILE_ID) not in row


async def test_a_switch_offer_names_the_website_the_chat_uses() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        await _owned_x_profile(composition)
        chat = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=OTHER
        )
        row = await _row(composition, chat.id, "Sign in to Example and get my readings")

    assert "; website_offer=https://example.org (" in row
    assert "connected to https://x.com instead" in row
    assert str(PROFILE_ID) not in row and str(OTHER) not in row


async def test_an_unconnected_chat_without_a_match_is_told_where_websites_connect() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(composition.principal, "general", {})
        row = await _row(composition, chat.id, "What is the weather tomorrow?")

    assert "; website=none (" in row
    assert "Website Access in this chat" in row
    assert "website_offer" not in row


async def test_a_connected_chat_without_a_new_match_gets_no_website_row() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=one_text_turn()
    ) as composition:
        await seed_browser_authority(composition)
        chat = await composition.services.sessions.create(
            composition.principal, "general", {}, browser_profile_id=PROFILE_ID
        )
        row = await _row(composition, chat.id, "Sign in to Example and get my readings")

    assert "website_offer" not in row and "website=none" not in row


# Accepting the card continues the request (ADR-0251 decisions 4 and 5) -----


def two_turns() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(text="Tap Connect", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="Continued", stop_reason=StopReason.END_TURN),
        ]
    )


async def _offered_chat(composition: Composition) -> UUID:
    await seed_browser_authority(composition)
    chat = await composition.services.sessions.create(composition.principal, "general", {})
    await _say(composition, chat.id, "Sign in to Example and get my readings", "source")
    return chat.id


async def test_the_open_offer_lists_the_matched_website_for_the_card() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        events = await _events(composition, chat_id)

    [offered] = [event for event in events if event.event_type == "session.browser.offered"]
    assert offer is not None
    assert str(offer.offer_id) == offered.payload["offer_id"]
    assert offer.source_sequence == offered.payload["source_sequence"]
    assert offer.connected_origin is None
    assert [(item.profile_id, item.origins, item.status) for item in offer.websites] == [
        (PROFILE_ID, ["https://example.org"], BrowserProfileStatus.READY)
    ]
    assert offer.expires_at == offered.created_at + timedelta(hours=24)


async def test_accepting_an_offer_connects_the_chat_and_continues_the_request() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        accepted = await composition.services.runs.connect_browser(
            composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
        )
        assert accepted is not None
        ended = await composition.runs.wait_terminal(accepted.run_id)
        stored = await composition.services.sessions.get(composition.principal, chat_id)
        events = await _events(composition, chat_id)
        [rotated] = [event for event in events if event.event_type == "context.epoch.rotated"]
        after = await composition.services.runs.website_offer(composition.principal, chat_id)

    assert ended.status is RunStatus.COMPLETED, ended.failure
    assert stored.metadata["browser_profile_id"] == str(PROFILE_ID)
    seed = next(
        event
        for event in events
        if event.event_type == "user.message.created" and event.run_id == accepted.run_id
    )
    # The owner's tap admits it: owner authority, but not text the owner typed.
    assert seed.actor_type == "principal"
    assert seed.payload["origin"] == {
        "kind": "browser_offer",
        "offer_id": str(offer.offer_id),
        "source_sequence": offer.source_sequence,
    }
    assert "example.org" in seed.payload["content"][0]["text"]
    # Connecting rotates the context at the continuation's admission (ADR-0172).
    assert rotated.sequence > seed.sequence
    assert "browser.navigate" in rotated.payload["plan"]["tool_names"]
    assert after is None


async def test_replaying_an_acceptance_returns_the_same_run() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        first = await composition.services.runs.connect_browser(
            composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
        )
        assert first is not None
        await composition.runs.wait_terminal(first.run_id)
        replay = await composition.services.runs.connect_browser(
            composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
        )
        events = await _events(composition, chat_id)

    assert replay is not None and replay.run_id == first.run_id and replay.replayed
    assert sum(event.event_type == "user.message.created" for event in events) == 2


async def test_a_later_owner_message_closes_the_offer() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text="Tap Connect", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="Sunny", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=session_bound_hosted_settings(), script=script) as composition:
        chat_id = await _offered_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        await _say(composition, chat_id, "What is the weather tomorrow?", "later")
        assert await composition.services.runs.website_offer(composition.principal, chat_id) is None
        with pytest.raises(ConflictError) as refused:
            await composition.services.runs.connect_browser(
                composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
            )
        stored = await composition.services.sessions.get(composition.principal, chat_id)

    assert refused.value.reason == "browser_offer_not_open"
    assert "browser_profile_id" not in stored.metadata


async def test_an_offer_expires_24_hours_after_its_source_message() -> None:
    clock = FixedClock(datetime(2026, 10, 25, 17, tzinfo=UTC))
    async with build(
        settings=session_bound_hosted_settings(), script=two_turns(), clock=clock
    ) as composition:
        chat_id = await _offered_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        clock.advance(timedelta(hours=24))
        assert await composition.services.runs.website_offer(composition.principal, chat_id) is None
        with pytest.raises(ConflictError) as refused:
            await composition.services.runs.connect_browser(
                composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
            )

    assert refused.value.reason == "browser_offer_not_open"


async def test_a_website_outside_the_offer_cannot_accept_it() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        await _owned_x_profile(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        with pytest.raises(ConflictError) as refused:
            await composition.services.runs.connect_browser(
                composition.principal, chat_id, OTHER, offer_id=offer.offer_id
            )
        stored = await composition.services.sessions.get(composition.principal, chat_id)

    assert refused.value.reason == "browser_offer_not_open"
    assert "browser_profile_id" not in stored.metadata


async def test_connecting_the_chat_another_way_closes_the_offer() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        await _owned_x_profile(composition)
        assert (
            await composition.services.runs.connect_browser(composition.principal, chat_id, OTHER)
            is None
        )
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)

    assert offer is None


# A source run that asks the owner a question (owner decision 3) -------------


def ask_then_continue() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={"question": "Tap Connect so I can sign in to Example."},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Continued", stop_reason=StopReason.END_TURN),
        ]
    )


def _worker(composition: Composition) -> DurableWorker:
    # PostgreSQL compositions queue runs; the test drives one worker by hand.
    return DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id="website-offer-fixture",
    )


async def _asking_chat(
    composition: Composition, worker: DurableWorker | None = None
) -> tuple[UUID, UUID]:
    await seed_browser_authority(composition)
    chat = await composition.services.sessions.create(composition.principal, "general", {})
    submitted = await composition.services.runs.submit(
        composition.principal,
        chat.id,
        [TextContentBlock(text="Sign in to Example and get my readings")],
        "source",
        None,
    )
    if worker is not None:
        assert await worker.run_once()
    for _ in range(200):
        current = await composition.services.runs.get(composition.principal, submitted.run_id)
        if current.status is RunStatus.WAITING_FOR_USER:
            break
        await asyncio.sleep(0.01)
    assert current.status is RunStatus.WAITING_FOR_USER, current.failure
    return chat.id, submitted.run_id


def _runtime_rows(composition: Composition, request: int) -> str:
    provider = cast(FakeModelProvider, composition.executor._model_provider)
    return "\n".join(
        part.text
        for item in provider.requests[request].conversation
        if isinstance(item, UserMessage)
        for part in item.content
        if isinstance(part, TextPart) and part.text.startswith("Runtime metadata")
    )


async def test_accepting_while_the_source_run_asks_answers_it_in_that_run(
    *, settings: Settings | None = None, storage: Literal["memory", "postgres"] = "memory"
) -> None:
    async with build(
        settings=settings or session_bound_hosted_settings(),
        script=ask_then_continue(),
        storage=storage,
    ) as composition:
        worker = _worker(composition) if storage == "postgres" else None
        chat_id, source_run = await _asking_chat(composition, worker)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        accepted = await composition.services.runs.connect_browser(
            composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
        )
        assert accepted is not None
        if worker is not None:
            assert await worker.run_once()
        ended = await composition.runs.wait_terminal(accepted.run_id)
        stored = await composition.services.sessions.get(composition.principal, chat_id)
        events = await _events(composition, chat_id)
        after = await composition.services.runs.website_offer(composition.principal, chat_id)
        async with composition.uow_factory() as uow:
            continued = await uow.runs.get(source_run, composition.principal)
            bound = await uow.sessions.get(chat_id, composition.principal)
            agent = await uow.agents.get_version(bound.agent_id, bound.agent_version)
        first_row, last_row = _runtime_rows(composition, 0), _runtime_rows(composition, -1)

    assert accepted.run_id == source_run and not accepted.replayed
    assert ended.status is RunStatus.COMPLETED, ended.failure
    assert stored.metadata["browser_profile_id"] == str(PROFILE_ID)
    asked = next(event for event in events if event.event_type == "run.waiting_for_user")
    [answer] = [
        event
        for event in events
        if event.event_type == "user.message.created" and event.sequence > asked.sequence
    ]
    # The owner's tap answers the question: owner authority, in the same run.
    assert answer.run_id == source_run
    assert answer.actor_type == "principal"
    assert answer.payload["question_id"] == asked.payload["question_id"]
    assert answer.payload["origin"] == {
        "kind": "browser_offer",
        "offer_id": str(offer.offer_id),
        "source_sequence": offer.source_sequence,
    }
    assert answer.derivation_key == f"browser_offer:{offer.offer_id}"
    assert "example.org" in answer.payload["content"][0]["text"]
    assert {event.run_id for event in events if event.event_type == "run.queued"} == {source_run}
    # A new capability epoch: the resumed run plans again with browser tools.
    replanned = [
        event
        for event in events
        if event.event_type in {"context.plan.created", "context.epoch.rotated"}
        and event.sequence > answer.sequence
    ]
    assert any("browser.navigate" in event.payload["plan"]["tool_names"] for event in replanned)
    assert continued.limits == run_limits_for_session(agent, bound)
    assert "website_offer=" in first_row and "website_offer=" not in last_row
    assert after is None


async def test_replaying_an_answering_acceptance_returns_the_same_run() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=ask_then_continue()
    ) as composition:
        chat_id, source_run = await _asking_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        first = await composition.services.runs.connect_browser(
            composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
        )
        assert first is not None
        await composition.runs.wait_terminal(first.run_id)
        replay = await composition.services.runs.connect_browser(
            composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
        )
        events = await _events(composition, chat_id)

    assert replay is not None and replay.run_id == source_run and replay.replayed
    assert sum(event.event_type == "user.message.created" for event in events) == 2


async def test_a_typed_answer_to_the_source_question_closes_the_offer() -> None:
    async with build(
        settings=session_bound_hosted_settings(), script=ask_then_continue()
    ) as composition:
        chat_id, source_run = await _asking_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        await composition.services.runs.submit(
            composition.principal, chat_id, [TextContentBlock(text="Switch")], "typed", None
        )
        await composition.runs.wait_terminal(source_run)
        with pytest.raises(ConflictError) as refused:
            await composition.services.runs.connect_browser(
                composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
            )
        stored = await composition.services.sessions.get(composition.principal, chat_id)

    assert refused.value.reason == "browser_offer_not_open"
    assert "browser_profile_id" not in stored.metadata


async def test_any_other_active_run_still_refuses_an_acceptance() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        offer = await composition.services.runs.website_offer(composition.principal, chat_id)
        assert offer is not None
        async with composition.uow_factory() as uow:
            source = await uow.runs.latest_for_session(chat_id, composition.principal)
            assert source is not None
            await uow.runs.create(
                source.model_copy(
                    update={"id": uuid4(), "status": RunStatus.RUNNING, "seed_event_sequence": 0}
                )
            )
        with pytest.raises(ConflictError) as refused:
            await composition.services.runs.connect_browser(
                composition.principal, chat_id, PROFILE_ID, offer_id=offer.offer_id
            )
        stored = await composition.services.sessions.get(composition.principal, chat_id)

    assert refused.value.reason == "active_run_exists"
    assert "browser_profile_id" not in stored.metadata


# The HTTP boundary (ADR-0251 decision 5) ------------------------------------


async def test_the_offer_and_its_acceptance_over_http() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        async with _client(composition) as client:
            read = await client.get(f"/v1/sessions/{chat_id}/website-offer")
            assert read.status_code == 200, read.text
            offer = read.json()["offer"]
            body = {"browser_profile_id": str(PROFILE_ID), "offer_id": offer["offer_id"]}
            accepted = await client.put(f"/v1/sessions/{chat_id}/website", json=body)
            assert accepted.status_code == 200, accepted.text
            run_id = UUID(accepted.json()["run_id"])
            await composition.runs.wait_terminal(run_id)
            replay = await client.put(f"/v1/sessions/{chat_id}/website", json=body)
            closed = await client.get(f"/v1/sessions/{chat_id}/website-offer")

    assert offer["websites"] == [
        {"profile_id": str(PROFILE_ID), "origins": ["https://example.org"], "status": "ready"}
    ]
    assert offer["connected_origin"] is None
    assert replay.status_code == 200 and replay.json()["run_id"] == str(run_id)
    assert closed.status_code == 200 and closed.json() == {"offer": None}


async def test_a_closed_offer_is_a_409_and_malformed_ids_are_400() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text="Tap Connect", stop_reason=StopReason.END_TURN),
            ScriptedTurn(text="Sunny", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=session_bound_hosted_settings(), script=script) as composition:
        chat_id = await _offered_chat(composition)
        async with _client(composition) as client:
            offer = (await client.get(f"/v1/sessions/{chat_id}/website-offer")).json()["offer"]
            await _say(composition, chat_id, "What is the weather tomorrow?", "later")
            stale = await client.put(
                f"/v1/sessions/{chat_id}/website",
                json={"browser_profile_id": str(PROFILE_ID), "offer_id": offer["offer_id"]},
            )
            malformed = await client.put(
                f"/v1/sessions/{chat_id}/website",
                json={"browser_profile_id": str(PROFILE_ID), "offer_id": "not-an-offer"},
            )

    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "conflict"
    assert stale.json()["error"]["details"]["reason"] == "browser_offer_not_open"
    assert malformed.status_code == 400


async def test_reading_an_offer_needs_website_scope_and_an_owned_chat() -> None:
    async with build(settings=session_bound_hosted_settings(), script=two_turns()) as composition:
        chat_id = await _offered_chat(composition)
        restricted = composition.principal.model_copy(update={"scopes": {"session.read"}})
        async with _client(composition, principal=restricted) as client:
            denied = await client.get(f"/v1/sessions/{chat_id}/website-offer")
        async with _client(composition) as client:
            missing = await client.get(f"/v1/sessions/{uuid4()}/website-offer")
            no_offer_chat = await composition.services.sessions.create(
                composition.principal, "general", {}
            )
            empty = await client.get(f"/v1/sessions/{no_offer_chat.id}/website-offer")

    assert denied.status_code == 403
    assert missing.status_code == 404
    assert empty.status_code == 200 and empty.json() == {"offer": None}
