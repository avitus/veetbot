"""Source-revision and historical personalization regressions."""

from typing import Any, cast
from uuid import uuid4

from agent_core.application.email import EmailExperienceService, save_value
from agent_core.bootstrap import Composition
from agent_core.domain.email import EmailAccount
from tests.gates.test_email_experience_m26 import email_client


def observation(
    *, sent: bool = False, body: str = "Please review the board materials."
) -> dict[str, Any]:
    return {
        "thread_id": "t1",
        "history_id": "100",
        "complete": True,
        "source_changed": False,
        "messages": [
            {
                "id": "m1",
                "thread_id": "t1",
                "history_id": "100",
                "internal_date": "1704067200000",
                "from": "owner@example.com" if sent else "ceo@example.com",
                "to": "ceo@example.com" if sent else "owner@example.com",
                "cc": "colleague@example.com",
                "subject": "Board materials",
                "body": body,
                "body_complete": True,
                "headers_complete": True,
                "attachments": [],
                "reply_to": "reply@example.com" if not sent else "",
                "message_id_header": "<m1@example.com>",
                "references": "<earlier@example.com>",
                "in_reply_to": "",
                "label_ids": ["SENT" if sent else "INBOX"],
                "direction": "sent" if sent else "received",
            }
        ],
    }


async def prepare(composition: Composition) -> EmailExperienceService:
    service = composition.services.email
    service.account_ids = ("work",)
    service.account_servers = {"work": {"read": "gmail_read", "send": "gmail_send"}}
    async with composition.uow_factory() as uow:
        await save_value(
            uow.email,
            composition.principal,
            "account",
            "work",
            EmailAccount(
                id="work",
                label="Work",
                email_address="owner@example.com",
                verified_addresses=["owner@example.com"],
                status="ready",
            ),
            service.clock.now(),
        )
    return service


async def test_resuming_learning_processes_previously_paused_unchanged_mail() -> None:
    async with email_client() as (composition, _):
        service = await prepare(composition)
        principal = composition.principal
        await service.pause_learning(principal, True)
        source = observation(
            sent=True,
            body=(
                "Thanks, Alex. I agree with the revised agenda.\n\n"
                "On Monday Alex wrote:\nQuoted original"
            ),
        )
        imported = await service.import_thread(principal, "work", source, uuid4())
        assert (await service.learning(principal)).style_examples == 0
        await service.pause_learning(principal, False)
        replay = await service.import_thread(principal, "work", source, uuid4())
        assert replay.revision == imported.revision
        assert (await service.learning(principal)).style_examples == 1
        context = cast(dict[str, Any], await service.learning_context(principal, replay))
        assert "Quoted original" not in context["style_examples"][0]["excerpt"]


async def test_explicit_regeneration_preserves_previous_revision_and_reply_headers() -> None:
    async with email_client() as (composition, _):
        service = await prepare(composition)
        principal = composition.principal
        source = await service.import_thread(principal, "work", observation(), uuid4())
        first = await service.save_generated_draft(
            principal,
            source.id,
            source.revision,
            "Thanks. I will review the materials.",
            run_id=uuid4(),
        )
        assert first.to == ["reply@example.com"]
        assert first.cc == ["colleague@example.com"]
        assert first.in_reply_to == "<m1@example.com>"
        assert first.references == ["<earlier@example.com>", "<m1@example.com>"]
        auto = await service.save_generated_draft(
            principal, source.id, source.revision, "Automatic replacement", run_id=uuid4()
        )
        assert auto.body == first.body
        regenerated = await service.save_generated_draft(
            principal,
            source.id,
            source.revision,
            "Thanks — I'll read them before the meeting.",
            run_id=uuid4(),
            instruction="More concise",
        )
        assert regenerated.id == first.id
        assert regenerated.revision == 2
        assert regenerated.body != first.body
        history = await service.draft_revisions(principal, first.id)
        assert isinstance(history["items"], list) and len(history["items"]) == 2


async def test_sender_title_alone_cannot_supply_relationship_importance() -> None:
    async with email_client() as (composition, _):
        service = await prepare(composition)
        source = await service.import_thread(composition.principal, "work", observation(), uuid4())
        context = cast(
            dict[str, Any], await service.learning_context(composition.principal, source)
        )
        assessed = await service.save_assessment(
            composition.principal,
            source.id,
            source.revision,
            {
                "summary": "Cold pitch",
                "reason": "Sender says CEO",
                "topics": [],
                "content_importance": 0,
                "relationship_importance": 1,
                "urgency": 0,
                "bulk": False,
                "needs_reply": False,
                "profile_revision": context["profile_revision"],
            },
        )
        assert assessed.priority < 0.7


async def test_repeated_learning_reset_uses_latest_watermark() -> None:
    from datetime import UTC, datetime

    from agent_core.adapters.determinism import FixedClock

    async with email_client() as (composition, _):
        service = await prepare(composition)
        service.clock = FixedClock(datetime(2023, 1, 1, tzinfo=UTC))
        await service.reset_learning(composition.principal, "all")
        source = observation(
            sent=True, body="Thanks Alex. I agree with your proposal and will follow up."
        )
        await service.import_thread(composition.principal, "work", source, uuid4())
        assert (await service.learning(composition.principal)).style_examples == 1
        service.clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
        await service.reset_learning(composition.principal, "all")
        await service.import_thread(composition.principal, "work", source, uuid4())
        assert (await service.learning(composition.principal)).style_examples == 0
        async with composition.uow_factory() as uow:
            assert not await uow.email.list(composition.principal, "relationship")


async def test_learning_context_is_bounded_to_relevant_correspondents() -> None:
    async with email_client() as (composition, _):
        service = await prepare(composition)
        thread = await service.import_thread(composition.principal, "work", observation(), uuid4())
        async with composition.uow_factory() as uow, uow.email.lock(composition.principal):
            for i in range(80):
                await service._put_data(
                    uow.email,
                    composition.principal,
                    "relationship",
                    str(i),
                    {
                        "sent_at": service.clock.now().isoformat(),
                        "recipients": [f"unrelated{i}@example.com"],
                    },
                )
        context = cast(
            dict[str, Any], await service.learning_context(composition.principal, thread)
        )
        assert context["reply_partner_counts"] == {}
        overview = await service.learning_context(composition.principal, None)
        assert overview["reply_partner_counts"] == {}
        assert overview["owner_feedback"] == []


async def test_saved_owner_edit_learns_only_authored_delta_not_generated_copy() -> None:
    from agent_core.domain.email import EmailDraftEdit

    async with email_client() as (composition, _):
        service = await prepare(composition)
        source = await service.import_thread(composition.principal, "work", observation(), uuid4())
        generated = await service.save_generated_draft(
            composition.principal,
            source.id,
            source.revision,
            "Thanks for the materials. I will review them soon.",
            run_id=uuid4(),
        )
        unchanged = await service.edit_draft(
            composition.principal,
            generated.id,
            EmailDraftEdit(
                expected_revision=1, to=generated.to, subject=generated.subject, body=generated.body
            ),
        )
        assert (await service.learning(composition.principal)).style_examples == 0
        revised = await service.edit_draft(
            composition.principal,
            generated.id,
            EmailDraftEdit(
                expected_revision=unchanged.revision,
                to=generated.to,
                subject=generated.subject,
                body=generated.body + "\n\nLet's sharpen the agenda before we meet, Alex.",
            ),
        )
        context = cast(
            dict[str, Any], await service.learning_context(composition.principal, source)
        )
        assert len(context["style_examples"]) == 1
        sample = context["style_examples"][0]
        assert sample["excerpt"] == "Let's sharpen the agenda before we meet, Alex."
        assert sample["authorship"] == "owner_edit_delta"
        assert sample["draft_revision"] == revised.revision


async def test_excluding_thread_includes_source_ids_removed_by_later_provider_versions() -> None:
    from agent_core.domain.agents import Principal

    async with email_client() as (composition, _):
        service = await prepare(composition)
        source = observation()
        original = await service.import_thread(composition.principal, "work", source, uuid4())
        source["messages"][0]["id"] = "replacement"
        updated = await service.import_thread(composition.principal, "work", source, uuid4())
        forgotten: set[str] = set()

        async def capture(
            principal: Principal, account: str, thread: str, ids: frozenset[str]
        ) -> None:
            forgotten.update(ids)

        service.forget_source = capture
        await service.exclude_source(composition.principal, original.id, updated.revision)
        assert forgotten == {"m1", "replacement"}


async def test_relationship_context_respects_provider_sensitivity_and_deal_recency() -> None:
    from datetime import timedelta

    from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity
    from tests.contract.memory_fixtures import memory

    async with email_client() as (composition, _):
        service = await prepare(composition)
        source = await service.import_thread(composition.principal, "work", observation(), uuid4())
        now = service.clock.now()
        async with composition.uow_factory() as uow:
            for i, (text, sensitivity, age) in enumerate(
                [
                    ("ceo@example.com is a fellow board member", Sensitivity.INTERNAL, 0),
                    ("ceo@example.com has a secret personal situation", Sensitivity.SENSITIVE, 0),
                    (
                        "Active investment discussion with ceo@example.com",
                        Sensitivity.INTERNAL,
                        100,
                    ),
                ]
            ):
                record = memory(belief_id=2000 + i, statement=text).model_copy(
                    update={
                        "tenant_id": composition.principal.tenant_id,
                        "principal_id": composition.principal.principal_id,
                        "scope": "general",
                        "origin_scopes": ["general"],
                        "subject": "ceo@example.com",
                        "belief_type": BeliefType.RELATIONSHIP,
                        "authority": MemoryAuthority.INFERRED,
                        "sensitivity": sensitivity,
                        "valid_from": now - timedelta(days=200),
                        "last_evidence_at": now - timedelta(days=age),
                    }
                )
                await uow.memories.upsert_belief(record)
        context = cast(
            dict[str, Any], await service.learning_context(composition.principal, source)
        )
        assert [item["statement"] for item in context["shared_memories"]] == [
            "ceo@example.com is a fellow board member"
        ]


async def test_generated_sent_lineage_survives_mime_whitespace_and_body_expiry() -> None:
    from datetime import UTC, datetime

    from agent_core.domain.email import EmailDraftStatus

    async with email_client() as (composition, _):
        service = await prepare(composition)
        source = await service.import_thread(composition.principal, "work", observation(), uuid4())
        generated = await service.save_generated_draft(
            composition.principal,
            source.id,
            source.revision,
            "Thanks for sending the agenda. I will review it before we meet.",
            run_id=uuid4(),
        )
        async with composition.uow_factory() as uow, uow.email.lock(composition.principal):
            await save_value(
                uow.email,
                composition.principal,
                "draft",
                str(generated.id),
                generated.model_copy(
                    update={
                        "status": EmailDraftStatus.SENT,
                        "updated_at": datetime(2024, 1, 1, tzinfo=UTC),
                    }
                ),
                service.clock.now(),
            )
        await service.expire_cache(composition.principal)
        await service.import_thread(
            composition.principal,
            "work",
            observation(sent=True, body=generated.body + "\r\n"),
            uuid4(),
        )
        assert (await service.learning(composition.principal)).style_examples == 0


async def test_owner_style_examples_survive_newer_inferred_samples_for_same_recipient() -> None:
    from datetime import timedelta

    async with email_client() as (composition, _):
        service = await prepare(composition)
        source = await service.import_thread(composition.principal, "work", observation(), uuid4())
        now = service.clock.now()
        async with composition.uow_factory() as uow, uow.email.lock(composition.principal):
            for index in range(12):
                await service._put_data(
                    uow.email,
                    composition.principal,
                    "style",
                    str(index),
                    {
                        "account_id": "work",
                        "thread_id": str(source.id),
                        "sent_at": (now - timedelta(days=12 - index)).isoformat(),
                        "recipients": ["ceo@example.com"],
                        "excerpt": f"Writing example {index}",
                        "authorship": "owner_endorsed"
                        if index == 0
                        else "historical_sent_attributed",
                    },
                )
            await service._prune_styles(uow.email, composition.principal)
        context = cast(
            dict[str, Any], await service.learning_context(composition.principal, source)
        )
        assert context["style_examples"][0]["authorship"] == "owner_endorsed"
