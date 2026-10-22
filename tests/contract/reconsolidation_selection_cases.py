"""Provider-sized selection must reach an actual supervised review outcome."""

import json
from collections.abc import AsyncIterator
from typing import cast

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
    TextPart,
    UserMessage,
)
from agent_core.memory.reconsolidation_worker import ReconsolidationPass
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_admission_cases import model, policy
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory, reply
from tests.contract.support import NOW, SESSION_ID, principal


class MergeProvider(ExecutionProvider):
    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        message = request.conversation[1]
        assert isinstance(message, UserMessage) and isinstance(message.content[0], TextPart)
        payload = json.loads(message.content[0].text)
        response = json.loads(reply(request))
        if "operations" in response:
            operation = response["operations"][0]
            operation["kind"] = "merge_equivalent"
            operation["clauses"] = [
                {
                    "text": memory().statement,
                    "support": [
                        {"belief_id": source["belief_id"], "excerpt_id": source["excerpt_ids"][0]}
                        for source in payload["groups"][0]["sources"]
                    ],
                }
            ]
        else:
            response["verdicts"] = [
                {
                    "operation_id": operation["id"],
                    "clause_index": index,
                    "status": "supported",
                    "reason": "entailed",
                }
                for operation in payload["operations"]
                for index, _ in enumerate(operation["clauses"])
            ]
        async for event in super().stream(request, resolved, attempt):
            assert isinstance(event, ModelCompletedEvent)
            yield event.model_copy(
                update={
                    "turn": event.turn.model_copy(
                        update={
                            "assistant_messages": [
                                AssistantMessage(
                                    item_index=0, content=[TextPart(text=json.dumps(response))]
                                )
                            ]
                        }
                    )
                }
            )


async def oversized_neighbors_reach_owner_review(factory: Factory, *, queued: bool = False) -> None:
    originals = []
    excerpts = []
    async with factory() as uow:
        for index in range(14):
            text = memory().statement + " Notes: " + ('"é" ' * 270) + str(index)
            assert len(text.encode()) <= 2048
            excerpts.append(text)
            await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=principal().principal_id,
                    payload={"content": text},
                )
            )
            original = memory(belief_id=501 + index).model_copy(
                update={
                    "store_position": index + 1,
                    "source_event_ids": [index + 1],
                }
            )
            originals.append(original)
            await uow.memories.upsert_belief(original)
        if queued:
            # Match a partially scanned production job: current anchors remain
            # in the change lane after the legacy oversized group was queued.
            for revision in range(10):
                originals = [
                    original.model_copy(update={"confidence": 0.80 + revision / 100})
                    for original in originals
                ]
                for original in originals:
                    await uow.memories.reinforce(original)
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "old-selection")
            assert lease is not None
            page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
            await uow.reconsolidation.checkpoint(
                principal(), lease.lease_token, page, (page.sources,), NOW
            )
            assert page.change_cursor < lease.change_bound
            await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
    tracked = TrackedFactory(factory)
    provider = MergeProvider(tracked)
    worker = ReconsolidationPass(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        worker_id="bounded-selection",
        admitted=lambda: True,
        ids=RandomIdFactory(),
        model=model(),
        provider=provider,
        egress_policy=policy,
        owner_review=True,
    )
    await worker.run_once()
    assert len(provider.requests) == 2, "Oversized neighbors must form a provider-sized group"
    assert all(len(request.model_dump_json().encode()) <= 16384 for request in provider.requests)
    message = provider.requests[0].conversation[1]
    assert isinstance(message, UserMessage) and isinstance(message.content[0], TextPart)
    payload = json.loads(message.content[0].text)
    assert all(2 <= len(group["sources"]) < 14 for group in payload["groups"])
    for group in payload["groups"]:
        assert all(excerpt["text"] in excerpts for excerpt in group["excerpts"])
    async with factory() as uow:
        runs = await uow.reconsolidation.dreaming_runs(principal(), NOW)
        assert runs[0].outcome == "proposed" and runs[0].proposals > 0
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
        assert not await uow.reconsolidation.active_merges(
            principal(), tuple(original.id for original in originals), NOW
        )
    assert await worker.run_once() == 0
    assert len(provider.requests) == 2, "Selection must not bypass the daily admission limit"


async def oversized_pair_remains_explicitly_blocked(factory: Factory) -> None:
    async with factory() as uow:
        for index in range(2):
            await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=principal().principal_id,
                    payload={
                        "content": [
                            {"kind": "text", "text": "Complete original text. " * 60}
                            for _ in range(16)
                        ]
                    },
                )
            )
            await uow.memories.upsert_belief(
                memory(belief_id=501 + index).model_copy(
                    update={
                        "store_position": index + 1,
                        "source_event_ids": [index + 1],
                    }
                )
            )
    tracked = TrackedFactory(factory)
    provider = ExecutionProvider(tracked)
    worker = ReconsolidationPass(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        worker_id="unfittable-pair",
        admitted=lambda: True,
        ids=RandomIdFactory(),
        model=model(),
        provider=provider,
        egress_policy=policy,
        owner_review=True,
    )
    await worker.run_once()
    assert not provider.requests
    async with factory() as uow:
        runs = await uow.reconsolidation.dreaming_runs(principal(), NOW)
        assert runs[0].outcome == "blocked" and runs[0].deferrals == ("input_budget",)
