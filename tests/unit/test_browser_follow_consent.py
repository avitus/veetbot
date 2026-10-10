"""Specific owner consent never becomes general browser permission."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from agent_core.application.browser_follow_consent import BrowserFollowConsentAuthorizer
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserAction, BrowserElementFacts, BrowserFieldKind
from agent_core.domain.browser_classification import dispatch_constraint_coverage
from agent_core.domain.browser_follow import follow_request
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import Run
from tests.contract.support import NOW
from tests.unit.test_browser_act_approval_view import snapshot
from tests.unit.test_browser_task_grant_authorizer import (
    SESSION,
    Harness,
    decision,
    harness,
    owner,
    proposed,
    run,
)


@pytest.mark.parametrize(
    "text,answer,expected",
    [
        ("This is perfect. Follow him", "[Armin](https://x.com/mitsuhiko)", "mitsuhiko"),
        ("Follow @mitsuhiko", "", "mitsuhiko"),
        ("Please follow her", "[Shreya](https://x.com/sh_reya)", "sh_reya"),
        ("Don't follow him", "https://x.com/mitsuhiko", None),
        ('He said "follow him"', "https://x.com/mitsuhiko", None),
        ("Follow him", "https://x.com/a https://x.com/b", None),
        ("Follow him", "https://x.com/mitsuhiko/status/123", None),
        ("Follow him", "https://x.com@evil.test/mitsuhiko", None),
        ("Follow him and send a message", "https://x.com/mitsuhiko", None),
        ("Yes", "https://x.com/mitsuhiko", None),
    ],
)
def test_closed_follow_consent(text: str, answer: str, expected: str | None) -> None:
    assert follow_request(text, answer) == expected


async def consent_harness(
    *, actor: str = "principal", target: str = "mitsuhiko"
) -> tuple[Harness, Principal, Run, BrowserFollowConsentAuthorizer]:
    h = await harness(
        with_grant=False,
        profile_update={"allowed_origins": ("https://x.com",)},
        page=snapshot(url="https://x.com/mitsuhiko", name="Follow @mitsuhiko"),
    )
    principal = owner().model_copy(update={"scopes": {"run.write", "browser.profile.read"}})
    async with h.uow_factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION,
                run_id=run().id,
                event_type="user.message.created",
                actor_type=actor,
                actor_id=principal.principal_id,
                payload={"content": "Follow him", "browser_follow_target": target},
            )
        )
    current = run(principal_scopes=set(principal.scopes), seed_event_sequence=event.sequence)
    authorizer = BrowserFollowConsentAuthorizer(
        provider=h.provider, uow_factory=h.uow_factory, now=lambda: NOW + timedelta(seconds=1)
    )
    return h, principal, current, authorizer


async def test_follow_consent_is_consumed_once_and_live_target_is_checked() -> None:
    __h, principal, current, authorizer = await consent_harness()
    kwargs: dict[str, Any] = {
        "action": proposed(),
        "decision": decision(),
        "principal": principal,
        "run": current,
        "agent_version": current.agent_version,
        "action_deadline": NOW + timedelta(minutes=1),
    }
    authorized = await authorizer.authorize(**kwargs)
    assert authorized.allowed
    assert not (await authorizer.authorize(**kwargs)).allowed
    constraint = authorized.dispatch_constraint
    assert constraint is not None
    common: dict[str, Any] = {
        "constraint": constraint,
        "action": BrowserAction.model_validate(proposed().arguments),
        "page_url": "https://x.com/mitsuhiko",
        "role": "button",
        "labels": ["Follow @mitsuhiko"],
        "facts": BrowserElementFacts(field_kind=BrowserFieldKind.NONE),
        "option_texts": [],
        "runtime_origins": ["https://x.com"],
        "now": NOW + timedelta(seconds=2),
        "disabled": False,
    }
    assert dispatch_constraint_coverage(**common).covered
    changes: list[dict[str, Any]] = [
        {"labels": ["Follow @someone_else"]},
        {"page_url": "https://x.com/someone_else"},
        {"labels": ["Follow @mitsuhiko", "Send message"]},
        {"disabled": True},
        {"facts": None},
        {"role": "link"},
        {"labels": ["Following @mitsuhiko"]},
        {"page_url": "https://evil.test/mitsuhiko"},
        {"now": NOW + timedelta(minutes=31)},
    ]
    for changed in changes:
        assert not dispatch_constraint_coverage(**(common | changed)).covered


@pytest.mark.parametrize(
    "actor,target", [("scheduler", "mitsuhiko"), ("surface", "mitsuhiko"), ("principal", "another")]
)
async def test_scheduler_surface_and_different_target_have_no_consent(
    actor: str, target: str
) -> None:
    __h, principal, current, authorizer = await consent_harness(actor=actor, target=target)
    result = await authorizer.authorize(
        action=proposed(),
        decision=decision(),
        principal=principal,
        run=current,
        agent_version=current.agent_version,
        action_deadline=NOW + timedelta(minutes=1),
    )
    assert not result.allowed


async def test_later_owner_message_revokes_pending_consent() -> None:
    h, principal, current, authorizer = await consent_harness()
    async with h.uow_factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION,
                run_id=current.id,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal.principal_id,
                payload={"content": "Actually, cancel that"},
            )
        )
    assert not (
        await authorizer.authorize(
            action=proposed(),
            decision=decision(),
            principal=principal,
            run=current,
            agent_version=current.agent_version,
            action_deadline=NOW + timedelta(minutes=1),
        )
    ).allowed


@pytest.mark.parametrize("evidence", ["confirmed", "missing", "wrong_page"])
async def test_follow_requires_positive_evidence_and_never_retries(evidence: str) -> None:
    from agent_core.domain.browser import BrowserElement, BrowserObservation
    from agent_core.tools.browser_act import BrowserActTool
    from tests.contract.support import tool_context
    from tests.unit.test_browser_tools import FakeBrowserProvider

    class FollowProvider(FakeBrowserProvider):
        def _observation(self, url: str) -> BrowserObservation:
            return BrowserObservation(
                url="https://x.com/another"
                if evidence == "wrong_page"
                else "https://x.com/mitsuhiko",
                title="Profile",
                revision="after",
                text="Profile",
                elements=(
                    BrowserElement(
                        ref="follow",
                        role="button",
                        name="Following @mitsuhiko"
                        if evidence != "missing"
                        else "Follow @mitsuhiko",
                    ),
                ),
            )

    _, principal, current, authorizer = await consent_harness()
    consent = await authorizer.authorize(
        action=proposed(),
        decision=decision(),
        principal=principal,
        run=current,
        agent_version=current.agent_version,
        action_deadline=NOW + timedelta(minutes=1),
    )
    provider = FollowProvider(allowed_origins=("https://x.com",))
    context = replace(tool_context(), dispatch_constraint=consent.dispatch_constraint)
    result = await BrowserActTool(provider).execute(proposed().arguments, context)
    assert result.ok == (evidence == "confirmed")
    if not result.ok:
        assert result.failure is not None
        assert result.failure.reason_code == "tool.browser.outcome_unknown"
        assert not result.failure.retryable
    assert len(provider.actions) == 1
