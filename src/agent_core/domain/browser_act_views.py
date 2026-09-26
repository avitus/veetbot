"""The ``browser.act`` approval view, task-grant eligibility and offer rules (ADR-0129).

Pure functions shared by the approval presenter, the task-grant authorizer
and the resolution service. The summary holds only server words, a closed
role word, the host and the path; page-authored strings reach a client only
as view fields, which it quotes and labels as website text.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserSnapshot,
    browser_origin,
)
from agent_core.domain.browser_classification import (
    classify_browser_action,
    normalize_text,
    path_is_sensitive,
    task_grant_coverage,
)
from agent_core.domain.browser_task_grants import (
    BrowserTaskGrant,
    BrowserTaskGrantOffer,
    BrowserTaskGrantScope,
    offer_scope,
)
from agent_core.domain.policies import AuthorizationTurn, TrustLevel
from agent_core.domain.runs import Run, RunKind
from agent_core.domain.sessions import (
    SESSION_BROWSER_PROFILE_METADATA_KEY,
    SESSION_SCHEDULE_ID_METADATA_KEY,
    Session,
)

BROWSER_ACT_VIEW: Final = "browser.act.v1"
# How tool.call.authorized and a denial name a task grant (ADR-0129 section 9).
TASK_GRANT_AUTHORIZATION_KIND: Final = "browser_task_grant"
TASK_GRANT_AUTHORIZED_REASON: Final = "browser.task_grant.authorized"
UNDESCRIBED_SUMMARY: Final = "Run a browser action the server can no longer describe"
BROWSER_TOOL_NAMES: frozenset[str] = frozenset(
    {"browser.navigate", "browser.observe", "browser.act"}
)
REDACTED_TEXT: Final = "[REDACTED]"
# Bounds of the untrusted and page-derived view fields (0129-design section 6.2).
_PATH_CHARACTERS: Final = 256
_TITLE_CHARACTERS: Final = 256
_ROLE_CHARACTERS: Final = 64
_NAME_CHARACTERS: Final = 256
_CONTEXT_CHARACTERS: Final = 128
_SUMMARY_PART_CHARACTERS: Final = 64
# Role words a summary may name; any other role reads "element".
_SUMMARY_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "checkbox",
        "radio",
        "switch",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "slider",
        "spinbutton",
        "treeitem",
    }
)
# Fields whose typed text an approval card never shows.
_HIDDEN_TEXT_FIELDS = frozenset(
    {BrowserFieldKind.PASSWORD, BrowserFieldKind.ONE_TIME_CODE, BrowserFieldKind.PAYMENT}
)
# Fields the isolated runtime refuses to type into, whatever authorized it.
_REFUSED_TYPING_FIELDS = frozenset({BrowserFieldKind.PASSWORD, BrowserFieldKind.ONE_TIME_CODE})


@dataclass(frozen=True, slots=True)
class TaskGrantSessionContext:
    """What the offer and grant checks read about a run's session (ADR-0129)."""

    session: Session | None
    profile: BrowserProfile | None
    active_grant: BrowserTaskGrant | None = None


@dataclass(frozen=True, slots=True)
class BrowserActView:
    """What the owner is asked to approve, and what the grant check needs."""

    summary: str
    arguments: dict[str, Any]
    described: bool
    observation: BrowserObservation | None = None
    element: BrowserElement | None = None
    facts: BrowserElementFacts | None = None


def _without_controls(value: str) -> str:
    cleaned = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in value
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(cleaned.split())


def _summary(action: BrowserAction, role: str, url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()[:_SUMMARY_PART_CHARACTERS]
    path = (parsed.path or "/")[:_SUMMARY_PART_CHARACTERS]
    where = f"{host}{path}"
    word = role if role in _SUMMARY_ROLES else "element"
    if action.kind is BrowserActionKind.CLICK:
        return f"Click a {word} on {where}"
    if action.kind is BrowserActionKind.TYPE:
        return f"Type into a text field on {where}"
    if action.kind is BrowserActionKind.SELECT:
        return f"Choose an option on {where}"
    if action.kind is BrowserActionKind.CHECK:
        return f"Toggle a {word} on {where}"
    if action.kind is BrowserActionKind.PRESS:
        key = "a key" if action.key is None else action.key.value
        return f"Press {key} on {where}"
    return f"Scroll the page on {where}"


def element_labels(element: BrowserElement, facts: BrowserElementFacts | None) -> list[str]:
    """Every label text the worker has: the model-visible name and each source."""

    return [element.name, *(() if facts is None else facts.labels.values())]


def option_texts(action: BrowserAction) -> tuple[str, ...]:
    if action.kind is BrowserActionKind.SELECT and action.value is not None:
        return (action.value,)
    return ()


def undescribed_view(action: BrowserAction) -> BrowserActView:
    return BrowserActView(
        summary=UNDESCRIBED_SUMMARY,
        arguments={"view": BROWSER_ACT_VIEW, "described": False, "kind": action.kind.value},
        described=False,
    )


def describe_browser_action(
    action: BrowserAction, snapshot: BrowserSnapshot | None
) -> BrowserActView:
    """The section 6.2 view of one action, or the undescribed view when the
    cached page no longer matches the action's revision and reference.

    It never carries the element reference, the revision, a query, a
    fragment, a link or form target, or any cookie, header or storage value.
    """

    if snapshot is None or snapshot.observation.revision != action.expected_revision:
        return undescribed_view(action)
    observation = snapshot.observation
    element = next((item for item in observation.elements if item.ref == action.ref), None)
    if element is None:
        return undescribed_view(action)
    try:
        origin = browser_origin(observation.url)
    except ValueError:
        return undescribed_view(action)
    facts = None
    if snapshot.facts is not None and snapshot.facts.revision == observation.revision:
        facts = snapshot.facts.elements.get(element.ref)
    role = _without_controls(element.role).casefold()
    consequence = classify_browser_action(
        kind=action.kind,
        role=element.role,
        labels=element_labels(element, facts),
        facts=facts,
        option_texts=option_texts(action),
    )
    arguments: dict[str, Any] = {
        "view": BROWSER_ACT_VIEW,
        "described": True,
        "kind": action.kind.value,
        "page_origin": origin,
        "page_path": (urlsplit(observation.url).path or "/")[:_PATH_CHARACTERS],
    }
    if observation.title:
        arguments["page_title"] = _without_controls(observation.title)[:_TITLE_CHARACTERS]
    arguments["element_role"] = role[:_ROLE_CHARACTERS]
    arguments["element_name"] = _without_controls(element.name)[:_NAME_CHARACTERS]
    if facts is not None:
        visible = facts.labels.get(BrowserLabelSource.VISIBLE_TEXT)
        if visible and normalize_text(visible) != normalize_text(element.name):
            arguments["element_text"] = _without_controls(visible)[:_NAME_CHARACTERS]
        if facts.context_name:
            arguments["element_context"] = _without_controls(facts.context_name)[
                :_CONTEXT_CHARACTERS
            ]
        arguments["field"] = facts.field_kind.value
    if action.kind is BrowserActionKind.TYPE:
        hidden = facts is not None and facts.field_kind in _HIDDEN_TEXT_FIELDS
        arguments["text"] = REDACTED_TEXT if hidden else (action.value or "")
    elif action.kind is BrowserActionKind.SELECT:
        arguments["option"] = action.value or ""
    elif action.kind is BrowserActionKind.PRESS and action.key is not None:
        arguments["key"] = action.key.value
    elif action.kind is BrowserActionKind.SCROLL:
        arguments["scroll_delta_y"] = action.delta_y
    elif action.kind is BrowserActionKind.CHECK:
        arguments["checked"] = element.checked
    arguments["consequence"] = consequence.value
    arguments["refused"] = (
        action.kind is BrowserActionKind.TYPE
        and facts is not None
        and facts.field_kind in _REFUSED_TYPING_FIELDS
    )
    return BrowserActView(
        summary=_summary(action, role, observation.url),
        arguments=arguments,
        described=True,
        observation=observation,
        element=element,
        facts=facts,
    )


def task_grant_offer_summary(scope: BrowserTaskGrantScope) -> str:
    """The fixed offer text (G9): what Veetbot recognises, and that the owner
    can stop it; never a promise that nothing else can happen."""

    host = urlsplit(scope.origin).hostname or ""
    return (
        f"Clicks and typing on {host}{scope.path_prefix} for 30 minutes, up to 200 actions. "
        "Passwords and one-time codes are never typed. Veetbot recognises payments, "
        "purchases, subscriptions and trials, account and settings changes, messages and "
        "posts, deletions, and signing out by the website's labels, and asks again for "
        "those. Stop this at any time."
    )


def session_is_task_grant_eligible(
    run: Run,
    *,
    session_tenant_id: str,
    session_principal_id: str,
    session_metadata: Mapping[str, Any],
    tenant_id: str,
    principal_id: str,
    turn: AuthorizationTurn | None,
) -> bool:
    """D1: an owner-driven, top-level interactive run of a profile-bound chat."""

    selected = session_metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    return (
        run.kind is RunKind.INTERACTIVE
        and run.parent_run_id is None
        and run.tenant_id == tenant_id
        and (session_tenant_id, session_principal_id) == (tenant_id, principal_id)
        and isinstance(selected, str)
        and bool(selected)
        and SESSION_SCHEDULE_ID_METADATA_KEY not in session_metadata
        and turn is not None
        and turn.newest_user_trust is TrustLevel.USER
    )


def turn_is_browser_only(turn: AuthorizationTurn | None) -> bool:
    """Only browser tools since the owner's newest message (G5)."""

    return turn is not None and turn.tool_names <= BROWSER_TOOL_NAMES


def task_grant_offer(
    *,
    enabled: bool,
    view: BrowserActView,
    action: BrowserAction,
    scopes: Collection[BrowserTaskGrantScope],
    eligible: bool,
    turn: AuthorizationTurn | None,
    profile: BrowserProfile | None,
    active_grant: BrowserTaskGrant | None,
) -> BrowserTaskGrantOffer | None:
    """Section 6.4: an offer only when every rule holds, else none.

    The offer's origin and prefix are the configured scope's, never the
    page's.
    """

    if not enabled or not view.described or view.observation is None or view.element is None:
        return None
    url = view.observation.url
    scope = offer_scope(url, tuple(scopes))
    if scope is None or path_is_sensitive(urlsplit(url).path):
        return None
    if not eligible or not turn_is_browser_only(turn):
        return None
    if (
        profile is None
        or profile.status is not BrowserProfileStatus.READY
        or scope.origin not in profile.allowed_origins
    ):
        return None
    coverage = task_grant_coverage(
        action=action,
        page_url=url,
        role=view.element.role,
        labels=element_labels(view.element, view.facts),
        facts=view.facts,
        option_texts=option_texts(action),
        origin=scope.origin,
        path_prefix=scope.path_prefix,
    )
    if not coverage.covered:
        return None
    if (
        active_grant is not None
        and active_grant.ended_at is None
        and (active_grant.origin, active_grant.path_prefix) == (scope.origin, scope.path_prefix)
    ):
        return None
    return BrowserTaskGrantOffer(
        origin=scope.origin,
        path_prefix=scope.path_prefix,
        summary=task_grant_offer_summary(scope),
    )
