"""ADR-0129: the shared domain types of browser task grants and element facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from agent_core.domain.browser import (
    BrowserActionConsequence,
    BrowserActionKind,
    BrowserCoverage,
    BrowserDispatchConstraint,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserSnapshot,
    BrowserTargetFacts,
)
from agent_core.domain.browser_task_grants import (
    MAXIMUM_TASK_GRANT_SCOPES,
    TASK_GRANT_ACTION_KINDS,
    TASK_GRANT_NOT_COVERED_REASONS,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    BrowserTaskGrantOffer,
    BrowserTaskGrantScope,
    TaskGrantNotCovered,
    parse_task_grant_scopes,
    task_grant_id_for_approval,
)
from agent_core.domain.policies import AuthorizationTurn, StandingAuthorization, TrustLevel
from tests.contract.support import NOW

ORIGIN = "https://www.example.com"
APPROVAL_ID = UUID("00000000-0000-0000-0000-0000000000a1")


def constraint(**changes: Any) -> BrowserDispatchConstraint:
    values: dict[str, Any] = {
        "grant_kind": "task",
        "origins": (ORIGIN,),
        "path_prefix": "/lesson",
        "not_after": NOW + timedelta(minutes=30),
        "consequence_ceiling": "unknown",
        "max_text_characters": 256,
    }
    values.update(changes)
    return BrowserDispatchConstraint(**values)


def standing(**changes: Any) -> BrowserDispatchConstraint:
    values: dict[str, Any] = {
        "grant_kind": "standing",
        "origins": (ORIGIN, "https://example.com"),
        "path_prefix": None,
        "consequence_ceiling": "routine",
        "max_text_characters": None,
    }
    values.update(changes)
    return constraint(**values)


def grant(**changes: Any) -> BrowserTaskGrant:
    values: dict[str, Any] = {
        "id": task_grant_id_for_approval(APPROVAL_ID),
        "tenant_id": "tenant-a",
        "principal_id": "principal-a",
        "session_id": UUID("00000000-0000-0000-0000-0000000000b1"),
        "profile_id": UUID("00000000-0000-0000-0000-0000000000c1"),
        "profile_generation": 3,
        "agent_version": "agent-version",
        "policy_version": "policy-version",
        "origin": ORIGIN,
        "path_prefix": "/lesson",
        "max_actions": 200,
        "actions_used": 0,
        "typed_characters": 0,
        "approval_id": APPROVAL_ID,
        "approved_by": "principal-a",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(changes)
    return BrowserTaskGrant(**values)


def test_constraint_fields_must_match_the_grant_kind() -> None:
    task = constraint()
    assert task.origins == (ORIGIN,)
    assert standing().consequence_ceiling == "routine"
    assert constraint(origins=("https://WWW.Example.com",)).origins == (ORIGIN,)

    refused: dict[str, Callable[[], object]] = {
        "standing with an unknown ceiling": lambda: standing(consequence_ceiling="unknown"),
        "standing with a text cap": lambda: standing(max_text_characters=256),
        "standing with a path prefix": lambda: standing(path_prefix="/lesson"),
        "task with a routine ceiling": lambda: constraint(consequence_ceiling="routine"),
        "task without a text cap": lambda: constraint(max_text_characters=None),
        "task without a prefix": lambda: constraint(path_prefix=None),
        "task with two origins": lambda: constraint(origins=(ORIGIN, "https://example.com")),
        "task with a two-segment prefix": lambda: constraint(path_prefix="/lesson/unit"),
        "task with the root as prefix": lambda: constraint(path_prefix="/"),
        "an origin with a path": lambda: constraint(origins=(f"{ORIGIN}/lesson",)),
        "a plaintext origin": lambda: constraint(origins=("http://www.example.com",)),
        "duplicate origins": lambda: standing(origins=(ORIGIN, "https://WWW.example.com")),
        "no origins": lambda: standing(origins=()),
        "a naive expiry": lambda: constraint(not_after=datetime(2026, 9, 25, 12, 0)),
        "an extra field": lambda: constraint(allow_everything=True),
    }
    accepted = []
    for name, build in refused.items():
        try:
            build()
        except ValidationError:
            continue
        accepted.append(name)
    assert accepted == []

    with pytest.raises(ValidationError):
        task.path_prefix = "/other"  # frozen


def test_scope_setting_refuses_multi_segment_and_duplicate_entries() -> None:
    assert parse_task_grant_scopes("") == ()
    assert parse_task_grant_scopes("   ") == ()
    assert parse_task_grant_scopes("https://www.duolingo.com/lesson") == (
        BrowserTaskGrantScope(origin="https://www.duolingo.com", path_prefix="/lesson"),
    )
    assert parse_task_grant_scopes(
        " https://WWW.Example.com:443/lesson , https://example.org/practice-hub "
    ) == (
        BrowserTaskGrantScope(origin=ORIGIN, path_prefix="/lesson"),
        BrowserTaskGrantScope(origin="https://example.org", path_prefix="/practice-hub"),
    )

    refused = {
        "https://www.example.com/lesson/unit": 1,
        "https://www.example.com/": 1,
        "https://www.example.com": 1,
        "https://www.example.com/lesson/": 1,
        "http://www.example.com/lesson": 1,
        "https://user@www.example.com/lesson": 1,
        "https://www.example.com:8443/lesson": 1,
        "https://www.example.com/lesson?unit=1": 1,
        "https://www.example.com/lesson?": 1,
        "https://www.example.com/lesson#top": 1,
        "https://localhost/lesson": 1,
        "https://10.0.0.1/lesson": 1,
        "https://www.example.com/les%20son": 1,
        "https://www.example.com/" + "a" * 65: 1,
        "www.example.com/lesson": 1,
        "https://example.org/practice,https://www.example.com/lesson/x": 2,
        "https://www.example.com/lesson,https://WWW.example.com:443/lesson": 2,
        "https://example.org/practice,,https://www.example.com/lesson": 2,
    }
    too_many = ",".join(
        f"https://site{n}.example.com/lesson" for n in range(MAXIMUM_TASK_GRANT_SCOPES + 1)
    )
    refused[too_many] = MAXIMUM_TASK_GRANT_SCOPES + 1

    accepted = []
    for raw, position in refused.items():
        try:
            parse_task_grant_scopes(raw)
        except ValueError as exc:
            assert f"entry {position}" in str(exc), (raw, str(exc))
            continue
        accepted.append(raw)
    assert accepted == []


def test_scope_contains_only_its_origin_and_prefix() -> None:
    scope = BrowserTaskGrantScope(origin=ORIGIN, path_prefix="/lesson")

    inside = [
        f"{ORIGIN}/lesson",
        f"{ORIGIN}/lesson/unit/3",
        f"{ORIGIN}/lesson?session=1#top",
        "https://WWW.EXAMPLE.COM/lesson",
    ]
    outside = [
        f"{ORIGIN}/lessons",
        f"{ORIGIN}/Lesson",
        f"{ORIGIN}/",
        f"{ORIGIN}/learn",
        f"{ORIGIN}/lesson/../settings",
        f"{ORIGIN}/lesson/%2e%2e/settings",
        "https://example.com/lesson",
        "https://evil.example.net/lesson",
        "http://www.example.com/lesson",
        "not a url",
    ]
    assert [url for url in inside if not scope.contains(url)] == []
    assert [url for url in outside if scope.contains(url)] == []

    refused = [
        {"origin": f"{ORIGIN}/lesson", "path_prefix": "/lesson"},
        {"origin": "http://www.example.com", "path_prefix": "/lesson"},
        {"origin": ORIGIN, "path_prefix": "lesson"},
        {"origin": ORIGIN, "path_prefix": "/lesson/unit"},
        {"origin": ORIGIN, "path_prefix": "/"},
    ]
    accepted = []
    for values in refused:
        try:
            BrowserTaskGrantScope(**values)
        except ValidationError:
            continue
        accepted.append(values)
    assert accepted == []


def test_grant_window_caps_and_end_pairing_are_validated() -> None:
    assert grant().origin == ORIGIN
    assert grant(origin="https://WWW.Example.com").origin == ORIGIN
    ended = grant(
        actions_used=200,
        typed_characters=4096,
        ended_at=NOW + timedelta(minutes=5),
        end_reason=BrowserTaskGrantEndReason.EXHAUSTED,
    )
    assert ended.end_reason is BrowserTaskGrantEndReason.EXHAUSTED
    revoked = grant(
        revoked_at=NOW + timedelta(minutes=1),
        ended_at=NOW + timedelta(minutes=1),
        end_reason=BrowserTaskGrantEndReason.REVOKED,
    )
    assert revoked.revoked_at is not None

    refused: dict[str, dict[str, Any]] = {
        "a 31-minute window": {"expires_at": NOW + timedelta(minutes=31)},
        "expiry at creation": {"expires_at": NOW},
        "expiry before creation": {"expires_at": NOW - timedelta(minutes=1)},
        "201 actions": {"max_actions": 201},
        "no actions": {"max_actions": 0},
        "uses past the cap": {"max_actions": 10, "actions_used": 11},
        "negative uses": {"actions_used": -1},
        "4097 typed characters": {"typed_characters": 4097},
        "negative typed characters": {"typed_characters": -1},
        "a negative generation": {"profile_generation": -1},
        "ended without a reason": {"ended_at": NOW},
        "a reason without an end": {"end_reason": BrowserTaskGrantEndReason.EXPIRED},
        "revoked but not ended": {"revoked_at": NOW},
        "revoked with another reason": {
            "revoked_at": NOW,
            "ended_at": NOW,
            "end_reason": BrowserTaskGrantEndReason.EXPIRED,
        },
        "a two-segment prefix": {"path_prefix": "/lesson/unit"},
        "a prefix without a slash": {"path_prefix": "lesson"},
        "an origin with a path": {"origin": f"{ORIGIN}/lesson"},
        "an empty tenant": {"tenant_id": ""},
        "an empty approver": {"approved_by": ""},
    }
    accepted = []
    for name, changes in refused.items():
        try:
            grant(**changes)
        except ValidationError:
            continue
        accepted.append(name)
    assert accepted == []


def test_task_grant_id_is_derived_from_the_approval() -> None:
    other = UUID("00000000-0000-0000-0000-0000000000a2")

    assert task_grant_id_for_approval(APPROVAL_ID) == uuid5(
        NAMESPACE_URL, f"veetbot:browser-task-grant:{APPROVAL_ID}"
    )
    assert task_grant_id_for_approval(APPROVAL_ID) == task_grant_id_for_approval(APPROVAL_ID)
    assert task_grant_id_for_approval(other) != task_grant_id_for_approval(APPROVAL_ID)


def test_offer_carries_the_fixed_limits_and_a_configured_scope() -> None:
    offer = BrowserTaskGrantOffer(
        origin="https://WWW.Example.com", path_prefix="/lesson", summary="Clicks and typing."
    )
    assert offer.origin == ORIGIN
    assert (offer.duration_seconds, offer.max_actions, offer.max_typed_characters) == (
        1800,
        200,
        4096,
    )
    assert offer.action_kinds == TASK_GRANT_ACTION_KINDS == tuple(BrowserActionKind)

    refused: dict[str, dict[str, Any]] = {
        "a longer window": {"duration_seconds": 3600},
        "more actions": {"max_actions": 500},
        "a two-segment prefix": {"path_prefix": "/lesson/unit"},
        "a plaintext origin": {"origin": "http://www.example.com"},
        "an empty summary": {"summary": ""},
    }
    accepted = []
    for name, changes in refused.items():
        values: dict[str, Any] = {
            "origin": ORIGIN,
            "path_prefix": "/lesson",
            "summary": "Clicks and typing.",
        }
        values.update(changes)
        try:
            BrowserTaskGrantOffer(**values)
        except ValidationError:
            continue
        accepted.append(name)
    assert accepted == []


def test_not_covered_reason_is_from_the_closed_list() -> None:
    grant_id = task_grant_id_for_approval(APPROVAL_ID)
    for consequence in BrowserActionConsequence:
        if consequence in {BrowserActionConsequence.ROUTINE, BrowserActionConsequence.UNKNOWN}:
            continue
        reason = f"browser.task_grant.excluded.{consequence.value}"
        assert reason in TASK_GRANT_NOT_COVERED_REASONS
        assert TaskGrantNotCovered(grant_id=grant_id, reason=reason).reason == reason
    assert "browser.task_grant.outside_prefix" in TASK_GRANT_NOT_COVERED_REASONS

    refused = [
        "browser.task_grant.excluded.routine",
        "browser.task_grant.excluded.unknown",
        "browser.task_grant.none",
        "outside_prefix",
        "",
    ]
    accepted = []
    for reason in refused:
        try:
            TaskGrantNotCovered(grant_id=grant_id, reason=reason)
        except ValidationError:
            continue
        accepted.append(reason)
    assert accepted == []


def test_element_facts_are_bounded_and_closed() -> None:
    facts = BrowserElementFacts(
        field_kind=BrowserFieldKind.NONE,
        labels={BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"},
        link_target=BrowserTargetFacts(same_origin=True, first_segment="lesson"),
        context_name="Try Super free",
    )
    assert facts.link_target is not None and facts.link_target.sensitive_path is True
    snapshot = BrowserSnapshot(
        observation=BrowserObservation(url=f"{ORIGIN}/lesson", revision="r1"),
        facts=BrowserObservationFacts(revision="r1", elements={"e1": facts}),
    )
    assert snapshot.facts is not None
    assert BrowserSnapshot(observation=snapshot.observation).facts is None

    refused: dict[str, Callable[[], object]] = {
        "a label over 256 characters": lambda: BrowserElementFacts(
            field_kind=BrowserFieldKind.NONE, labels={BrowserLabelSource.TITLE: "x" * 257}
        ),
        "a dialog name over 128 characters": lambda: BrowserElementFacts(
            field_kind=BrowserFieldKind.NONE, context_name="x" * 129
        ),
        "a first segment over 64 characters": lambda: BrowserTargetFacts(
            same_origin=True, first_segment="x" * 65
        ),
        "an unknown field kind": lambda: BrowserElementFacts.model_validate(
            {"field_kind": "credit_card"}
        ),
        "an unknown label source": lambda: BrowserElementFacts.model_validate(
            {"field_kind": "none", "labels": {"href": "https://evil.example.net"}}
        ),
        "an extra fact": lambda: BrowserElementFacts.model_validate(
            {"field_kind": "none", "href": "https://evil.example.net"}
        ),
        "an extra target field": lambda: BrowserTargetFacts.model_validate(
            {"same_origin": True, "url": "https://evil.example.net/pay"}
        ),
        "257 elements": lambda: BrowserObservationFacts(
            revision="r1",
            elements={
                f"e{n}": BrowserElementFacts(field_kind=BrowserFieldKind.NONE) for n in range(257)
            },
        ),
        "an empty revision": lambda: BrowserObservationFacts(revision=""),
    }
    accepted = []
    for name, build in refused.items():
        try:
            build()
        except ValidationError:
            continue
        accepted.append(name)
    assert accepted == []

    with pytest.raises(ValidationError):
        facts.download = True  # frozen


def test_coverage_names_a_reason_exactly_when_not_covered() -> None:
    assert BrowserCoverage(covered=True, consequence=BrowserActionConsequence.UNKNOWN).covered
    refused = BrowserCoverage(
        covered=False,
        consequence=BrowserActionConsequence.PAYMENT,
        reason="excluded.payment",
    )
    assert refused.reason == "excluded.payment"

    with pytest.raises(ValidationError):
        BrowserCoverage(covered=True, consequence=BrowserActionConsequence.ROUTINE, reason="x")
    with pytest.raises(ValidationError):
        BrowserCoverage(covered=False, consequence=BrowserActionConsequence.UNKNOWN)


def test_only_an_allowed_standing_authorization_carries_a_constraint_and_a_use() -> None:
    allowed = StandingAuthorization(
        allowed=True,
        reason_code="browser.task_grant.authorized",
        authorization_kind="browser_task_grant",
        authorization_ref=str(task_grant_id_for_approval(APPROVAL_ID)),
        dispatch_constraint=constraint(),
        use_ordinal=1,
        authorization_view={"view": "browser.act.v1", "described": True},
    )
    assert allowed.dispatch_constraint is not None
    denial_naming_the_grant = StandingAuthorization(
        allowed=False,
        reason_code="browser.task_grant.excluded.payment",
        authorization_kind="browser_task_grant",
        authorization_ref=str(task_grant_id_for_approval(APPROVAL_ID)),
    )
    assert denial_naming_the_grant.dispatch_constraint is None

    refused: dict[str, dict[str, Any]] = {
        "a denial with a constraint": {"allowed": False, "dispatch_constraint": constraint()},
        "a denial with a use": {"allowed": False, "use_ordinal": 1},
        "a denial with a view": {"allowed": False, "authorization_view": {"view": "x"}},
        "a use numbered zero": {"use_ordinal": 0},
    }
    accepted = []
    for name, changes in refused.items():
        values: dict[str, Any] = {
            "allowed": True,
            "reason_code": "browser.task_grant.authorized",
            "authorization_kind": "browser_task_grant",
            "authorization_ref": "grant",
        }
        values.update(changes)
        try:
            StandingAuthorization(**values)
        except ValidationError:
            continue
        accepted.append(name)
    assert accepted == []


def test_authorization_turn_is_a_frozen_summary_of_the_active_turn() -> None:
    turn = AuthorizationTurn(
        newest_user_trust=TrustLevel.USER,
        tool_names=frozenset({"browser.observe", "browser.act"}),
    )
    assert "browser.act" in turn.tool_names
    assert AuthorizationTurn(newest_user_trust=TrustLevel.MEMORY).tool_names == frozenset()
    with pytest.raises(ValidationError):
        turn.newest_user_trust = TrustLevel.EXTERNAL_UNTRUSTED
