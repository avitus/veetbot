"""Fabricated metric fixtures test admission, never supply release evidence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.general_recall_release import (
    RecallReleaseBundle,
    RecallReleaseIdentity,
    RecallScenario,
)
from agent_core.memory.general_recall_evidence import bind_semantic_evidence, score_release
from agent_core.memory.reconsolidation_evidence import implementation_digest

ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 10, 11, tzinfo=UTC)
FAMILIES = (
    "attributes",
    "preferences",
    "relationships",
    "goals",
    "decisions",
    "routines",
    "temporal",
    "complex",
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def scorer_hash() -> str:
    return hashlib.sha256(
        b"".join(
            (ROOT / name).read_bytes()
            for name in (
                "src/agent_core/domain/general_recall_release.py",
                "src/agent_core/memory/general_recall_evidence.py",
            )
        )
    ).hexdigest()


@lru_cache(maxsize=1)
def fixture() -> RecallReleaseBundle:
    """All identities are fabricated. This is NOT an independently authored corpus."""
    prereg = {
        "registered_at": AT.isoformat(),
        "baseline_report_sha256": digest("baseline"),
        "context_tokens": 2000,
        "p95_latency_ms": 1000.0,
        "maximum_case_usd": 0.01,
    }
    from agent_core.domain.general_recall_release import RecallPreregistration

    registration_hash = digest(RecallPreregistration.model_validate(prereg).model_dump_json())
    identity = {
        "build_ref": "a" * 40,
        "implementation_sha256": implementation_digest(ROOT),
        "corpus_sha256": digest("fabricated unit corpus"),
        "scorer_sha256": scorer_hash(),
        "configuration_sha256": digest("effective policy, profiles and authority"),
        "embedding_configuration_sha256": digest("embedding tuple, price, egress, budgets"),
        "formation_configuration_sha256": digest("formation tuple"),
        "reader_configuration_sha256": digest("answer tuple"),
        "index_manifest_sha256": digest("index snapshot"),
        "independence_review_sha256": digest("fabricated review fixture"),
        "preregistration_sha256": registration_hash,
        "frozen_controls_sha256": digest("M16/M32 artifacts"),
    }
    scenarios, rows = [], []
    for family in FAMILIES:
        for i in range(50):
            identifier = f"{family}-{i}"
            split = "development" if i < 25 else "holdout"
            scenarios.append(
                {
                    "id": identifier,
                    "family": family,
                    "split": split,
                    "scenario_lineage": identifier,
                    "query_lineage": identifier,
                    "history_sha256": digest(identifier),
                    "required_evidence": [identifier],
                    "required_answer_atoms": [identifier],
                    "expected_action": "answer",
                    "known_incident": i == 0,
                    "implicit_task": True,
                }
            )
            for path in ("automatic", "deliberate"):
                for pipeline in ("seeded", "formation"):
                    for channel in ("sparse", "dense", "hybrid"):
                        recalled = channel != "sparse" or i % 5 != 0
                        correct = channel != "sparse" or i % 5 > 1
                        rows.append(
                            {
                                "scenario_id": identifier,
                                "path": path,
                                "pipeline": pipeline,
                                "channel": channel,
                                "observed_at": (AT + timedelta(days=1)).isoformat(),
                                "formed": [identifier],
                                "candidates": [identifier],
                                "ranked": [identifier] if recalled else [],
                                "rendered": [identifier] if recalled else [],
                                "answer_atoms": [identifier] if correct else [],
                                "action": "answer",
                                "unsupported_atoms": [],
                                "invalid_current_ids": [],
                                "boundary_failures": [],
                                "searches": 1,
                                "rounds": 1,
                                "context_tokens": 100,
                                "input_tokens": 150,
                                "output_tokens": 20,
                                "elapsed_ms": 100.0,
                                "cost_usd": 0.001,
                            }
                        )
    identity["scenario_manifest_sha256"] = hashlib.sha256(
        b"".join(
            RecallScenario.model_validate(s).model_dump_json().encode() + b"\n" for s in scenarios
        )
    ).hexdigest()
    identity_hash = digest(RecallReleaseIdentity.model_validate(identity).model_dump_json())
    for row in rows:
        row["identity_sha256"] = identity_hash
    return RecallReleaseBundle.model_validate(
        {
            "version": "general-recall-release@1",
            "identity": identity,
            "preregistration": prereg,
            "scenarios": scenarios,
            "observations": rows,
            "gates": [
                {
                    "identity_sha256": identity_hash,
                    "gate": n,
                    "report_sha256": digest(str(n)),
                    "passed": 1,
                    "failed": 0,
                }
                for n in range(1, 12)
            ],
        }
    )


def altered(
    *, row: dict[str, Any] | None = None, scenario: dict[str, Any] | None = None
) -> RecallReleaseBundle:
    source = fixture()
    return source.model_copy(
        update={
            "observations": (
                source.observations[2].model_copy(update=row or {}),
                *source.observations[3:],
                *source.observations[:2],
            ),
            "scenarios": (
                source.scenarios[0].model_copy(update=scenario or {}),
                *source.scenarios[1:],
            ),
        }
    )


def test_complete_comparison_recomputes_quality_and_paired_uncertainty() -> None:
    report = score_release(fixture())
    assert report["activation_eligible"] is True
    cells = report["cells"]
    assert isinstance(cells, list)
    holdout = next(
        c
        for c in cells
        if c["family"] == "all"
        and c["split"] == "holdout"
        and c["path"] == "automatic"
        and c["pipeline"] == "formation"
        and c["channel"] == "hybrid"
    )
    assert holdout["ranked_recall_at_20"] == 1
    assert holdout["answer_lift"] == pytest.approx(0.4)
    assert holdout["answer_lift_ci95"][0] < 0.4 < holdout["answer_lift_ci95"][1]
    assert holdout["formed_recall"] == holdout["rendered_recall"] == 1
    assert holdout["p95_ms"] == 100
    assert holdout["cost_usd"] == pytest.approx(0.2)
    sparse = next(
        c
        for c in cells
        if c["family"] == "all"
        and c["split"] == "holdout"
        and c["path"] == "automatic"
        and c["pipeline"] == "formation"
        and c["channel"] == "sparse"
    )
    assert sparse["retrieved_but_unused"] == 40


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "mixed_tuple", "diagnostic"])
def test_partial_or_mixed_experiments_cannot_pass(change: str) -> None:
    source = fixture()
    if change == "missing":
        bad = source.model_copy(update={"observations": source.observations[:-1]})
    elif change == "duplicate":
        bad = source.model_copy(
            update={"observations": (*source.observations[:-1], source.observations[0])}
        )
    elif change == "unknown":
        bad = altered(row={"scenario_id": "unknown"})
    elif change == "mixed_tuple":
        bad = source.model_copy(
            update={
                "identity": source.identity.model_copy(update={"preregistration_sha256": "f" * 64})
            }
        )
    else:
        bad = source.model_copy(update={"version": "general-recall-diagnostic@1"})
    with pytest.raises(ValueError):
        score_release(bad)


@pytest.mark.parametrize("field", ["scenario_lineage", "query_lineage"])
def test_shared_development_holdout_lineage_is_not_independent(field: str) -> None:
    with pytest.raises(ValueError, match="lineage"):
        score_release(altered(scenario={field: fixture().scenarios[25].id}))


@pytest.mark.parametrize(
    "change", ["duplicate_id", "duplicate_history", "missing_family", "missing_split"]
)
def test_corpus_census_cannot_be_inflated(change: str) -> None:
    source = fixture()
    if change == "duplicate_id":
        scenarios = (source.scenarios[1], *source.scenarios[1:])
    elif change == "duplicate_history":
        scenarios = (
            source.scenarios[0].model_copy(
                update={"history_sha256": source.scenarios[25].history_sha256}
            ),
            *source.scenarios[1:],
        )
    else:
        field, value = (
            ("family", "preferences") if change == "missing_family" else ("split", "development")
        )
        scenarios = tuple(
            s.model_copy(update={field: value}) if s.family == "attributes" else s
            for s in source.scenarios
        )
    with pytest.raises(ValueError):
        score_release(source.model_copy(update={"scenarios": scenarios}))


@pytest.mark.parametrize(
    "change",
    [
        {"boundary_failures": ("cross-owner",)},
        {"invalid_current_ids": ("erased",)},
        {"unsupported_atoms": ("invented-default",)},
        {"rendered": ()},
        {"answer_atoms": ()},
        {"action": "abstain"},
        {"context_tokens": 2001},
        {"cost_usd": 0.011},
        {"observed_at": AT - timedelta(seconds=1)},
        {"searches": 5},
        {"rounds": 3},
    ],
)
def test_one_incident_or_boundary_failure_blocks_admission(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        score_release(altered(row=change))


@pytest.mark.parametrize("stage", ["formed", "candidates", "ranked", "rendered", "answer_atoms"])
def test_stages_cannot_be_substituted_for_final_quality(stage: str) -> None:
    source = fixture()
    rows = tuple(
        r.model_copy(update={stage: ()}) if r.channel == "hybrid" else r
        for r in source.observations
    )
    with pytest.raises(ValueError):
        score_release(source.model_copy(update={"observations": rows}))


@pytest.mark.parametrize(
    "case",
    ["no_lift", "family_regression", "latency", "recall_floor", "gate_failure", "gate_duplicate"],
)
def test_aggregate_success_cannot_hide_a_failing_family_or_gate(case: str) -> None:
    source = fixture()
    rows, gates = source.observations, source.gates
    if case == "no_lift":
        rows = tuple(
            r.model_copy(
                update={
                    "ranked": (r.scenario_id,),
                    "rendered": (r.scenario_id,),
                    "answer_atoms": (r.scenario_id,),
                }
            )
            if r.channel == "sparse"
            else r
            for r in rows
        )
    elif case == "family_regression":
        rows = tuple(
            r.model_copy(update={"answer_atoms": ()})
            if r.scenario_id.startswith("preferences-")
            and int(r.scenario_id.rsplit("-", 1)[1]) >= 25
            and r.channel == "hybrid"
            else r
            for r in rows
        )
    elif case == "latency":
        rows = tuple(
            r.model_copy(update={"elapsed_ms": 1001.0}) if r.channel == "hybrid" else r
            for r in rows
        )
    elif case == "recall_floor":
        rows = tuple(
            r.model_copy(update={"ranked": (), "rendered": ()})
            if r.channel == "hybrid" and int(r.scenario_id.rsplit("-", 1)[1]) >= 45
            else r
            for r in rows
        )
    elif case == "gate_failure":
        gates = (gates[0].model_copy(update={"failed": 1}), *gates[1:])
    else:
        gates = (gates[1], *gates[1:])
    with pytest.raises(ValueError):
        score_release(source.model_copy(update={"observations": rows, "gates": gates}))


def write_bundle(path: Path) -> None:
    path.write_text(fixture().model_dump_json())


def test_exact_binding_revokes_permanently_on_replacement(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    write_bundle(path)
    raw = path.read_bytes()
    admitted = bind_semantic_evidence(ROOT, path, fixture().identity)
    assert admitted()
    path.write_bytes(raw + b" ")
    assert not admitted()
    path.write_bytes(raw)
    assert not admitted(), "revocation must not revive old in-flight handles"


@pytest.mark.parametrize("field", list(RecallReleaseIdentity.model_fields))
def test_every_active_identity_dependency_is_bound(tmp_path: Path, field: str) -> None:
    path = tmp_path / "release.json"
    write_bundle(path)
    expected = fixture().identity.model_copy(
        update={field: "f" * (40 if field == "build_ref" else 64)}
    )
    with pytest.raises(ValueError, match="tuple"):
        bind_semantic_evidence(ROOT, path, expected)


@pytest.mark.parametrize("fault", ["missing", "directory", "oversize", "symlink", "fifo"])
def test_unreadable_or_unbounded_files_are_not_admitted(tmp_path: Path, fault: str) -> None:
    import os

    path = tmp_path / "release.json"
    if fault == "directory":
        path.mkdir()
    elif fault == "oversize":
        with path.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
    elif fault == "symlink":
        target = tmp_path / "target.json"
        write_bundle(target)
        path.symlink_to(target)
    elif fault == "fifo":
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        bind_semantic_evidence(ROOT, path, fixture().identity)


def test_diagnostic_output_cannot_be_relabelled_as_release_evidence(tmp_path: Path) -> None:
    path = tmp_path / "diagnostic.json"
    path.write_text(
        json.dumps(
            {"version": "general-recall-release@1", "activation_eligible": True, "cases": 400}
        )
    )
    with pytest.raises(ValueError):
        bind_semantic_evidence(ROOT, path, fixture().identity)


@pytest.mark.parametrize("target", ["row", "gate", "manifest"])
def test_mixed_observation_and_gate_versions_are_rejected(target: str) -> None:
    source = fixture()
    if target == "row":
        bad = altered(row={"identity_sha256": "f" * 64})
    elif target == "gate":
        bad = source.model_copy(
            update={
                "gates": (
                    source.gates[0].model_copy(update={"identity_sha256": "f" * 64}),
                    *source.gates[1:],
                )
            }
        )
    else:
        bad = altered(scenario={"history_sha256": "f" * 64})
    with pytest.raises(ValueError, match="tuple"):
        score_release(bad)


def retuple(bundle: RecallReleaseBundle) -> RecallReleaseBundle:
    """Re-sign fabricated test metadata after intentionally changing its manifest."""
    manifest_hash = hashlib.sha256(
        b"".join(s.model_dump_json().encode() + b"\n" for s in bundle.scenarios)
    ).hexdigest()
    identity = bundle.identity.model_copy(update={"scenario_manifest_sha256": manifest_hash})
    identity_hash = digest(identity.model_dump_json())
    return bundle.model_copy(
        update={
            "identity": identity,
            "observations": tuple(
                r.model_copy(update={"identity_sha256": identity_hash}) for r in bundle.observations
            ),
            "gates": tuple(
                g.model_copy(update={"identity_sha256": identity_hash}) for g in bundle.gates
            ),
        }
    )


def test_explicit_questions_cannot_supply_the_required_implicit_answer_lift() -> None:
    source = fixture()
    scenarios = tuple(
        s.model_copy(update={"implicit_task": int(s.id.rsplit("-", 1)[1]) % 25 >= 10})
        for s in source.scenarios
    )
    rows = tuple(
        r.model_copy(
            update={
                "ranked": (r.scenario_id,),
                "rendered": (r.scenario_id,),
                "answer_atoms": (r.scenario_id,)
                if int(r.scenario_id.rsplit("-", 1)[1]) % 25 >= 10
                else (),
            }
        )
        if r.channel == "sparse"
        else r
        for r in source.observations
    )
    with pytest.raises(ValueError, match="implicit"):
        score_release(
            retuple(source.model_copy(update={"scenarios": scenarios, "observations": rows}))
        )


@pytest.mark.parametrize("action", ["clarify", "abstain"])
def test_ambiguity_and_unknowns_require_the_correct_action_without_invented_atoms(
    action: str,
) -> None:
    source = fixture()
    unknown = source.scenarios[1].id
    scenarios = tuple(
        s.model_copy(
            update={"required_evidence": (), "required_answer_atoms": (), "expected_action": action}
        )
        if s.id == unknown
        else s
        for s in source.scenarios
    )
    rows = tuple(
        r.model_copy(
            update={
                "formed": (),
                "candidates": (),
                "ranked": (),
                "rendered": (),
                "answer_atoms": (),
                "action": action,
            }
        )
        if r.scenario_id == unknown
        else r
        for r in source.observations
    )
    bundle = retuple(source.model_copy(update={"scenarios": scenarios, "observations": rows}))
    assert score_release(bundle)["activation_eligible"] is True
    # One non-incident false answer is counted as an answer failure, not erased
    # by a vacuous evidence match. It need not fail the aggregate release floor.
    bad = bundle.model_copy(
        update={
            "observations": tuple(
                r.model_copy(
                    update={
                        "action": "answer",
                        "answer_atoms": ("invented",),
                        "unsupported_atoms": ("invented",),
                    }
                )
                if r.scenario_id == unknown and r.channel == "hybrid"
                else r
                for r in bundle.observations
            )
        }
    )
    report = score_release(bad)
    assert any(
        c["family"] == "attributes" and c["channel"] == "hybrid" and c["answer_success"] < 1
        for c in report["cells"]
    )
    assert any(c["unsupported_answers"] > 0 for c in report["cells"])


@pytest.mark.parametrize("path_kind", ["automatic", "deliberate"])
async def test_file_revocation_withdraws_real_runtime_recall_and_indexing(
    tmp_path: Path, path_kind: str
) -> None:
    from uuid import UUID

    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from agent_core.memory.semantic_indexing import SemanticIndexingPass
    from agent_core.memory.semantic_runtime import RunSemanticQueries
    from agent_core.tools.memory_search import MemorySearchTool
    from tests.contract.memory_fixtures import memory
    from tests.contract.semantic_indexing_cases import INDEX_CONFIG
    from tests.contract.semantic_memory_cases import SPACE, VECTOR
    from tests.contract.support import RUN_ID, SESSION_ID, principal, tool_context
    from tests.contract.test_runtime_semantic_queries_contract import (
        AUTHORITY,
        query,
        runtime_stack,
    )

    path = tmp_path / "release.json"
    write_bundle(path)
    original_bytes = path.read_bytes()
    admitted = bind_semantic_evidence(ROOT, path, fixture().identity)
    clock, factory, provider, _, _, _ = await runtime_stack()
    runtime = RunSemanticQueries(
        factory,
        clock,
        principal(),
        provider,
        INDEX_CONFIG,
        admitted=admitted,
        authority=lambda *_: AUTHORITY,
    )
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(10000, 11000)),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
        runtime_semantic_queries=runtime,
    )
    original = memory(statement="User prefers a plant-based diet").model_copy(
        update={"subject": "diet"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
    assert (
        await retriever.prepare_semantic_queries(
            [query(path_kind)], session_id=SESSION_ID, run_id=RUN_ID
        )
        == 1
    )

    async def recall() -> str:
        if path_kind == "automatic":
            return (await retriever.recall(query(), session_id=SESSION_ID, run_id=RUN_ID)).rendered
        result = await MemorySearchTool(retriever).execute(
            {"text": query().text, "scope": "project-a"}, tool_context()
        )
        return str(result.content)

    assert original.statement in await recall()
    path.unlink()
    assert original.statement not in await recall()
    path.write_bytes(original_bytes)
    assert (
        await retriever.prepare_semantic_queries(
            [query(path_kind)], session_id=SESSION_ID, run_id=RUN_ID
        )
        == 0
    )
    indexer = SemanticIndexingPass(
        factory, clock, principal(), provider, INDEX_CONFIG, admitted=admitted
    )
    assert (await indexer.run_slice()).reason == "disabled"
    assert len(provider.calls) == 1, "withdrawal prevents both query and indexing egress"


@pytest.mark.parametrize("field", ["implementation_sha256", "scorer_sha256"])
def test_matching_claims_still_have_to_match_local_source_bytes(tmp_path: Path, field: str) -> None:
    source = fixture()
    stale = retuple(
        source.model_copy(update={"identity": source.identity.model_copy(update={field: "f" * 64})})
    )
    path = tmp_path / "stale.json"
    path.write_text(stale.model_dump_json())
    with pytest.raises(ValueError, match="active dependency tuple"):
        bind_semantic_evidence(ROOT, path, stale.identity)


def test_implicit_lift_cannot_hide_a_regression_in_the_whole_family() -> None:
    source = fixture()
    scenarios = tuple(
        s.model_copy(update={"implicit_task": int(s.id.rsplit("-", 1)[1]) % 25 < 5})
        for s in source.scenarios
    )
    rows = tuple(
        r.model_copy(update={"answer_atoms": ()})
        if r.channel == "hybrid"
        and r.scenario_id.startswith("preferences-")
        and int(r.scenario_id.rsplit("-", 1)[1]) >= 30
        else r
        for r in source.observations
    )
    with pytest.raises(ValueError, match="family regression"):
        score_release(
            retuple(source.model_copy(update={"scenarios": scenarios, "observations": rows}))
        )


def test_duplicate_json_fields_cannot_hide_a_failed_or_different_claim(tmp_path: Path) -> None:
    path = tmp_path / "ambiguous.json"
    payload = (
        fixture()
        .model_dump_json()
        .replace(
            '"version":"general-recall-release@1"',
            '"version":"general-recall-diagnostic@1","version":"general-recall-release@1"',
            1,
        )
    )
    path.write_text(payload)
    with pytest.raises(ValueError, match="duplicate"):
        bind_semantic_evidence(ROOT, path, fixture().identity)


def test_incorrect_action_is_not_misreported_as_unused_retrieved_evidence() -> None:
    source = fixture()
    changed = source.model_copy(
        update={
            "observations": tuple(
                r.model_copy(update={"action": "abstain"})
                if r.channel == "hybrid" and r.scenario_id == "attributes-1"
                else r
                for r in source.observations
            )
        }
    )
    cells = score_release(changed)["cells"]
    affected = [
        c
        for c in cells
        if c["channel"] == "hybrid" and c["family"] == "attributes" and c["split"] == "development"
    ]
    assert all(
        c["answer_success"] < 1 and c["retrieved_but_unused"] == 0 and c["incorrect_actions"] == 1
        for c in affected
    )
