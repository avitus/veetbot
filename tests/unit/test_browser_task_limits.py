"""ADR-0130: which run limits a chat's runs get.

A chat bound to a website profile runs under the browser-task overlay that
its pinned agent version carries; every other chat, and every version
without the overlay, keeps the agent's own limits.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from agent_core.domain.agents import (
    BROWSER_TASK_LIMITS_METADATA_KEY,
    AgentSpec,
    chat_model_variant,
    run_limits_for_session,
)
from agent_core.domain.delegations import DelegationBrief, DelegationDefaults, derive_child_limits
from agent_core.domain.runs import Run, RunLimits, RunStatus, RunUsage
from agent_core.domain.sessions import (
    SESSION_BROWSER_PROFILE_METADATA_KEY,
    Session,
    SessionStatus,
)
from tests.contract.support import NOW

INTERACTIVE = RunLimits(
    max_steps=32,
    max_model_calls=24,
    max_tool_calls=64,
    synthesis_reserve_model_calls=2,
    synthesis_reserve_tool_calls=4,
)
BROWSER_TASK = RunLimits(
    max_steps=160,
    max_model_calls=120,
    max_tool_calls=160,
    max_cost=Decimal("30"),
    synthesis_reserve_model_calls=2,
    synthesis_reserve_tool_calls=4,
    synthesis_reserve_cost=Decimal("3"),
)
PROFILE_ID = "00000000-0000-0000-0000-0000000000f1"


def agent(*, overlay: RunLimits | None = BROWSER_TASK) -> AgentSpec:
    return AgentSpec(
        id=UUID(int=0xA1),
        version="1.0.0",
        name="general",
        instructions="Help.",
        model_policy="astra",
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        policy_profile="default",
        limits=INTERACTIVE,
        metadata=(
            {}
            if overlay is None
            else {BROWSER_TASK_LIMITS_METADATA_KEY: overlay.model_dump(mode="json")}
        ),
    )


def session(*, bound: bool = True) -> Session:
    return Session(
        id=UUID(int=0xB1),
        tenant_id="tenant-a",
        principal_id="principal-a",
        agent_id=UUID(int=0xA1),
        agent_version="1.0.0",
        status=SessionStatus.ACTIVE,
        metadata={SESSION_BROWSER_PROFILE_METADATA_KEY: PROFILE_ID} if bound else {},
        created_at=NOW,
        updated_at=NOW,
    )


def test_run_limits_for_a_bound_session_come_from_the_pinned_overlay() -> None:
    limits = run_limits_for_session(agent(), session())

    assert limits == BROWSER_TASK
    assert limits.max_cost == Decimal("30")


def test_a_chat_model_variant_keeps_the_overlay() -> None:
    variant = chat_model_variant(agent(), "fable")

    assert variant.id != agent().id
    assert run_limits_for_session(variant, session()) == BROWSER_TASK


def test_an_unbound_session_keeps_the_agent_limits() -> None:
    assert run_limits_for_session(agent(), session(bound=False)) == INTERACTIVE
    blank = session().model_copy(update={"metadata": {SESSION_BROWSER_PROFILE_METADATA_KEY: ""}})
    assert run_limits_for_session(agent(), blank) == INTERACTIVE


def test_a_version_without_the_overlay_keeps_the_agent_limits() -> None:
    assert run_limits_for_session(agent(overlay=None), session()) == INTERACTIVE


def test_the_selected_limits_are_a_copy() -> None:
    pinned = agent()
    limits = run_limits_for_session(pinned, session())
    limits.max_steps = 1

    assert run_limits_for_session(pinned, session()).max_steps == 160
    assert pinned.limits == INTERACTIVE


def test_a_delegated_child_of_a_bound_chat_gets_child_limits() -> None:
    """Guard: children derive their own limits, bounded by the bound parent."""

    parent = Run(
        id=UUID(int=0xC1),
        session_id=UUID(int=0xB1),
        tenant_id="tenant-a",
        agent_id=UUID(int=0xA1),
        agent_version="1.0.0",
        status=RunStatus.RUNNING,
        limits=run_limits_for_session(agent(), session()),
        usage=RunUsage(cost=Decimal("29")),
        created_at=NOW,
        updated_at=NOW,
    )
    defaults = DelegationDefaults(
        max_steps=16, max_model_calls=12, max_tool_calls=24, max_cost=Decimal("2"), wall_seconds=600
    )

    brief = DelegationBrief(
        objective="Look it up.", success_condition="An answer.", allowed_tools=["web.search"]
    )
    (child,) = derive_child_limits(parent, [brief], defaults, now=NOW)

    assert child.max_cost == Decimal("1")
    assert (child.max_steps, child.max_model_calls) == (16, 12)
