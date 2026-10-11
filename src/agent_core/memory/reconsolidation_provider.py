"""Validate the two provider response batches before any local planning or writes.

This pure boundary has no provider, store, or fallback. A verified candidate
still needs the local kind-specific planner and transactional source recheck.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel

from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.reconsolidation_inputs import PreparedVerificationRequest
from agent_core.domain.reconsolidation_provider import (
    CandidateReason,
    CandidateReview,
    ClauseVerdict,
    OfferedGroup,
    ProposalContext,
    ProposalEnvelope,
    ProposalReview,
    ProposedOperation,
    VerificationEnvelope,
)
from agent_core.domain.security import contains_credential

MAX_RESPONSE_BYTES = 65_536
_PROSE_CREDENTIAL = re.compile(
    r"\b(?:password|api[_ -]?key|secret|credential|access[_ -]?token)\s+(?:is|was)\s+\S+",
    re.IGNORECASE,
)
BatchFailure = Literal[
    "invalid_proposal",
    "invalid_verification",
    "response_too_large",
    "batch_mismatch",
    "missing_verification",
    "incomplete_verification",
]


def review_prepared_verification(
    prepared: PreparedVerificationRequest, verification_json: str | None
) -> ProposalReview:
    """Review only requested verdicts; keep local exclusions outside provider control."""
    validate_prepared_verification(prepared)
    return _review_proposal(
        prepared.context,
        parse_proposal(prepared.context, prepared.proposal.model_dump_json()),
        verification_json,
        prepared.local_rejections,
    )


def validate_prepared_verification(prepared: PreparedVerificationRequest) -> None:
    """Keep the offered candidate projection bound through local application."""
    try:
        if (
            hashlib.sha256(prepared.serialized_request.encode()).hexdigest()
            != prepared.request_digest
        ):
            raise ValueError
        message = prepared.request.conversation[1]
        assert isinstance(message, UserMessage) and len(message.content) == 1
        part = message.content[0]
        assert isinstance(part, TextPart)
        payload = json.loads(part.text)
        excluded = {r.operation.id for r in prepared.local_rejections}
        expected = [
            dict(op.model_dump(mode="json"), trust="untrusted_proposal_data")
            for op in prepared.proposal.operations
            if op.id not in excluded
        ]
        assert payload["schema_version"] == "reconsolidation-verification-input@1"
        assert payload["batch_id"] == str(prepared.context.batch_id)
        assert payload["proposal_digest"] == proposal_digest(prepared.proposal)
        assert payload["operations"] == expected
    except (ValueError, TypeError, KeyError, IndexError, AssertionError):
        raise ProviderBatchError("batch_mismatch") from None


class ProviderBatchError(ValueError):
    """Content-free boundary failure; never retain provider text in an audit."""

    def __init__(self, reason: BatchFailure) -> None:
        self.reason = reason
        super().__init__(reason)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode[Model: BaseModel](model: type[Model], raw: str, failure: BatchFailure) -> Model:
    try:
        if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ProviderBatchError("response_too_large")
        # Pydantic alone accepts repeated JSON keys. Check the bounded raw wire
        # first so a later 'supported' cannot mask an earlier 'unsupported'.
        json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        return model.model_validate_json(raw, strict=True)
    except ProviderBatchError:
        raise
    except (ValueError, RecursionError):
        raise ProviderBatchError(failure) from None


def proposal_digest(proposal: ProposalEnvelope) -> str:
    """Bind verification to the exact parsed proposal, including its batch nonce."""
    return hashlib.sha256(
        json.dumps(
            proposal.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def parse_proposal(context: ProposalContext, raw: str) -> ProposalEnvelope:
    """Parse structure and bind groups; this alone proves no clause is grounded."""
    proposal = _decode(ProposalEnvelope, raw, "invalid_proposal")
    if proposal.batch_id != context.batch_id or set(proposal.group_ids) != {
        group.id for group in context.groups
    }:
        raise ProviderBatchError("batch_mismatch")
    return proposal


def candidate_input_failure(
    operation: ProposedOperation, group: OfferedGroup
) -> CandidateReason | None:
    """Check local identities and hazards before exporting proposed clause text."""

    offered = {item.source.belief_id: item for item in group.sources}
    inputs = {item.belief_id for item in operation.inputs}
    for ref in operation.inputs:
        if ref.belief_id not in offered:
            return "unknown_source"
        if ref != offered[ref.belief_id].source:
            return "stale_source"
    if operation.kind == "no_change" and inputs != set(offered):
        return "invalid_support"
    for clause in operation.clauses:
        if unsafe_provider_text(clause.text):
            return "unsafe_output"
        for support in clause.support:
            if support.belief_id not in inputs:
                return "invalid_support"
            if support.excerpt_id not in offered[support.belief_id].excerpt_ids:
                return "unknown_excerpt"
        if (
            operation.kind in ("merge_equivalent", "infer_connection")
            and {support.belief_id for support in clause.support} != inputs
        ):
            return "invalid_support"
    return None


def _review_candidate(
    operation: ProposedOperation, group: OfferedGroup, verdicts: tuple[ClauseVerdict, ...]
) -> CandidateReview:
    def result(reason: CandidateReason) -> CandidateReview:
        return CandidateReview(operation=operation, reason=reason)

    failure = candidate_input_failure(operation, group)
    if failure is not None:
        return result(failure)
    # Every clause must have passed; an uncertain second clause is not rescued
    # by a supported first clause. Unsupported takes precedence for stable audit.
    if any(v.status == "unsupported" for v in verdicts):
        return result("unsupported_clause")
    if any(v.status == "uncertain" for v in verdicts):
        return result("uncertain_clause")
    return result("verified")


def unsafe_provider_text(text: str) -> bool:
    """Use the same content floor on original input and provider-authored output."""
    return (
        contains_injection_pattern(text)
        or contains_secret_material(text)
        or contains_credential(text)
        or _PROSE_CREDENTIAL.search(text) is not None
    )


def review_provider_batch(
    context: ProposalContext, proposal_json: str, verification_json: str | None
) -> ProposalReview:
    """Reject malformed batches atomically; isolate valid-shaped candidate failures."""
    proposal = parse_proposal(context, proposal_json)
    return _review_proposal(context, proposal, verification_json, ())


def _review_proposal(
    context: ProposalContext,
    proposal: ProposalEnvelope,
    verification_json: str | None,
    local_rejections: tuple[CandidateReview, ...],
) -> ProposalReview:
    operations = {op.id: op for op in proposal.operations}
    rejected = {r.operation.id: r for r in local_rejections}
    if len(rejected) != len(local_rejections) or any(
        r.reason == "verified" or operations.get(r.operation.id) != r.operation
        for r in local_rejections
    ):
        raise ProviderBatchError("batch_mismatch")
    if verification_json is None:
        raise ProviderBatchError("missing_verification")
    verification = _decode(VerificationEnvelope, verification_json, "invalid_verification")
    digest = proposal_digest(proposal)
    if verification.batch_id != context.batch_id or verification.proposal_digest != digest:
        raise ProviderBatchError("batch_mismatch")
    expected = {
        (op.id, index)
        for op in proposal.operations
        if op.id not in rejected
        for index in range(len(op.clauses))
    }
    if expected != {(v.operation_id, v.clause_index) for v in verification.verdicts}:
        raise ProviderBatchError("incomplete_verification")
    groups = {group.id: group for group in context.groups}
    return ProposalReview(
        tenant_id=context.tenant_id,
        principal_id=context.principal_id,
        batch_id=context.batch_id,
        proposal_digest=digest,
        candidates=tuple(
            rejected[op.id]
            if op.id in rejected
            else _review_candidate(
                op,
                groups[op.group_id],
                tuple(v for v in verification.verdicts if v.operation_id == op.id),
            )
            for op in proposal.operations
        ),
    )
