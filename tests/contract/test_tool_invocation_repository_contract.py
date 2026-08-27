from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemoryToolInvocationRepository
from agent_core.domain.errors import ConflictError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from tests.contract.support import NOW, RUN_ID, SESSION_ID, memory_stack, principal, run


async def test_tool_invocation_repository_deduplicates_stable_keys() -> None:
    _clock, _sessions, runs, _events = await memory_stack()
    await runs.create(run())
    repository = InMemoryToolInvocationRepository(runs)
    invocation = ToolInvocation(
        id=UUID(int=81),
        run_id=RUN_ID,
        session_id=SESSION_ID,
        step_number=1,
        call_id="call-1",
        tool_name="math.calculate",
        tool_version="1.0.0",
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=ToolInvocationStatus.PROPOSED,
        raw_arguments='{"expression":"1+1"}',
        idempotency_key="same-key",
        created_at=NOW,
        updated_at=NOW,
    )
    first = await repository.create(invocation)
    duplicate = await repository.create(invocation.model_copy(update={"id": UUID(int=82)}))
    assert duplicate.id == first.id
    assert len(await repository.list_for_run(RUN_ID, principal())) == 1

    invalid = first.model_copy(update={"status": ToolInvocationStatus.SUCCEEDED})
    with pytest.raises(ConflictError, match="invalid tool transition"):
        await repository.transition(first.id, ToolInvocationStatus.PROPOSED, invalid)

    changed_call = first.model_copy(
        update={"status": ToolInvocationStatus.AUTHORIZED, "call_id": "changed"}
    )
    with pytest.raises(ConflictError, match="immutable"):
        await repository.transition(first.id, ToolInvocationStatus.PROPOSED, changed_call)

    read_only_unknown = first.model_copy(
        update={
            "id": UUID(int=83),
            "idempotency_key": "read-only-unknown",
            "idempotency_class": IdempotencyClass.READ_ONLY,
            "status": ToolInvocationStatus.UNCERTAIN,
            "normalized_arguments_hash": "matching-arguments",
        }
    )
    await repository.create(read_only_unknown)
    assert not await repository.has_uncertain_non_idempotent(
        RUN_ID,
        tool_name=first.tool_name,
        normalized_arguments_hash="matching-arguments",
        principal=principal(),
    )

    non_idempotent_unknown = read_only_unknown.model_copy(
        update={
            "id": UUID(int=84),
            "idempotency_key": "non-idempotent-unknown",
            "idempotency_class": IdempotencyClass.NON_IDEMPOTENT,
        }
    )
    await repository.create(non_idempotent_unknown)
    assert await repository.has_uncertain_non_idempotent(
        RUN_ID,
        tool_name=first.tool_name,
        normalized_arguments_hash="matching-arguments",
        principal=principal(),
    )
