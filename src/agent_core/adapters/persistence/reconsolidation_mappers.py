"""Explicit row/domain translations for M32 maintenance records."""

from typing import Any, Literal, cast

from agent_core.adapters.persistence.sqlalchemy_models import (
    ReconsolidationGroupRow,
    ReconsolidationJobRow,
    ReconsolidationSpendRow,
)
from agent_core.domain.reconsolidation import (
    CallAudit,
    ReconsolidationGroup,
    ReconsolidationJob,
    ReconsolidationSpend,
    SourceVersion,
)


def job_to_domain(row: ReconsolidationJobRow) -> ReconsolidationJob:
    return ReconsolidationJob(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        policy=cast(Literal["reconsolidation@1"], row.policy),
        due_day=row.due_day,
        generation=row.generation,
        full_bound=row.full_bound,
        change_bound=row.change_bound,
        full_cursor=row.full_cursor,
        change_cursor=row.change_cursor,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        slice_started_at=row.slice_started_at,
        slice_day=row.slice_day,
        slice_spent=row.slice_spent,
        requests=row.requests,
        claimed_groups=row.claimed_groups,
        operations=row.operations,
        lease_expirations=row.lease_expirations,
        state=cast(Literal["running", "ready", "complete"], row.state),
        revision=row.revision,
    )


def job_values(value: ReconsolidationJob) -> dict[str, Any]:
    return {
        "id": value.id,
        "tenant_id": value.tenant_id,
        "principal_id": value.principal_id,
        "policy": value.policy,
        "due_day": value.due_day,
        "generation": value.generation,
        "full_bound": value.full_bound,
        "change_bound": value.change_bound,
        "full_cursor": value.full_cursor,
        "change_cursor": value.change_cursor,
        "lease_owner": value.lease_owner,
        "lease_token": value.lease_token,
        "lease_expires_at": value.lease_expires_at,
        "slice_started_at": value.slice_started_at,
        "slice_day": value.slice_day,
        "slice_spent": value.slice_spent,
        "requests": value.requests,
        "claimed_groups": value.claimed_groups,
        "operations": value.operations,
        "lease_expirations": value.lease_expirations,
        "state": value.state,
        "revision": value.revision,
    }


def group_to_domain(row: ReconsolidationGroupRow) -> ReconsolidationGroup:
    return ReconsolidationGroup(
        id=row.id,
        job_id=row.job_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        policy=cast(Literal["reconsolidation@1"], row.policy),
        sources=tuple(SourceVersion.model_validate(item) for item in row.sources),
        input_digest=row.input_digest,
        created_at=row.created_at,
        state=cast(
            Literal["pending", "claimed", "no_change", "failed", "stale", "committed"], row.state
        ),
        attempts=row.attempts,
        lease_token=row.lease_token,
        reason=cast(
            Literal[
                "selected",
                "no_change",
                "retry",
                "attempts_exhausted",
                "source_changed",
                "equivalent",
            ],
            row.reason,
        ),
    )


def group_values(value: ReconsolidationGroup) -> dict[str, Any]:
    return {
        "id": value.id,
        "job_id": value.job_id,
        "tenant_id": value.tenant_id,
        "principal_id": value.principal_id,
        "policy": value.policy,
        "sources": [item.model_dump(mode="json") for item in value.sources],
        "input_digest": value.input_digest,
        "created_at": value.created_at,
        "state": value.state,
        "attempts": value.attempts,
        "lease_token": value.lease_token,
        "reason": value.reason,
    }


def spend_to_domain(row: ReconsolidationSpendRow) -> ReconsolidationSpend:
    return ReconsolidationSpend(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        job_id=row.job_id,
        lease_token=row.lease_token,
        day=row.day,
        request_digest=row.request_digest,
        maximum_usd=row.maximum_usd,
        charged_usd=row.charged_usd,
        state=cast(Literal["reserved", "settled", "unknown"], row.state),
        call_audit=None if row.call_audit is None else CallAudit.model_validate(row.call_audit),
    )


def spend_values(value: ReconsolidationSpend) -> dict[str, Any]:
    return {
        "id": value.id,
        "tenant_id": value.tenant_id,
        "principal_id": value.principal_id,
        "job_id": value.job_id,
        "lease_token": value.lease_token,
        "day": value.day,
        "request_digest": value.request_digest,
        "maximum_usd": value.maximum_usd,
        "charged_usd": value.charged_usd,
        "state": value.state,
        "call_audit": None
        if value.call_audit is None
        else value.call_audit.model_dump(mode="json"),
    }
