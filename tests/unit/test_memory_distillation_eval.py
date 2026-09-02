"""Offline comparative-evaluation tests for formation@9."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import agent_core.evals.memory_distillation as memory_eval
from agent_core.cli.main import app
from agent_core.domain.memory import MemoryClaimKind, MemoryDerivation, MemoryLongevity
from agent_core.evals.memory_distillation import MemoryDistillationCase


def test_distillation_scorer_matches_closed_fields_and_counts_false_positives() -> None:
    belief_type = getattr(memory_eval, "DistillationEvaluationBelief", None)
    score_case = getattr(memory_eval, "score_distillation_case", None)
    assert belief_type is not None
    assert score_case is not None
    case = MemoryDistillationCase.model_validate(
        {
            "id": "personal-agent-901",
            "label": "must_form",
            "scenario": "personal-agent",
            "events": [{"actor": "user", "text": "I am building a personal AI agent."}],
            "expected": [
                {
                    "claim_kind": "ongoing_project",
                    "derivation": "direct",
                    "longevity": "ongoing",
                    "subjects": ["personal AI agent"],
                    "statements": ["User is building a personal AI agent."],
                    "evidence_text": ["building a personal AI agent"],
                }
            ],
        }
    )
    beliefs = [
        belief_type(
            claim_kind="ongoing_project",
            derivation="direct",
            longevity="ongoing",
            subject="personal AI agent",
            statement="User is building a personal AI agent.",
        ),
        belief_type(
            claim_kind="role",
            derivation="direct",
            longevity="durable",
            subject="chief executive",
            statement="User is a chief executive.",
        ),
    ]

    score = score_case(case, beliefs)

    assert score.expected == 1
    assert score.matched == 1
    assert score.predicted == 2
    assert score.false_positives == 1
    assert score.direct_must_form_expected == 1
    assert score.direct_must_form_matched == 1


def test_distillation_scorer_accepts_semantically_equivalent_canonical_wording() -> None:
    belief_type = memory_eval.DistillationEvaluationBelief
    case = MemoryDistillationCase.model_validate(
        {
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
    )

    score = memory_eval.score_distillation_case(
        case,
        [
            belief_type(
                claim_kind=MemoryClaimKind.GOAL,
                derivation=MemoryDerivation.DIRECT,
                longevity=MemoryLongevity.ONGOING,
                subject="User",
                statement="User's goal is to finish the marathon.",
            )
        ],
    )

    assert score.matched == 1
    assert score.false_positives == 0


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
