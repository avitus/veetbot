"""The browser research permission is bound to owner provenance and live constraints."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.application.browser_research import BrowserResearchAuthorizer
from agent_core.domain.browser import (
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserProfileStatus,
    BrowserTargetFacts,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.policies import AuthorizationTurn, PolicyDecisionType, TrustLevel
from tests.unit.test_browser_act_approval_view import ORIGIN, snapshot
from tests.unit.test_browser_task_grant_authorizer import (
    BROWSER_TURN,
    RUN,
    SESSION,
    WHEN,
    decision,
    harness,
    owner,
    proposed,
    run,
)

REFUSAL_REASONS = {
    "untrusted_turn": "untrusted_turn",
    "untrusted_seed": "not_owner_seed",
    "different_actor": "not_owner_seed",
    "child": "ineligible_run",
    "agent_changed": "ineligible_run",
    "missing_scope": "ineligible_run",
    "missing_binding": "profile_unavailable",
    "revoked": "profile_unavailable",
    "foreign_profile": "profile_unavailable",
    "foreign_origin": "outside_profile_origin",
    "stale_page": "observation_unavailable",
    "policy_changed": "policy_changed",
    "policy_denied": "policy_changed",
    "expired": "deadline_passed",
    "sensitive_page": "sensitive_path",
    "named_consequence": "excluded.publication",
    "incomplete_facts": "facts_unavailable",
    "cross_origin_link": "link_outside_prefix",
    "typed_text_after_email": "turn_not_browser_only",
    "after_other_tool": "turn_not_browser_only",
}


@pytest.mark.parametrize(
    "problem",
    [
        None,
        "scheduler",
        "dated_post_link",
        "untrusted_turn",
        "untrusted_seed",
        "different_actor",
        "child",
        "agent_changed",
        "missing_binding",
        "revoked",
        "foreign_profile",
        "foreign_origin",
        "stale_page",
        "missing_scope",
        "policy_changed",
        "policy_denied",
        "expired",
        "sensitive_page",
        "named_consequence",
        "incomplete_facts",
        "cross_origin_link",
        "typed_text",
        "typed_text_after_email",
        "after_other_tool",
    ],
)
async def test_research_permission_boundaries(problem: str | None) -> None:
    """Allow trusted ordinary controls while each missing authority or live fact fails closed."""
    page = snapshot()
    profile_update: dict[str, Any] = {}
    metadata = None
    if problem == "missing_binding":
        metadata = {}
    if problem == "scheduler":
        from tests.unit.test_browser_task_grant_authorizer import PROFILE

        metadata = {"browser_profile_id": str(PROFILE), "schedule_id": str(UUID(int=9))}
    if problem == "revoked":
        profile_update["status"] = BrowserProfileStatus.REVOKED
    if problem == "foreign_profile":
        profile_update["principal_id"] = "someone-else"
    if problem == "foreign_origin":
        page = snapshot(url="https://other.example/lesson")
    if problem == "sensitive_page":
        page = snapshot(url=ORIGIN + "/settings")
    if problem == "named_consequence":
        page = snapshot(name="Send message")
    if problem in {"typed_text", "typed_text_after_email"}:
        page = snapshot(role="textbox", facts=BrowserElementFacts(field_kind=BrowserFieldKind.TEXT))
    if problem == "dated_post_link":
        # Friday's interruption: a dated post link inside the profile's origin.
        page = snapshot(
            url=ORIGIN + "/rasbt",
            role="link",
            name="Sep 9",
            facts=BrowserElementFacts(
                field_kind=BrowserFieldKind.NONE,
                link_target=BrowserTargetFacts(
                    same_origin=True, first_segment="rasbt", sensitive_path=False
                ),
            ),
        )
    if problem in {"incomplete_facts", "cross_origin_link"}:
        assert page.facts is not None
        facts = page.facts.elements["revision-7:0"]
        change = (
            {"labels_truncated": True}
            if problem == "incomplete_facts"
            else {
                "link_target": BrowserTargetFacts(
                    same_origin=False, first_segment="elsewhere", sensitive_path=False
                )
            }
        )
        page.facts.elements["revision-7:0"] = facts.model_copy(update=change)
    h = await harness(page=page, metadata=metadata, profile_update=profile_update, with_grant=False)
    principal = owner().model_copy(update={"scopes": {"browser.profile.read"}})
    if problem == "missing_scope":
        principal = principal.model_copy(update={"scopes": set()})
    current = run()
    async with h.uow_factory() as uow:
        await uow.runs.create(current)
        seed = await uow.events.append(
            NewEvent(
                session_id=SESSION,
                run_id=RUN,
                event_type="user.message.created",
                actor_type="surface"
                if problem == "untrusted_seed"
                else "scheduler"
                if problem == "scheduler"
                else "principal",
                actor_id="someone-else" if problem == "different_actor" else principal.principal_id,
                payload={"content": "Research the account."},
            )
        )
    current = current.model_copy(update={"seed_event_sequence": seed.sequence})
    if problem == "child":
        current = current.model_copy(update={"parent_run_id": UUID(int=8)})
    action = proposed()
    if problem in {"typed_text", "typed_text_after_email"}:
        action = proposed(
            {
                "kind": "type",
                "expected_revision": "revision-7",
                "ref": "revision-7:0",
                "value": "private value",
            }
        )
    turn = BROWSER_TURN
    if problem == "untrusted_turn":
        turn = AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    if problem in {"typed_text_after_email", "after_other_tool"}:
        # ADR-0129 G5: a turn that read mail, memory or another tool's output
        # could type that data into the page, so it keeps ordinary approval.
        other = "email.read" if problem == "typed_text_after_email" else "web.search"
        turn = AuthorizationTurn(
            newest_user_trust=TrustLevel.USER, tool_names=frozenset({other, "browser.act"})
        )
    if problem == "stale_page":
        action.arguments["expected_revision"] = "old"
    if problem == "policy_changed":
        h.policy.version = "changed"
    if problem == "policy_denied":
        h.policy.decision = PolicyDecisionType.DENY
    result = await BrowserResearchAuthorizer(
        provider=h.provider,
        uow_factory=h.uow_factory,
        policy=h.policy,
        now=lambda: WHEN,
    ).authorize(
        action=action,
        decision=decision(),
        principal=principal,
        run=current,
        agent_version="changed" if problem == "agent_changed" else current.agent_version,
        action_deadline=WHEN if problem == "expired" else WHEN + timedelta(seconds=30),
        turn=turn,
    )
    assert result.allowed is (problem in {None, "scheduler", "dated_post_link", "typed_text"})
    if not result.allowed:
        # ADR-0263: each refusal names its closed, content-free reason.
        assert problem is not None
        assert result.authorization_kind == "browser_research"
        assert result.reason_code == f"browser.research.{REFUSAL_REASONS[problem]}"
    if result.allowed:
        assert result.dispatch_constraint is not None
        assert result.dispatch_constraint.grant_kind == "research"
        assert result.dispatch_constraint.path_prefix is None
        assert result.dispatch_constraint.origins == (ORIGIN,)
        assert result.authorization_ref is not None
