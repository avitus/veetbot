"""ADR-0302 decisions 2, 3 and 5: what one request approval covers.

The request grant covers ordinary reading on any page of the login's origins,
including a download into the chat. Every write the classifier names, uploads,
sensitive fields and other origins still ask. After an outside read only
dates, numbers and short searches may be typed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionKind,
    BrowserCoverage,
    BrowserDispatchConstraint,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserTargetFacts,
)
from agent_core.domain.browser_classification import (
    classify_browser_action,
    dispatch_constraint_coverage,
    request_grant_coverage,
    short_typed_value,
)

ORIGIN = "https://secure.chase.com"
OTHER = "https://www.chase.com"
DASHBOARD = f"{ORIGIN}/web/auth/dashboard#/dashboard/overview"
DOWNLOADS = f"{ORIGIN}/web/auth/dashboard#/dashboard/transactions/downloads/1/CARD/BAC"
NOW = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
PLAIN = BrowserElementFacts(field_kind=BrowserFieldKind.NONE)
TEXTBOX = BrowserElementFacts(field_kind=BrowserFieldKind.TEXT)


def click() -> BrowserAction:
    return BrowserAction(kind=BrowserActionKind.CLICK, expected_revision="r1", ref="r1:0")


def type_text(value: str) -> BrowserAction:
    return BrowserAction(
        kind=BrowserActionKind.TYPE, expected_revision="r1", ref="r1:0", value=value
    )


def cover(
    action: BrowserAction,
    *,
    name: str,
    role: str = "button",
    facts: BrowserElementFacts | None = PLAIN,
    url: str = DASHBOARD,
    options: tuple[str, ...] = (),
    free_text: bool = True,
    origins: tuple[str, ...] = (ORIGIN,),
    disabled: bool = False,
) -> BrowserCoverage:
    return request_grant_coverage(
        action=action,
        page_url=url,
        role=role,
        labels=(name,),
        facts=facts,
        option_texts=options,
        origins=origins,
        disabled=disabled,
        free_text=free_text,
    )


@pytest.mark.parametrize(
    ("name", "role", "url"),
    [
        ("Freedom (...5668)", "button", DASHBOARD),
        ("See all transactions", "button", DASHBOARD),
        ("Activity, Since last statement", "button", DOWNLOADS),
        ("Last year Last year", "option", DOWNLOADS),
        ("Year to date Year to date", "option", DOWNLOADS),
        ("2024 Statements, collapsed 2024 Statements", "option", DOWNLOADS),
        ("Download other activity", "button", DOWNLOADS),
        ("Download account activity", "button", DASHBOARD),
        ("Download", "button", DOWNLOADS),
    ],
)
def test_the_chase_review_controls_are_covered_on_the_authenticated_path(
    name: str, role: str, url: str
) -> None:
    coverage = cover(click(), name=name, role=role, url=url)

    assert coverage.covered, coverage.reason


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("Pay card", "excluded.payment"),
        ("Make a payment", "excluded.payment"),
        ("Transfer money", "excluded.payment"),
        ("Send message", "excluded.publication"),
        ("Update email address", "excluded.security_change"),
        ("Account settings", "excluded.security_change"),
        ("Delete payee", "excluded.payment"),
        ("Remove card from wallet", "excluded.payment"),
        ("Cancel autopay", "excluded.destructive"),
        ("Sign out", "excluded.authentication_change"),
        ("Accept terms", "excluded.legal_acceptance"),
        ("Upload documents", "excluded.file_transfer"),
        ("Attach statement", "excluded.file_transfer"),
        ("Import transactions file", "excluded.file_transfer"),
    ],
)
def test_named_writes_still_ask(name: str, reason: str) -> None:
    coverage = cover(click(), name=name)

    assert not coverage.covered
    assert coverage.reason == reason


def test_a_file_input_still_asks_even_when_labelled_download() -> None:
    facts = BrowserElementFacts(field_kind=BrowserFieldKind.FILE)

    coverage = cover(click(), name="Download", facts=facts)

    assert (coverage.covered, coverage.reason) == (False, "excluded.file_transfer")


@pytest.mark.parametrize(
    "kind",
    [
        BrowserFieldKind.PASSWORD,
        BrowserFieldKind.ONE_TIME_CODE,
        BrowserFieldKind.PAYMENT,
        BrowserFieldKind.IDENTITY,
    ],
)
def test_sensitive_fields_still_ask(kind: BrowserFieldKind) -> None:
    coverage = cover(
        type_text("12/31/2024"), name="Value", facts=BrowserElementFacts(field_kind=kind)
    )

    assert not coverage.covered


def test_a_link_to_another_origin_still_asks() -> None:
    facts = BrowserElementFacts(
        field_kind=BrowserFieldKind.NONE,
        link_target=BrowserTargetFacts(same_origin=False, first_segment="offers"),
    )

    coverage = cover(click(), name="Explore offers", role="link", facts=facts)

    assert (coverage.covered, coverage.reason) == (False, "link_outside_origin")


def test_a_same_origin_link_on_a_sensitive_path_is_covered() -> None:
    facts = BrowserElementFacts(
        field_kind=BrowserFieldKind.NONE,
        link_target=BrowserTargetFacts(same_origin=True, first_segment="web", sensitive_path=True),
    )

    coverage = cover(click(), name="Statements & documents", role="link", facts=facts)

    assert coverage.covered, coverage.reason


def test_a_form_posting_to_another_origin_still_asks() -> None:
    facts = BrowserElementFacts(
        field_kind=BrowserFieldKind.NONE,
        form_target=BrowserTargetFacts(same_origin=False, first_segment="collect"),
    )

    coverage = cover(click(), name="Search", facts=facts)

    assert (coverage.covered, coverage.reason) == (False, "form_outside_origin")


def test_a_page_outside_the_grant_origins_asks() -> None:
    coverage = cover(click(), name="See all transactions", url=f"{OTHER}/personal")

    assert (coverage.covered, coverage.reason) == (False, "outside_origin")


def test_any_of_the_login_origins_is_covered() -> None:
    coverage = cover(
        click(), name="See all transactions", url=f"{OTHER}/personal", origins=(ORIGIN, OTHER)
    )

    assert coverage.covered, coverage.reason


def test_unnamed_disabled_and_truncated_elements_ask() -> None:
    assert cover(click(), name="").reason == "unnamed_element"
    assert cover(click(), name="Next", disabled=True).reason == "element_disabled"
    truncated = BrowserElementFacts(field_kind=BrowserFieldKind.NONE, labels_truncated=True)
    assert cover(click(), name="Next", facts=truncated).reason == "facts_unavailable"
    assert cover(click(), name="Next", facts=None).reason == "facts_unavailable"


@pytest.mark.parametrize(
    "value",
    [
        "11/06/2024",
        "12/31/2024",
        "2024-11-06",
        "Nov 6, 2024",
        "6 November 2024",
        "1,234.56",
        "$54.97",
        "42",
        "Adobe",
        "K&L Wines",
        "spotify premium",
        "Jazz Wire",
    ],
)
def test_dates_numbers_and_short_searches_are_short_values(value: str) -> None:
    assert short_typed_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "13/45/2024",
        "a" * 41,
        "me@example.com",
        "https://evil.example",
        "www.example.com",
        "4111 1111 1111 1111",
        "4111111111111111",
        "acct 123456789",
        "password: hunter2",
        "line one\nline two",
        "",
    ],
)
def test_other_text_is_not_a_short_value(value: str) -> None:
    assert not short_typed_value(value)


def test_short_values_are_typed_after_an_outside_read() -> None:
    for value in ("11/06/2024", "Adobe", "54.97"):
        coverage = cover(
            type_text(value), name="From", role="textbox", facts=TEXTBOX, free_text=False
        )
        assert coverage.covered, (value, coverage.reason)


def test_free_text_asks_after_an_outside_read() -> None:
    coverage = cover(
        type_text("recurring charges from my wine club and Adobe plans"),
        name="Search",
        role="textbox",
        facts=TEXTBOX,
        free_text=False,
    )

    assert (coverage.covered, coverage.reason) == (False, "text_not_covered")


def test_free_text_is_covered_in_a_request_with_no_outside_read() -> None:
    coverage = cover(
        type_text("recurring charges from my wine club and Adobe plans"),
        name="Search",
        role="textbox",
        facts=TEXTBOX,
    )

    assert coverage.covered, coverage.reason


def test_free_text_still_refuses_credential_shapes_and_long_text() -> None:
    assert (
        cover(type_text("x" * 257), name="Search", role="textbox", facts=TEXTBOX).reason
        == "text_too_long"
    )
    assert (
        cover(type_text("ping me@example.com"), name="Search", role="textbox", facts=TEXTBOX).reason
        == "text_not_covered"
    )


def test_account_names_a_document_beside_activity_or_statements() -> None:
    for name in (
        "Download account activity",
        "Account statements",
        "Account summary",
        "Account details",
        "Account history",
    ):
        consequence = classify_browser_action(
            kind=click().kind, role="button", labels=(name,), facts=PLAIN, option_texts=()
        )
        assert consequence is not BrowserActionConsequence.SECURITY_CHANGE, name
    still = classify_browser_action(
        kind=click().kind, role="button", labels=("Manage account",), facts=PLAIN, option_texts=()
    )
    assert still is BrowserActionConsequence.SECURITY_CHANGE


def constraint(**update: object) -> BrowserDispatchConstraint:
    values: dict[str, object] = {
        "grant_kind": "request",
        "origins": (ORIGIN,),
        "path_prefix": None,
        "not_after": NOW + timedelta(hours=1),
        "consequence_ceiling": "unknown",
        "max_text_characters": 256,
    }
    values.update(update)
    return BrowserDispatchConstraint.model_validate(values)


def test_a_request_constraint_has_no_prefix_and_a_text_cap() -> None:
    assert constraint().free_text is False
    assert constraint(free_text=True).free_text is True
    with pytest.raises(ValueError):
        constraint(path_prefix="/lesson")
    with pytest.raises(ValueError):
        constraint(max_text_characters=None)
    with pytest.raises(ValueError):
        constraint(consequence_ceiling="routine")
    with pytest.raises(ValueError):
        constraint(card_activity_only=True)


def test_a_request_constraint_without_free_text_serializes_as_before() -> None:
    assert "free_text" not in constraint().model_dump(mode="json")


def live(action: BrowserAction, name: str, **update: object) -> BrowserCoverage:
    values: dict[str, object] = {
        "constraint": constraint(),
        "page_url": DOWNLOADS,
        "role": "button",
        "facts": PLAIN,
        "runtime_origins": (ORIGIN,),
        "now": NOW,
    }
    values.update(update)
    grant = values.pop("constraint")
    return dispatch_constraint_coverage(
        grant,  # type: ignore[arg-type]
        action=action,
        labels=(name,),
        option_texts=(),
        disabled=False,
        **values,  # type: ignore[arg-type]
    )


def test_the_runtime_repeats_request_coverage_on_the_live_page() -> None:
    assert live(click(), "Download").covered
    assert live(click(), "Make a payment").reason == "excluded.payment"
    assert live(click(), "Download", now=NOW + timedelta(hours=2)).reason == "expired"
    assert live(click(), "Download", runtime_origins=(OTHER,)).reason == "outside_origin"


def test_the_runtime_types_free_text_only_when_the_worker_allowed_it() -> None:
    text = type_text("recurring charges from my wine club and Adobe plans")

    assert live(text, "Search", role="textbox", facts=TEXTBOX).reason == "text_not_covered"
    assert live(
        text, "Search", role="textbox", facts=TEXTBOX, constraint=constraint(free_text=True)
    ).covered


def test_a_request_acts_document_fence_is_the_login_origins_on_any_path() -> None:
    from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime

    runtime = PythonPlaywrightRuntime()
    runtime._document_fence = (frozenset({ORIGIN, OTHER}), None)

    assert runtime._inside_document_fence(f"{ORIGIN}/web/auth/statements")
    assert runtime._inside_document_fence(f"{OTHER}/personal/credit-cards")
    assert not runtime._inside_document_fence("https://tracker.example/collect")


def test_a_task_acts_document_fence_keeps_its_prefix_and_sensitive_paths() -> None:
    from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime

    runtime = PythonPlaywrightRuntime()
    runtime._document_fence = (frozenset({ORIGIN}), "/lesson")

    assert runtime._inside_document_fence(f"{ORIGIN}/lesson/2")
    assert not runtime._inside_document_fence(f"{ORIGIN}/web/auth/statements")
    assert not runtime._inside_document_fence(f"{OTHER}/lesson/2")
