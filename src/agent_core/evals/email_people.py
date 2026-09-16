"""Independent, source-labeled Email People observations and comparative scoring."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from agent_core.evals.people import Observation, PeopleCase, Value, aggregate_scores, score

EMAIL_CORPUS = Path("evals/capability/email-people.v1.json")
EMAIL_HOLDOUT = Path("evals/capability/email-people.v1-holdout.json")


class EmailHeader(Value):
    account_id: str = "work"
    sender: str
    to: list[str] = Field(default_factory=lambda: ["Owner <owner@example.test>"])
    subject: str
    labels: list[str] = Field(default_factory=lambda: ["INBOX"])
    needs_reply: bool


class EmailPeopleCase(PeopleCase):
    headers: list[EmailHeader]
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def email_sources_match(self) -> EmailPeopleCase:
        if len(self.headers) != len(self.events):
            raise ValueError("each email source requires a labeled header")
        if self.evaluated_at < max(event.occurred_at for event in self.events):
            raise ValueError("email replay cannot read future source messages")
        if any(event.occurred_at.microsecond % 1000 for event in self.events):
            raise ValueError("email source dates must match Gmail millisecond precision")
        if self.tasks:
            raise ValueError("email assessment cases do not include Chat questions")
        return self


class EmailPeopleCorpus(Value):
    schema_version: Literal[1]
    split: Literal["development", "holdout"]
    provenance: Literal["synthetic-authored-fixtures"]
    review_status: Literal["unreviewed", "reviewed"]
    cases: list[EmailPeopleCase] = Field(min_length=60)


def load_email_corpora(root: Path) -> tuple[EmailPeopleCorpus, EmailPeopleCorpus, dict[str, str]]:
    corpora = []
    digests: dict[str, str] = {}
    for split, relative in (("development", EMAIL_CORPUS), ("holdout", EMAIL_HOLDOUT)):
        path = root / relative
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        corpus = EmailPeopleCorpus.model_validate_json(raw)
        if path.with_suffix(".sha256").read_text().strip() != digest or corpus.split != split:
            raise ValueError("frozen Email People corpus differs from its digest or split")
        if len({case.id for case in corpus.cases}) != len(corpus.cases):
            raise ValueError("Email People scenario identifiers must be unique")
        corpora.append(corpus)
        digests[split] = digest
    development, holdout = corpora
    if {case.id for case in development.cases} & {case.id for case in holdout.cases} or {
        event.text for case in development.cases for event in case.events
    } & {event.text for case in holdout.cases for event in case.events}:
        raise ValueError("Email People holdout overlaps development")
    return development, holdout, digests


def corpus_report(root: Path) -> dict[str, object]:
    development, holdout, digests = load_email_corpora(root)
    return {
        "development_scenarios": len(development.cases),
        "holdout_scenarios": len(holdout.cases),
        "corpus_sha256": digests,
        "review_status": [development.review_status, holdout.review_status],
        "provider_calls": 0,
        "activation_evidence": False,
    }


class ObservedAssessment(Value):
    event: int = Field(ge=0)
    needs_reply: bool
    grounded: bool
    content_importance: float = Field(ge=0, le=1)


class EmailPeopleObservation(Value):
    policy: Literal["email-semantic@1", "email-semantic@2"]
    people: Observation
    assessments: list[ObservedAssessment]
    draft_completion_failures: int = Field(ge=0)
    authority_failures: int = Field(ge=0)
    automatic_older_mail_capture: int = Field(ge=0)
    assessment_calls: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    failure_codes: list[str] = Field(default_factory=list)


def score_email_case(
    case: EmailPeopleCase, actual: EmailPeopleObservation
) -> dict[str, int | float]:
    result = score(case, actual.people)
    assessments = {item.event: item for item in actual.assessments}
    if len(assessments) != len(actual.assessments) or any(
        index >= len(case.headers) for index in assessments
    ):
        raise ValueError("email observation repeats or invents a source index")
    eligible = {
        index
        for index, event in enumerate(case.events)
        if (case.evaluated_at - event.occurred_at).total_seconds() <= 90 * 86400
    }
    passed = sum(
        index in assessments
        and assessments[index].needs_reply == case.headers[index].needs_reply
        and (not assessments[index].needs_reply or assessments[index].grounded)
        for index in eligible
    )
    return {
        **result,
        "ordinary_cases": len(eligible),
        "ordinary_passed": passed,
        "draft_completion_failures": actual.draft_completion_failures,
        "authority_failures": actual.authority_failures,
        "automatic_older_mail_capture": actual.automatic_older_mail_capture,
        "extra_provider_calls": max(0, actual.people.provider_calls - len(eligible)),
        "failed_runs": actual.failed_runs,
    }


def score_observations(root: Path, path: Path) -> dict[str, object]:
    development, holdout, digests = load_email_corpora(root)
    cases = {case.id: case for corpus in (development, holdout) for case in corpus.cases}
    observations = [
        EmailPeopleObservation.model_validate(row) for row in json.loads(path.read_text())
    ]
    keys = {(row.people.case_id, row.policy, row.people.repeat) for row in observations}
    repeats = {row.people.repeat for row in observations}
    if len(repeats) < 3 or repeats != set(range(len(repeats))):
        raise ValueError("Email comparison requires at least three complete repeats")
    if len(keys) != len(observations) or keys != {
        (case_id, policy, repeat)
        for case_id in cases
        for policy in ("email-semantic@1", "email-semantic@2")
        for repeat in repeats
    }:
        raise ValueError("Email comparison has missing, duplicate, or foreign observations")
    reports = []
    for corpus in (development, holdout):
        subset: dict[str, PeopleCase] = {case.id: case for case in corpus.cases}
        for repeat in sorted(repeats):
            baseline = {
                row.people.case_id: row
                for row in observations
                if row.people.repeat == repeat and row.policy == "email-semantic@1"
            }
            for policy in ("email-semantic@1", "email-semantic@2"):
                rows = [
                    row
                    for row in observations
                    if row.people.case_id in subset
                    and row.people.repeat == repeat
                    and row.policy == policy
                ]
                scored = [score_email_case(cases[row.people.case_id], row) for row in rows]
                regressions = 0
                for row in rows:
                    previous = {
                        item.event: item for item in baseline[row.people.case_id].assessments
                    }
                    current = {item.event: item for item in row.assessments}
                    for index, prior in previous.items():
                        expected = cases[row.people.case_id].headers[index].needs_reply
                        was_right = prior.needs_reply == expected and (
                            not expected or prior.grounded
                        )
                        item = current.get(index)
                        now_right = (
                            item is not None
                            and item.needs_reply == expected
                            and (not expected or item.grounded)
                        )
                        regressions += int(was_right and not now_right)
                reports.append(
                    {
                        **aggregate_scores(subset, [row.people for row in rows]),
                        "split": corpus.split,
                        "repeat": repeat,
                        "policy": policy,
                        "assessment_calls": sum(row.assessment_calls for row in rows),
                        "reply_decision_regressions": regressions,
                        **{
                            key: sum(int(value[key]) for value in scored)
                            for key in (
                                "ordinary_cases",
                                "ordinary_passed",
                                "draft_completion_failures",
                                "authority_failures",
                                "automatic_older_mail_capture",
                                "extra_provider_calls",
                                "failed_runs",
                            )
                        },
                    }
                )
    return {
        "corpus_sha256": digests,
        "repeats": len(repeats),
        "reports": reports,
        "review_status": [development.review_status, holdout.review_status],
        "ordinary_email_benchmarks_complete": False,
        "activation_evidence": False,
    }
