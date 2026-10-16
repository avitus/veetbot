"""Retrieval measurements after bulk seeding an opted-in scratch clone.

Repeated fabricated distractors and fixed 384-dimensional vectors isolate database
and retriever load. This does not measure encoding, formation, answer quality,
indexing throughput, deployment admission or production end-to-end latency.
"""

import argparse
import asyncio
import hashlib
import json
import platform
from dataclasses import replace
from datetime import timedelta
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import text

from agent_core.adapters.persistence.semantic_memory import PostgresSemanticMemoryIndex
from agent_core.adapters.persistence.sqlalchemy_models import MemoryRow, MemoryVectorRow
from agent_core.domain.memory import BeliefType, RecallQuery, RecallResult
from agent_core.domain.semantic_memory import EmbeddingSpace, SemanticQueryVector, query_digest
from agent_core.evals.general_recall_composition import build, fixed_clock, system_clock
from agent_core.evals.general_recall_database import evaluation_database
from agent_core.memory.reconsolidation_evidence import implementation_digest
from agent_core.memory.retrieval import DeterministicQueryFormer, HybridMemoryRetriever
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import memory
from tests.contract.support import NOW, SESSION_ID, principal, run, session, tool_context
from tests.integration.disposable_database import clone_recall_database
from tests.integration.m2_support import database_settings

SPACE = EmbeddingSpace(provider="synthetic", model="load-fixture", revision="v1", dimensions=384)
VECTOR = (1.0,) + (0.0,) * 383
NOISE_VECTOR = (0.0, 1.0) + (0.0,) * 382
TEXT = "copper sentinel deployment approval"


class PreparedVector:
    def lookup(self, query: RecallQuery) -> SemanticQueryVector:
        return SemanticQueryVector(query_sha256=query_digest(query), space=SPACE, vector=VECTOR)


class MeasuredRetriever(HybridMemoryRetriever):
    observed: RecallResult | None = None

    async def recall(self, query: RecallQuery, **kwargs: Any) -> RecallResult:
        self.observed = await super().recall(query, **kwargs)
        return self.observed


async def collect(output: Path) -> None:
    if output.exists():
        raise ValueError("Retain existing measurements; choose a new output")
    root = Path(__file__).resolve().parents[4]
    digest = implementation_digest(root)
    report: dict[str, Any] = {
        "version": "recall-postgres-load@1",
        "activation_evidence": False,
        "registered_at": system_clock().now().isoformat(),
        "implementation_sha256": digest,
        "measurement_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "system": platform.platform(),
        "cpu": platform.processor(),
        "vector_dimensions": 384,
        "provider_calls": 0,
        "state": "registered",
        "measurements": [],
        "limitations": __doc__,
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    started = perf_counter()
    try:
        async with clone_recall_database() as url:
            settings = replace(database_settings(), database_url=url)
            async with (
                evaluation_database(settings, storage="postgres", authorized=True),
                build(
                    settings=settings,
                    storage="postgres",
                    principal=principal(),
                    clock=fixed_clock(NOW),
                    enabled_tools=["memory.search"],
                    enabled_skills=[],
                ) as app,
            ):
                original = memory(statement="Copper sentinel is the deployment approval phrase.")
                original = original.model_copy(
                    update={
                        "belief_type": BeliefType.FACT,
                        "subject": "deployment approval",
                        "valid_from": NOW - timedelta(days=14),
                        "created_at": NOW - timedelta(days=14),
                        "updated_at": NOW - timedelta(days=14),
                    }
                )
                noise = memory(
                    belief_id=503, statement="The project review recorded a deployment note."
                ).model_copy(
                    update={
                        "belief_type": BeliefType.FACT,
                        "subject": "routine review",
                        "store_position": 2,
                    }
                )
                async with app.uow_factory() as uow:
                    await uow.sessions.create(session())
                    await uow.runs.create(run())
                    for record, vector in ((original, VECTOR), (noise, NOISE_VECTOR)):
                        await uow.memories.upsert_belief(record)
                        source = await uow.semantic_memory.capture(principal(), record.id, SPACE)
                        assert source is not None
                        assert await uow.semantic_memory.put(principal(), source, vector)
                    assert isinstance(uow.semantic_memory, PostgresSemanticMemoryIndex)
                    db = uow.semantic_memory._session
                    report["postgres"] = await db.scalar(text("SELECT version()"))
                    report["pgvector"] = await db.scalar(
                        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                    )
                previous = 1
                for competing in (100, 1000, 10000, 100000):
                    setup = perf_counter()
                    async with app.uow_factory() as uow:
                        assert isinstance(uow.semantic_memory, PostgresSemanticMemoryIndex)
                        db = uow.semantic_memory._session
                        values = []
                        for column in MemoryRow.__table__.columns:
                            values.append(
                                "md5('recall-load:' || n::text)::uuid"
                                if column.name == "id"
                                else "n + 2"
                                if column.name in {"creation_sequence", "store_position"}
                                else f'm."{column.name}"'
                            )
                        columns = ", ".join(f'"{c.name}"' for c in MemoryRow.__table__.columns)
                        await db.execute(
                            text(
                                f"INSERT INTO memories ({columns}) SELECT {', '.join(values)} "
                                "FROM memories m CROSS JOIN generate_series("
                                "CAST(:first AS bigint), CAST(:last AS bigint)) n "
                                "WHERE m.id = :template"
                            ),
                            {"first": previous, "last": competing - 1, "template": noise.id},
                        )
                        columns = ", ".join(
                            f'"{c.name}"' for c in MemoryVectorRow.__table__.columns
                        )
                        values = [
                            "md5('recall-load:' || n::text)::uuid"
                            if c.name == "belief_id"
                            else "n + 2"
                            if c.name == "creation_sequence"
                            else f'v."{c.name}"'
                            for c in MemoryVectorRow.__table__.columns
                        ]
                        await db.execute(
                            text(
                                f"INSERT INTO memory_vectors ({columns}) "
                                f"SELECT {', '.join(values)} FROM memory_vectors v "
                                "CROSS JOIN generate_series("
                                "CAST(:first AS bigint), CAST(:last AS bigint)) n "
                                "WHERE v.belief_id = :template"
                            ),
                            {"first": previous, "last": competing - 1, "template": noise.id},
                        )
                        assert (
                            await db.scalar(text("SELECT count(*) FROM memories")) == competing + 1
                        )
                        assert (
                            await db.scalar(text("SELECT count(*) FROM memory_vectors"))
                            == competing + 1
                        )
                        await db.execute(text("ANALYZE memories"))
                        await db.execute(text("ANALYZE memory_vectors"))
                    previous = competing
                    setup_seconds = perf_counter() - setup
                    for channel in ("sparse", "dense", "hybrid"):
                        profile = app.memory_profiles.retrieval.model_copy(
                            update={
                                "semantic_enabled": channel != "sparse",
                                "semantic_channel": "dense" if channel == "dense" else "hybrid",
                                "reranking": "relevance",
                            }
                        )
                        retriever = MeasuredRetriever(
                            app.uow_factory,
                            app.clock,
                            app.ids,
                            principal(),
                            profile=profile,
                            semantic_queries=PreparedVector(),
                            reranking_admitted=lambda: True,
                        )
                        tool = MemorySearchTool(retriever, model_planning=True)
                        query = DeterministicQueryFormer(
                            principal(), current_scope="project-a"
                        ).from_text(TEXT)[0]
                        for path in ("automatic", "deliberate"):
                            samples = []
                            for repeat in range(10):
                                before = perf_counter()
                                async with asyncio.timeout(30):
                                    if path == "automatic":
                                        result = await retriever.recall(
                                            query, session_id=SESSION_ID
                                        )
                                    else:
                                        called = await tool.execute(
                                            {
                                                "text": TEXT,
                                                "scope": "project-a",
                                                "type_hints": ["preference"],
                                            },
                                            tool_context(),
                                        )
                                        assert called.ok and retriever.observed is not None
                                        result = retriever.observed
                                elapsed_ms = (perf_counter() - before) * 1000
                                assert result.stages is not None
                                ranked = result.stages.ranked_ids
                                returned = [item.belief_id for item in result.items]
                                samples.append(
                                    {
                                        "repeat": repeat,
                                        "elapsed_ms": elapsed_ms,
                                        "candidate_count": len(result.stages.candidate_ids),
                                        "target_rank": ranked.index(original.id) + 1
                                        if original.id in ranked
                                        else None,
                                        "target_rendered": original.id in returned,
                                        "tokens": result.tokens,
                                        "items": len(returned),
                                    }
                                )
                            times = sorted(row["elapsed_ms"] for row in samples)
                            row = {
                                "competing_records": competing,
                                "channel": channel,
                                "path": path,
                                "bulk_fixture_seconds": setup_seconds,
                                "p50_ms": times[ceil(len(times) * 0.5) - 1],
                                "p95_ms": times[ceil(len(times) * 0.95) - 1],
                                "samples": samples,
                            }
                            report["measurements"].append(row)
                            output.write_text(json.dumps(report, indent=2) + "\n")
                            print(
                                json.dumps({k: v for k, v in row.items() if k != "samples"}),
                                flush=True,
                            )
        assert implementation_digest(root) == digest, "Implementation changed during measurement"
        report["state"] = "completed"
    except BaseException as error:
        report.update(state="failed", error_class=type(error).__name__)
        raise
    finally:
        report["wall_seconds"] = perf_counter() - started
        report["finished_at"] = system_clock().now().isoformat()
        output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(collect(args.output))
