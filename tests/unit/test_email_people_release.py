"""Email activation needs complete repeats and independently paired Email quality."""

from typing import Any

import pytest

from tests.contract.support import NOW
from tests.unit.test_people_release import passing_inputs


def email_evidence_fixture() -> dict[str, Any]:
    from agent_core.evals.people_release import assemble_evidence

    metadata, scores, ordinary, acceptance = passing_inputs()
    people = assemble_evidence(metadata, scores, ordinary, acceptance, labeled_mentions=2100)
    metrics = {
        "scenarios": 60,
        "supported_claim_precision": 1,
        "supported_claim_recall": 1,
        "identity_precision": 1,
        "resolvable_identity_recall": 1,
        "attribution_accuracy": 1,
        "direction_accuracy": 1,
        "temporal_accuracy": 1,
        "false_merges": 0,
        "draft_completion_failures": 0,
        "authority_failures": 0,
        "ordinary_email_regressions": 0,
        "automatic_older_mail_capture": 0,
        "extra_provider_calls": 0,
        "assessment_calls": 60,
        "cost_usd": "1",
    }
    return {
        "policy_version": "email-semantic@2",
        "provider": people.provider,
        "model": people.model,
        "build_ref": people.build_ref,
        "schema_sha256": "1" * 64,
        "implementation_sha256": "2" * 64,
        "corpus_sha256": "3" * 64,
        "holdout_sha256": "4" * 64,
        "people": people.model_dump(mode="json"),
        "run_metrics": [metrics] * 3,
        "holdout_metrics": [metrics] * 3,
        "evaluated_at": NOW.isoformat(),
    }


def test_email_activation_requires_full_ordinary_email_proof() -> None:
    from agent_core.domain.email_people_evidence import EmailPeopleEvidence

    with pytest.raises(ValueError, match="ordinary_email"):
        EmailPeopleEvidence.model_validate(email_evidence_fixture())


@pytest.mark.parametrize("failure", ["repeats", "holdout", "implementation", "ordinary_corpus"])
def test_email_evidence_reports_the_mismatched_binding(failure: str) -> None:
    from agent_core.domain.email_people_evidence import EmailPeopleEvidence
    from agent_core.evals.email_people_release import assemble_email_evidence

    metadata, scores, ordinary, people = assembly_inputs()
    data = assemble_email_evidence(
        metadata, scores, ordinary, people, run_sha256="a" * 64, evaluated_at=NOW
    ).model_dump(mode="json")
    if failure == "repeats":
        data["run_metrics"].append(data["run_metrics"][0])
        message = "repeat counts must match"
    elif failure == "holdout":
        data["holdout_sha256"] = data["corpus_sha256"]
        message = "development and holdout corpora must differ"
    elif failure == "implementation":
        data["ordinary_email"]["candidate_implementation_sha256"] = "f" * 64
        message = "ordinary Email candidate implementation must match"
    else:
        data["ordinary_email"]["candidate_corpus_sha256"] = data["ordinary_email"][
            "baseline_corpus_sha256"
        ]
        message = "ordinary Email baseline and candidate corpora must differ"
    with pytest.raises(ValueError, match=message):
        EmailPeopleEvidence.model_validate(data)


def assembly_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]:
    from agent_core.domain.people_evidence import PeopleFormationEvidence
    from agent_core.evals.email_people_ordinary import compare_ordinary_email
    from tests.unit.test_email_people_ordinary import pair

    data = email_evidence_fixture()
    people = PeopleFormationEvidence.model_validate(data["people"])
    metadata = {
        key: getattr(people, key)
        for key in (
            "provider",
            "model",
            "build_ref",
            "model_policy",
            "policy_profile",
            "policy_version",
            "reasoning_configuration",
            "repeats",
        )
    }
    metadata.update(
        state="completed",
        review_status=["reviewed", "reviewed"],
        reserved_usd="0",
        schema_sha256=data["schema_sha256"],
        implementation_sha256=data["implementation_sha256"],
        corpus_sha256={"development": data["corpus_sha256"], "holdout": data["holdout_sha256"]},
    )
    reports = [
        {
            "policy": "email-semantic@2",
            "split": split,
            "repeat": repeat,
            "cases": 60,
            "direct_fact_precision": 1,
            "direct_fact_recall": 1,
            "identity_precision": 1,
            "resolvable_identity_recall": 1,
            "attribution_accuracy": 1,
            "direction_accuracy": 1,
            "temporal_accuracy": 1,
            "collision_false_merges": 0,
            "draft_completion_failures": 0,
            "authority_failures": 0,
            "reply_decision_regressions": 0,
            "automatic_older_mail_capture": 0,
            "extra_provider_calls": 0,
            "assessment_calls": 60,
            "ordinary_cases": 60,
            "failed_runs": 0,
            "cost_usd": "1",
        }
        for split in ("development", "holdout")
        for repeat in range(3)
    ]
    scores = {
        "reports": reports,
        "review_status": metadata["review_status"],
        "repeats": 3,
        "corpus_sha256": metadata["corpus_sha256"],
    }
    baseline, candidate = pair()
    for labels in (baseline, candidate):
        labels["policy"].update({key: metadata[key] for key in ("provider", "model", "build_ref")})
    candidate["policy"]["implementation_sha256"] = metadata["implementation_sha256"]
    return metadata, scores, compare_ordinary_email(baseline, candidate), people


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "unreviewed",
        "ordinary",
        "tuple",
        "repeat",
        "missing",
        "duplicate",
        "unsettled",
        "assessment",
        "extra_calls",
    ],
)
def test_email_publication_checks_every_repeat_and_ordinary_comparison(failure: str | None) -> None:
    from agent_core.evals.email_people_release import assemble_email_evidence

    metadata, scores, ordinary, people = assembly_inputs()
    if failure == "unreviewed":
        metadata["review_status"] = ["unreviewed", "reviewed"]
    elif failure == "ordinary":
        ordinary["status"] = "pending"
    elif failure == "tuple":
        ordinary["baseline_policy"]["build_ref"] = "f" * 40
    elif failure == "repeat":
        scores["reports"][-1]["direct_fact_recall"] = 0.5
    elif failure == "missing":
        scores["reports"].pop()
    elif failure == "duplicate":
        scores["reports"].append(scores["reports"][-1])
    elif failure == "unsettled":
        metadata["reserved_usd"] = "0.01"
    elif failure == "assessment":
        scores["reports"][-1]["assessment_calls"] = 59
    elif failure == "extra_calls":
        scores["reports"][-1]["extra_provider_calls"] = 1
    if failure:
        with pytest.raises(ValueError):
            assemble_email_evidence(
                metadata, scores, ordinary, people, run_sha256="a" * 64, evaluated_at=NOW
            )
    else:
        result = assemble_email_evidence(
            metadata, scores, ordinary, people, run_sha256="a" * 64, evaluated_at=NOW
        )
        assert len(result.holdout_metrics) == len(result.run_metrics) == 3
        assert result.ordinary_email.threads == 200 and result.ordinary_email.passed
        assert result.ordinary_email.candidate_implementation_sha256 == result.implementation_sha256


def test_email_evidence_cli_identifies_run_without_publishing(tmp_path: Any) -> None:
    import json

    from typer.testing import CliRunner

    from agent_core.cli.main import app
    from agent_core.evals.email_people_release import run_digest

    for name in ("run.json", "observations.json", "provider-costs.jsonl"):
        (tmp_path / name).write_text("{}")
    result = CliRunner().invoke(
        app, ["eval", "email-people-evidence", "--run-directory", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"run_sha256": run_digest(tmp_path)}
    refused = CliRunner().invoke(
        app,
        [
            "eval",
            "email-people-evidence",
            "--run-directory",
            str(tmp_path),
            "--output",
            str(tmp_path / "evidence.json"),
        ],
    )
    assert refused.exit_code == 2
    assert not (tmp_path / "evidence.json").exists()


def test_email_publisher_rescores_complete_inputs_and_refuses_tampering(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic labels exercise publication plumbing, never actual activation."""
    import hashlib
    import json
    from pathlib import Path

    from agent_core import config
    from agent_core.domain.email_people_evidence import EmailPeopleEvidence
    from agent_core.evals import email_people_release as release
    from agent_core.evals.email_people import load_email_corpora
    from agent_core.memory.email_people_evidence import (
        email_people_implementation_digest,
        email_people_schema_digest,
    )
    from agent_core.memory.email_semantics import semantic_implementation_sha256
    from agent_core.memory.people_evidence import implementation_digest, schema_digest
    from tests.unit.test_email_people_ordinary import pair

    root = tmp_path / "repository"
    metadata, _scores, _ordinary, people = assembly_inputs()
    digests = {}
    # Only temporary test copies are marked reviewed. Real corpora stay unreviewed.
    for relative in (
        "evals/capability/email-people.v1.json",
        "evals/capability/email-people.v1-holdout.json",
        "evals/capability/people.v1.json",
        "evals/capability/people.v1-holdout.json",
        str(config.MEMORY_DISTILLATION_CORPUS_PATH),
        str(config.MEMORY_DISTILLATION_HOLDOUT_PATH),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        value = json.loads((Path.cwd() / relative).read_text())
        if "review_status" in value:
            value["review_status"] = "reviewed"
        target.write_text(json.dumps(value))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_suffix(".sha256").write_text(digest + "\n")
        digests[relative] = digest
    monkeypatch.setattr(config, "REPOSITORY_ROOT", root)
    checked = []
    monkeypatch.setattr(
        release, "require_committed_tree", lambda path, head: checked.append((path, head))
    )
    development, holdout, email_digests = load_email_corpora(root)
    metadata.update(
        implementation_sha256=email_people_implementation_digest(),
        schema_sha256=email_people_schema_digest(),
        corpus_sha256=email_digests,
        policy_version=config.shipped_policy_version(),
        evaluated_at=NOW.isoformat(),
        spent_usd="0",
        maximum_cost_usd="100",
    )
    people = people.model_copy(
        update={
            "implementation_sha256": implementation_digest(),
            "schema_sha256": schema_digest(),
            "policy_version": metadata["policy_version"],
            "corpus_sha256": digests["evals/capability/people.v1.json"],
            "holdout_sha256": digests["evals/capability/people.v1-holdout.json"],
            "ordinary_corpus_sha256": digests[str(config.MEMORY_DISTILLATION_CORPUS_PATH)],
            "ordinary_holdout_sha256": digests[str(config.MEMORY_DISTILLATION_HOLDOUT_PATH)],
        }
    )
    rows: list[dict[str, Any]] = []
    for repeat in range(3):
        for case in [*development.cases, *holdout.cases]:
            assessments = [
                {
                    "event": i,
                    "needs_reply": header.needs_reply,
                    "grounded": True,
                    "content_importance": 0.1,
                }
                for i, header in enumerate(case.headers)
                if (case.evaluated_at - case.events[i].occurred_at).total_seconds() <= 90 * 86400
            ]
            for policy in ("email-semantic@1", "email-semantic@2"):
                rows.append(
                    {
                        "policy": policy,
                        "people": {
                            "case_id": case.id,
                            "pipeline": "full-people",
                            "repeat": repeat,
                            "mentions": [
                                {
                                    "event": m.event,
                                    "start": m.start,
                                    "end": m.end,
                                    "person_id": m.entity if m.resolvable else None,
                                }
                                for m in case.mentions
                            ],
                            "organizations": [
                                {
                                    "organization_id": o.entity,
                                    "display_name": o.text,
                                    "evidence_events": [o.event],
                                }
                                for o in case.organizations
                            ],
                            "facts": [
                                {
                                    **f.model_dump(
                                        mode="json",
                                        exclude={"subject", "object", "terms", "evidence_spans"},
                                    ),
                                    "subject_id": f.subject,
                                    "object_id": f.object,
                                    "statement": " ".join(f.terms),
                                }
                                for f in case.facts
                            ],
                            "tasks": [],
                            "provider_calls": len(assessments),
                            "cost_usd": "0",
                        },
                        "assessments": assessments,
                        "assessment_calls": len(assessments),
                        "draft_completion_failures": 0,
                        "authority_failures": 0,
                        "automatic_older_mail_capture": 0,
                        "failed_runs": 0,
                    }
                )
    metadata["provider_calls"] = sum(row["people"]["provider_calls"] for row in rows)
    journal = []
    for index in range(1, metadata["provider_calls"] + 1):
        reservation = {
            "index": index,
            "provider": metadata["provider"],
            "model": metadata["model"],
            "request_sha256": "1" * 64,
            "reservation_usd": "0.01",
            "outcome": "reserved",
        }
        journal.extend(
            [reservation, {**reservation, "outcome": "completed", "actual_cost_usd": "0"}]
        )
    baseline, candidate = pair()
    for labels in (baseline, candidate):
        labels["policy"].update({key: metadata[key] for key in ("provider", "model", "build_ref")})
    baseline["policy"]["implementation_sha256"] = semantic_implementation_sha256()
    candidate["policy"]["implementation_sha256"] = metadata["implementation_sha256"]
    directory = tmp_path / "comparison"
    directory.mkdir()
    originals = {
        "run.json": json.dumps(metadata),
        "observations.json": json.dumps(rows),
        "provider-costs.jsonl": "\n".join(json.dumps(row) for row in journal),
        "people.json": people.model_dump_json(),
        "baseline.json": json.dumps(baseline, default=str),
        "candidate.json": json.dumps(candidate, default=str),
    }
    for name, body in originals.items():
        (directory / name).write_text(body)
    output = tmp_path / "synthetic-test-evidence.json"

    def publish() -> EmailPeopleEvidence:
        return release.publish_evidence(
            root,
            directory,
            directory / "people.json",
            directory / "baseline.json",
            directory / "candidate.json",
            output,
        )

    evidence = publish()
    assert EmailPeopleEvidence.model_validate_json(output.read_text()) == evidence
    assert checked == [(root, metadata["build_ref"])]
    assert evidence.run_sha256 == release.run_digest(directory)
    assert all(metric.supported_claim_recall == 1 for metric in evidence.holdout_metrics)
    with pytest.raises(FileExistsError):
        publish()
    output.unlink()
    for name, replacement in (
        ("run.json", json.dumps({**metadata, "schema_sha256": "f" * 64})),
        ("observations.json", json.dumps(rows[:-1])),
        ("provider-costs.jsonl", "\n".join(json.dumps(row) for row in journal[:-1])),
        ("candidate.json", json.dumps({**candidate, "style": []}, default=str)),
    ):
        (directory / name).write_text(replacement)
        with pytest.raises(ValueError):
            publish()
        assert not output.exists()
        (directory / name).write_text(originals[name])
