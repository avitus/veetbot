"""Offline comparative-evaluation tests for formation@9."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import agent_core.evals.memory_distillation as memory_eval
from agent_core.cli.main import app
from agent_core.domain.memory import MemoryClaimKind, MemoryDerivation, MemoryLongevity
from agent_core.evals.memory_distillation import (
    DistillationEvaluationBelief,
    MemoryDistillationCase,
    MemoryDistillationCorpus,
    score_distillation_case,
)
from agent_core.memory.equivalence import (
    DISTILLATION_SCORER_VERSION,
    statements_equivalent,
    subject_matches,
)


def _case(**overrides: object) -> MemoryDistillationCase:
    payload: dict[str, object] = {
        "id": "goal-semantic-901",
        "label": "must_form",
        "scenario": "ordinary",
        "events": [{"actor": "user", "text": "My goal is to finish the marathon."}],
        "expected": [
            {
                "claim_kind": "goal",
                "derivation": "direct",
                "longevity": "ongoing",
                "subjects": ["marathon"],
                "statements": ["User wants to finish the marathon."],
                "evidence_text": ["finish the marathon"],
            }
        ],
    }
    payload.update(overrides)
    return MemoryDistillationCase.model_validate(payload)


def _belief(**overrides: object) -> DistillationEvaluationBelief:
    payload: dict[str, object] = {
        "claim_kind": MemoryClaimKind.GOAL,
        "derivation": MemoryDerivation.DIRECT,
        "longevity": MemoryLongevity.ONGOING,
        "subject": "marathon",
        "statement": "User wants to finish the marathon.",
    }
    payload.update(overrides)
    return DistillationEvaluationBelief.model_validate(payload)


def test_distillation_scorer_matches_closed_fields_and_counts_false_positives() -> None:
    case = _case(
        id="personal-agent-901",
        scenario="personal-agent",
        events=[{"actor": "user", "text": "I am building a personal AI agent."}],
        expected=[
            {
                "claim_kind": "ongoing_project",
                "derivation": "direct",
                "longevity": "ongoing",
                "subjects": ["personal AI agent"],
                "statements": ["User is building a personal AI agent."],
                "evidence_text": ["building a personal AI agent"],
            }
        ],
    )
    beliefs = [
        _belief(
            claim_kind="ongoing_project",
            subject="personal AI agent",
            statement="User is building a personal AI agent.",
        ),
        _belief(
            claim_kind="role",
            longevity="durable",
            subject="chief executive",
            statement="User is a chief executive.",
        ),
    ]

    score = score_distillation_case(case, beliefs)

    assert score.scorer_version == DISTILLATION_SCORER_VERSION
    assert score.scoring == "strict"
    assert score.expected == 1
    assert score.matched == 1
    assert score.predicted == 2
    assert score.false_positives == 1
    assert score.direct_must_form_expected == 1
    assert score.direct_must_form_matched == 1


def test_distillation_scorer_accepts_a_paraphrased_verb_with_a_specific_subject() -> None:
    score = score_distillation_case(
        _case(),
        [_belief(statement="User's goal is to finish the marathon.")],
    )

    assert score.matched == 1
    assert score.false_positives == 0


def test_distillation_scorer_rejects_a_generic_user_subject() -> None:
    """Subjects are conflict keys; "User" is a bucket, not a key."""

    score = score_distillation_case(
        _case(),
        [_belief(subject="User", statement="User wants to finish the marathon.")],
    )

    assert score.matched == 0
    assert score.false_positives == 1
    assert not subject_matches("User", ["marathon"])
    assert subject_matches("marathon finish", ["marathon"])
    with pytest.raises(ValidationError, match="specific conflict key"):
        _case(
            expected=[
                {
                    "claim_kind": "goal",
                    "derivation": "direct",
                    "longevity": "ongoing",
                    "subjects": ["User"],
                    "statements": ["User wants to finish the marathon."],
                    "evidence_text": ["finish the marathon"],
                }
            ]
        )


@pytest.mark.parametrize(
    ("candidate", "reference"),
    [
        ("User has a son named Robert who lives in Berlin.", "User has a son."),
        ("User has a daughter and a debt problem.", "User has a daughter."),
        (
            "User is interested in pottery baking and glazing kilns.",
            "User is interested in pottery.",
        ),
        (
            "User's current 5x5 progress has stalled.",
            "User's current 5x5 progress has not stalled.",
        ),
        ("User can take meetings on Fridays.", "User cannot take meetings on Fridays."),
        ("User is not interested in urban history.", "User is interested in urban history."),
        ("User has at least one sister.", "User has at least two sisters."),
        (
            "User regularly runs on non-strength-training days.",
            "User regularly swims on non-strength-training days.",
        ),
        ("User runs two times a week.", "User runs three times a week."),
        ("User no longer drives to work.", "User drives to work."),
        ("User was promoted in 2019.", "User was promoted in 2024."),
        ("User pays 1500 per month in rent.", "User pays 2500 per month in rent."),
        ("User prefers tea to coffee.", "User prefers coffee to tea."),
        ("User prefers coffee over tea.", "User prefers tea to coffee."),
        ("User moved from Paris to Rome.", "User moved from Rome to Paris."),
        ("User prefers examples before theory.", "User prefers theory before examples."),
        (
            "User ran 100 miles in training for the marathon last month.",
            "User ran 200 miles in training for the marathon last month.",
        ),
    ],
)
def test_scorer_never_equates_supersets_negations_counts_or_siblings(
    candidate: str, reference: str
) -> None:
    assert not statements_equivalent(candidate, reference)
    assert not statements_equivalent(reference, candidate)


@pytest.mark.parametrize(
    ("candidate", "reference"),
    [
        ("User's goal is to finish the marathon.", "User wants to finish the marathon."),
        ("User is building the recipe app.", "User is building a recipe app."),
        (
            "User follows the standard 5x5 strength training routine two to three times per week.",
            "User does the standard 5x5 strength-training routine 2-3 times per week.",
        ),
        ("User cannot take meetings on Fridays.", "User cannot take meetings on Fridays."),
        ("User has at least two sisters.", "User has two sisters."),
        ("User has at least one daughter.", "User has a daughter."),
        (
            "User's daughter starts college next year, in 2027.",
            "User's daughter starts college next year.",
        ),
        ("User prefers tea over coffee.", "User prefers tea to coffee."),
        ("User does not drive to work.", "User doesn't drive to work."),
    ],
)
def test_scorer_accepts_equivalent_wording(candidate: str, reference: str) -> None:
    assert statements_equivalent(candidate, reference)


def test_lenient_scoring_credits_a_control_policy_for_the_statement_it_formed() -> None:
    """A frozen control cannot express claim kinds, so it is scored on statements."""

    legacy = _belief(
        claim_kind="project_fact",
        longevity="durable",
        subject="goal",
        statement="User wants to finish the marathon.",
    )

    strict = score_distillation_case(_case(), [legacy])
    lenient = score_distillation_case(_case(), [legacy], closed_fields=False)

    assert strict.matched == 0
    assert lenient.scoring == "lenient"
    assert lenient.matched == 1
    assert lenient.false_positives == 0


def test_distillation_scorer_counts_evidence_units_the_provider_labelled_away() -> None:
    score = score_distillation_case(
        _case(),
        [_belief()],
        evidence_units=3,
        evidence_units_formed=2,
    )

    assert score.evidence_units == 3
    assert score.evidence_units_formed == 2


def _corpus_payload(**overrides: object) -> dict[str, Any]:
    kinds = list(MemoryClaimKind)
    cases: list[dict[str, object]] = []
    for index, kind in enumerate(kinds * 5):
        cases.append(
            {
                "id": f"{kind.value.replace('_', '-')}-{index:03d}",
                "label": "must_form",
                "scenario": "ordinary",
                "events": [{"actor": "user", "text": f"I keep item {index} in the Blue folder."}],
                "expected": [
                    {
                        "claim_kind": kind.value,
                        "derivation": "direct",
                        "longevity": "durable",
                        "subjects": [f"item {index}"],
                        "statements": [f"User keeps item {index} in the Blue folder."],
                        "evidence_text": [f"item {index}"],
                    }
                ],
            }
        )
    cases.append(
        {
            "id": "personal-agent-900",
            "label": "must_form",
            "scenario": "personal-agent",
            "events": [{"actor": "user", "text": "I am building a personal AI agent."}],
            "expected": [
                {
                    "claim_kind": "skill",
                    "derivation": "hypothesis",
                    "longevity": "tentative",
                    "subjects": ["software-development experience"],
                    "statements": ["User likely has software-development experience."],
                    "evidence_text": ["building a personal AI agent"],
                }
            ],
        }
    )
    for scenario in (
        "compound",
        "misleading-professional-cue",
        "evidence-promotion",
        "lifecycle-retirement",
        "self-citation",
    ):
        cases.append(
            {
                "id": f"{scenario}-900",
                "label": "reasonable_to_form",
                "scenario": scenario,
                "events": [{"actor": "user", "text": "I run every morning before breakfast."}],
                "expected": [
                    {
                        "claim_kind": "habit",
                        "derivation": "direct",
                        "longevity": "ongoing",
                        "subjects": ["morning running"],
                        "statements": ["User runs every morning before breakfast."],
                        "evidence_text": ["run every morning"],
                    }
                ],
            }
        )
    cases.append(
        {
            "id": "rich-conversation-900",
            "label": "must_form",
            "scenario": "rich-conversation",
            "prior_beliefs_pool": "populated",
            "represented_text": ["I keep seed 1 in the Blue folder"],
            "events": [
                {"actor": "user", "text": "I swim, run, or bike most days."},
                {"actor": "user", "text": "I lift weights three times a week."},
                {"actor": "user", "text": "I keep seed 1 in the Blue folder."},
            ],
            "expected": [
                {
                    "claim_kind": "habit",
                    "derivation": "direct",
                    "longevity": "ongoing",
                    "subjects": ["swimming"],
                    "statements": ["User regularly swims."],
                    "evidence_text": ["I swim, run, or bike most days"],
                }
            ],
        }
    )
    for index in range(4):
        cases.append(
            {
                "id": f"boundary-secret-9{index:02d}",
                "label": "must_not_form",
                "scenario": "trust-boundary",
                "events": [{"actor": "user", "text": f"password: hunter{index}"}],
                "expected": [],
            }
        )
    payload: dict[str, object] = {
        "schema_version": 3,
        "seed_pools": {
            "populated": [
                {
                    "claim_kind": "resource",
                    "subject": f"seed {index}",
                    "statement": f"User keeps seed {index} in the Blue folder.",
                }
                for index in range(25)
            ]
        },
        "cases": cases,
    }
    payload.update(overrides)
    return payload


def test_corpus_requires_a_seeded_multi_event_positive_case() -> None:
    corpus = MemoryDistillationCorpus.model_validate(_corpus_payload())
    rich = next(case for case in corpus.cases if case.scenario == "rich-conversation")
    assert len(corpus.seeds_for(rich)) == 25

    unseeded = _corpus_payload(seed_pools={})
    unseeded["cases"] = [
        {
            key: value
            for key, value in case.items()
            if key not in {"prior_beliefs_pool", "represented_text"}
        }
        for case in _corpus_payload()["cases"]
    ]
    with pytest.raises(ValidationError, match="populated store"):
        MemoryDistillationCorpus.model_validate(unseeded)
    with pytest.raises(ValidationError, match="undeclared seed pool"):
        MemoryDistillationCorpus.model_validate(_corpus_payload(seed_pools={"other": []}))


def test_distillation_cli_command_is_registered() -> None:
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "memory-distillation",
            "--model-policy",
            "balanced",
            "--policy-profile",
            "default",
            "--build-ref",
            "build-123",
            "--output",
            str(Path("evidence.json")),
            "--help",
        ],
    )

    assert result.exit_code == 0
    assert "formation@7" in result.output
    assert "formation@8" in result.output
    assert "formation@9" in result.output


def test_live_evaluation_refuses_a_non_commit_build_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    import asyncio

    with pytest.raises(ValueError, match="commit sha"):
        asyncio.run(
            memory_eval.run_live_evaluation(
                Path.cwd(),
                model_policy="balanced",
                policy_profile="default",
                build_ref="content-6973e8ddc75c6e40947e1f368abf2a96",
                output=tmp_path / "evidence.json",
            )
        )


def test_generic_subject_check_normalizes_curly_apostrophes() -> None:
    from agent_core.memory.equivalence import is_generic_subject

    assert is_generic_subject("User\u2019s")
    assert is_generic_subject("the user")
    assert not is_generic_subject("user\u2019s training routine")
    with pytest.raises(ValidationError, match="specific conflict key"):
        _case(
            expected=[
                {
                    "claim_kind": "goal",
                    "derivation": "direct",
                    "longevity": "ongoing",
                    "subjects": ["User\u2019s"],
                    "statements": ["User wants to finish the marathon."],
                    "evidence_text": ["finish the marathon"],
                }
            ]
        )


def test_equivalence_survives_oversized_digit_runs() -> None:
    from agent_core.memory.equivalence import content_terms, quantity_terms

    huge = "User's reference number is " + "9" * 5000 + "."
    assert quantity_terms(huge) == frozenset()
    assert "9" * 5000 in content_terms(huge)
    assert not statements_equivalent(huge, "User's reference number is 12.")


def test_scorer_finds_the_largest_valid_pairing_regardless_of_order() -> None:
    """A claim that could use two beliefs must not strand the claim that needs one."""

    case = _case(
        expected=[
            {
                "claim_kind": "habit",
                "derivation": "direct",
                "longevity": "ongoing",
                "subjects": ["swimming", "running"],
                "statements": ["User regularly swims.", "User regularly runs."],
                "evidence_text": ["finish the marathon"],
            },
            {
                "claim_kind": "habit",
                "derivation": "direct",
                "longevity": "ongoing",
                "subjects": ["running"],
                "statements": ["User regularly runs."],
                "evidence_text": ["finish the marathon"],
            },
        ]
    )
    running = _belief(
        claim_kind="habit", longevity="ongoing", subject="running", statement="User regularly runs."
    )
    swimming = _belief(
        claim_kind="habit",
        longevity="ongoing",
        subject="swimming",
        statement="User regularly swims.",
    )

    score = score_distillation_case(case, [running, swimming])

    assert score.matched == 2
    assert score.false_positives == 0
    assert score.direct_must_form_matched == 2


def test_rich_conversation_case_requires_a_populated_pool() -> None:
    """Another seeded multi-event case satisfies the global rule; only the rich one fails."""

    payload = _corpus_payload()
    payload["seed_pools"]["empty"] = []
    for case in payload["cases"]:
        if case["id"] == "personal-agent-900":
            case["prior_beliefs_pool"] = "populated"
            case["events"] = [
                {"actor": "user", "text": "I am building a personal AI agent."},
                {"actor": "user", "text": "I am building a personal AI agent."},
            ]
        if case["scenario"] == "rich-conversation":
            case["prior_beliefs_pool"] = "empty"
    with pytest.raises(
        ValidationError, match="rich-conversation scenario must run against a populated store"
    ):
        MemoryDistillationCorpus.model_validate(payload)


def test_live_evaluation_refuses_a_dirty_or_mismatched_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import subprocess

    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=repository, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True
    ).stdout.strip()

    with pytest.raises(ValueError, match="checked-out HEAD"):
        asyncio.run(
            memory_eval.run_live_evaluation(
                repository,
                model_policy="balanced",
                policy_profile="default",
                build_ref="0" * 40,
                output=tmp_path / "evidence.json",
            )
        )
    tracked.write_text("two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="uncommitted changes"):
        asyncio.run(
            memory_eval.run_live_evaluation(
                repository,
                model_policy="balanced",
                policy_profile="default",
                build_ref=head,
                output=tmp_path / "evidence.json",
            )
        )


@pytest.mark.parametrize(
    ("statement", "clause"),
    [
        ("User can take meetings on Fridays.", "I cannot take meetings on Fridays"),
        ("User runs two times a week.", "I run three times a week now"),
        ("User prefers tea to coffee.", "I prefer coffee to tea these days"),
        ("User pays 1500 per month in rent.", "my rent is 2500 per month"),
    ],
)
def test_clause_support_rejects_polarity_count_and_direction_changes(
    statement: str, clause: str
) -> None:
    """A memory never represents a clause that corrects it.

    Anticipation may label a correction as already represented; the only
    thing standing between that label and a silently lost correction is this
    check, so it must say no to a negation, a different count, or a reversed
    direction, not merely count shared words.
    """

    from agent_core.memory.equivalence import statement_supports_clause

    assert not statement_supports_clause(statement, clause)


@pytest.mark.parametrize(
    ("statement", "clause"),
    [
        ("User cannot take meetings on Fridays.", "I can't take meetings on Fridays"),
        ("User lives in Portland.", "I still live in Portland"),
        ("User prefers tea to coffee.", "as I said, tea over coffee for me"),
    ],
)
def test_clause_support_accepts_a_restated_claim(statement: str, clause: str) -> None:
    from agent_core.memory.equivalence import statement_supports_clause

    assert statement_supports_clause(statement, clause)


def test_scorer_version_advanced_with_its_semantics() -> None:
    """A changed scorer cannot keep the version an old artifact was published under."""

    assert DISTILLATION_SCORER_VERSION == "distillation-scorer@3"


def test_represented_text_requires_a_pool_and_exact_user_text() -> None:
    with pytest.raises(ValidationError, match="requires a seed pool"):
        _case(represented_text=["My goal is to finish the marathon"])
    with pytest.raises(ValidationError, match="exact user substring"):
        _case(prior_beliefs_pool="populated", represented_text=["I live in Portland"])
    case = _case(prior_beliefs_pool="populated", represented_text=["finish the marathon"])
    assert case.represented_text == ["finish the marathon"]


def test_corpus_requires_a_seeded_case_that_restates_a_seed() -> None:
    """A populated store proves nothing unless the provider is made to use it."""

    payload = _corpus_payload()
    for case in payload["cases"]:
        case.pop("represented_text", None)
    with pytest.raises(ValidationError, match="one of its seeds represents"):
        MemoryDistillationCorpus.model_validate(payload)

    unsupported = _corpus_payload()
    for case in unsupported["cases"]:
        if "represented_text" in case:
            case["events"].append({"actor": "user", "text": "I moved to Lisbon."})
            case["represented_text"] = ["I moved to Lisbon"]
    with pytest.raises(ValidationError, match="one of its seeds represents"):
        MemoryDistillationCorpus.model_validate(unsupported)


def _results(
    corpus: MemoryDistillationCorpus, *, represented_verified: bool
) -> list[memory_eval.DistillationCaseResult]:
    from datetime import UTC, datetime

    from agent_core.evals.memory_distillation import (
        DistillationArmResult,
        DistillationCaseResult,
        PolicyVersion,
    )

    now = datetime(2026, 9, 4, tzinfo=UTC)
    results: list[DistillationCaseResult] = []
    for case in corpus.cases:
        formed = [
            _belief(
                claim_kind=expected.claim_kind,
                derivation=expected.derivation,
                longevity=expected.longevity,
                subject=expected.subjects[0],
                statement=expected.statements[0],
            )
            for expected in case.expected
        ]
        represented_units = len(case.represented_text)
        arms: dict[PolicyVersion, DistillationArmResult] = {}
        for policy in memory_eval._POLICIES:
            current = policy == "formation@9"
            beliefs = formed if current else []
            arms[policy] = DistillationArmResult(
                policy_version=policy,
                beliefs=beliefs,
                score=score_distillation_case(case, beliefs, closed_fields=current),
                provider_calls=3 if current else 0,
                expected_provider_calls=3 if current else 0,
                seeded_beliefs=len(corpus.seeds_for(case)),
                represented_units=represented_units if current else 0,
                represented_units_verified=(
                    represented_units if current and represented_verified else 0
                ),
                evaluated_at=now,
            )
        results.append(
            DistillationCaseResult(
                case_id=case.id, label=case.label, scenario=case.scenario, arms=arms
            )
        )
    return results


def test_publication_requires_a_verifiably_represented_seeded_clause() -> None:
    """Zero predictions and zero attributed redundancies can no longer publish."""

    corpus = MemoryDistillationCorpus.model_validate(_corpus_payload())

    def gate_failures(represented_verified: bool) -> list[str]:
        results = _results(corpus, represented_verified=represented_verified)
        summaries = {
            policy: memory_eval._policy_metrics(policy, results) for policy in memory_eval._POLICIES
        }
        return memory_eval.evaluate_publication_gates(corpus, results, summaries)

    assert gate_failures(True) == []
    failures = gate_failures(False)
    assert "rich-conversation-900 restates a seeded belief that was not verifiably represented" in (
        failures
    )
    assert "no seeded case demonstrated attributed representation" in failures
    assert memory_eval.represented_case_count(_results(corpus, represented_verified=True)) == 1
