"""Run M33 diagnostic seeds through real automatic and deliberate recall.

No answer model, formation model or live owner database is reachable here.
This runner measures only correctly seeded retrieval and cannot certify release.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.config import (
    AuthMode,
    DeploymentMode,
    MemoryProviderExtractionMode,
    SandboxMechanism,
    Settings,
)
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Portability,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.policies import ExecutionTarget
from agent_core.domain.tools import ToolExecutionContext
from agent_core.evals.general_memory_recall import Observation, Seed, load_seeds, summarize
from agent_core.memory.retrieval import DeterministicQueryFormer
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.tools.memory_search import MemorySearchTool

AT = datetime(2026, 10, 11, 12, tzinfo=UTC)
OWNER = Principal(
    tenant_id="evaluation",
    principal_id="general-recall",
    roles={"evaluator"},
    scopes=set(PLATFORM_SCOPES),
)


def identifier(seed: Seed) -> UUID:
    return uuid5(NAMESPACE_URL, f"veetbot:general-recall:{seed.id}")


async def no_effect() -> None:
    raise RuntimeError("diagnostic memory reads must not send effects")


def tool_context(session_id: UUID) -> ToolExecutionContext:
    return ToolExecutionContext(
        invocation_id=uuid5(session_id, "invocation"),
        call_id="diagnostic",
        run_id=uuid5(session_id, "run"),
        session_id=session_id,
        tenant_id=OWNER.tenant_id,
        principal=OWNER,
        step_number=1,
        attempt_number=1,
        lease_epoch=0,
        idempotency_key="diagnostic",
        deadline_at=AT + timedelta(seconds=10),
        timeout_seconds=10,
        maximum_output_bytes=65536,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        workspace=None,
        artifacts=None,
        credentials=None,
        bridge_dispatch=None,
        working_state={},
        cancellation=None,
        mark_effect_sent=no_effect,
    )


async def collect(seeds: tuple[Seed, ...], *, noise: int = 1000) -> tuple[Observation, ...]:
    if noise < 0 or noise > 10000:
        raise ValueError("diagnostic load must be between zero and 10000")
    bootstrap = importlib.import_module("agent_core.bootstrap")
    observations: list[Observation] = []
    keys = {identifier(s): s.id for s in seeds}
    mode: Literal["recent", "relevance"]
    path: Literal["automatic", "deliberate"]
    with tempfile.TemporaryDirectory(prefix="general-recall-") as temporary:
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
            artifact_root=Path(temporary) / "artifacts",
            people_enabled=False,
        )
        for mode in ("recent", "relevance"):
            async with bootstrap.build(
                settings=settings,
                storage="memory",
                principal=OWNER,
                fixed_clock_at=AT,
                sequential_ids=True,
                enabled_tools=[],
                enabled_skills=[],
                memory_legacy_retrieval_control=mode == "recent",
                script=FakeModelScript(turns=[ScriptedTurn(text="unused")]),
            ) as app:
                session_id = await app.sessions.create()
                await _seed(app, session_id, seeds, noise)
                former = DeterministicQueryFormer(
                    OWNER, current_scope="general", candidate_selection=mode, min_score=0.1
                )
                for seed in seeds:
                    for path in ("automatic", "deliberate"):
                        started = perf_counter()
                        if path == "automatic":
                            query = former.from_text(seed.question)[0]
                            result = await app.memory_retriever.recall(query, session_id=session_id)
                            trace_id, tokens = result.trace_id, result.tokens
                        else:
                            tool_result = await MemorySearchTool(app.memory_retriever).execute(
                                {"text": seed.question, "scope": "general"},
                                tool_context(session_id),
                            )
                            assert tool_result.structured is not None
                            trace_id = UUID(tool_result.structured["trace_id"])
                            tokens = 0
                        elapsed = (perf_counter() - started) * 1000
                        async with app.uow_factory() as uow:
                            trace = await uow.traces.get(trace_id, OWNER)
                            candidates = await uow.memories.query(trace.query)
                        if path == "deliberate":
                            # Same default token accounting used by the retriever.
                            from agent_core.memory.retrieval import _token_estimate

                            tokens = _token_estimate(trace.rendered) if trace.returned else 0
                        observations.append(
                            Observation(
                                seed_id=seed.id,
                                mode=mode,
                                path=path,
                                candidate_ids=tuple(keys.get(r.id, str(r.id)) for r in candidates),
                                returned_ids=tuple(keys.get(i, str(i)) for i in trace.returned),
                                dropped_ids=tuple(
                                    keys.get(i, str(i)) for i in trace.dropped_for_budget
                                ),
                                blocked_ids=tuple(keys.get(i, str(i)) for i in trace.blocked),
                                tokens=tokens,
                                elapsed_ms=elapsed,
                            )
                        )
    return tuple(observations)


async def _seed(app: Any, session_id: UUID, seeds: tuple[Seed, ...], noise: int) -> None:
    async with app.uow_factory() as uow:
        for index, seed in enumerate(seeds):
            event = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=OWNER.principal_id,
                    payload={"content": seed.statement},
                )
            )
            kind = {
                "preferences": BeliefType.PREFERENCE,
                "relationships": BeliefType.RELATIONSHIP,
                "attributes": BeliefType.USER_MODEL_ATTR,
            }.get(seed.family, BeliefType.FACT)
            row = MemoryRecord(
                id=identifier(seed),
                tenant_id=OWNER.tenant_id,
                principal_id=OWNER.principal_id,
                scope="general",
                subject=seed.subject,
                statement=seed.statement,
                source_session_id=session_id,
                source_event_ids=[event.sequence],
                confidence=0.9,
                sensitivity=Sensitivity.INTERNAL,
                valid_from=AT - timedelta(days=14),
                status=MemoryStatus.ACTIVE,
                belief_type=kind,
                portability=Portability.CONTEXTUAL,
                origin_scopes=["general"],
                last_reinforced_at=AT - timedelta(days=14),
                formation_run_id=uuid5(identifier(seed), "formation"),
                consolidation_policy_version="diagnostic-seeded@1",
                authority=MemoryAuthority.USER,
                store_position=index + 1,
                created_at=AT - timedelta(days=14),
                updated_at=AT - timedelta(days=14),
            )
            await uow.memories.upsert_belief(row)
        for index in range(noise):
            await uow.memories.upsert_belief(
                row.model_copy(
                    update={
                        "id": uuid5(NAMESPACE_URL, f"general-noise:{index}"),
                        "subject": f"Project ticket {index}",
                        "statement": f"User requested project ticket {index} for the next review.",
                        "store_position": len(seeds) + index + 1,
                        "belief_type": BeliefType.FACT,
                    }
                )
            )


def implementation_hashes() -> dict[str, str]:
    """Bind diagnostics to the live selector, formation, scoring and driver source."""
    root = Path(__file__).resolve().parents[3]
    names = (
        "domain/memory.py",
        "domain/memory_candidates.py",
        "adapters/memory/in_memory.py",
        "memory/retrieval.py",
        "memory/profiles.py",
        "memory/profiles.yaml",
        "tools/memory_search.py",
        "evals/general_memory_recall.py",
        "evals/general_memory_recall_driver.py",
        "bootstrap.py",
    )
    return {
        name: hashlib.sha256((root / "src/agent_core" / name).read_bytes()).hexdigest()
        for name in names
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seeds", type=Path, default=Path("evals/capability/general-memory-recall-seeds.v1.tsv")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--noise", type=int, default=1000)
    args = parser.parse_args()
    seeds = load_seeds(args.seeds)
    rows = asyncio.run(collect(seeds, noise=args.noise))
    report = summarize(seeds, rows)
    report.update(
        {
            "seed_sha256": hashlib.sha256(args.seeds.read_bytes()).hexdigest(),
            "implementation_sha256": implementation_hashes(),
            "noise": args.noise,
            "setting": (
                "synthetic pooled statements, shared owner, in-memory; "
                "not a production-shaped or end-to-end benchmark"
            ),
            "budgets": {"tokens": 2000, "items": 20, "min_score": 0.1},
            "observations": [r.model_dump(mode="json") for r in rows],
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "observations"}, indent=2))


if __name__ == "__main__":
    main()
