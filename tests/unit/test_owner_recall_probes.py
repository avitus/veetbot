"""Owner recall probes: production-shaped incident replays through the run loop.

The probes observe where an expected belief stops on its way to an answer:
Saved -> Stored -> Eligible -> Candidate -> Ranked -> Packed -> Delivered ->
Used. They never touch the frozen Milestone 16/33 controls and report
identifiers, not belief text.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecallMoment,
    RecallQuery,
    RecallStages,
    RecallTrace,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.evals.owner_recall_probes import evidence_stage, load_probes
from tests.contract.owner_recall_probe_cases import FakeWebProvider, delivery_case, save_case
from tests.gates.test_memory_m16 import _START
from tests.integration.m2_support import memory_settings

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "evals/capability/owner-recall-probes/probes.v1.jsonl"


def test_loader_refuses_the_frozen_evaluation_controls() -> None:
    for frozen in (
        ROOT / "evals/capability/general-recall/draft/development.jsonl",
        ROOT / "evals/capability/memory-benchmark.v1.json",
    ):
        with pytest.raises(ValueError, match="frozen"):
            load_probes(frozen)


def test_synthetic_probes_load_with_a_digest_and_unique_identifiers() -> None:
    loaded = load_probes(SYNTHETIC)
    assert loaded.digest == hashlib.sha256(SYNTHETIC.read_bytes()).hexdigest()
    assert len(loaded.probes) >= 8
    assert len({probe.id for probe in loaded.probes}) == len(loaded.probes)
    assert {"home-city-address", "owner-save-after-web", "board-seats-learned"} <= {
        probe.id for probe in loaded.probes
    }
    write = next(probe for probe in loaded.probes if probe.id == "owner-save-after-web")
    assert write.write is not None and write.write.web_results


def test_evidence_stage_names_where_a_belief_stopped() -> None:
    belief_id = UUID(int=7)
    stages = RecallStages(candidate_ids=(belief_id,), ranked_ids=(belief_id,))
    empty = RecallStages(candidate_ids=(), ranked_ids=())
    live = _record(belief_id, MemoryStatus.ACTIVE)
    trace = _trace(returned=[belief_id])
    nothing: set[UUID] = set()
    assert evidence_stage(None, stages, trace, delivered=nothing, answer=None) == "not_stored"
    retired = _record(belief_id, MemoryStatus.RETIRED)
    assert (
        evidence_stage(retired, stages, trace, delivered=nothing, answer=None)
        == "stored_but_retired"
    )
    sensitive = live.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
    assert (
        evidence_stage(
            sensitive,
            empty,
            _trace(returned=[]),
            delivered=nothing,
            answer=None,
            ceiling=Sensitivity.INTERNAL,
        )
        == "ineligible:sensitivity"
    )
    assert evidence_stage(live, empty, _trace(returned=[]), delivered=nothing, answer=None) == (
        "stored"
    )
    only_candidate = RecallStages(candidate_ids=(belief_id,), ranked_ids=())
    assert (
        evidence_stage(live, only_candidate, _trace(returned=[]), delivered=nothing, answer=None)
        == "candidate"
    )
    dropped = _trace(returned=[], dropped=[belief_id])
    assert evidence_stage(live, stages, dropped, delivered=nothing, answer=None) == "ranked"
    assert evidence_stage(live, stages, trace, delivered=nothing, answer=None) == "packed"
    assert evidence_stage(live, stages, trace, delivered={belief_id}, answer=None) == "delivered"
    cited = f"[m:{str(belief_id)[:8]}]"
    assert evidence_stage(live, stages, trace, delivered={belief_id}, answer=cited) == "used"


async def test_probes_report_delivery_retirement_and_latency_through_the_run_loop(
    tmp_path: Path,
) -> None:
    clock = FixedClock(_START)
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=clock,
    ) as app:
        await delivery_case(app, script)


async def test_write_probe_reports_the_save_outcome_after_web_results(tmp_path: Path) -> None:
    """The Saved stage: a verbatim owner span commits, a paraphrase is refused today."""

    clock = FixedClock(_START)
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    web = FakeWebProvider()
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=clock,
        web_search_provider_override=web,
    ) as app:
        await save_case(app, script, web)


def _record(belief_id: UUID, status: MemoryStatus) -> MemoryRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return MemoryRecord(
        id=belief_id,
        tenant_id="local",
        principal_id="local-user",
        scope="general",
        subject="car",
        statement="User has a 2026 Aster S8.",
        source_session_id=UUID(int=1),
        source_event_ids=[1],
        confidence=0.9,
        sensitivity=Sensitivity.INTERNAL,
        valid_from=now,
        status=status,
        belief_type=BeliefType.FACT,
        portability=Portability.CONTEXTUAL,
        origin_scopes=["general"],
        last_reinforced_at=now,
        last_evidence_at=now,
        formation_run_id=UUID(int=2),
        consolidation_policy_version="explicit@1",
        authority=MemoryAuthority.USER,
        store_position=1,
        created_at=now,
        updated_at=now,
        valid_to=now + timedelta(days=1) if status is MemoryStatus.RETIRED else None,
    )


def _trace(*, returned: list[UUID], dropped: list[UUID] | None = None) -> RecallTrace:
    return RecallTrace(
        id=UUID(int=99),
        tenant_id="local",
        principal_id="local-user",
        session_id=UUID(int=3),
        run_id=UUID(int=4),
        turn_id=UUID(int=4),
        moment=RecallMoment.IN_TURN,
        query=RecallQuery(
            tenant_id="local",
            principal_id="local-user",
            current_scope="general",
            text="car",
            budget_tokens=2000,
            max_items=20,
            min_score=0.12,
        ),
        surface_id="private",
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        rendered="",
        rendered_sha256="0" * 64,
        candidates=len(returned) + len(dropped or []),
        returned=returned,
        dropped_for_budget=dropped or [],
        retrieval_policy_version="retrieval@5",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        operator_fields_expire_at=datetime(2026, 1, 31, tzinfo=UTC),
    )
