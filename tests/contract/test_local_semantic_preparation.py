"""Offline execution broadens processing, never the visibility of source evidence."""

from decimal import Decimal

import pytest

from agent_core.domain.local_embeddings import LOCAL_EMBEDDING_REVISION, LocalEmbeddingConfig
from agent_core.domain.memory import Sensitivity
from agent_core.memory.semantic_indexing import SemanticIndexingPass
from agent_core.memory.semantic_queries import SemanticQueryPreparation
from agent_core.memory.semantic_runtime import RunSemanticQueries
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider
from tests.contract.support import RUN_ID, SESSION_ID, principal
from tests.contract.test_runtime_semantic_queries_contract import AUTHORITY, runtime_stack
from tests.contract.test_semantic_query_preparation import CONTEXT, query

LOCAL = LocalEmbeddingConfig(
    model="test-local",
    dimensions=3,
    model_sha256="a" * 64,
    tokenizer_sha256="b" * 64,
    maximum_tokens=256,
    threads=1,
)
CONFIG = INDEX_CONFIG.model_copy(
    update={
        "space": LOCAL.space,
        "usd_per_million_tokens": Decimal(0),
        "pricing_revision": "local-cpu@1",
        "egress_revision": LOCAL_EMBEDDING_REVISION,
        "residency_provider": "local",
    }
)


async def test_local_index_keeps_restricted_sources_under_current_visibility() -> None:
    clock, factory, _, _ = await formation_stack()
    original = memory(statement="The user's private diagnosis constrains breakfast").model_copy(
        update={"sensitivity": Sensitivity.RESTRICTED}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    provider = Provider(factory)
    worker = SemanticIndexingPass(
        factory, clock, principal(), provider, CONFIG, admitted=lambda: True, local=LOCAL
    )
    assert (await worker.run_slice()).indexed == 1, (
        "offline indexing must cover restricted originals"
    )
    async with factory() as uow:
        assert await uow.semantic_work.spent(principal(), clock.now().date()) == 0
        low = recall_query().model_copy(update={"sensitivity_ceiling": Sensitivity.PUBLIC})
        assert await uow.semantic_memory.search(low, LOCAL.space, (1.0, 0.0, 0.0), limit=1) == []
        high = low.model_copy(update={"sensitivity_ceiling": Sensitivity.RESTRICTED})
        assert [
            hit.record.id
            for hit in await uow.semantic_memory.search(high, LOCAL.space, (1.0, 0.0, 0.0), limit=1)
        ] == [original.id]
        await uow.memories.reinforce(
            original.model_copy(update={"statement": "Corrected diagnosis"})
        )
        assert await uow.semantic_memory.search(high, LOCAL.space, (1.0, 0.0, 0.0), limit=1) == []


async def test_local_queries_keep_restricted_floor_and_owner_scope() -> None:
    clock, factory, _, _ = await formation_stack()
    provider = Provider(factory)
    context = CONTEXT.model_copy(update={"sensitivity_floor": Sensitivity.RESTRICTED})
    lookup = SemanticQueryPreparation(
        factory,
        clock,
        principal(),
        provider,
        CONFIG,
        admitted=lambda: True,
        context=lambda: context,
        local=LOCAL,
    )
    personal = query("Which breakfast fits my medical constraints?")
    assert (await lookup.prepare([personal])).prepared == 1, (
        "offline queries need no outbound downgrade"
    )
    assert lookup.lookup(personal) is not None
    assert (
        await lookup.prepare([personal.model_copy(update={"principal_id": "foreign"})])
    ).prepared == 0
    assert len(provider.calls) == 1


async def test_run_bound_preparation_passes_trusted_offline_configuration() -> None:
    clock, factory, provider, _, _, state = await runtime_stack()
    state.authority = AUTHORITY.model_copy(update={"sensitivity_floor": Sensitivity.RESTRICTED})
    runtime = RunSemanticQueries(
        factory,
        clock,
        principal(),
        provider,
        CONFIG,
        admitted=lambda: True,
        authority=lambda *_: state.authority,
        local=LOCAL,
    )
    assert (
        await runtime.prepare([query()], session_id=SESSION_ID, run_id=RUN_ID, surface_id="private")
        == 1
    )
    lookup = await runtime.bind(session_id=SESSION_ID, run_id=RUN_ID, surface_id="private")
    assert lookup is not None and lookup.lookup(query()) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"space": INDEX_CONFIG.space},
        {"residency_provider": "openai"},
        {"egress_revision": "remote-policy@1"},
        {"usd_per_million_tokens": Decimal("0.02")},
    ],
)
async def test_offline_authority_cannot_be_applied_to_a_remote_tuple(
    change: dict[str, object],
) -> None:
    clock, factory, _, _ = await formation_stack()
    with pytest.raises(ValueError, match="local embedding configuration"):
        SemanticIndexingPass(
            factory,
            clock,
            principal(),
            Provider(factory),
            CONFIG.model_copy(update=change),
            admitted=lambda: True,
            local=LOCAL,
        )
