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
        ("User hired Alice and fired Bob.", "User hired Bob and fired Alice."),
        ("User lent Alice Bob's book.", "User lent Bob Alice's book."),
        ("The cat chased the dog in the garden.", "The dog chased the cat in the garden."),
        (
            "User takes important client meetings remotely on Fridays.",
            "Although busy, User does not take important client meetings remotely on Fridays.",
        ),
        (
            "User takes important client meetings remotely on Fridays.",
            "Although busy User does not take important client meetings remotely on Fridays.",
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
        # One displaced shared term is a paraphrase, not a swapped argument.
        (
            "User wants to modify the standard 5x5 strength-training routine to improve it.",
            "User wants to improve their standard 5x5 strength training routine.",
        ),
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
                        "compatible_kinds": ["project_fact"] if kind.value == "skill" else [],
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
    assert "--development-only" in result.output
    assert "--repeats" in result.output


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
        ("User runs with music.", "I run without music"),
        ("User runs.", "When not lifting, user does not run."),
        (
            "User can take meetings on Fridays.",
            "When travelling, I cannot take meetings on Fridays",
        ),
        ("User hired Alice and fired Bob.", "I fired Alice and hired Bob"),
        (
            "User takes important client meetings remotely on Fridays.",
            "Although busy I do not take important client meetings remotely on Fridays",
        ),
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


def test_main_clause_negation_survives_a_leading_subordinate_clause() -> None:
    from agent_core.memory.equivalence import negated

    assert negated("When not lifting, user does not run.")
    assert not negated("User bikes on days when not lifting.")


def test_scorer_version_advanced_with_its_semantics() -> None:
    """A changed scorer cannot keep the version an old artifact was published under."""

    assert DISTILLATION_SCORER_VERSION == "distillation-scorer@7"


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
    # The gate is the aggregate: one case is one anticipation call's chance.
    assert "no seeded case demonstrated attributed representation" in failures
    assert not any("restates a seeded belief" in failure for failure in failures)
    assert memory_eval.represented_case_count(_results(corpus, represented_verified=True)) == 1


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("When travelling, I cannot take meetings on Fridays", True),
        ("Although busy, I do not take client meetings on Fridays", True),
        ("User does not eat meat because of allergies.", True),
        ("User bikes on some days when not doing the 5x5 strength routine.", False),
        ("User swims when it is not raining.", False),
        ("User runs without music.", False),
        # A fronted subordinate clause with no comma still ends where the main
        # clause's subject begins.
        ("Although busy I do not take important client meetings remotely on Fridays.", True),
        ("When travelling I cannot take meetings on Fridays", True),
        ("When I am not lifting I run", False),
        ("If it is not raining user runs outdoors", False),
        ("User bikes on days when I am not lifting", False),
        # A fronted subordinate clause that ends at a comma or semicolon is
        # a circumstance in full, even when it carries its own subject and
        # negation; the subject search applies only without a separator.
        ("Although I do not run, User bikes.", False),
        ("Although I do not run; User bikes.", False),
        ("Although I run User does not bike", True),
    ],
)
def test_negation_is_scoped_to_the_main_clause(statement: str, expected: bool) -> None:
    """A fronted subordinate clause does not hide the main clause's negation."""

    from agent_core.memory.equivalence import negated

    assert negated(statement) is expected


def test_affirmative_main_clause_stays_compatible_after_a_negated_fronted_clause() -> None:
    """A negation confined to a comma-terminated fronted clause is not a denial.

    "Although I do not run, User bikes." asserts that the user bikes; the
    compatibility floor must not read the circumstance's "not" as the claim's
    polarity and reject the affirmative belief.
    """

    from agent_core.memory.equivalence import statements_compatible

    assert statements_compatible("Although I do not run, User bikes.", "User bikes.")


def test_compatible_kinds_match_with_the_longevity_local_policy_assigns() -> None:
    """A gold claim may name kinds a correct belief could reasonably carry.

    "Restarted 5x5 a year ago" is a skill to the label author and a project
    fact to the model; both are the same memory. A belief filed under a
    compatible kind matches only with the longevity local policy assigns that
    kind, so the loosening is bounded to taxonomy and never reaches the
    lifecycle a memory will get.
    """

    case = _case(
        expected=[
            {
                "claim_kind": "skill",
                "derivation": "direct",
                "longevity": "durable",
                "compatible_kinds": ["project_fact"],
                "subjects": ["5x5 history"],
                "statements": ["User restarted 5x5 a year ago."],
                "evidence_text": ["marathon"],
            }
        ]
    )
    as_project_fact = _belief(
        claim_kind="project_fact",
        derivation="direct",
        longevity="ongoing",
        subject="5x5 history",
        statement="User restarted 5x5 a year ago.",
    )
    wrong_longevity = as_project_fact.model_copy(update={"longevity": MemoryLongevity.DURABLE})
    as_habit = as_project_fact.model_copy(
        update={"claim_kind": MemoryClaimKind.HABIT, "longevity": MemoryLongevity.ONGOING}
    )
    primary = as_project_fact.model_copy(
        update={"claim_kind": MemoryClaimKind.SKILL, "longevity": MemoryLongevity.DURABLE}
    )

    assert score_distillation_case(case, [as_project_fact]).matched == 1
    assert score_distillation_case(case, [wrong_longevity]).matched == 0
    assert score_distillation_case(case, [as_habit]).matched == 0
    assert score_distillation_case(case, [primary]).matched == 1


def test_compatible_kinds_are_bounded_and_exclude_the_primary_kind() -> None:
    base = {
        "claim_kind": "skill",
        "derivation": "direct",
        "longevity": "durable",
        "subjects": ["5x5 history"],
        "statements": ["User restarted 5x5 a year ago."],
        "evidence_text": ["marathon"],
    }
    with pytest.raises(ValidationError, match="primary"):
        _case(expected=[{**base, "compatible_kinds": ["skill"]}])
    with pytest.raises(ValidationError):
        _case(expected=[{**base, "compatible_kinds": ["project_fact", "habit", "role", "goal"]}])
    with pytest.raises(ValidationError, match="unique"):
        _case(expected=[{**base, "compatible_kinds": ["project_fact", "project_fact"]}])


def test_claim_kind_coverage_counts_the_kind_the_provider_formed() -> None:
    """Matching through a compatible kind must not certify the kind it avoided."""

    corpus = MemoryDistillationCorpus.model_validate(_corpus_payload())
    results = _results(corpus, represented_verified=True)
    rewritten = []
    for result in results:
        arm = result.arms["formation@9"]
        beliefs = [
            belief.model_copy(
                update={
                    "claim_kind": MemoryClaimKind.PROJECT_FACT,
                    "longevity": (
                        MemoryLongevity.TENTATIVE
                        if belief.derivation is MemoryDerivation.HYPOTHESIS
                        else MemoryLongevity.ONGOING
                    ),
                }
            )
            if belief.claim_kind is MemoryClaimKind.SKILL
            else belief
            for belief in arm.beliefs
        ]
        rewritten.append(
            result.model_copy(
                update={
                    "arms": {
                        **result.arms,
                        "formation@9": arm.model_copy(update={"beliefs": beliefs}),
                    }
                }
            )
        )
    summaries = {
        policy: memory_eval._policy_metrics(policy, rewritten) for policy in memory_eval._POLICIES
    }

    failures = memory_eval.evaluate_publication_gates(corpus, rewritten, summaries)

    assert any(
        failure.startswith("claim-kind coverage is incomplete: skill") for failure in failures
    )


def test_holdout_requires_three_seeded_restatements_and_every_kind(tmp_path: Path) -> None:
    from agent_core.evals.memory_distillation import MemoryDistillationHoldout

    root = Path(__file__).resolve().parents[2]
    holdout, digest = memory_eval.load_distillation_holdout(root)
    assert len(digest) == 64
    represented = [case for case in holdout.cases if case.represented_text]
    assert len(represented) >= 3
    payload = holdout.model_dump(mode="json")
    payload["cases"] = [case for case in payload["cases"] if not case.get("represented_text")]
    with pytest.raises(ValidationError, match="three seeded cases"):
        MemoryDistillationHoldout.model_validate(payload)
    thin = holdout.model_dump(mode="json")
    thin["cases"] = [
        case
        for case in thin["cases"]
        if not any(expected["claim_kind"] == "resource" for expected in case["expected"])
    ]
    with pytest.raises(ValidationError, match="every claim kind"):
        MemoryDistillationHoldout.model_validate(thin)


def test_holdout_loader_refuses_an_edit_after_the_freeze(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).resolve().parents[2]
    for relative in ("evals/capability", "src"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    shutil.copy(root / "evals/capability/memory-formation.v3.json", tmp_path / "evals/capability/")
    shutil.copy(
        root / "evals/capability/memory-formation.v3-holdout.sha256", tmp_path / "evals/capability/"
    )
    edited = (
        (root / "evals/capability/memory-formation.v3-holdout.json")
        .read_text()
        .replace("medieval cartography", "renaissance cartography", 1)
    )
    (tmp_path / "evals/capability/memory-formation.v3-holdout.json").write_text(edited)
    with pytest.raises(ValueError, match="edited since its digest was frozen"):
        memory_eval.load_distillation_holdout(tmp_path)


def test_holdout_gates_mirror_the_thresholds_without_scenario_rules() -> None:
    from agent_core.evals.memory_distillation import evaluate_holdout_gates

    root = Path(__file__).resolve().parents[2]
    holdout, _digest = memory_eval.load_distillation_holdout(root)
    corpus_like = MemoryDistillationCorpus.model_construct(
        cases=holdout.cases, seed_pools=holdout.seed_pools
    )
    results = _results(corpus_like, represented_verified=True)
    summaries = {
        policy: memory_eval._policy_metrics(policy, results) for policy in memory_eval._POLICIES
    }

    assert evaluate_holdout_gates(results, summaries) == []
    unrepresented = _results(corpus_like, represented_verified=False)
    summaries = {
        policy: memory_eval._policy_metrics(policy, unrepresented)
        for policy in memory_eval._POLICIES
    }
    assert (
        "no holdout seeded case demonstrated attributed representation"
        in evaluate_holdout_gates(unrepresented, summaries)
    )


@pytest.mark.parametrize(
    ("subject", "expected_subjects", "expected_statements"),
    [
        (
            "weightlifting",
            ["lifting weights", "weights"],
            ["User lifts weights three times a week."],
        ),
        (
            "tomato growing",
            ["balcony gardening"],
            ["User grows tomatoes on the balcony every summer."],
        ),
        (
            "commuting by bicycle",
            ["cycling to the office", "commute"],
            ["User usually cycles to the office."],
        ),
        ("response brevity preference", ["answer style"], ["User prefers concise answers."]),
    ],
)
def test_subject_rule_accepts_an_inflected_or_compounded_key(
    subject: str, expected_subjects: list[str], expected_statements: list[str]
) -> None:
    """A key spelt as a variant of the gold's words names the same thing.

    The first holdout run lost four statement-equivalent beliefs to their
    keys alone: "weightlifting" against "lifting weights", "tomato growing"
    against "tomatoes", "commuting" against "commute", and "preference"
    against "prefers". The rule that a subject must name the gold conflict
    key stands; it now compares lemmas and treats a stem as naming its
    inflections and compounds.
    """

    assert subject_matches(subject, expected_subjects, expected_statements)


def test_subject_rule_still_rejects_an_unrelated_or_generic_key() -> None:
    assert not subject_matches("verbosity", ["answer style"], ["User prefers concise answers."])
    assert not subject_matches("User", ["answer style"], ["User prefers concise answers."])
    assert not subject_matches("running", ["marathon"], ["User wants to finish the marathon."])


def test_lemma_strips_an_oes_plural() -> None:
    from agent_core.memory.equivalence import lemma

    assert lemma("tomatoes") == "tomato"
    assert lemma("heroes") == "hero"
    assert lemma("shoes") == "shoe"
    assert lemma("goes") == "go"


def test_a_development_only_result_passes_without_evidence() -> None:
    """Tuning runs score the development corpus alone and publish nothing."""

    result = memory_eval.MemoryDistillationEvaluationResult(
        passed=True, failure_summary=None, cases=[], policies={}, development_only=True
    )

    assert result.evidence is None
    assert result.holdout_cases == []
    with pytest.raises(ValidationError, match="development-only"):
        memory_eval.MemoryDistillationEvaluationResult(
            passed=True, failure_summary=None, cases=[], policies={}
        )


def test_a_development_only_run_never_touches_the_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holdout is spent by every run against it, so tuning runs skip it.

    A development-only run loads the corpus, scores the three arms on it,
    reports the publication gates, and neither reads the holdout nor writes
    evidence, whatever the gates say.
    """

    import asyncio
    from datetime import UTC, datetime

    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    monkeypatch.setattr(memory_eval, "require_committed_tree", lambda root, ref: None)
    monkeypatch.setattr(memory_eval, "load_settings", lambda: object())
    monkeypatch.setattr(memory_eval, "_evaluation_settings", lambda settings, root: settings)

    def refuse(_root: Path) -> tuple[Any, str]:
        raise AssertionError("a development-only run must not load the holdout")

    monkeypatch.setattr(memory_eval, "load_distillation_holdout", refuse)

    async def silent_arm(
        _settings: Any,
        case: Any,
        *,
        model_policy: str,
        policy_profile: str,
        policy_version: Any,
        seeds: Any,
    ) -> Any:
        return memory_eval.DistillationArmResult(
            policy_version=policy_version,
            beliefs=[],
            score=memory_eval.score_distillation_case(case, []),
            identity=("openai", "gpt-5.6-sol", "default@1"),
            provider_calls=0,
            expected_provider_calls=0,
            evaluated_at=datetime(2026, 9, 10, tzinfo=UTC),
        )

    monkeypatch.setattr(memory_eval, "_evaluate_case", silent_arm)
    output = tmp_path / "evidence.json"

    result = asyncio.run(
        memory_eval.run_live_evaluation(
            Path.cwd(),
            model_policy="balanced",
            policy_profile="default",
            build_ref="0" * 40,
            output=output,
            development_only=True,
        )
    )

    assert result is not None
    assert result.development_only
    assert result.holdout_cases == []
    assert result.holdout_policies == {}
    assert not result.passed
    assert "direct must-form recall" in (result.failure_summary or "")
    assert result.evidence is None
    assert not output.exists()


@pytest.mark.parametrize(
    ("candidate", "reference"),
    [
        (
            "User has written firmware for insulin pumps for eight years.",
            "User has eight years of experience writing firmware for insulin pumps.",
        ),
        ("User's database currently uses SQLite.", "The user's database is SQLite for now."),
        (
            "User always prefers metric measurements, never imperial.",
            "User wants measurements in metric, never imperial.",
        ),
        ("User has a son named Robert who lives in Berlin.", "User has a son."),
        (
            "User is starting a podcast about local history.",
            "User has started a local history podcast.",
        ),
    ],
)
def test_claim_match_accepts_an_elaboration_of_the_gold(candidate: str, reference: str) -> None:
    """A correct claim stated with more detail is the claim, not a different one.

    The owner decided on 2026-09-10 that a personal agent should be scored
    recall-first: a belief that carries every content term of the gold, with
    the same polarity, counts, numbers, directions, and term order, matches
    however many words it adds. Inflections agree, and bare qualifiers such
    as "currently" and "always" are not content. This is
    distillation-scorer@7; the runtime combiner keeps the stricter rule.
    """

    from agent_core.memory.equivalence import statement_matches_claim

    assert statement_matches_claim(candidate, reference)


@pytest.mark.parametrize(
    ("candidate", "reference"),
    [
        (
            "User ran 200 miles in training last month.",
            "User ran 100 miles in training last month.",
        ),
        ("User prefers coffee to tea.", "User prefers tea to coffee."),
        ("User runs without music.", "User runs with music."),
        ("User does not run outdoors.", "User runs outdoors."),
        ("User grows tomatoes.", "User grows tomatoes and chillies on their balcony every summer."),
        ("User bikes on the rest of the days.", "User swims on the rest of the days."),
        ("User has a daughter.", "User has two daughters."),
    ],
)
def test_claim_match_still_rejects_a_different_claim(candidate: str, reference: str) -> None:
    from agent_core.memory.equivalence import statement_matches_claim

    assert not statement_matches_claim(candidate, reference)


def test_holdout_precision_floor_is_seventy_five_percent() -> None:
    """The holdout's precision floor is 0.75; the development corpus keeps 0.90.

    Of forty-two extra holdout beliefs across three pooled runs on
    2026-09-10, about twenty-six were true things the labels never listed
    and about twelve were correct facts under a different kind or wording;
    four were poor. The floor exists to keep wrong memories out, and the
    measurement was dominated by memories the labels missed, so the owner set
    it at 0.75 and kept every other threshold.
    """

    from agent_core.evals.memory_distillation import evaluate_holdout_gates

    root = Path(__file__).resolve().parents[2]
    holdout, _digest = memory_eval.load_distillation_holdout(root)
    corpus_like = MemoryDistillationCorpus.model_construct(
        cases=holdout.cases, seed_pools=holdout.seed_pools
    )
    results = _results(corpus_like, represented_verified=True)
    summaries = {
        policy: memory_eval._policy_metrics(policy, results) for policy in memory_eval._POLICIES
    }
    lenient = summaries["formation@9"].model_copy(update={"benign_precision": 0.76})
    strict = summaries["formation@9"].model_copy(update={"benign_precision": 0.74})

    assert evaluate_holdout_gates(results, {**summaries, "formation@9": lenient}) == []
    assert "holdout benign precision 0.740 is below 0.75" in evaluate_holdout_gates(
        results, {**summaries, "formation@9": strict}
    )


def _rerun(
    results: list[memory_eval.DistillationCaseResult], run_index: int
) -> list[memory_eval.DistillationCaseResult]:
    return [result.model_copy(update={"run_index": run_index}) for result in results]


def test_repeated_runs_gate_on_the_pooled_aggregate() -> None:
    """Gates are decided over every repeat, not by the draw of one run.

    Two runs of one unchanged policy differed by up to 0.11 per gate on
    2026-09-10, so the owner decided to gate on an aggregate: recall,
    precision, lift, and disposition pool every run; the personal-agent and
    rich cores need each expected memory in a majority of runs rather than
    all of them in one; the represented gate takes the weakest run; and
    boundary failures and call counts still fail on any run.
    """

    corpus = MemoryDistillationCorpus.model_validate(_corpus_payload())
    full = _results(corpus, represented_verified=True)
    runs = [_rerun(full, 0), _rerun(full, 1)]
    weaker = []
    for result in _rerun(full, 2):
        if result.case_id == "rich-conversation-900":
            arm = result.arms["formation@9"]
            case = next(case for case in corpus.cases if case.id == result.case_id)
            beliefs = arm.beliefs[1:]
            arm = arm.model_copy(
                update={"beliefs": beliefs, "score": score_distillation_case(case, beliefs)}
            )
            result = result.model_copy(update={"arms": {**result.arms, "formation@9": arm}})
        weaker.append(result)
    runs.append(weaker)
    pooled = [result for run in runs for result in run]
    summaries = {
        policy: memory_eval._policy_metrics(policy, pooled) for policy in memory_eval._POLICIES
    }

    failures = memory_eval.evaluate_publication_gates(corpus, pooled, summaries, repeats=3)

    assert not any("rich multi-turn" in failure for failure in failures), failures
    assert not any("direct must-form recall" in failure for failure in failures), failures
    single = {
        policy: memory_eval._policy_metrics(policy, weaker) for policy in memory_eval._POLICIES
    }
    assert any(
        "rich multi-turn" in failure
        for failure in memory_eval.evaluate_publication_gates(corpus, weaker, single)
    )

    # The represented gate, like the cores, needs a majority of runs: one
    # anticipation call's chance per run, so one dry run of three is not a
    # failure and two of three is.
    unrepresented = _results(corpus, represented_verified=False)
    pooled = [*runs[0], *runs[1], *_rerun(unrepresented, 2)]
    summaries = {
        policy: memory_eval._policy_metrics(policy, pooled) for policy in memory_eval._POLICIES
    }
    assert "no seeded case demonstrated attributed representation" not in (
        memory_eval.evaluate_publication_gates(corpus, pooled, summaries, repeats=3)
    )
    pooled = [*runs[0], *_rerun(unrepresented, 1), *_rerun(unrepresented, 2)]
    summaries = {
        policy: memory_eval._policy_metrics(policy, pooled) for policy in memory_eval._POLICIES
    }
    assert "no seeded case demonstrated attributed representation" in (
        memory_eval.evaluate_publication_gates(corpus, pooled, summaries, repeats=3)
    )


def test_a_result_records_its_repeat_count() -> None:
    result = memory_eval.MemoryDistillationEvaluationResult(
        passed=True, failure_summary=None, cases=[], policies={}, development_only=True, repeats=3
    )

    assert result.repeats == 3
    assert (
        memory_eval.MemoryDistillationEvaluationResult(
            passed=True, failure_summary=None, cases=[], policies={}, development_only=True
        ).repeats
        == 1
    )
