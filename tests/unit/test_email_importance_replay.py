"""The offline importance replay: leakage-safe, label-only, and non-activating.

Synthetic inputs exercise the runner and cannot establish usefulness.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.domain.judgment import (
    JudgmentAnswer,
    JudgmentFailure,
    JudgmentProviderError,
    JudgmentRequest,
    JudgmentResult,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)
from agent_core.domain.messages import FakeModelScript, ModelUsage, ScriptedTurn
from agent_core.evals import email_importance_replay as replay
from agent_core.evals.email_importance_replay import (
    ArmCounters,
    DecisionRule,
    EmailImportanceBundle,
    bundle_errors,
    candidate_corpus,
    compare_email_importance,
    judged_assessment,
    judgment_request,
    replay_snapshot,
    run_replay,
)
from agent_core.evals.email_quality import EmailQualityCorpus
from tests.integration.m2_support import memory_settings

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
HOLDOUT = START + timedelta(days=10)
OWNER = "owner@example.test"
PROFILE = UUID(int=1)
URGENT, ROUTINE, BULK = UUID(int=101), UUID(int=102), UUID(int=103)
CANDIDATES = [URGENT, ROUTINE, BULK]
SUBJECTS = {
    PROFILE: "Earlier planning thread",
    URGENT: "Decision needed on the term sheet",
    ROUTINE: "Notes from the weekly sync",
    BULK: "Your monthly newsletter",
}
MAIL_CANARY = "SYNTHETIC_PRIVATE_BOARD_DETAIL_917"
FUTURE_CANARY = "future-canary-sent-after-the-observation-time"


def _label(identity: UUID, *, holdout: bool, important: bool) -> dict[str, Any]:
    return {
        "id": str(identity),
        "conversation_id": str(UUID(int=identity.int + 5000)),
        "account": "account_1",
        "split": "holdout" if holdout else "training",
        "source_at": (HOLDOUT + timedelta(hours=1) if holdout else START).isoformat(),
        "relationship": "close_collaborator" if important else "bulk",
        "important": important,
        "content_kind": "actionable",
        "needs_reply": important,
        "unambiguous": True,
        "fully_supported": True,
    }


def corpus_document() -> dict[str, Any]:
    pool = [str(identity) for identity in CANDIDATES]
    return {
        "schema_version": 1,
        "origin": "synthetic",
        "frozen_at": (START - timedelta(days=1)).isoformat(),
        "tuning_started_at": START.isoformat(),
        "holdout_started_at": HOLDOUT.isoformat(),
        "policy": {
            "ranker_version": "email-ranker@1",
            "style_version": "email-style@1",
            "semantic_policy_version": "email-semantic@1",
            "provider": "synthetic",
            "model": "synthetic",
            "build_ref": "0" * 40,
            "implementation_sha256": "0" * 64,
        },
        "threads": [
            _label(PROFILE, holdout=False, important=False),
            _label(URGENT, holdout=True, important=True),
            _label(ROUTINE, holdout=True, important=False),
            _label(BULK, holdout=True, important=False),
        ],
        "snapshots": [
            {
                "id": str(UUID(int=3000 + index)),
                "observed_at": (HOLDOUT + timedelta(days=1 + index)).isoformat(),
                "profile_evidence_through": (START + timedelta(days=1)).isoformat(),
                "profile_thread_ids": [str(PROFILE)],
                "complete_candidate_pool": True,
                "independent_snapshot": True,
                "candidate_ids": pool,
                "ranked_ids": pool,
                "displayed_ids": pool[:1],
                "baselines": {
                    "newest_first": pool,
                    "gmail_important": pool,
                    "without_personalization": pool,
                },
            }
            for index in range(2)
        ],
    }


def bundle_document(corpus_sha256: str) -> dict[str, Any]:
    def thread(identity: UUID, sent_at: datetime, *, later: bool = False) -> dict[str, Any]:
        messages = [
            {
                "id": f"m-{identity.int}",
                "sender": "Pat Example <pat@example.test>",
                "to": [OWNER],
                "subject": SUBJECTS[identity],
                "body": f"{SUBJECTS[identity]}. {MAIL_CANARY}",
                "sent_at": sent_at.isoformat(),
            }
        ]
        if later:
            messages.append(
                {
                    "id": f"m-{identity.int}-later",
                    "sender": "Pat Example <pat@example.test>",
                    "to": [OWNER],
                    "subject": SUBJECTS[identity],
                    "body": FUTURE_CANARY,
                    "sent_at": (HOLDOUT + timedelta(days=30)).isoformat(),
                }
            )
        return {"id": str(identity), "messages": messages}

    return {
        "schema_version": 1,
        "corpus_sha256": corpus_sha256,
        "owner_addresses": {"account_1": OWNER},
        "threads": [
            thread(PROFILE, START),
            thread(URGENT, HOLDOUT + timedelta(hours=1), later=True),
            thread(ROUTINE, HOLDOUT + timedelta(hours=1)),
            thread(BULK, HOLDOUT + timedelta(hours=1)),
        ],
    }


def inputs(tmp_path: Path) -> tuple[EmailQualityCorpus, EmailImportanceBundle, Path, Path]:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    corpus_path, bundle_path = private / "corpus.json", private / "bundle.json"
    corpus_path.write_text(json.dumps(corpus_document()), encoding="utf-8")
    digest = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    bundle_path.write_text(json.dumps(bundle_document(digest)), encoding="utf-8")
    return (
        EmailQualityCorpus.model_validate_json(corpus_path.read_bytes()),
        EmailImportanceBundle.model_validate_json(bundle_path.read_bytes()),
        corpus_path,
        bundle_path,
    )


def _levels(subject: str) -> tuple[float, float, float, float, float]:
    """content, relationship, urgency, needs_reply, bulk for one synthetic subject."""

    if "Decision" in subject:
        return 4.0, 3.0, 4.0, 0.95, 0.02
    if "newsletter" in subject:
        return 0.0, 0.0, 0.0, 0.02, 0.97
    return 1.0, 1.0, 0.0, 0.1, 0.1


def scripted_judge(*, expired: float = 0.0) -> FakeJudgmentProvider:
    def respond(request: JudgmentRequest) -> JudgmentResult:
        state = request.state
        assert isinstance(state, dict)
        thread = state["thread"]
        assert isinstance(thread, dict)
        content, relationship, urgency, reply, bulk = _levels(str(thread["subject"]))
        answers: dict[str, JudgmentAnswer] = {}
        for key, question in request.questions.items():
            if isinstance(question, ScoreQuestion):
                value = {"content_importance": content, "relationship_importance": relationship}
                score = value.get(key, urgency)
                probabilities = tuple(
                    1.0 if index == round(score) else 0.0 for index in range(len(question.levels))
                )
                answers[key] = ScoreAnswer(score=score, probabilities=probabilities, confidence=1)
            else:
                assert isinstance(question, NoulQuestion)
                probability = {"needs_reply": reply, "bulk": bulk, "expired": expired}.get(key, 0.0)
                answers[key] = NoulAnswer(probability=probability)
        return JudgmentResult(
            answers=answers,
            usage=ModelUsage(
                input_tokens=900, cost=Decimal("0.0000378"), provider="fake", model="scripted"
            ),
        )

    return FakeJudgmentProvider(respond)


def scripted_model() -> FakeModelScript:
    """One production assessment per candidate, in candidate order, for two snapshots."""

    def turn(identity: UUID) -> ScriptedTurn:
        content, relationship, urgency, reply, bulk = _levels(SUBJECTS[identity])
        return ScriptedTurn(
            text=json.dumps(
                {
                    "summary": "synthetic",
                    "reason": "synthetic",
                    "topics": [],
                    "content_importance": content / 4,
                    "relationship_importance": relationship / 4,
                    "urgency": urgency / 4,
                    "needs_reply": reply >= 0.5,
                    "bulk": bulk >= 0.5,
                    "attention_expires_at": None,
                    "reply_blocked_reason": None,
                    "supported_evidence": [SUBJECTS[identity]],
                    "relationship_memory_ids": [],
                    "semantic_facts": [],
                }
            )
        )

    return FakeModelScript(turns=[turn(identity) for identity in CANDIDATES * 2])


def test_a_bundle_is_refused_for_each_way_it_could_leak_or_mislead(tmp_path: Path) -> None:
    corpus, bundle, corpus_path, bundle_path = inputs(tmp_path)
    digest = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    root = tmp_path / "repository"
    root.mkdir()

    def errors(candidate: EmailImportanceBundle, *, path: Path = bundle_path) -> list[str]:
        return bundle_errors(
            corpus, candidate, corpus_sha256=digest, bundle_path=path, repository_root=root
        )

    assert errors(bundle) == []
    assert "outside the repository" in errors(bundle, path=root / "bundle.json")[0]
    assert (
        "different corpus digest"
        in errors(bundle.model_copy(update={"corpus_sha256": "f" * 64}))[0]
    )
    foreign = bundle.model_copy(
        update={
            "threads": [*bundle.threads, bundle.threads[0].model_copy(update={"id": UUID(int=9)})]
        }
    )
    assert errors(foreign) == ["1 bundle thread(s) carry no label in the corpus"]
    assert errors(bundle.model_copy(update={"threads": bundle.threads[:2]})) == [
        "2 replayed thread(s) are missing from the source bundle"
    ]
    late = bundle.threads[2].model_copy(
        update={
            "messages": [
                message.model_copy(update={"sent_at": HOLDOUT + timedelta(days=60)})
                for message in bundle.threads[2].messages
            ]
        }
    )
    unobservable = bundle.model_copy(
        update={"threads": [*bundle.threads[:2], late, bundle.threads[3]]}
    )
    assert errors(unobservable) == ["2 candidate(s) have no message at their observation time"]
    assert errors(bundle.model_copy(update={"owner_addresses": {}})) == [
        "1 account(s) have no owner address in the source bundle"
    ]
    # No identifier ever appears in a bundle error.
    assert all(str(identity) not in " ".join(errors(foreign)) for identity in (PROFILE, URGENT))
    with pytest.raises(ValidationError):
        EmailImportanceBundle.model_validate({**bundle.model_dump(mode="json"), "note": "extra"})


def test_the_judgment_request_carries_evidence_and_no_local_identifier() -> None:
    evidence = {
        "thread": {
            "id": "11111111-1111-1111-1111-111111111111",
            "account_id": "account_1",
            "subject": "Decision needed on the term sheet",
            "messages": [{"body": "Please decide by Friday."}],
        },
        "learning": {
            "owner_feedback": [],
            "reply_partner_counts": {"pat@example.test": 2.0},
            "shared_memories": [{"id": "m:1", "statement": "Pat is a portfolio founder."}],
        },
    }
    request = judgment_request(evidence, HOLDOUT)

    rendered = request.model_dump_json()
    assert "11111111-1111-1111-1111-111111111111" not in rendered
    assert "account_1" not in rendered
    assert "Please decide by Friday." in rendered
    assert sorted(request.questions) == [
        "bulk",
        "content_importance",
        "expired",
        "memory_0",
        "needs_reply",
        "relationship_importance",
        "urgency",
    ]
    questions = json.dumps({k: q.model_dump() for k, q in request.questions.items()})
    assert "Please decide" not in questions and "term sheet" not in questions


def test_typed_answers_map_onto_the_production_assessment_without_prose() -> None:
    evidence = {
        "thread": {"subject": "Decision needed on the term sheet", "messages": [{"body": "x"}]},
        "learning": {"shared_memories": [{"id": "m:1", "statement": "s"}]},
    }
    request = judgment_request(evidence, HOLDOUT)
    answers: dict[str, JudgmentAnswer] = {}
    for key, question in request.questions.items():
        if isinstance(question, ScoreQuestion):
            answers[key] = ScoreAnswer(score=3.0, probabilities=(0, 0, 0, 1, 0), confidence=1)
        else:
            answers[key] = NoulAnswer(probability=0.9)
    result = JudgmentResult(answers=answers, usage=ModelUsage(provider="fake", model="scripted"))

    with_expiry = judged_assessment(result, evidence, HOLDOUT, expiry=True)
    without = judged_assessment(result, evidence, HOLDOUT, expiry=False)

    assert (with_expiry.summary, with_expiry.reason, with_expiry.topics) == ("", "", [])
    assert with_expiry.content_importance == with_expiry.urgency == 0.75
    assert with_expiry.needs_reply is True and with_expiry.bulk is True
    assert with_expiry.relationship_memory_ids == ["m:1"]
    assert with_expiry.supported_evidence == ["Decision needed on the term sheet"]
    assert with_expiry.attention_expires_at == HOLDOUT
    assert without.attention_expires_at is None


async def test_both_arms_rank_the_pool_in_production_order_with_no_future_mail(
    tmp_path: Path,
) -> None:
    corpus, bundle, _corpus_path, _bundle_path = inputs(tmp_path)
    snapshot = corpus.snapshots[0]
    judge = scripted_judge()
    produced, judged = ArmCounters(), ArmCounters()

    production = await replay_snapshot(
        memory_settings(),
        corpus,
        bundle,
        snapshot,
        "production",
        counters=produced,
        build_options={"script": scripted_model()},
    )
    judgment = await replay_snapshot(
        memory_settings(),
        corpus,
        bundle,
        snapshot,
        "judgment",
        counters=judged,
        build_options={"judgment_provider_override": judge},
    )

    assert production == judgment == [URGENT, ROUTINE, BULK]
    assert (produced.assessed, produced.model_calls, produced.judgment_requests) == (3, 3, 0)
    assert (judged.assessed, judged.model_calls, judged.judgment_requests) == (3, 0, 3)
    assert judged.judgment_input_tokens == 2700
    sent = "".join(request.model_dump_json() for request in judge.requests)
    # The mail reaches the judge, the profile thread is never assessed, and nothing
    # sent after the observation time is visible.
    assert MAIL_CANARY in sent
    assert FUTURE_CANARY not in sent
    assert SUBJECTS[PROFILE] not in sent


async def test_a_judgment_failure_abstains_like_a_rejected_production_result(
    tmp_path: Path,
) -> None:
    corpus, bundle, _corpus_path, _bundle_path = inputs(tmp_path)
    failing = FakeJudgmentProvider(
        [JudgmentProviderError(JudgmentFailure.PROVIDER_UNAVAILABLE, retryable=True)] * 3
    )
    counters = ArmCounters()

    ranking = await replay_snapshot(
        memory_settings(),
        corpus,
        bundle,
        corpus.snapshots[0],
        "judgment",
        counters=counters,
        build_options={"judgment_provider_override": failing},
    )

    assert sorted(ranking, key=lambda item: item.int) == CANDIDATES
    assert counters.judgment_errors == 3
    assert counters.abstained == 3
    assert counters.failed_runs == 0


async def test_an_expired_conversation_drops_only_in_the_arm_that_can_say_so(
    tmp_path: Path,
) -> None:
    corpus, bundle, _corpus_path, _bundle_path = inputs(tmp_path)
    with_expiry, without = ArmCounters(), ArmCounters()

    await replay_snapshot(
        memory_settings(),
        corpus,
        bundle,
        corpus.snapshots[0],
        "judgment",
        counters=with_expiry,
        build_options={"judgment_provider_override": scripted_judge(expired=0.99)},
    )
    await replay_snapshot(
        memory_settings(),
        corpus,
        bundle,
        corpus.snapshots[0],
        "judgment_without_expiry",
        counters=without,
        build_options={"judgment_provider_override": scripted_judge(expired=0.99)},
    )

    assert with_expiry.expired_at_observation == 3
    assert (without.expired_at_observation, without.future_expiries) == (0, 0)


def test_a_candidate_corpus_keeps_the_frozen_labels_and_pairs_with_production(
    tmp_path: Path,
) -> None:
    corpus, _bundle, _corpus_path, _bundle_path = inputs(tmp_path)
    rankings = {snapshot.id: [BULK, URGENT, ROUTINE] for snapshot in corpus.snapshots}
    production = candidate_corpus(
        corpus,
        {snapshot.id: list(CANDIDATES) for snapshot in corpus.snapshots},
        provider="production",
        model="production",
        ranker_version="replay-production",
    )
    candidate = candidate_corpus(
        corpus, rankings, provider="judgment", model="judgment", ranker_version="replay-judgment"
    )

    assert candidate.threads == corpus.threads
    assert [s.baselines for s in candidate.snapshots] == [s.baselines for s in corpus.snapshots]
    assert all(s.displayed_ids == s.ranked_ids[:5] for s in candidate.snapshots)
    comparison = compare_email_importance(
        production, candidate, failures=0, attempts=6, rule=DecisionRule()
    )
    assert comparison["activation_evidence"] is False
    assert comparison["verdict"] == "pending"
    assert comparison["snapshots"] == 2
    assert str(URGENT) not in json.dumps(comparison)
    relabelled = candidate.model_copy(
        update={
            "threads": [
                candidate.threads[0],
                candidate.threads[1].model_copy(update={"important": False}),
                *candidate.threads[2:],
            ]
        }
    )
    with pytest.raises(ValueError, match="same frozen labels"):
        compare_email_importance(
            production, relabelled, failures=0, attempts=6, rule=DecisionRule()
        )


async def test_a_run_follows_the_live_conventions_and_writes_no_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _corpus, _bundle, corpus_path, bundle_path = inputs(tmp_path)
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(replay, "require_committed_tree", lambda root, build_ref: None)
    arguments: dict[str, Any] = {
        "corpus_path": corpus_path,
        "bundle_path": bundle_path,
        "build_ref": "0" * 40,
        "maximum_cost": Decimal("1"),
        "settings": memory_settings(),
        "arms": ("production", "judgment"),
        "build_options": {
            "script": scripted_model(),
            "judgment_provider_override": scripted_judge(),
        },
    }

    monkeypatch.delenv("RUN_LIVE_MODEL_TESTS", raising=False)
    with pytest.raises(ValueError, match="RUN_LIVE_MODEL_TESTS=1"):
        await run_replay(root, output=tmp_path / "refused", **arguments)
    monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
    with pytest.raises(ValueError, match="positive cost ceiling"):
        await run_replay(
            root, output=tmp_path / "free", **{**arguments, "maximum_cost": Decimal("0")}
        )

    output = tmp_path / "run"
    report = await run_replay(root, output=output, **arguments)

    assert report["state"] == "completed"
    assert report["activation_evidence"] is False
    assert report["arms"]["judgment"]["judgment_requests"] == 6
    assert report["comparisons"]["judgment"]["verdict"] == "pending"
    assert (output.stat().st_mode & 0o777) == 0o700
    written = "".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()
    )
    for private in (MAIL_CANARY, FUTURE_CANARY, OWNER, "pat@example.test", *SUBJECTS.values()):
        assert private not in written
    with pytest.raises(FileExistsError):
        await run_replay(root, output=output, **arguments)


def test_the_cli_checks_a_bundle_and_reports_counts_only(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    _corpus, _bundle, corpus_path, bundle_path = inputs(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "email-importance",
            "--check-bundle",
            "--corpus",
            str(corpus_path),
            "--bundle",
            str(bundle_path),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report == {
        "activation_evidence": False,
        "bundle_threads": 4,
        "candidates": 6,
        "origin": "synthetic",
        "snapshots": 2,
        "usable": True,
    }


def test_the_cli_requires_exactly_one_mode_and_the_live_options_for_a_run(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    _corpus, _bundle, corpus_path, bundle_path = inputs(tmp_path)
    both = CliRunner().invoke(app, ["eval", "email-importance", "--check-bundle", "--run"])
    incomplete = CliRunner().invoke(
        app,
        [
            "eval",
            "email-importance",
            "--run",
            "--corpus",
            str(corpus_path),
            "--bundle",
            str(bundle_path),
        ],
    )

    assert both.exit_code != 0 and "exactly one" in both.output
    assert incomplete.exit_code != 0 and "--max-cost-usd" in incomplete.output


def test_the_cli_refuses_a_live_run_without_the_opt_in_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    _corpus, _bundle, corpus_path, bundle_path = inputs(tmp_path)
    monkeypatch.delenv("RUN_LIVE_MODEL_TESTS", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "email-importance",
            "--run",
            "--corpus",
            str(corpus_path),
            "--bundle",
            str(bundle_path),
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


def test_a_malformed_bundle_is_refused_without_quoting_private_mail(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    _corpus, _bundle, corpus_path, bundle_path = inputs(tmp_path)
    document = json.loads(bundle_path.read_text(encoding="utf-8"))
    document["threads"][0]["messages"][0]["unexpected"] = MAIL_CANARY
    bundle_path.write_text(json.dumps(document), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "email-importance",
            "--check-bundle",
            "--corpus",
            str(corpus_path),
            "--bundle",
            str(bundle_path),
        ],
    )

    assert result.exit_code == 1
    assert "does not match its schema" in result.output
    assert MAIL_CANARY not in result.output
    assert OWNER not in result.output
