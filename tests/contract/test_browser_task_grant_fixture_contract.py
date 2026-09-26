"""ADR-0129: the shared server-client contract fixture for task grants.

`tests/fixtures/api/browser_task_grant_contract.json` holds one example of
each wire shape of 0129-design section 15. The Apple client decodes the same
file (copied into its test bundle with its checksum asserted); this suite
holds the server's domain models to it, and the tracks that add the routes,
the approval view and the events extend it rather than invent new shapes.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain.browser import BrowserActionConsequence, BrowserActionKind
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_ACTION_KINDS,
    TASK_GRANT_MAX_ACTIONS,
    TASK_GRANT_MAX_TYPED_CHARACTERS,
    TASK_GRANT_NOT_COVERED_REASONS,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    BrowserTaskGrantOffer,
    BrowserTaskGrantScope,
    BrowserTaskGrantStatus,
    BrowserTaskGrantView,
    TaskGrantEcho,
    TaskGrantNotCovered,
    task_grant_id_for_approval,
)
from agent_core.domain.views import Page

FIXTURE = Path(__file__).parents[1] / "fixtures" / "api" / "browser_task_grant_contract.json"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def rejected_views(examples: list[dict[str, Any]]) -> list[str]:
    rejected = []
    for example in examples:
        try:
            BrowserTaskGrantView.model_validate(example["view"])
        except ValidationError:
            rejected.append(example["name"])
    return rejected


def grant_behind(view: dict[str, Any]) -> BrowserTaskGrant:
    """The stored grant a server would hold for one fixture view."""

    ended = view["ended_at"]
    return BrowserTaskGrant(
        id=view["id"],
        tenant_id="tenant-a",
        principal_id="principal-a",
        session_id=view["session_id"],
        profile_id=view["profile_id"],
        profile_generation=4,
        agent_version="agent-version",
        policy_version="policy-version",
        origin=view["origin"],
        path_prefix=view["path_prefix"],
        max_actions=view["max_actions"],
        actions_used=view["actions_used"],
        typed_characters=view["typed_characters"],
        approval_id=view["approval_id"],
        approved_by=view["approved_by"],
        created_at=view["created_at"],
        expires_at=view["expires_at"],
        last_used_at=view["last_used_at"],
        revoked_at=ended if view["end_reason"] == "revoked" else None,
        ended_at=ended,
        end_reason=view["end_reason"],
    )


def test_every_task_grant_view_example_validates_and_round_trips(
    contract: dict[str, Any],
) -> None:
    examples = contract["task_grant_views"]

    assert rejected_views(examples) == []
    changed = [
        example["name"]
        for example in examples
        if BrowserTaskGrantView.model_validate(example["view"]).model_dump(mode="json")
        != example["view"]
    ]
    assert changed == []
    assert {example["view"]["status"] for example in examples} == {
        status.value for status in BrowserTaskGrantStatus
    }
    page = Page[BrowserTaskGrantView].model_validate(contract["task_grant_page"])
    assert page.model_dump(mode="json") == contract["task_grant_page"]


def test_the_server_derives_each_view_from_its_stored_grant(contract: dict[str, Any]) -> None:
    as_of = datetime.fromisoformat(contract["as_of"])

    derived = {
        example["name"]: BrowserTaskGrantView.from_grant(
            grant_behind(example["view"]), now=as_of
        ).model_dump(mode="json")
        for example in contract["task_grant_views"]
    }

    assert derived == {example["name"]: example["view"] for example in contract["task_grant_views"]}


def test_a_view_never_contradicts_its_end(contract: dict[str, Any]) -> None:
    active = dict(contract["task_grant_views"][0]["view"])
    refused = {
        "active with an end reason": {**active, "end_reason": "revoked"},
        "revoked without an end": {**active, "status": "revoked"},
        "ended with another status": {
            **active,
            "status": "active",
            "end_reason": "scope_removed",
            "ended_at": "2026-09-25T18:05:00Z",
        },
        "an end time without a reason": {**active, "ended_at": "2026-09-25T18:05:00Z"},
        "a tenant field": {**active, "tenant_id": "tenant-a"},
        "a page URL": {**active, "page_url": "https://www.duolingo.com/lesson/unit-3?x=1"},
        "a prefix of two segments": {**active, "path_prefix": "/lesson/unit"},
    }
    accepted = []
    for name, value in refused.items():
        try:
            BrowserTaskGrantView.model_validate(value)
        except ValidationError:
            continue
        accepted.append(name)
    assert accepted == []


def test_approval_examples_carry_offers_reasons_and_views_the_domain_accepts(
    contract: dict[str, Any],
) -> None:
    keys = set(contract["approval_view_keys"])
    decisions = set(contract["decisions"])
    consequences = {consequence.value for consequence in BrowserActionConsequence}
    for example in contract["approval_views"]:
        approval = example["approval"]
        name = example["name"]
        assert approval["tool_name"] == "browser.act", name
        assert approval["decision"] is None or approval["decision"] in decisions, name
        arguments = approval["arguments"]
        assert arguments["view"] == "browser.act.v1", name
        assert set(arguments) <= keys, name
        assert "ref" not in arguments and "expected_revision" not in arguments, name
        assert BrowserActionKind(arguments["kind"]), name
        if arguments["described"]:
            assert arguments["consequence"] in consequences, name
            host = urlsplit(arguments["page_origin"]).hostname
            assert host is not None and host in approval["action_summary"], name
            page_authored = [
                arguments.get(key)
                for key in ("page_title", "element_name", "element_text", "element_context")
            ]
            assert [
                text for text in page_authored if text and text in approval["action_summary"]
            ] == [], name
        offer = approval["task_grant_offer"]
        if offer is not None:
            parsed = BrowserTaskGrantOffer.model_validate(offer)
            assert parsed.model_dump(mode="json") == offer, name
            assert parsed.action_kinds == TASK_GRANT_ACTION_KINDS, name
            BrowserTaskGrantScope(origin=parsed.origin, path_prefix=parsed.path_prefix)
        not_covered = approval["task_grant_not_covered"]
        if not_covered is not None:
            TaskGrantNotCovered.model_validate(not_covered)
        resolved_for_task = approval["decision"] == "approve_for_task"
        assert (approval["task_grant_id"] is not None) == resolved_for_task, name
        if resolved_for_task:
            assert offer is not None, name
            assert approval["status"] == "APPROVED", name
            assert UUID(approval["task_grant_id"]) == task_grant_id_for_approval(
                UUID(approval["id"])
            ), name


def test_resolve_bodies_repeat_the_offer_only_with_approve_for_task(
    contract: dict[str, Any],
) -> None:
    resolved_offer = next(
        example["approval"]["task_grant_offer"]
        for example in contract["approval_views"]
        if example["approval"]["decision"] == "approve_for_task"
    )
    for example in contract["resolve_bodies"]:
        body = example["body"]
        assert body["decision"] in contract["decisions"]
        assert ("task_grant" in body) == (body["decision"] == "approve_for_task")
        if "task_grant" in body:
            echo = TaskGrantEcho.model_validate(body["task_grant"])
            assert (echo.origin, echo.path_prefix) == (
                resolved_offer["origin"],
                resolved_offer["path_prefix"],
            )


def test_event_examples_name_the_grant_and_its_fixed_limits(contract: dict[str, Any]) -> None:
    events = contract["events"]
    created = events["browser.task_grant.created"]
    ended = events["browser.task_grant.ended"]
    authorized = events["tool.call.authorized"]
    resolved = events["approval.resolved"]

    assert set(created) == {
        "grant_id",
        "approval_id",
        "origin",
        "path_prefix",
        "expires_at",
        "max_actions",
        "max_typed_characters",
    }
    assert UUID(created["grant_id"]) == task_grant_id_for_approval(UUID(created["approval_id"]))
    assert (created["max_actions"], created["max_typed_characters"]) == (
        TASK_GRANT_MAX_ACTIONS,
        TASK_GRANT_MAX_TYPED_CHARACTERS,
    )
    BrowserTaskGrantScope(origin=created["origin"], path_prefix=created["path_prefix"])
    assert set(ended) == {"grant_id", "reason", "actions_used", "typed_characters"}
    assert BrowserTaskGrantEndReason(ended["reason"])
    assert authorized["authorization_kind"] == "browser_task_grant"
    assert authorized["authorization_ref"] == created["grant_id"]
    assert authorized["authorization_use"] >= 1
    assert set(authorized["authorization_view"]) <= set(contract["approval_view_keys"])
    assert resolved["task_grant_id"] == created["grant_id"]
    assert resolved["resolution"] == "approve_for_task"


def test_closed_lists_match_the_domain(contract: dict[str, Any]) -> None:
    assert contract["not_covered_reasons"] == sorted(TASK_GRANT_NOT_COVERED_REASONS)
    assert contract["ended_reasons"] == [reason.value for reason in BrowserTaskGrantEndReason]
    assert contract["statuses"] == [status.value for status in BrowserTaskGrantStatus]
    assert contract["decisions"] == ["approve_once", "approve_for_task", "deny"]
    assert contract["conflict_reasons"] == [
        "approval_already_resolved",
        "task_grant_unavailable",
        "task_grant_offer_mismatch",
    ]
