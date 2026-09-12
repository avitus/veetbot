"""Chat uses the same email context/profile without promoting mail to owner authority."""

from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

from agent_core.bootstrap import Composition
from agent_core.domain.events import NewEvent
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolExecutionContext
from agent_core.tools.email_context import EmailContextTool, EmailFeedbackTool
from tests.gates.test_email_experience_m26 import email_client, seed_mail


def context(composition: Composition, session_id: UUID) -> ToolExecutionContext:
    return cast(
        ToolExecutionContext,
        SimpleNamespace(
            principal=composition.principal,
            session_id=session_id,
            run_id=uuid4(),
            idempotency_key="chat-feedback",
            origin_trust=TrustLevel.USER,
            argument_trust={},
        ),
    )


async def assert_selected_email_context() -> None:
    async with email_client() as (composition, _):
        thread, _ = await seed_mail(composition)
        service = composition.services.email
        selected = await service.discussion(composition.principal, thread.id)
        ctx = context(composition, UUID(str(selected["session_id"])))
        result = await EmailContextTool(service).execute({}, ctx)
        assert result.ok
        assert result.structured is not None
        assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
        assert result.structured["context"]["source"] == {
            "account_id": "work",
            "provider_thread_id": "gmail-thread",
            "message_ids": ["m1"],
        }
        assert result.structured["context"]["messages"][0]["body"] == thread.messages[0].body
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(ctx.session_id, 0, composition.principal)
        assert not any(e.event_type == "user.message.created" for e in events)


async def assert_chat_feedback_requires_current_owner_source_and_replays_shared_rule() -> None:
    async with email_client() as (composition, _):
        thread, _ = await seed_mail(composition)
        service = composition.services.email
        selected = await service.discussion(composition.principal, thread.id)
        ctx = context(composition, UUID(str(selected["session_id"])))
        tool = EmailFeedbackTool(service)
        arguments = {
            "thread_id": str(thread.id),
            "expected_revision": 1,
            "target": "person",
            "judgment": "less_important",
            "owner_quote": "This sender is less important.",
        }
        denied = await tool.execute(arguments, ctx)
        assert not denied.ok
        async with composition.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=composition.principal.principal_id,
                    payload={"content": arguments["owner_quote"], "trust": "external_untrusted"},
                )
            )
        assert not (await tool.execute(arguments, ctx)).ok
        async with composition.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=composition.principal.principal_id,
                    payload={"content": arguments["owner_quote"]},
                )
            )
        applied = await tool.execute(arguments, ctx)
        assert applied.ok
        assert applied.structured is not None
        assert applied.structured["status"] == "applied"
        assert set(applied.structured) == {"feedback_id", "status"}
        assert (await tool.execute(arguments, ctx)).structured == applied.structured
        assert (await service.threads(composition.principal, view="priority"))["items"] == []


async def assert_chat_shared_style() -> None:
    from agent_core.domain.email import EmailDraftEdit

    async with email_client() as (composition, _):
        thread, draft = await seed_mail(composition)
        service = composition.services.email
        await service.edit_draft(
            composition.principal,
            draft.id,
            EmailDraftEdit(
                expected_revision=1,
                to=draft.to,
                subject=draft.subject,
                body=draft.body + "\nLet's sharpen the agenda together before we meet.",
            ),
        )
        selected = await service.discussion(composition.principal, thread.id)
        result = await EmailContextTool(service).execute(
            {}, context(composition, UUID(str(selected["session_id"])))
        )
        assert result.structured is not None
        examples = result.structured["writing_profile"]["examples"]
        assert len(examples) == 1
        assert examples[0]["source"]["provider_thread_id"] == "gmail-thread"
        assert (
            examples[0]["messages"][0]["body"]
            == "Let's sharpen the agenda together before we meet."
        )
        assert examples[0]["authorship"] == "owner_edit_delta"


async def test_selected_email_context_is_source_attributed_untrusted_and_bounded() -> None:
    await assert_selected_email_context()


async def test_chat_feedback_requires_current_owner_source_and_replays_shared_rule() -> None:
    await assert_chat_feedback_requires_current_owner_source_and_replays_shared_rule()


async def test_chat_context_uses_shared_owner_style_with_independent_source_binding() -> None:
    await assert_chat_shared_style()
