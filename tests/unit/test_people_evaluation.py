"""Scoring cannot reward abstention, identity collisions, or ungrounded claims."""

from pathlib import Path

import pytest

from agent_core.evals.people import (
    Observation,
    ObservedFact,
    ObservedMention,
    ObservedOrganization,
    ObservedTask,
    load_corpora,
    paired_interval,
    score,
)


def test_authored_corpora_cover_all_product_questions_with_exact_fact_evidence() -> None:
    development, holdout, _ = load_corpora(Path.cwd())
    expected_categories = {
        "connections",
        "background",
        "last_interaction",
        "commitments",
        "changes",
        "evidence",
    }
    for corpus in (development, holdout):
        for case in corpus.cases:
            raw = case.model_dump()
            assert {task.get("category") for task in raw["tasks"]} == expected_categories
            assert all(fact.get("evidence_spans") for fact in raw["facts"])
            assert case.organizations
            assert any(fact.predicate == "employment" for fact in case.facts)
            assert any(
                fact.predicate == "commitment" and fact.subject == "owner" for fact in case.facts
            )


def test_reviewed_labels_cannot_omit_or_fabricate_exact_fact_spans() -> None:
    from pydantic import ValidationError

    from agent_core.evals.people import Corpus

    development, _, _ = load_corpora(Path.cwd())
    raw = development.model_dump(mode="json")
    raw["review_status"] = "reviewed"
    raw["cases"][0]["facts"][0]["evidence_spans"] = []
    with pytest.raises(ValidationError, match="exact fact evidence"):
        Corpus.model_validate(raw)
    raw["cases"][0]["facts"][0]["evidence_spans"] = [
        {"event": 2, "start": 0, "end": 4, "text": "fake"}
    ]
    with pytest.raises(ValidationError, match="exact source span"):
        Corpus.model_validate(raw)


def test_people_scorer_penalizes_abstention_collision_and_fabricated_evidence() -> None:
    development, _, _ = load_corpora(Path.cwd())
    case = development.cases[0]
    observed = Observation(
        case_id=case.id,
        pipeline="full-people",
        repeat=0,
        mentions=[
            ObservedMention(
                event=m.event,
                start=m.start,
                end=m.end,
                person_id=m.entity if m.resolvable else None,
            )
            for m in case.mentions
        ],
        facts=[
            ObservedFact(
                subject_id=f.subject,
                authority=f.authority,
                derivation=f.derivation,
                predicate=f.predicate,
                object_id=f.object,
                statement=" ".join(f.terms),
                evidence_events=f.evidence_events,
                valid_from=f.valid_from,
                valid_to=f.valid_to,
                precision=f.precision,
                commitment_state=f.commitment_state,
                due_at=f.due_at,
                due_precision=f.due_precision,
                source_timezone=f.source_timezone,
            )
            for f in case.facts
        ],
        organizations=[
            ObservedOrganization(
                organization_id=label.entity, display_name=label.text, evidence_events=[label.event]
            )
            for label in case.organizations
        ],
        tasks=[
            ObservedTask(
                index=i,
                answer=" ".join(t.terms),
                evidence_events=t.evidence_events,
                retrieved_evidence_events=t.evidence_events,
            )
            for i, t in enumerate(case.tasks)
        ],
        provider_calls=3,
        cost_usd="0",
    )
    gold = score(case, observed)
    assert gold["identity_precision"] == gold["resolvable_identity_recall"] == 1
    assert gold["direct_fact_recall"] == gold["task_success"] == 1
    invented_reference = observed.model_copy(
        update={
            "tasks": [
                task.model_copy(update={"unsupported_citations": 1}) for task in observed.tasks
            ]
        }
    )
    assert score(case, invented_reference)["task_success"] == 0
    merged = observed.model_copy(
        update={
            "mentions": [
                m.model_copy(
                    update={
                        "person_id": "one-person" if m.person_id else None,
                    }
                )
                for m in observed.mentions
            ]
        }
    )
    bad = score(case, merged)
    assert bad["collision_false_merges"] == 1
    assert bad["identity_precision"] == 0
    abstaining = observed.model_copy(
        update={"mentions": [m.model_copy(update={"person_id": None}) for m in observed.mentions]}
    )
    assert score(case, abstaining)["resolvable_identity_recall"] == 0
    wrong_sources = observed.model_copy(
        update={"facts": [f.model_copy(update={"evidence_events": []}) for f in observed.facts]}
    )
    assert score(case, wrong_sources)["direct_fact_recall"] == 0


def test_paired_interval_preserves_zero_and_negative_results() -> None:
    assert paired_interval([0.0] * 10, samples=1000) == (0.0, 0.0)
    low, high = paired_interval([-0.2] * 10, samples=1000)
    assert low < 0 and high < 0


def test_temporal_gold_distinguishes_replaced_and_current_preferences() -> None:
    development, _, _ = load_corpora(Path.cwd())
    case = development.cases[0]
    # June 9 explicitly replaces the June 3 chess preference with pottery.
    earlier = next(fact for fact in case.facts if fact.subject == "a" and "chess" in fact.terms)
    assert earlier.valid_to == case.events[8].occurred_at
    assert any(
        fact.subject == "a"
        and "pottery" in fact.terms
        and fact.valid_from == case.events[8].occurred_at
        and fact.valid_to is None
        for fact in case.facts
    )


def test_retrieval_score_uses_injected_evidence_separately_from_answer_citations() -> None:
    development, _, _ = load_corpora(Path.cwd())
    case = development.cases[0]
    observed = Observation.model_validate(
        {
            "case_id": case.id,
            "pipeline": "full-people",
            "repeat": 0,
            "mentions": [],
            "facts": [],
            "provider_calls": 1,
            "cost_usd": "0",
            "tasks": [
                {
                    "index": index,
                    "answer": "I do not know.",
                    "evidence_events": [],
                    "retrieved_evidence_events": task.evidence_events,
                }
                for index, task in enumerate(case.tasks)
            ],
        }
    )
    result = score(case, observed)
    assert result["retrieval_evidence_recall"] == 1
    assert result["task_success"] == 0
    fabricated = observed.model_copy(
        update={
            "tasks": [
                task.model_copy(
                    update={
                        "answer": " ".join(case.tasks[task.index].terms),
                        "evidence_events": case.tasks[task.index].evidence_events,
                        "retrieved_evidence_events": [],
                    }
                )
                for task in observed.tasks
            ]
        }
    )
    assert score(case, fabricated)["retrieval_evidence_recall"] == 0
    assert score(case, fabricated)["task_success"] == 0


def test_paired_comparison_treats_repeated_cases_as_one_sampling_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from agent_core.evals import people

    development, holdout, _ = load_corpora(Path.cwd())
    cases = [*development.cases, *holdout.cases]
    observations = []
    for case in cases:
        for pipeline in ("current-memory", "identity-links", "full-people"):
            for repeat in range(3):
                success = pipeline == "full-people" and repeat == 0
                observations.append(
                    {
                        "case_id": case.id,
                        "pipeline": pipeline,
                        "repeat": repeat,
                        "mentions": [],
                        "facts": [],
                        "provider_calls": 1,
                        "cost_usd": "0",
                        "tasks": [
                            {
                                "index": index,
                                "answer": " ".join(task.terms) if success else "Unknown",
                                "evidence_events": task.evidence_events if success else [],
                                "retrieved_evidence_events": task.evidence_events
                                if success
                                else [],
                            }
                            for index, task in enumerate(case.tasks)
                        ],
                    }
                )
    path = tmp_path / "synthetic-observations.json"
    path.write_text(json.dumps(observations))
    captured: list[float] = []

    def interval(values: list[float]) -> tuple[float, float]:
        captured.extend(values)
        return (0.3, 0.4)

    monkeypatch.setattr(people, "paired_interval", interval)
    report = people.score_observations(Path.cwd(), path)
    summaries = report["summaries"]
    assert isinstance(summaries, list) and len(summaries) == 18
    summary = next(
        row
        for row in summaries
        if row["pipeline"] == "full-people" and row["repeat"] == 0 and row["split"] == "development"
    )
    assert summary["task_success"] == 1
    assert summary["cases"] == len(development.cases)
    assert len(captured) == len(cases)
    assert all(value == pytest.approx(1 / 3) for value in captured)


def test_singleton_identity_and_missing_observations_have_honest_recall() -> None:
    development, _, _ = load_corpora(Path.cwd())
    original = development.cases[0]
    label = next(m for m in original.mentions if m.resolvable)
    case = original.model_copy(update={"mentions": [label], "facts": [], "tasks": []})
    observed = Observation(
        case_id=case.id,
        pipeline="full-people",
        repeat=0,
        mentions=[
            ObservedMention(
                event=label.event, start=label.start, end=label.end, person_id="singleton"
            )
        ],
        facts=[],
        tasks=[],
        provider_calls=3,
        cost_usd="0",
    )
    assert score(case, observed)["resolvable_identity_recall"] == 1
    missing = observed.model_copy(update={"mentions": []})
    assert score(case, missing)["resolvable_identity_recall"] == 0
    assert score(case, missing)["abstentions"] == 1


def test_quality_report_separately_measures_direction_attribution_and_time() -> None:
    from agent_core.evals.people import FactLabel

    development, _, _ = load_corpora(Path.cwd())
    original = development.cases[0]
    label = next(m for m in original.mentions if m.resolvable)
    fact = FactLabel(
        subject=str(label.entity),
        predicate="parent",
        object="owner",
        terms=["parent"],
        evidence_events=[label.event],
        valid_from=original.events[label.event].occurred_at,
        precision="month",
        source_timezone="America/Los_Angeles",
    )
    case = original.model_copy(update={"mentions": [label], "facts": [fact], "tasks": []})
    actual = ObservedFact(
        subject_id="owner",
        predicate="parent",
        object_id="person",
        statement="parent",
        evidence_events=fact.evidence_events,
        valid_from=fact.valid_from,
        precision="month",
        source_timezone="America/Los_Angeles",
    )
    observed = Observation(
        case_id=case.id,
        pipeline="full-people",
        repeat=0,
        mentions=[
            ObservedMention(event=label.event, start=label.start, end=label.end, person_id="person")
        ],
        facts=[actual],
        tasks=[],
        provider_calls=3,
        cost_usd="0",
    )
    result = score(case, observed)
    assert result["direction_accuracy"] == 0
    assert result["temporal_accuracy"] == 1
    # Absent authority metadata cannot count as measured correct attribution.
    assert result["attribution_accuracy"] == 0
    assert result["direct_fact_recall"] == 0
    for field, wrong in [("precision", "day"), ("source_timezone", "UTC")]:
        changed = observed.model_copy(update={"facts": [actual.model_copy(update={field: wrong})]})
        assert score(case, changed)["temporal_accuracy"] == 0


@pytest.mark.parametrize("evidence", ["exact", "wrong_source", "wrong_name", "unmapped_id"])
def test_employment_scoring_maps_organizations_only_from_source_evidence(evidence: str) -> None:
    from agent_core.evals.people import PeopleCase

    case = PeopleCase.model_validate(
        {
            "id": "employment",
            "family": "employment",
            "collision": False,
            "events": [
                {
                    "session": "a",
                    "occurred_at": "2025-01-01T00:00:00Z",
                    "text": "Maya works at Cedar.",
                },
                {
                    "session": "b",
                    "occurred_at": "2025-01-02T00:00:00Z",
                    "text": "Maya enjoys chess.",
                },
            ],
            "mentions": [
                {
                    "event": 0,
                    "start": 0,
                    "end": 4,
                    "text": "Maya",
                    "entity": "maya",
                    "resolvable": True,
                }
            ],
            "organizations": [
                {"event": 0, "start": 14, "end": 19, "text": "Cedar", "entity": "employer"}
            ],
            "facts": [
                {
                    "subject": "maya",
                    "predicate": "employee_of",
                    "object": "employer",
                    "terms": ["works", "Cedar"],
                    "evidence_events": [0],
                }
            ],
            "tasks": [],
        }
    )
    actual = Observation.model_validate(
        {
            "case_id": case.id,
            "pipeline": "full-people",
            "repeat": 0,
            "mentions": [{"event": 0, "start": 0, "end": 4, "person_id": "person-uuid"}],
            "organizations": [
                {
                    "organization_id": "org-uuid",
                    "display_name": "Birch" if evidence == "wrong_name" else "Cedar",
                    "evidence_events": [1] if evidence == "wrong_source" else [0],
                }
            ],
            "facts": [
                {
                    "subject_id": "person-uuid",
                    "predicate": "employee_of",
                    "object_id": "employer" if evidence == "unmapped_id" else "org-uuid",
                    "statement": "Maya works at Cedar",
                    "evidence_events": [0],
                    "authority": "user",
                    "derivation": "direct",
                }
            ],
            "tasks": [],
            "provider_calls": 3,
            "cost_usd": "0",
        }
    )
    result = score(case, actual)
    assert result["direct_fact_recall"] == int(evidence == "exact")
    assert result["identity_precision"] == 1


def test_commitment_scoring_requires_the_labeled_state_and_due_date() -> None:
    from agent_core.evals.people import FactLabel

    development, _, _ = load_corpora(Path.cwd())
    original = development.cases[0]
    label = original.mentions[0]
    fact = FactLabel.model_validate(
        {
            "subject": label.entity,
            "predicate": "commitment",
            "object": "owner",
            "terms": ["agenda"],
            "evidence_events": [0],
            "commitment_state": "completed",
            "due_at": original.events[0].occurred_at,
            "due_precision": "month",
            "source_timezone": "America/Los_Angeles",
        }
    )
    case = original.model_copy(update={"facts": [fact], "tasks": [], "mentions": [label]})
    actual = ObservedFact.model_validate(
        {
            "subject_id": "person",
            "predicate": "commitment",
            "object_id": "owner",
            "statement": "agenda",
            "evidence_events": [0],
            "authority": "user",
            "derivation": "direct",
            "commitment_state": "completed",
            "due_at": original.events[0].occurred_at,
            "due_precision": "month",
            "source_timezone": "America/Los_Angeles",
        }
    )
    observation = Observation(
        case_id=case.id,
        pipeline="full-people",
        repeat=0,
        mentions=[ObservedMention(event=0, start=label.start, end=label.end, person_id="person")],
        facts=[actual],
        tasks=[],
        provider_calls=3,
        cost_usd="0",
    )
    assert score(case, observation)["direct_fact_recall"] == 1
    for field, wrong in [
        ("commitment_state", "proposed"),
        ("due_at", None),
        ("due_precision", "day"),
        ("source_timezone", "UTC"),
    ]:
        assert (
            score(
                case,
                observation.model_copy(
                    update={"facts": [actual.model_copy(update={field: wrong})]}
                ),
            )["direct_fact_recall"]
            == 0
        )
