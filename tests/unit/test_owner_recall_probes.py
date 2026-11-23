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
from agent_core.domain.recall_questions import (
    QuestionSplit,
    RecallQuestion,
    RecallQuestionContent,
)
from agent_core.evals import owner_recall_probe_driver as driver
from agent_core.evals.owner_recall_probe_driver import (
    _holders,
    _resolve,
    _StoreSnapshot,
    observe_probes,
    scripted_chat,
)
from agent_core.evals.owner_recall_probes import (
    ExpectedEvidence,
    Probe,
    ProbeReport,
    evidence_stage,
    load_probes,
)
from agent_core.evals.owner_recall_questions import loaded_from_questions
from tests.contract.owner_recall_probe_cases import (
    FakeWebProvider,
    chat_model_case,
    delivery_case,
    home_city_case,
    post_snapshot_home_case,
    save_case,
    shared_text_case,
    stated,
)
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


async def test_a_scripted_probe_runs_as_the_chat_model_a_real_chat_uses(tmp_path: Path) -> None:
    """A scripted run labelled as a fake model measures recall no real chat gets (ADR-0343)."""

    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with scripted_chat(
        script,
        model_policy="astra",
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        clock=FixedClock(_START),
    ) as (app, stand_in):
        await chat_model_case(app, script, stand_in, model_policy="astra")


async def test_home_city_probe_delivers_the_address_beside_same_subject_noise(
    tmp_path: Path,
) -> None:
    """ADR-0248: the affirmed address is delivered on both paths and carries no
    false link to the five email facts about the same subject."""

    clock = FixedClock(_START)
    script = FakeModelScript(
        turns=[ScriptedTurn(text="The garden is in Los Altos Hills; here is the calendar.")],
        on_exhausted="repeat_last",
    )
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=clock,
    ) as app:
        await home_city_case(app, script)


async def test_a_residence_stated_after_the_snapshot_reaches_a_garden_task(
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
        await post_snapshot_home_case(app)


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
        retrieval_policy_version="retrieval@8",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        operator_fields_expire_at=datetime(2026, 1, 31, tzinfo=UTC),
    )


async def test_family_probe_checks_the_names_the_overview_delivers(tmp_path: Path) -> None:
    """ADR-0259: an overview answer is People lines; the probe checks names, never ids."""

    from tests.contract.owner_recall_probe_cases import family_names_case

    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(
            memory_settings(), artifact_root=tmp_path / "artifacts", people_enabled=True
        ),
        storage="memory",
        script=script,
        clock=FixedClock(_START),
    ) as app:
        await family_names_case(app, script)


async def test_a_fact_is_delivered_when_any_live_memory_holding_its_text_is(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=FixedClock(_START),
    ) as app:
        report, _car, _mention = await shared_text_case(app, script)

    # The rule a report was scored under is named, and an earlier report still reads.
    assert report.version == "owner-recall-probes@2"
    earlier = report.model_dump(mode="json") | {"version": "owner-recall-probes@1"}
    assert ProbeReport.model_validate(earlier).version == "owner-recall-probes@1"


async def test_live_memories_stand_for_a_text_and_a_retired_one_only_when_none_is_live(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=FixedClock(_START),
    ) as app:
        session_id = await app.sessions.create()
        first = await stated(app, session_id, "User keeps bees at the allotment", "bees")
        second = await stated(app, session_id, "The allotment society keeps bees too", "society")
        gone = await stated(app, session_id, "User plays the cello on Sundays", "cello")
        older_gone = await stated(app, session_id, "User plays the cello at school", "school")
        async with app.uow_factory() as uow:
            for retired in (older_gone, gone):
                record = await uow.memories.get(retired.id, app.principal)
                await uow.memories.reinforce(
                    record.model_copy(
                        update={"status": MemoryStatus.RETIRED, "valid_to": app.clock.now()},
                        deep=True,
                    )
                )

        live = await _holders(app, None, "keeps bees")
        retired_only = await _holders(app, None, "plays the cello")
        pinned = await _holders(app, first.id, None)
        nothing = await _holders(app, None, "owns a submarine")

    assert [record.id for record in live] == [second.id, first.id]
    assert len(retired_only) == 1 and retired_only[0].status is MemoryStatus.RETIRED
    assert retired_only[0].store_position == max(gone.store_position, older_gone.store_position)
    assert [record.id for record in pinned] == [first.id]
    assert nothing == []


async def test_an_expected_text_is_found_however_many_beliefs_the_store_holds(
    tmp_path: Path,
) -> None:
    """A text locator must search the whole store, not its first page.

    The owner's store holds thousands of beliefs. A locator that read one page
    reported an expected fact as never stored whenever it lay outside that
    page, which blames formation for what recall did or did not do.
    """

    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=FixedClock(_START),
    ) as app:
        session_id = await app.sessions.create()
        oldest = await stated(app, session_id, "User keeps bees at the allotment", "bees")
        for number in range(205):
            await stated(
                app, session_id, f"User filed receipt number {number}", f"receipt {number}"
            )
        newest = await stated(app, session_id, "User plays the cello on Sundays", "cello")

        found_oldest = await _resolve(app, None, "keeps bees")
        found_newest = await _resolve(app, None, "plays the cello")
        missing = await _resolve(app, None, "owns a submarine")

    assert found_oldest is not None and found_oldest.id == oldest.id
    assert found_newest is not None and found_newest.id == newest.id
    assert missing is None


async def test_one_observation_reads_the_store_once_for_all_its_expected_texts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=FixedClock(_START),
    ) as app:
        session_id = await app.sessions.create()
        bees = await stated(app, session_id, "User keeps bees at the allotment", "bees")
        cello = await stated(app, session_id, "User plays the cello on Sundays", "cello")
        reads = 0
        whole_store = driver._every_belief

        async def counted(target: object) -> list[MemoryRecord]:
            nonlocal reads
            reads += 1
            return await whole_store(target)

        monkeypatch.setattr(driver, "_every_belief", counted)
        store = _StoreSnapshot(app)
        found_bees = await _resolve(app, None, "keeps bees", store)
        found_cello = await _resolve(app, None, "plays the cello", store)
        by_id = await _resolve(app, bees.id, None, store)

    assert found_bees is not None and found_bees.id == bees.id
    assert found_cello is not None and found_cello.id == cello.id
    assert by_id is not None and by_id.id == bees.id
    assert reads == 1


async def test_a_stored_question_run_carries_the_fingerprint_of_what_it_tested(
    tmp_path: Path,
) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=script,
        clock=FixedClock(_START),
    ) as app:
        session_id = await app.sessions.create()
        await stated(app, session_id, "User lives in Example Town", "home")
        question = RecallQuestion(
            id=UUID(int=1),
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            key="home-city",
            split=QuestionSplit.HOLDOUT,
            content=RecallQuestionContent.model_validate(
                {
                    "question": "Where do I live?",
                    "family": "attributes",
                    "expected": ({"statement_contains": "Example Town"},),
                }
            ),
            created_at=_START,
            updated_at=_START,
        )
        incident = Probe(
            id="bees",
            incident="bees",
            family="attributes",
            question="Do I keep bees?",
            paths=("automatic",),
            expected=(ExpectedEvidence(statement_contains="keeps bees"),),
        )
        stored = loaded_from_questions([question], label="holdout").probes
        report = await observe_probes(app, script, (*stored, incident), paths=("automatic",))

    by_id = {item.probe_id: item for item in report.probes}
    assert by_id["home-city"].content_sha256 == question.content.criteria_sha256
    # An incident probe is not a stored question and is bound to none.
    assert by_id["bees"].content_sha256 is None
