"""Verify an existing independent release approval and all final-head statuses."""

from datetime import datetime
from typing import Any, Protocol

from pydantic import Field

from agent_core.domain.general_recall_release import ReleaseValue
from agent_core.memory.recall_handoff import HandoffError
from agent_core.operations.recall_handoff_controller import Clearance


class Gates(ReleaseValue):
    """Operator enrollment pins the independent reviewers and the CI issuer."""

    approver_ids: frozenset[int] = Field(min_length=1)
    coderabbit_app_id: int = Field(gt=0)
    circleci_creator_id: int = Field(gt=0)
    required_contexts: frozenset[str] = Field(min_length=1)


class GitHub(Protocol):
    def __call__(self, path: str, query: dict[str, Any] | None = None) -> Any: ...


def resolve_clearance(
    identity: dict[str, str],
    gates: Gates,
    github: GitHub,
) -> Clearance:
    """An enrolled release approver vouches for all review forms through GitHub."""
    import hashlib
    import json

    def sha(data: object) -> str:
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    head = identity["reviewed_head"]
    number = int(identity["pr"])
    merge_time = datetime.fromisoformat(identity["merged_at"])
    reviews = github(f"/pulls/{number}/reviews?per_page=100")
    checks = github(f"/commits/{head}/check-runs?per_page=100")
    combined = github(f"/commits/{head}/status?per_page=100")
    threads_query = """query($number:Int!) { repository(owner:"avitus",name:"veetbot") {
      pullRequest(number:$number) { reviewThreads(first:100) {
        pageInfo { hasNextPage } nodes { id isResolved } } } } }"""
    result = github("/graphql", {"query": threads_query, "variables": {"number": number}})
    threads = result["data"]["repository"]["pullRequest"]["reviewThreads"]
    if (
        identity["merge_tree"] != identity["reviewed_tree"]
        or len(reviews) >= 100
        or checks["total_count"] != len(checks["check_runs"])
        or combined["total_count"] != len(combined["statuses"])
        or combined["sha"] != head
        or result.get("errors")
        or threads["pageInfo"]["hasNextPage"]
        or any(not row["isResolved"] for row in threads["nodes"])
    ):
        raise HandoffError("Incomplete final-head review or CI inventory")

    rabbit = [
        r
        for r in checks["check_runs"]
        if r["name"] == "CodeRabbit" and r["app"]["id"] == gates.coderabbit_app_id
    ]
    if len(rabbit) != 1 or rabbit[0]["conclusion"] != "success" or rabbit[0]["head_sha"] != head:
        raise HandoffError("Missing successful final CodeRabbit review")
    reviewed = datetime.fromisoformat(rabbit[0]["completed_at"])
    latest = {}
    for row in sorted(reviews, key=lambda r: r["id"]):
        if row["state"] in {"APPROVED", "DISMISSED", "CHANGES_REQUESTED"}:
            latest[row["user"]["id"]] = row
    approvals = [
        r
        for uid, r in latest.items()
        if uid in gates.approver_ids
        and uid != int(identity["author_id"])
        and r["state"] == "APPROVED"
        and r["commit_id"] == head
        and reviewed <= datetime.fromisoformat(r["submitted_at"]) <= merge_time
    ]
    if not approvals or any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()):
        raise HandoffError("Independent final-head release approval missing")
    # Combined statuses contain only the latest context but omit its issuer.
    # Join exact status IDs to the raw authenticated statuses for that commit.
    issuers = {
        row["id"]: row["creator"]["id"] for row in github(f"/commits/{head}/statuses?per_page=100")
    }
    statuses = combined["statuses"]
    names = {r["context"] for r in statuses if issuers.get(r["id"]) == gates.circleci_creator_id}
    if (
        not gates.required_contexts <= names
        or combined["state"] != "success"
        or any(
            r["state"] != "success" or datetime.fromisoformat(r["updated_at"]) > merge_time
            for r in statuses
        )
        or any(
            r["status"] != "completed"
            or r["conclusion"] != "success"
            or datetime.fromisoformat(r["completed_at"]) > merge_time
            for r in checks["check_runs"]
        )
    ):
        raise HandoffError("Required checks did not all succeed before merge")
    return Clearance(
        target_release=identity["release"],
        target_commit=identity["merge"],
        reviewed_head=head,
        reviewed_tree=identity["reviewed_tree"],
        merge_tree=identity["merge_tree"],
        final_head_review_clear=True,
        all_review_forms_disposed=True,
        final_head_hosted_ci_passed=True,
        review_receipt_sha256=sha(
            [head, rabbit[0]["id"], sorted(r["id"] for r in approvals), threads]
        ),
        ci_receipt_sha256=sha(
            [
                head,
                sorted((r["context"], r["id"]) for r in statuses),
                sorted(r["id"] for r in checks["check_runs"]),
            ]
        ),
    )
