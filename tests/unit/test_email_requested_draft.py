"""An explicit reply request takes precedence over an automatic assessment."""

from dataclasses import replace
from uuid import uuid4

import pytest

from agent_core.application.email import save_value
from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_learning_m26 import observation, prepare
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory


@pytest.mark.parametrize("instruction", [None, "Ask what they need from me."])
async def test_requested_draft_overrides_automatic_reply_abstention(
    instruction: str | None,
) -> None:
    """A copied recipient may request a reply without changing automatic reply need."""
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=FakeModelScript(turns=[ScriptedTurn(text='{"body": "How can I help?"}')]),
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", observation(), uuid4())
        thread = thread.model_copy(
            update={"needs_reply": False, "reply_blocked_reason": "The owner is only copied."}
        )
        async with app.uow_factory() as uow:
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), thread, app.clock.now()
            )

        # The assessment still prevents an unsolicited automatic proposal.
        with pytest.raises(ConflictError):
            await service.save_generated_draft(
                app.principal, thread.id, thread.revision, "Automatic reply", run_id=uuid4()
            )

        operation = await service.submit_task(
            app.principal, kind="draft", thread_id=thread.id, instruction=instruction
        )
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        detail = await service.thread(app.principal, thread.id)
        draft = detail["draft"]
        assert isinstance(draft, dict)
        assert draft["body"] == "How can I help?"
        assert draft["status"] == "ready"
        assert detail["needs_reply"] is False

        # Owner intent does not permit saving against an obsolete source revision.
        with pytest.raises(ConflictError):
            await service.save_generated_draft(
                app.principal,
                thread.id,
                thread.revision + 1,
                "Stale reply",
                run_id=uuid4(),
                instruction="",
            )
