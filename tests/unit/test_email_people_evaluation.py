"""Email comparison replays the real assessor and retains source attribution."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import FakeModelScript, ResolvedModel, ScriptedTurn
from agent_core.evals.email_people import EmailPeopleCase
from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings


def test_email_people_corpus_has_an_independent_offline_cli() -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    result = CliRunner().invoke(app, ["eval", "email-people", "--check-corpus"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["development_scenarios"] >= 60 and report["holdout_scenarios"] >= 60
    assert report["activation_evidence"] is False and report["provider_calls"] == 0


@pytest.mark.parametrize("policy", ["email-semantic@1", "email-semantic@2"])
async def test_email_comparison_uses_production_assessment_without_gold_labels(
    tmp_path: Path, policy: Literal["email-semantic@1", "email-semantic@2"]
) -> None:
    from agent_core.evals.email_people_execution import evaluate_email_case

    case = EmailPeopleCase.model_validate(
        {
            "id": "email-eval",
            "family": "source",
            "collision": False,
            "events": [
                {
                    "session": "a",
                    "occurred_at": NOW.replace(microsecond=0).isoformat(),
                    "actor": "third_party",
                    "text": "Alex prefers tea.",
                },
                {
                    "session": "b",
                    "occurred_at": "2024-01-01T00:00:00Z",
                    "actor": "third_party",
                    "text": "Alex once liked coffee.",
                },
                # The owner's earlier reply is what adds Alex to People (ADR-0121).
                {
                    "session": "c",
                    "occurred_at": (NOW - timedelta(days=1)).replace(microsecond=0).isoformat(),
                    "actor": "owner",
                    "text": "Thanks Alex, see you Friday.",
                },
            ],
            "mentions": [
                {
                    "event": 0,
                    "start": 0,
                    "end": 4,
                    "text": "Alex",
                    "entity": "GOLD_ONLY_IDENTITY",
                    "resolvable": True,
                }
            ],
            "facts": [],
            "tasks": [],
            "headers": [
                {
                    "sender": "Alex <alex@example.test>",
                    "subject": "Preferences",
                    "needs_reply": False,
                },
                {
                    "sender": "Alex <alex@example.test>",
                    "subject": "Old preferences",
                    "needs_reply": False,
                },
                {
                    "sender": "Owner <owner@example.test>",
                    "to": ["Alex <alex@example.test>"],
                    "subject": "Re: Preferences",
                    "labels": ["SENT"],
                    "needs_reply": False,
                },
            ],
            "evaluated_at": NOW.isoformat(),
        }
    )
    assessment: dict[str, Any] = {
        "summary": "Alex prefers tea.",
        "reason": "A preference update.",
        "topics": [],
        "content_importance": 0.1,
        "relationship_importance": 0,
        "urgency": 0,
        "needs_reply": False,
        "bulk": False,
        "supported_evidence": ["Alex prefers tea."],
    }
    fact = {
        "message_id": "message-0",
        "quote": "Alex prefers tea.",
        "belief_type": "preference",
        "subject": "Alex preference",
        "predicate": "prefers",
        "value": "tea",
        "confidence": 0.4,
    }
    if policy == "email-semantic@2":
        assessment["people_facts"] = [
            {
                **fact,
                "people": {
                    "mentions": [
                        {
                            "key": "alex",
                            "source_event_id": 1,
                            "start": 0,
                            "end": 4,
                            "text": "Alex",
                            "display_name": "Alex",
                            "identifier_kind": "name",
                            "identifier_value": "Alex",
                            "namespace": "owner",
                            "context": "",
                            "role": "subject",
                            "referent_key": None,
                        }
                    ],
                    "organizations": [],
                    "relationship": None,
                    "commitment": None,
                },
            }
        ]
    else:
        assessment["semantic_facts"] = [fact]
    reply: dict[str, Any] = {
        "summary": "The owner replied to Alex.",
        "reason": "A reply the owner sent.",
        "topics": [],
        "content_importance": 0.1,
        "relationship_importance": 0,
        "urgency": 0,
        "needs_reply": False,
        "bulk": False,
        "supported_evidence": ["Thanks Alex, see you Friday."],
        "people_facts" if policy == "email-semantic@2" else "semantic_facts": [],
    }
    fake = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=json.dumps(reply)),
                ScriptedTurn(text=json.dumps(assessment)),
            ]
        ),
        FixedClock(NOW),
    )
    provider = BudgetedProvider(fake, EvaluationBudget(Decimal(1), tmp_path / "cost.jsonl"))
    actual = await evaluate_email_case(
        memory_settings(),
        case,
        policy=policy,
        repeat=0,
        model_policy="deterministic",
        policy_profile="default",
        provider=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
    )
    assert actual.people.provider_calls == actual.assessment_calls == 2
    assert actual.failed_runs == actual.automatic_older_mail_capture == 0
    assert [item.event for item in actual.assessments] == [2, 0]
    serialized = "\n".join(request.model_dump_json() for request in fake.requests)
    assert "GOLD_ONLY_IDENTITY" not in serialized and "once liked coffee" not in serialized
    assert "Current assessment time:" in serialized and "Alex prefers tea" in serialized
    assert all(request.tools == [] for request in fake.requests)
    if policy == "email-semantic@2":
        assert len(actual.people.facts) == len(actual.people.mentions) == 1
        assert actual.people.mentions[0].person_id is not None
        assert actual.people.facts[0].evidence_events == [0]
        assert actual.people.facts[0].predicate == "preference"
        assert actual.people.facts[0].authority is not None
        assert actual.people.facts[0].authority.value == "inferred"
        assert actual.people.facts[0].derivation is not None
        assert actual.people.facts[0].derivation.value == "hypothesis"


def test_live_email_comparison_refuses_without_explicit_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    monkeypatch.delenv("RUN_LIVE_MODEL_TESTS", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "email-people",
            "--run",
            "--output",
            str(tmp_path / "run"),
            "--model-policy",
            "balanced",
            "--build-ref",
            "b" * 40,
            "--max-cost-usd",
            "1",
        ],
    )
    assert result.exit_code == 1
    assert "RUN_LIVE_MODEL_TESTS=1" in result.output
    assert not (tmp_path / "run").exists()


async def test_budgeted_email_smoke_writes_actual_calls_and_keeps_activation_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core import bootstrap
    from agent_core.evals import email_people_execution as execution
    from agent_core.evals.email_people import load_email_corpora
    from agent_core.model.registry import StaticModelRouter

    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    checked = []
    monkeypatch.setattr(execution, "require_committed_tree", lambda root, ref: checked.append(ref))

    async def resolve(*args: Any, **kwargs: Any) -> ResolvedModel:
        return ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    def adapters(*args: Any, **kwargs: Any) -> dict[str, FakeModelProvider]:
        return {
            "fake": FakeModelProvider(
                FakeModelScript(
                    turns=[
                        ScriptedTurn(
                            text=json.dumps(
                                {
                                    "summary": "Preference update",
                                    "reason": "Information only",
                                    "topics": [],
                                    "content_importance": 0.1,
                                    "relationship_importance": 0,
                                    "urgency": 0,
                                    "needs_reply": False,
                                    "bulk": False,
                                    "supported_evidence": [],
                                }
                            )
                        )
                    ]
                    * 2
                ),
                FixedClock(NOW),
            )
        }

    monkeypatch.setattr(StaticModelRouter, "resolve", resolve)
    monkeypatch.setattr(bootstrap, "_provider_adapters", adapters)
    root = Path.cwd()
    development, _, _ = load_email_corpora(root)
    output = tmp_path / "run"
    result = await execution.run_comparison(
        root,
        output=output,
        model_policy="deterministic",
        policy_profile="default",
        build_ref="b" * 40,
        maximum_cost=Decimal("1"),
        repeats=3,
        development_case=development.cases[0].id,
        settings=memory_settings(),
    )
    assert checked == ["b" * 40]
    assert result["state"] == "completed" and result["provider_calls"] == 12
    assert result["activation_evidence"] is False
    assert result["ordinary_email_benchmarks_complete"] is False
    assert Decimal(str(result["reserved_usd"])) == 0
    observations = json.loads((output / "observations.json").read_text())
    assert len(observations) == 6
    assert all(item["assessment_calls"] == 2 and item["failed_runs"] == 0 for item in observations)
    journal = [
        json.loads(line) for line in (output / "provider-costs.jsonl").read_text().splitlines()
    ]
    assert len(journal) == 24 and journal[0]["outcome"] == "reserved"
    assert json.loads((output / "scores.json").read_text())["smoke_only"] is True


def test_evaluation_settings_disable_live_communication_connectors(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_core.evals.memory_distillation import _evaluation_settings

    configured = replace(
        memory_settings(),
        email_enabled=True,
        email_mode_enabled=True,
        call_enabled=True,
        call_ingress_enabled=True,
        call_notifications_enabled=True,
        email_semantic_evidence=Path("private.json"),
    )
    isolated = _evaluation_settings(configured, tmp_path)
    assert not any(
        (
            isolated.email_enabled,
            isolated.email_mode_enabled,
            isolated.call_enabled,
            isolated.call_ingress_enabled,
            isolated.call_notifications_enabled,
        )
    )
    assert isolated.email_semantic_evidence is None


def test_email_scoring_requires_complete_repeats_and_reports_reply_regressions(
    tmp_path: Path,
) -> None:
    from agent_core.evals.email_people import (
        EmailPeopleObservation,
        ObservedAssessment,
        load_email_corpora,
        score_observations,
    )
    from agent_core.evals.people import Observation

    root = Path.cwd()
    development, holdout, _ = load_email_corpora(root)
    rows = []
    for repeat in range(3):
        for case in [*development.cases, *holdout.cases]:
            assessments = [
                ObservedAssessment(
                    event=index,
                    needs_reply=header.needs_reply,
                    grounded=True,
                    content_importance=0.1,
                )
                for index, header in enumerate(case.headers)
                if (case.evaluated_at - case.events[index].occurred_at).total_seconds()
                <= 90 * 86400
            ]
            for policy in ("email-semantic@1", "email-semantic@2"):
                candidate = policy == "email-semantic@2"
                rows.append(
                    EmailPeopleObservation(
                        policy=policy,
                        people=Observation(
                            case_id=case.id,
                            pipeline="full-people",
                            repeat=repeat,
                            mentions=[],
                            facts=[],
                            tasks=[],
                            provider_calls=len(assessments),
                            cost_usd="0",
                        ),
                        assessments=[] if candidate else assessments,
                        draft_completion_failures=0,
                        authority_failures=0,
                        automatic_older_mail_capture=0,
                        assessment_calls=len(assessments),
                        failed_runs=0,
                    ).model_dump(mode="json")
                )
    path = tmp_path / "observations.json"
    path.write_text(json.dumps(rows))
    report = score_observations(root, path)
    assert report["activation_evidence"] is False
    assert report["ordinary_email_benchmarks_complete"] is False
    summaries = report["reports"]
    assert isinstance(summaries, list) and len(summaries) == 12
    for item in summaries:
        assert item["extra_provider_calls"] == 0
        if item["policy"] == "email-semantic@2":
            assert item["reply_decision_regressions"] == item["ordinary_cases"] > 0
            assert item["ordinary_passed"] == 0
        else:
            assert item["reply_decision_regressions"] == 0
            assert item["ordinary_passed"] == item["ordinary_cases"]
    for incomplete in (rows[:-1], [*rows, rows[-1]], rows[: len(rows) // 3]):
        path.write_text(json.dumps(incomplete))
        with pytest.raises(ValueError, match=r"complete repeats|observations"):
            score_observations(root, path)
