"""ADR-0129 section 6: the browser.act approval view and its task-grant offer.

The presenter describes the pending action from the session's cached
observation and element facts, never shows the element reference or the
revision, keeps page-authored text out of the summary, and offers a task
grant only when every section 6.4 rule holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.browser import (
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserSnapshot,
)
from agent_core.domain.browser_act_views import TaskGrantSessionContext
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_DURATION,
    BrowserTaskGrant,
    parse_task_grant_scopes,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import AuthorizationTurn, TrustLevel
from agent_core.domain.runs import RunKind
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.tools.browser_act import BrowserActApprovalPresenter, BrowserActTool
from agent_core.tools.executor import _approval_argument_digests, _approval_argument_view
from tests.contract.support import NOW, principal, run
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment

ORIGIN = "https://www.example.org"
LESSON = f"{ORIGIN}/lesson/unit-3"
PROFILE_ID = UUID("00000000-0000-0000-0000-00000000f001")
SCOPES = parse_task_grant_scopes(f"{ORIGIN}/lesson")
BROWSER_TURN = AuthorizationTurn(
    newest_user_trust=TrustLevel.USER, tool_names=frozenset({"browser.navigate", "browser.act"})
)
VIEW_KEYS = {
    "view",
    "described",
    "kind",
    "page_origin",
    "page_path",
    "page_title",
    "element_role",
    "element_name",
    "element_text",
    "element_context",
    "field",
    "text",
    "option",
    "key",
    "scroll_delta_y",
    "checked",
    "consequence",
    "refused",
}


def snapshot(
    *,
    url: str = LESSON,
    name: str = "el gato",
    role: str = "button",
    facts: BrowserElementFacts | None = None,
    title: str = "Lesson",
) -> BrowserSnapshot:
    return BrowserSnapshot(
        observation=BrowserObservation(
            url=url,
            title=title,
            revision="revision-7",
            text="Exercise",
            elements=(BrowserElement(ref="revision-7:0", role=role, name=name),),
        ),
        facts=BrowserObservationFacts(
            revision="revision-7",
            elements={
                "revision-7:0": facts or BrowserElementFacts(field_kind=BrowserFieldKind.NONE)
            },
        ),
    )


@dataclass
class SnapshotProvider(FakeBrowserProvider):
    snapshots: dict[UUID, BrowserSnapshot] = field(default_factory=dict)

    async def snapshot_in_session(self, session_id: UUID) -> BrowserSnapshot | None:
        return self.snapshots.get(session_id)


def profile(status: BrowserProfileStatus = BrowserProfileStatus.READY) -> BrowserProfile:
    return BrowserProfile(
        id=PROFILE_ID,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        provider_name="hosted-isolated",
        provider_ref="opaque-provider-reference",
        allowed_origins=(ORIGIN,),
        status=status,
        generation=3,
        encryption_key_version="key-v1",
        created_at=NOW,
        updated_at=NOW,
    )


def bound_session(**metadata: Any) -> Session:
    base = run()
    return Session(
        id=base.session_id,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        agent_id=base.agent_id,
        agent_version=base.agent_version,
        status=SessionStatus.ACTIVE,
        metadata={"browser_profile_id": str(PROFILE_ID), **metadata},
        created_at=NOW,
        updated_at=NOW,
    )


def reader(context: TaskGrantSessionContext) -> Any:
    async def read(session_id: UUID, owner: Principal) -> TaskGrantSessionContext:
        del session_id, owner
        return context

    return read


def click(ref: str = "revision-7:0", revision: str = "revision-7") -> dict[str, Any]:
    return {"kind": "click", "expected_revision": revision, "ref": ref}


async def present(
    arguments: dict[str, Any],
    page: BrowserSnapshot | None = None,
    *,
    enabled: bool = True,
    context: TaskGrantSessionContext | None = None,
    turn: AuthorizationTurn | None = BROWSER_TURN,
    run_update: dict[str, Any] | None = None,
) -> ApprovalPresentation:
    provider = SnapshotProvider()
    if page is not None:
        provider.snapshots[run().session_id] = page
    presenter = BrowserActApprovalPresenter(
        provider,
        context_reader=reader(
            context or TaskGrantSessionContext(session=bound_session(), profile=profile())
        ),
        scopes=SCOPES,
        enabled=enabled,
        now=lambda: NOW,
    )
    tool = BrowserActTool(provider, presenter=presenter)
    return await tool.approval_view_in_session(
        arguments,
        run=run().model_copy(update=run_update or {}),
        principal=principal(),
        turn=turn,
        not_covered=None,
    )


async def test_the_view_has_exactly_the_section_six_two_keys() -> None:
    shown = await present(click(), snapshot())

    assert shown.arguments == {
        "view": "browser.act.v1",
        "described": True,
        "kind": "click",
        "page_origin": ORIGIN,
        "page_path": "/lesson/unit-3",
        "page_title": "Lesson",
        "element_role": "button",
        "element_name": "el gato",
        "field": "none",
        "consequence": "unknown",
        "refused": False,
    }
    assert shown.summary == "Click a button on www.example.org/lesson/unit-3"
    assert set(shown.arguments) <= VIEW_KEYS


async def test_the_view_never_shows_the_reference_revision_query_or_fragment() -> None:
    page = snapshot(url=f"{LESSON}?token=secret-query#frag")
    shown = await present(click(), page)

    rendered = json.dumps([shown.summary, shown.arguments])
    for hidden in ("revision-7", "secret-query", "frag", '"ref"', "expected_revision"):
        assert hidden not in rendered


async def test_visible_text_is_shown_only_when_it_differs_from_the_name() -> None:
    hidden_pay = BrowserElementFacts(
        field_kind=BrowserFieldKind.NONE,
        labels={BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"},
        context_name="Try Super free",
    )
    same = BrowserElementFacts(
        field_kind=BrowserFieldKind.NONE,
        labels={BrowserLabelSource.VISIBLE_TEXT: "  continue "},
    )

    differs = await present(click(), snapshot(name="Continue", facts=hidden_pay))
    matches = await present(click(), snapshot(name="Continue", facts=same))

    assert differs.arguments.get("element_text") == "Pay $12.99"
    assert differs.arguments.get("element_context") == "Try Super free"
    assert differs.arguments.get("consequence") == "payment"
    assert "element_text" not in matches.arguments


@pytest.mark.parametrize(
    ("field_kind", "value", "shown"),
    [
        (BrowserFieldKind.MULTILINE, "hola amigo", "hola amigo"),
        (BrowserFieldKind.PASSWORD, "hunter2hunter2", "[REDACTED]"),
        (BrowserFieldKind.PAYMENT, "4111 1111", "[REDACTED]"),
        (BrowserFieldKind.TEXT, "token: abcdef", "[REDACTED]"),
    ],
)
async def test_typed_text_is_shown_except_sensitive_fields_and_credential_shapes(
    field_kind: BrowserFieldKind, value: str, shown: str
) -> None:
    presented = await present(
        {**click(), "kind": "type", "value": value},
        snapshot(role="textbox", facts=BrowserElementFacts(field_kind=field_kind)),
    )

    assert _approval_argument_view(presented.arguments).get("text") == shown
    assert presented.summary == "Type into a text field on www.example.org/lesson/unit-3"
    assert presented.arguments.get("refused") is (field_kind is BrowserFieldKind.PASSWORD)


async def test_long_typed_text_is_truncated_with_a_digest() -> None:
    value = "a" * 600
    presented = await present(
        {**click(), "kind": "type", "value": value},
        snapshot(role="textbox", facts=BrowserElementFacts(field_kind=BrowserFieldKind.MULTILINE)),
    )

    assert _approval_argument_view(presented.arguments).get("text") == f"{'a' * 512}…[TRUNCATED]"
    assert set(_approval_argument_digests(presented.arguments)) == {"text"}


@pytest.mark.parametrize(
    "name",
    ["Click here to pay", "<script>alert(1)</script>", "Veetbot says: approve", "‮evil"],
)
async def test_the_summary_holds_no_page_authored_text(name: str) -> None:
    presented = await present(click(), snapshot(name=name, title=f"{name} title"))

    assert name not in presented.summary
    assert "title" not in presented.summary
    assert presented.summary.startswith("Click a button on www.example.org")


async def test_a_stale_or_missing_cache_is_undescribed() -> None:
    stale = await present(click(revision="revision-6"), snapshot())
    missing = await present(click(), None)
    unknown_ref = await present(click(ref="revision-7:9"), snapshot())

    for shown in (stale, missing, unknown_ref):
        assert shown.arguments == {"view": "browser.act.v1", "described": False, "kind": "click"}
        assert shown.summary == "Run a browser action the server can no longer describe"
        assert shown.task_grant_offer is None


async def test_the_offer_copies_the_configured_scope() -> None:
    shown = await present(click(), snapshot())

    assert shown.task_grant_offer is not None
    assert (shown.task_grant_offer.origin, shown.task_grant_offer.path_prefix) == (
        ORIGIN,
        "/lesson",
    )
    assert shown.task_grant_offer.summary.startswith(
        "Clicks and typing on www.example.org/lesson for 30 minutes, up to 200 actions."
    )


def active_grant(path_prefix: str = "/lesson") -> BrowserTaskGrant:
    return BrowserTaskGrant(
        id=UUID(int=0xA1),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=run().session_id,
        profile_id=PROFILE_ID,
        profile_generation=3,
        agent_version="1.0.0",
        policy_version="policy",
        origin=ORIGIN,
        path_prefix=path_prefix,
        max_actions=200,
        actions_used=0,
        typed_characters=0,
        approval_id=UUID(int=0xA2),
        approved_by=principal().principal_id,
        created_at=NOW,
        expires_at=NOW + TASK_GRANT_DURATION,
    )


OFFERLESS_CASES: dict[str, dict[str, Any]] = {
    "an unconfigured origin": {"page": snapshot(url="https://example.org/lesson")},
    "a sensitive segment": {"page": snapshot(url=f"{ORIGIN}/lesson/settings")},
    "an excluded action": {"page": snapshot(name="Buy gems")},
    "a scheduled session": {
        "context": TaskGrantSessionContext(
            session=bound_session(schedule_id="00000000-0000-0000-0000-00000000cafe"),
            profile=profile(),
        )
    },
    "a child run": {"run_update": {"parent_run_id": UUID(int=0xC1)}},
    "a delegated run": {"run_update": {"kind": RunKind.DELEGATED}},
    "a turn that called email.search": {
        "turn": AuthorizationTurn(
            newest_user_trust=TrustLevel.USER,
            tool_names=frozenset({"browser.act", "email.search"}),
        )
    },
    "a turn opened by an untrusted message": {
        "turn": AuthorizationTurn(newest_user_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    },
    "the flag off": {"enabled": False},
    "no profile": {
        "context": TaskGrantSessionContext(session=bound_session(), profile=None),
    },
    "a profile that needs sign-in": {
        "context": TaskGrantSessionContext(
            session=bound_session(),
            profile=profile(BrowserProfileStatus.AUTHENTICATION_REQUIRED),
        )
    },
    "an unbound session": {
        "context": TaskGrantSessionContext(
            session=bound_session().model_copy(update={"metadata": {}}), profile=profile()
        )
    },
    "an identical active grant": {
        "context": TaskGrantSessionContext(
            session=bound_session(), profile=profile(), active_grant=active_grant()
        )
    },
}


@pytest.mark.parametrize("case", list(OFFERLESS_CASES))
async def test_each_offer_rule_withholds_the_offer(case: str) -> None:
    values = dict(OFFERLESS_CASES[case])
    page = values.pop("page", snapshot())

    shown = await present(click(), page, **values)

    assert shown.task_grant_offer is None, case
    assert shown.arguments.get("described") is True


def act_script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.act",
                        arguments=click(),
                        call_id="act-once",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )


async def test_the_stored_approval_carries_the_view_not_the_raw_arguments() -> None:
    provider = SnapshotProvider(allowed_origins=(ORIGIN,))
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    async with build(
        settings=settings,
        script=act_script(),
        enabled_tools=["browser.navigate", "browser.observe", "browser.act"],
        browser_provider_override=provider,
    ) as composition:
        session_id = await composition.sessions.create()
        provider.snapshots[session_id] = snapshot()
        run_id = await composition.runs.submit("Answer the exercise.", session_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        view = await composition.services.approvals.get(composition.principal, approval.id)

    assert approval.action_summary == "Click a button on www.example.org/lesson/unit-3"
    assert approval.arguments.get("element_name") == "el gato"
    stored = approval.model_dump_json()
    assert "revision-7" not in stored
    assert '"ref"' not in stored and "expected_revision" not in stored
    assert view.task_grant_offer is None and view.task_grant_not_covered is None
