"""Reproducible local-encoder diagnostics through actual governed recall.

The fixed synthetic seed pool is development data, not release evidence. No
formation/answer provider, owner database, credentials or external tool is used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from collections import Counter
from decimal import Decimal
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from agent_core.config import (
    AuthMode,
    DeploymentMode,
    MemoryProviderExtractionMode,
    SandboxMechanism,
    Settings,
)
from agent_core.domain.local_embeddings import LOCAL_EMBEDDING_REVISION, LocalEmbeddingConfig
from agent_core.domain.memory import RecallQuery, Sensitivity
from agent_core.domain.semantic_work import EmbeddingConfig
from agent_core.evals.general_memory_recall import load_seeds
from agent_core.evals.general_memory_recall_driver import AT, OWNER, _seed, identifier
from agent_core.memory.reconsolidation_evidence import implementation_digest
from agent_core.memory.retrieval import DeterministicQueryFormer, HybridMemoryRetriever
from agent_core.memory.semantic_indexing import SemanticIndexingPass
from agent_core.memory.semantic_queries import SemanticQueryContext, SemanticQueryPreparation
from agent_core.tools.memory_search import MemorySearchTool

ROOT = Path(__file__).resolve().parents[3]
SEEDS = ROOT / "evals/capability/general-memory-recall-seeds.v1.tsv"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows or len({row["scenario_id"] for row in rows}) != len(rows):
        raise ValueError("Diagnostic requires distinct, nonempty scenarios")

    def cell(group: list[dict[str, Any]]) -> dict[str, Any]:
        failures: Counter[str] = Counter()
        for row in group:
            if row["rendered_rank"] is not None and row["rendered_rank"] <= 20:
                continue
            failures[
                "candidate"
                if row["candidate_rank"] is None
                else "ranking"
                if row["ranked_rank"] is None or row["ranked_rank"] > 20
                else "context_budget"
            ] += 1
        times = sorted(row["elapsed_ms"] for row in group)
        return {
            "scenarios": len(group),
            **{
                f"{stage}_recall_at_20": sum(
                    row[f"{stage}_rank"] is not None and row[f"{stage}_rank"] <= 20 for row in group
                )
                / len(group)
                for stage in ("dense", "ranked", "rendered")
            },
            "candidate_recall": sum(row["candidate_rank"] is not None for row in group)
            / len(group),
            "failures": dict(failures),
            "p50_ms": times[ceil(len(times) * 0.5) - 1],
            "p95_ms": times[ceil(len(times) * 0.95) - 1],
            "mean_context_tokens": sum(row["tokens"] for row in group) / len(group),
        }

    return {
        "overall": cell(rows),
        "families": {
            family: cell([row for row in rows if row["family"] == family])
            for family in sorted({row["family"] for row in rows})
        },
    }


class _LegacyQueries(SemanticQueryPreparation):
    """Frozen pre-ADR-0188 encoding, only for this isolated local comparison."""

    def _text(self, query: RecallQuery, context: SemanticQueryContext) -> str | None:
        if not self._local:
            raise ValueError("Legacy diagnostic encoding requires offline execution")
        text = json.dumps([query.text, *query.subjects], ensure_ascii=False, separators=(",", ":"))
        return text if len(text.encode()) <= 4096 else None


async def collect(
    configuration: LocalEmbeddingConfig,
    model_directory: Path,
    *,
    channel: Literal["sparse", "dense", "hybrid"],
    path: Literal["automatic", "deliberate"],
    encoding: Literal["json@1", "natural@2"],
    noise: int,
    report: dict[str, Any],
    output: Path,
) -> None:
    from agent_core.evals.general_recall_composition import build, local_embedding_provider

    seeds = load_seeds(SEEDS)
    rows: list[dict[str, Any]] = report["observations"]
    provider = local_embedding_provider(model_directory, configuration)
    embedding = EmbeddingConfig(
        space=configuration.space,
        usd_per_million_tokens=Decimal(0),
        pricing_revision="local-cpu@1",
        egress_revision=LOCAL_EMBEDDING_REVISION,
        residency_provider="local",
        request_usd=Decimal("0.01"),
        daily_usd=Decimal("0.10"),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="general-recall-local-") as temporary:
            settings = Settings(
                database_url="postgresql+asyncpg://127.0.0.1:1/unused",
                deployment_mode=DeploymentMode.DEVELOPMENT,
                auth_mode=AuthMode.DEV,
                auth_token=None,
                sandbox=SandboxMechanism.FAKE,
                config_dir=None,
                credentials={},
                interpolation={"OPENAI_MODEL": ""},
                memory_provider_extraction_mode=MemoryProviderExtractionMode.OFF,
                artifact_root=Path(temporary),
                people_enabled=False,
            )
            async with build(
                settings=settings,
                principal=OWNER,
                fixed_clock_at=AT,
                sequential_ids=True,
                enabled_tools=[],
                enabled_skills=[],
            ) as app:
                session_id = await app.sessions.create()
                await _seed(app, session_id, seeds, noise)
                worker = SemanticIndexingPass(
                    app.uow_factory,
                    app.clock,
                    OWNER,
                    provider,
                    embedding,
                    admitted=lambda: True,
                    local=configuration,
                )
                indexed = 0
                if channel != "sparse":
                    for _ in range(2 * ceil((len(seeds) + noise) / 16) + 2):
                        result = await worker.run_slice()
                        indexed += result.indexed
                        if result.reason == "not_due":
                            break
                        if result.reason not in {"indexed", "scanned"}:
                            raise ValueError(f"Diagnostic index did not finish: {result.reason}")
                    if indexed != len(seeds) + noise:
                        raise ValueError("Incomplete diagnostic index")
                report["indexed"] = indexed
                preparation = _LegacyQueries if encoding == "json@1" else SemanticQueryPreparation
                lookup = preparation(
                    app.uow_factory,
                    app.clock,
                    OWNER,
                    provider,
                    embedding,
                    admitted=lambda: True,
                    context=lambda: SemanticQueryContext(
                        policy_version="diagnostic@1",
                        retrieval_policy=f"{channel}@1",
                        surface_id="private",
                        task_sha256="a" * 64,
                        sensitivity_floor=Sensitivity.RESTRICTED,
                    ),
                    local=configuration,
                )
                retriever = HybridMemoryRetriever(
                    app.uow_factory,
                    app.clock,
                    app.ids,
                    OWNER,
                    profile=app.memory_profiles.retrieval.model_copy(
                        update={
                            "semantic_enabled": channel != "sparse",
                            "semantic_channel": "dense" if channel == "dense" else "hybrid",
                        }
                    ),
                    semantic_queries=lookup,
                )
                former = DeterministicQueryFormer(OWNER, min_score=0.1)
                tool = MemorySearchTool(retriever)
                keys = {identifier(seed): seed.id for seed in seeds}
                for seed in seeds:
                    query = (
                        former.from_text(seed.question)[0]
                        if path == "automatic"
                        else tool.query(
                            {"text": seed.question, "scope": "general"},
                            tenant_id=OWNER.tenant_id,
                            principal_id=OWNER.principal_id,
                        )
                    )
                    query = retriever._effective_query(query, "in_turn")
                    started = perf_counter()
                    if channel != "sparse":
                        prepared = await lookup.prepare([query])
                        if prepared.prepared != 1 and prepared.reason != "cached":
                            raise ValueError("Sparse fallback cannot supply semantic observations")
                    recalled = await retriever.recall(query, session_id=session_id)
                    elapsed_ms = (perf_counter() - started) * 1000
                    if recalled.stages is None:
                        raise ValueError("Missing actual retrieval stage instrumentation")
                    stages = recalled.stages
                    expected = identifier(seed)
                    row: dict[str, Any] = {
                        "scenario_id": seed.id,
                        "family": seed.family,
                        "tokens": recalled.tokens,
                        "elapsed_ms": elapsed_ms,
                        "returned": [
                            keys.get(item.belief_id, str(item.belief_id)) for item in recalled.items
                        ],
                    }
                    for name, values in (
                        ("candidate", stages.candidate_ids),
                        ("dense", stages.dense_ids),
                        ("ranked", stages.ranked_ids),
                        ("rendered", tuple(item.belief_id for item in recalled.items)),
                    ):
                        row[f"{name}_rank"] = (
                            values.index(expected) + 1 if expected in values else None
                        )
                        row[f"{name}_count"] = len(values)
                    rows.append(row)
                    if len(rows) % 50 == 0:
                        output.write_text(json.dumps(report, indent=2) + "\n")
                        print(
                            json.dumps({"completed": len(rows), "path": path, "channel": channel}),
                            flush=True,
                        )
        report.update(status="completed", summary=summarize(rows))
    finally:
        await provider.close()


async def run(args: argparse.Namespace) -> None:
    if not 0 <= args.noise <= 10000 or not 1 <= args.deadline_seconds <= 7200:
        raise ValueError("Diagnostic needs bounded load and deadline")
    configuration = LocalEmbeddingConfig.model_validate_json(args.configuration.read_bytes())
    report: dict[str, Any] = {
        "version": "general-local-recall-diagnostic@1",
        "activation_eligible": False,
        "status": "registered",
        "seed_sha256": hashlib.sha256(SEEDS.read_bytes()).hexdigest(),
        "implementation_sha256": implementation_digest(ROOT),
        "configuration": configuration.model_dump(mode="json"),
        "channel": args.channel,
        "path": args.path,
        "query_encoding": args.encoding,
        "noise": args.noise,
        "budgets": {
            "items": 20,
            "context_tokens": 2000,
            "provider_usd": 0,
            "deadline_seconds": args.deadline_seconds,
        },
        "setting": (
            "Synthetic pooled statements, in-memory, seeded retrieval; development data only"
        ),
        "limitations": (
            "No formation, answer use, holdout, production latency or activation certificate"
        ),
        "observations": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        handle.write(json.dumps(report, indent=2) + "\n")
    try:
        async with asyncio.timeout(args.deadline_seconds):
            await collect(
                configuration,
                args.model_directory,
                channel=args.channel,
                path=args.path,
                encoding=args.encoding,
                noise=args.noise,
                report=report,
                output=args.output,
            )
    except BaseException as exc:
        report.update(status="failed", failure_class=type(exc).__name__)
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", choices=("sparse", "dense", "hybrid"), required=True)
    parser.add_argument("--path", choices=("automatic", "deliberate"), required=True)
    parser.add_argument("--encoding", choices=("json@1", "natural@2"), default="natural@2")
    parser.add_argument("--noise", type=int, default=1000)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
