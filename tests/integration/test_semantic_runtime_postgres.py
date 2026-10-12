"""Run-bound preparation and governed retrieval over committed PostgreSQL state."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import build
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
from agent_core.memory.retrieval import DeterministicQueryFormer, HybridMemoryRetriever
from agent_core.memory.semantic_runtime import RunSemanticQueries
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import memory
from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider
from tests.contract.semantic_memory_cases import SPACE, VECTOR
from tests.contract.support import (
    NOW,
    RUN_ID,
    SESSION_ID,
    agent,
    principal,
    run,
    session,
    tool_context,
)
from tests.contract.test_runtime_semantic_queries_contract import AUTHORITY, query
from tests.gates.test_context_m7 import _builder
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("path", ["automatic", "deliberate", "context"])
async def test_runtime_binding_and_source_correction(path: str) -> None:
    clock = FixedClock(NOW)
    async with build(
        settings=database_settings(), storage="postgres", principal=principal(), clock=clock
    ) as app:
        original = memory(statement="Dairy causes the user digestive discomfort").model_copy(
            update={"subject": "diet"}
        )
        checkpoint = RunCheckpoint(
            run_id=RUN_ID,
            version=1,
            status=RunStatus.RUNNING,
            created_at=NOW,
            working_state={"objective": "Plan breakfast"},
            conversation=[UserMessage(content=[TextPart(text="Suggest breakfast options")])],
        )
        async with app.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(session())
            live_run = run(status=RunStatus.RUNNING).model_copy(
                update={
                    "lease_owner": "worker",
                    "lease_epoch": 1,
                    "lease_expires_at": NOW + timedelta(minutes=10),
                }
            )
            await uow.runs.create(live_run)
            await uow.checkpoints.write(RUN_ID, checkpoint, full=True)
            await uow.memories.upsert_belief(original)
            source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
            assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        provider = Provider(app.uow_factory)
        runtime = RunSemanticQueries(
            app.uow_factory,
            clock,
            principal(),
            provider,
            INDEX_CONFIG,
            admitted=lambda: True,
            authority=lambda *_: AUTHORITY,
        )
        retriever = HybridMemoryRetriever(
            app.uow_factory,
            clock,
            SequenceIdFactory(UUID(int=i) for i in range(8000, 8100)),
            principal(),
            profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
            reconsolidation_enabled=True,
            reconsolidation_owner_approved_only=True,
            runtime_semantic_queries=runtime,
        )
        context_builder = _builder(
            memory_retriever=retriever,
            query_former=DeterministicQueryFormer(principal(), current_scope="project-a"),
        )
        if path == "context":
            before = await context_builder.build(live_run, checkpoint, agent(), principal())
            assert original.statement not in before.model_dump_json()
        assert (
            await retriever.prepare_semantic_queries(
                [query("automatic" if path == "context" else path)],
                session_id=SESSION_ID,
                run_id=RUN_ID,
            )
            == 1
        )

        async def recall() -> str:
            if path == "context":
                request = await context_builder.build(live_run, checkpoint, agent(), principal())
                assert request.metadata["prefix_sha256"] == before.metadata["prefix_sha256"]
                return request.model_dump_json()
            if path == "automatic":
                return (
                    await retriever.recall(query(path), session_id=SESSION_ID, run_id=RUN_ID)
                ).rendered
            result = await MemorySearchTool(retriever).execute(
                {"text": query(path).text, "scope": "project-a"}, tool_context()
            )
            return str(result.content)

        assert original.statement in await recall()
        async with app.uow_factory() as uow:
            await uow.memories.reinforce(
                original.model_copy(update={"statement": "User enjoys fruit"})
            )
        assert original.statement not in await recall()
        async with app.uow_factory() as uow:
            await uow.checkpoints.write(
                RUN_ID,
                checkpoint.model_copy(
                    update={"version": 2, "working_state": {"objective": "Plan lunch"}}
                ),
                full=True,
            )
        assert (
            await runtime.bind(session_id=SESSION_ID, run_id=RUN_ID, surface_id="private") is None
        )
        assert len(provider.calls) == 1
