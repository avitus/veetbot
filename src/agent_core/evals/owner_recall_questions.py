"""Between the owner's stored recall questions and the probes the driver runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime

from agent_core.domain.recall_questions import (
    QuestionEvidence,
    QuestionPathResult,
    QuestionResult,
    RecallQuestion,
    RecallQuestionContent,
)
from agent_core.evals.owner_recall_probes import (
    ExpectedEvidence,
    ForbiddenEvidence,
    LoadedProbes,
    Probe,
    ProbeReport,
)

_DEFAULT_PATHS = ("automatic", "deliberate")


def probe_from_question(question: RecallQuestion) -> Probe:
    """The probe one stored question runs as; its key is the probe identifier."""

    content = question.content
    return Probe(
        id=question.key,
        incident=f"{QUESTION_INCIDENT_PREFIX}{question.split.value}",
        family=content.family,
        question=content.question,
        search_text=content.search_text,
        expected=tuple(
            ExpectedEvidence(
                belief_id=item.belief_id,
                statement_contains=item.statement_contains,
                required=item.required,
            )
            for item in content.expected
        ),
        # Every required fact has to be delivered for the question to pass.
        min_required_rendered=sum(item.required for item in content.expected),
        forbidden=tuple(ForbiddenEvidence(statement_contains=text) for text in content.forbidden),
        delivered_must_contain=content.delivered_must_contain,
        answer_must_contain=content.answer_must_contain,
        answer_must_not_contain=content.answer_must_not_contain,
        notes=content.notes,
    )


def content_from_probe(probe: Probe) -> RecallQuestionContent:
    """Read one probe-file line as question content, refusing what a question cannot hold."""

    if probe.write is not None:
        raise ValueError(f"{probe.id}: a save probe is not a recall question")
    if probe.scope != "general" or tuple(probe.paths) != _DEFAULT_PATHS:
        raise ValueError(f"{probe.id}: a recall question runs both paths in the general scope")
    if probe.latency_ceiling_ms is not None:
        raise ValueError(f"{probe.id}: a recall question carries no latency ceiling")
    return RecallQuestionContent(
        question=probe.question,
        search_text=probe.search_text,
        family=probe.family,
        expected=tuple(
            QuestionEvidence(
                belief_id=item.belief_id,
                statement_contains=item.statement_contains,
                required=item.required,
            )
            for item in probe.expected
        ),
        forbidden=tuple(item.statement_contains for item in probe.forbidden),
        delivered_must_contain=probe.delivered_must_contain,
        answer_must_contain=probe.answer_must_contain,
        answer_must_not_contain=probe.answer_must_not_contain,
        notes=probe.notes,
    )


QUESTION_INCIDENT_PREFIX = "owner-questions-"


def question_fingerprint(probe: Probe) -> str | None:
    """The content fingerprint a stored question's run is bound to, else None.

    It is computed from the probe that ran, so a result can only ever be
    recorded against the content that was tested.
    """

    if not probe.incident.startswith(QUESTION_INCIDENT_PREFIX):
        return None
    try:
        return content_from_probe(probe).criteria_sha256
    except ValueError:
        return None


def loaded_from_questions(questions: Iterable[RecallQuestion], *, label: str) -> LoadedProbes:
    """One probe set from stored questions, in key order, bound to a digest of its content."""

    probes = tuple(
        probe_from_question(question) for question in sorted(questions, key=lambda item: item.key)
    )
    if not probes:
        raise ValueError(f"no questions in the {label} set")
    canonical = json.dumps(
        [probe.model_dump(mode="json") for probe in probes],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return LoadedProbes(
        path=f"database:owner-questions:{label}",
        digest=hashlib.sha256(canonical).hexdigest(),
        probes=probes,
    )


def results_from_report(
    report: ProbeReport, *, report_digest: str, recorded_at: datetime
) -> dict[str, QuestionResult]:
    """Each question's outcome in one probe report, keyed by question key.

    Only observations that ran as stored questions count: an incident probe in
    the same report is not a question, whatever its identifier. A run that does
    not say what content it tested is bound to no question and is left out.
    """

    results: dict[str, QuestionResult] = {}
    for observation in report.probes:
        if not observation.incident.startswith(QUESTION_INCIDENT_PREFIX) or not observation.paths:
            continue
        if observation.content_sha256 is None:
            continue
        results[observation.probe_id] = QuestionResult(
            recorded_at=recorded_at,
            report_digest=report_digest,
            content_sha256=observation.content_sha256,
            paths=tuple(
                QuestionPathResult(
                    path=path.path,
                    stages=tuple(item.stage for item in path.evidence),
                    required_total=sum(item.required for item in path.evidence),
                    required_delivered=path.required_delivered,
                    forbidden_delivered=path.forbidden_delivered,
                    delivered_found=path.delivered_found,
                    answer_correct=path.answer_correct,
                )
                for path in observation.paths
            ),
        )
    return results
