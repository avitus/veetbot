"""Provider-sized selection must reach an actual supervised review outcome."""

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import cast

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelPricing,
    ModelRequest,
    ModelUsage,
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


async def oversized_neighbors_reach_owner_review(
    factory: Factory, *, queued: bool = False, priced: bool = False
) -> None:
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

    class PricedMergeProvider(MergeProvider):
        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            async for event in super().stream(request, resolved, attempt):
                assert isinstance(event, ModelCompletedEvent)
                yield event.model_copy(
                    update={
                        "turn": event.turn.model_copy(
                            update={
                                "usage": ModelUsage(
                                    input_tokens=4111,
                                    output_tokens=1200,
                                    cache_write_input_tokens=4108,
                                    provider=resolved.provider,
                                    model=resolved.model,
                                )
                            }
                        )
                    }
                )

    provider = PricedMergeProvider(tracked) if priced else MergeProvider(tracked)
    resolved = model()
    if priced:
        resolved = resolved.model_copy(
            update={
                "pricing": ModelPricing(
                    input_per_mtok=Decimal("5"),
                    output_per_mtok=Decimal("30"),
                    cache_write_per_mtok=Decimal("6.25"),
                )
            }
        )
    worker = ReconsolidationPass(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        worker_id="bounded-selection",
        admitted=lambda: True,
        ids=RandomIdFactory(),
        model=resolved,
        provider=provider,
        egress_policy=policy,
        owner_review=True,
    )
    await worker.run_once()
    assert len(provider.requests) == 2, "Oversized neighbors must form a provider-sized group"
    if priced:
        assert provider.requests[0].maximum_output_tokens == 4096
        output_cap = provider.requests[1].maximum_output_tokens
        assert output_cap is not None and 1200 <= output_cap < 4096
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
        if priced:
            assert runs[0].charged_usd == Decimal("0.12338")
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


async def privacy_refused_neighbors_preserve_review(
    factory: Factory, *, queued: bool = False
) -> None:
    from agent_core.memory.reconsolidation_policy import local_egress_policy

    originals = []
    async with factory() as uow:
        for index in range(14):
            # The first memory is harmless; its complete original also contains
            # private material. Its other parts must not be stripped for export.
            text = memory().statement + (" My passport number is 123456789." if index == 0 else "")
            await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=principal().principal_id,
                    payload={"content": text + " Notes: " + ('"é" ' * 260)},
                )
            )
            original = memory(belief_id=501 + index).model_copy(
                update={"store_position": index + 1, "source_event_ids": [index + 1]}
            )
            originals.append(original)
            await uow.memories.upsert_belief(original)
        if queued:
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "old-selection")
            assert lease is not None
            page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
            old_groups = await uow.reconsolidation.checkpoint(
                principal(), lease.lease_token, page, (page.sources,), NOW
            )
            released = await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
            assert released.full_cursor == released.full_bound
            assert released.change_cursor == released.change_bound
            assert released.state == "ready"
    at = NOW + timedelta(days=1) if queued else NOW
    tracked = TrackedFactory(factory)
    provider = MergeProvider(tracked)
    worker = ReconsolidationPass(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(at),
        principal(),
        worker_id="privacy-selection",
        admitted=lambda: True,
        ids=RandomIdFactory(),
        model=model(),
        provider=provider,
        egress_policy=local_egress_policy(model().provider),
        owner_review=True,
    )
    await worker.run_once()
    assert len(provider.requests) == 2, "Private neighbors must not block eligible owner review"
    for request in provider.requests:
        assert len(request.model_dump_json().encode()) <= 16384
        assert "passport" not in request.model_dump_json()
    message = provider.requests[0].conversation[1]
    assert isinstance(message, UserMessage) and isinstance(message.content[0], TextPart)
    payload = json.loads(message.content[0].text)
    assert all(
        source["belief_id"] != str(originals[0].id)
        for group in payload["groups"]
        for source in group["sources"]
    )
    async with factory() as uow:
        runs = await uow.reconsolidation.dreaming_runs(principal(), at)
        assert runs[0].outcome == "proposed" and runs[0].proposals > 0
        assert "egress_denied" in runs[0].deferrals
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
        assert not await uow.reconsolidation.active_merges(
            principal(), tuple(original.id for original in originals), at
        )
        if queued:
            # Original blocked work is still retried, with its immutable membership.
            retry_lease = await uow.reconsolidation.claim_due(principal(), at, "inspect-retry")
            assert retry_lease is not None
            retry = await uow.reconsolidation.claim_group(principal(), retry_lease.lease_token, at)
            assert retry is not None and retry.id == old_groups[0].id
            assert retry.sources == old_groups[0].sources and retry.attempts == 2
            await uow.reconsolidation.finish_group(
                principal(), retry_lease.lease_token, retry.id, "retry", at
            )
            await uow.reconsolidation.release(principal(), retry_lease.lease_token, at)
    assert await worker.run_once() == 0
    assert len(provider.requests) == 2


async def shared_refused_original_preserves_independent_review(factory: Factory) -> None:
    from agent_core.memory.reconsolidation_policy import local_egress_policy

    originals = []
    async with factory() as uow:
        for _ in range(2):
            await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=principal().principal_id,
                    payload={"content": memory().statement},
                )
            )
        for index in range(4):
            original = memory(belief_id=501 + index).model_copy(
                update={
                    "store_position": index + 1,
                    "source_event_ids": [1 if index < 2 else 2],
                    "statement": "User prefers confidential answers."
                    if index == 0
                    else memory().statement,
                }
            )
            originals.append(original)
            await uow.memories.upsert_belief(original)
    tracked = TrackedFactory(factory)
    provider = MergeProvider(tracked)
    worker = ReconsolidationPass(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        worker_id="shared-refused-source",
        admitted=lambda: True,
        ids=RandomIdFactory(),
        model=model(),
        provider=provider,
        egress_policy=local_egress_policy(model().provider),
        owner_review=True,
    )
    await worker.run_once()
    assert len(provider.requests) == 2, "Independent sources must still reach owner review"
    message = provider.requests[0].conversation[1]
    assert isinstance(message, UserMessage) and isinstance(message.content[0], TextPart)
    payload = json.loads(message.content[0].text)
    exported = {item["belief_id"] for group in payload["groups"] for item in group["sources"]}
    assert exported == {str(original.id) for original in originals[2:]}, (
        "A permitted memory must not export the original it shares with a refused memory"
    )
    assert all(
        excerpt["text"] == memory().statement
        for group in payload["groups"]
        for excerpt in group["excerpts"]
    )
    async with factory() as uow:
        runs = await uow.reconsolidation.dreaming_runs(principal(), NOW)
        assert runs[0].outcome == "proposed" and runs[0].proposals > 0
        assert "egress_denied" in runs[0].deferrals
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
