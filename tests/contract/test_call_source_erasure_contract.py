"""Call erasure removes retained and derived copies while preserving owner input."""

import json
from uuid import uuid4

from agent_core.domain.events import NewEvent
from agent_core.domain.messages import TextPart, ToolResultItem
from agent_core.domain.policies import RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.support import NOW, memory_uow_factory, principal, run, session
from tests.contract.test_bland_client_contract import CALL_ID


async def assert_call_source_erasure(factory: UnitOfWorkFactory) -> None:
    sid, rid, later_rid, iid = uuid4(), uuid4(), uuid4(), uuid4()
    private_text = "PRIVATE CALL TRANSCRIPT CANARY"
    source = {
        "call_id": CALL_ID,
        "source": "bland",
        "account_id": "primary",
        "transcript": private_text,
        "summary": "Private call summary",
    }
    message = ToolResultItem(
        call_id="tool-call",
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        content=[TextPart(text=json.dumps(source))],
    )
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": sid}))
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(
                update={"id": rid, "session_id": sid, "final_message": private_text}
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                actor_type="runtime",
                event_type="tool.call.completed",
                payload={
                    "name": "mcp.bland_read.get_call",
                    "result_item": message.model_dump(mode="json"),
                },
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                actor_type="principal",
                actor_id=principal().principal_id,
                event_type="user.message.created",
                payload={"content": "Keep my original note"},
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                actor_type="runtime",
                event_type="assistant.message.completed",
                payload={"content": private_text},
            )
        )
        await uow.checkpoints.write(
            rid,
            RunCheckpoint(
                run_id=rid,
                version=1,
                status=RunStatus.COMPLETED,
                conversation=[message],
                last_event_sequence=3,
                created_at=NOW,
            ),
            full=True,
        )
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(
                update={"id": later_rid, "session_id": sid, "final_message": private_text}
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=later_rid,
                actor_type="runtime",
                event_type="assistant.message.completed",
                payload={"content": private_text},
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=later_rid,
                actor_type="runtime",
                event_type="tool.call.proposed",
                payload={"name": "file.write", "raw_arguments": json.dumps({"text": private_text})},
            )
        )
        await uow.invocations.create(
            ToolInvocation(
                id=iid,
                run_id=later_rid,
                session_id=sid,
                step_number=1,
                call_id="derived",
                tool_name="file.write",
                tool_version="1",
                side_effect=SideEffectClass.NONE,
                risk=RiskLevel.LOW,
                status=ToolInvocationStatus.SUCCEEDED,
                raw_arguments=json.dumps({"text": private_text}),
                normalized_arguments={"text": private_text},
                idempotency_key=str(iid),
                result_item=message,
                structured_result={"text": private_text},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    async with factory() as uow:
        counts = await uow.session_deletions.erase_call_source(principal(), CALL_ID, NOW)
    assert counts["events"] >= 1 and counts["checkpoints"] == 1
    async with factory() as uow:
        events = await uow.events.list_after(sid, 0, principal())
        serialized = json.dumps([event.model_dump(mode="json") for event in events])
        assert private_text not in serialized and "Private call summary" not in serialized
        assert "Keep my original note" in serialized
        assert (await uow.runs.get(rid, principal())).final_message is None
        assert (await uow.runs.get(later_rid, principal())).final_message is None
        assert await uow.checkpoints.latest(rid) is None
        invocation = await uow.invocations.find_by_idempotency_key(later_rid, str(iid))
        assert invocation is not None
        assert private_text not in invocation.model_dump_json()


async def test_memory_call_source_erasure() -> None:
    _clock, factory = await memory_uow_factory()
    await assert_call_source_erasure(factory)
