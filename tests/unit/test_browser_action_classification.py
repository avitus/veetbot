"""ADR-0129: the shared, deny-biased browser action classifier and task-grant coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionKind,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserKey,
    BrowserLabelSource,
    BrowserObservation,
    BrowserTargetFacts,
)
from agent_core.domain.browser_classification import (
    classify_browser_action,
    dispatch_constraint_coverage,
    path_is_sensitive,
    segment_is_sensitive,
    standing_ceiling_coverage,
    task_grant_coverage,
    typed_text_allowed,
)
from agent_core.domain.browser_task_grants import (
    BrowserTaskGrantScope,
    offer_scope,
    parse_task_grant_scopes,
)
from tests.contract.support import NOW, tool_context
from tests.unit.test_hosted_browser_provider import FakeSessions, ready_provider


def words(text: str) -> tuple[str, ...]:
    return tuple(text.split())


C = BrowserActionConsequence
ORIGIN = "https://www.example.com"
LESSON = f"{ORIGIN}/lesson/unit-3"

# The exclusion vocabulary of the design, category by category, in the order
# the classifier tries them. This table is the specification the module must
# meet, so it is written out here rather than imported.
VOCABULARY: dict[C, tuple[str, ...]] = {
    C.PAYMENT: (
        *words(
            "pay pays paying payment payments paypal billing bill bills invoice invoices "
            "checkout card cards credit debit wallet refund refunds donate donation tip "
            "tips cvv cvc iban usd eur gbp jpy cny inr cad aud chf gem gems lingot lingots "
            "coin coins spend refill transfer transfers wire withdraw withdrawal deposit "
            "payee remit"
        ),
        "check out",
    ),
    C.PURCHASE: (
        *words(
            "buy buying purchase purchases order orders ordering cart basket shop store "
            "subscribe subscribed subscription subscriptions trial trials free upgrade "
            "upgrades premium renew renewal gift gifts redeem coupon promo membership plans "
            "sell trade invest bid booking reservation"
        ),
        "choose a plan",
        "choose plan",
        "select plan",
        "change plan",
        "add to cart",
    ),
    C.AUTHENTICATION_CHANGE: (
        *words(
            "password passwords passcode passkey passkeys otp 2fa mfa authenticator verify "
            "verification login logout logon logoff signin signout signup register "
            "registration reauthenticate"
        ),
        "log in",
        "log out",
        "log off",
        "sign in",
        "sign out",
        "sign off",
        "sign up",
        "two factor",
        "one time code",
        "switch account",
        "switch accounts",
    ),
    C.ACCOUNT_RECOVERY: words("recover recovery forgot forgotten reset"),
    C.SECURITY_CHANGE: words(
        "account accounts settings setting preferences preference privacy security "
        "profile username email emails phone address notifications notification edit "
        "change update manage"
    ),
    C.PERMISSION_CHANGE: words(
        "permission permissions allow block unblock connect disconnect link unlink "
        "authorize authorise grant revoke unsubscribe"
    ),
    C.LEGAL_ACCEPTANCE: words("accept accepted agree agreed consent terms tos eula"),
    C.PUBLICATION: words(
        "send sent post posts publish share reply comment comments message messages chat "
        "invite tweet submit report follow unfollow friend friends discuss discussion "
        "forum feedback contact dm nudge repost retweet reblog upvote"
    ),
    C.DESTRUCTIVE: words(
        "delete deleting deletion remove removing erase discard trash destroy deactivate "
        "cancel cancellation wipe purge unenroll archive"
    ),
    C.FILE_TRANSFER: words(
        "upload uploads download downloads attach attachment attachments import export"
    ),
}
ROUTINE_NAMES = (
    "continue",
    "done",
    "finish",
    "got it",
    "next",
    "practice",
    "review",
    "skip",
    "start",
    "try again",
)


def facts(**changes: Any) -> BrowserElementFacts:
    values: dict[str, Any] = {"field_kind": BrowserFieldKind.NONE}
    values.update(changes)
    return BrowserElementFacts(**values)


def click(**changes: Any) -> BrowserAction:
    values: dict[str, Any] = {
        "kind": BrowserActionKind.CLICK,
        "expected_revision": "r1",
        "ref": "r1:0",
    }
    values.update(changes)
    return BrowserAction(**values)


def classify(
    *labels: str,
    kind: BrowserActionKind = BrowserActionKind.CLICK,
    role: str = "button",
    element: BrowserElementFacts | None = None,
    options: tuple[str, ...] = (),
) -> C:
    return classify_browser_action(
        kind=kind, role=role, labels=labels, facts=element, option_texts=options
    )


def cover(
    action: BrowserAction | None = None,
    *,
    labels: tuple[str, ...] = ("Continue",),
    element: BrowserElementFacts | str | None = "default",
    page_url: str = LESSON,
    role: str = "button",
) -> tuple[bool, str | None, C]:
    chosen = facts() if element == "default" else element
    assert not isinstance(chosen, str)
    act = action or click()
    coverage = task_grant_coverage(
        action=act,
        page_url=page_url,
        role=role,
        labels=labels,
        facts=chosen,
        option_texts=(act.value,) if act.kind is BrowserActionKind.SELECT and act.value else (),
        origin=ORIGIN,
        path_prefix="/lesson",
    )
    return coverage.covered, coverage.reason, coverage.consequence


def test_every_vocabulary_entry_names_its_consequence() -> None:
    wrong = {
        entry: classify(entry)
        for category, entries in VOCABULARY.items()
        for entry in entries
        if classify(entry) is not category
    }
    assert wrong == {}


def test_inflected_and_irregular_forms_name_the_consequence() -> None:
    expected = {
        "paid": C.PAYMENT,
        "Spent": C.PAYMENT,
        "withdrew": C.PAYMENT,
        "withdrawn": C.PAYMENT,
        "Checking out": C.PAYMENT,
        "checked out": C.PAYMENT,
        "Refill gems": C.PAYMENT,
        "tipped": C.PAYMENT,
        "bought": C.PURCHASE,
        "sold": C.PURCHASE,
        "Buyer": C.PURCHASE,
        "subscribing": C.PURCHASE,
        "Chose a plan": C.PURCHASE,
        "chosen plan": C.PURCHASE,
        "Added to cart": C.PURCHASE,
        "Upgraded": C.PURCHASE,
        "verified": C.AUTHENTICATION_CHANGE,
        "Verifies": C.AUTHENTICATION_CHANGE,
        "Signed in": C.AUTHENTICATION_CHANGE,
        "signing up": C.AUTHENTICATION_CHANGE,
        "Logged out": C.AUTHENTICATION_CHANGE,
        "Switched accounts": C.AUTHENTICATION_CHANGE,
        "Resetting": C.ACCOUNT_RECOVERY,
        "changed": C.SECURITY_CHANGE,
        "Updating": C.SECURITY_CHANGE,
        "linked": C.PERMISSION_CHANGE,
        "agreeing": C.LEGAL_ACCEPTANCE,
        "sent": C.PUBLICATION,
        "Sender": C.PUBLICATION,
        "shared": C.PUBLICATION,
        "Deleted": C.DESTRUCTIVE,
        "cancelled": C.DESTRUCTIVE,
        "uploading": C.FILE_TRANSFER,
    }
    assert {text: classify(text) for text in expected} == expected


def test_the_old_hard_exclusions_are_named_consequences() -> None:
    expected = {
        "accept": C.LEGAL_ACCEPTANCE,
        "agree": C.LEGAL_ACCEPTANCE,
        "buy": C.PURCHASE,
        "order": C.PURCHASE,
        "checkout": C.PAYMENT,
        "pay": C.PAYMENT,
        "delete": C.DESTRUCTIVE,
        "remove": C.DESTRUCTIVE,
        "download": C.FILE_TRANSFER,
        "upload": C.FILE_TRANSFER,
        "password": C.AUTHENTICATION_CHANGE,
        "post": C.PUBLICATION,
        "publish": C.PUBLICATION,
        "submit": C.PUBLICATION,
        "recover": C.ACCOUNT_RECOVERY,
        "security": C.SECURITY_CHANGE,
    }
    assert {word: classify(word) for word in expected} == expected
    for word in expected:
        for kind in BrowserActionKind:
            assert classify(word, kind=kind) not in {C.ROUTINE, C.UNKNOWN}, (word, kind)


def test_every_label_source_is_classified_separately() -> None:
    hidden_pay = facts(labels={BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"})
    assert classify("Continue", element=hidden_pay) is C.PAYMENT

    for source in BrowserLabelSource:
        element = facts(labels={source: "Buy now"})
        assert classify("Continue", *element.labels.values(), element=element) is C.PURCHASE
    assert classify("Continue", element=facts(context_name="Try Super free")) is C.PURCHASE
    assert (
        classify("Choose", kind=BrowserActionKind.SELECT, role="combobox", options=("Remove",))
        is C.DESTRUCTIVE
    )

    standing = standing_ceiling_coverage(
        action=click(),
        page_url=LESSON,
        role="button",
        labels=("Continue", "Pay $12.99"),
        facts=hidden_pay,
        option_texts=(),
        origins=(ORIGIN,),
    )
    assert (standing.covered, standing.consequence) == (False, C.PAYMENT)


def test_case_hyphen_zero_width_and_combining_variants_are_normalized() -> None:
    variants = {
        "PAY NOW": C.PAYMENT,
        "pay-ment": C.PAYMENT,
        "Pay\u200bment": C.PAYMENT,
        "Pa\u0300yment": C.PAYMENT,
        "\uff30\uff21\uff39": C.PAYMENT,
        "check-out": C.PAYMENT,
        "Sign\u00adin": C.AUTHENTICATION_CHANGE,
        "log\u2011in": C.AUTHENTICATION_CHANGE,
        "D\u200delete": C.DESTRUCTIVE,
        "BUY": C.PURCHASE,
        "Delete\u0000": C.DESTRUCTIVE,
    }
    assert {text: classify(text) for text in variants} == variants


def test_right_to_left_overrides_read_as_the_text_they_display() -> None:
    """A right-to-left override shows the letters after it reversed, so
    "\u202eyaP" displays "Pay"; the classifier reads the displayed order too."""

    variants = {
        "\u202eyaP": C.PAYMENT,
        "Keep \u202eyaP\u202c now": C.PAYMENT,
        "D\u202eetele\u202c": C.DESTRUCTIVE,
        "\u202etuo kcehc": C.PAYMENT,
        "Keep \u202eebircsbuS\u202c going": C.PURCHASE,
        "\u202eyaP\u2069 and \u202eeteleD": C.PAYMENT,
        "\u202dPay": C.PAYMENT,
    }
    assert {text: classify(text) for text in variants} == variants
    assert classify("\u202eeunitnoC\u202c") is C.UNKNOWN


def test_letters_that_look_latin_read_as_the_latin_letters() -> None:
    """Cyrillic, Greek, Armenian and Latin small-capital lookalikes fold to the
    Latin letters they show; genuine words in those scripts stay unknown."""

    variants = {
        "D\u0435l\u0435t\u0435": C.DESTRUCTIVE,
        "\u0420\u0430y": C.PAYMENT,
        "\u03a1\u03b1y": C.PAYMENT,
        "\u0405ubscrib\u0435": C.PURCHASE,
        "\u0405UBS\u0421RIB\u0415": C.PURCHASE,
        "\u1d18\u1d00\u028f": C.PAYMENT,
        "S\u0578\u0585": C.UNKNOWN,
        "\u0441\u0430rd": C.PAYMENT,
        "\u0433\u0435\u043c": C.UNKNOWN,
        "\u043a\u043e\u0442": C.UNKNOWN,
        "\u043d\u0435\u0442": C.UNKNOWN,
        "\u03bd\u03b1\u03b9": C.UNKNOWN,
    }
    assert {text: classify(text) for text in variants} == variants


def test_currency_symbols_are_payments() -> None:
    for text in ("\u20ac5", "12 \u20b9", "\u00a5", "\u00a3 3", "$", "\uff04 1", "Go \u20bf"):
        assert classify(text) is C.PAYMENT, text


def test_only_one_routine_word_on_every_label_is_routine() -> None:
    for name in ROUTINE_NAMES:
        for role in ("button", "link"):
            assert classify(name.upper(), role=role) is C.ROUTINE, (name, role)
    assert classify("Got it!") is C.ROUTINE
    assert classify("Continue", "CONTINUE") is C.ROUTINE
    assert classify(kind=BrowserActionKind.SCROLL) is C.ROUTINE

    not_routine = [
        classify("Continue", "Next"),
        classify("Continue", role="checkbox"),
        classify("Continue", kind=BrowserActionKind.TYPE),
        classify(),
        classify(""),
        classify("Continue to lesson"),
        classify("Answer"),
    ]
    assert not_routine == [C.UNKNOWN] * len(not_routine)
    assert classify("Buy", kind=BrowserActionKind.SCROLL) is C.PURCHASE


def test_field_kinds_downloads_and_switches_name_their_consequence() -> None:
    expected = {
        BrowserFieldKind.PASSWORD: C.AUTHENTICATION_CHANGE,
        BrowserFieldKind.ONE_TIME_CODE: C.AUTHENTICATION_CHANGE,
        BrowserFieldKind.PAYMENT: C.PAYMENT,
        BrowserFieldKind.IDENTITY: C.SECURITY_CHANGE,
        BrowserFieldKind.EMAIL: C.SECURITY_CHANGE,
        BrowserFieldKind.TELEPHONE: C.SECURITY_CHANGE,
        BrowserFieldKind.FILE: C.FILE_TRANSFER,
    }
    found = {
        kind: classify("Answer", kind=BrowserActionKind.TYPE, element=facts(field_kind=kind))
        for kind in expected
    }
    assert found == expected
    assert classify("Continue", element=facts(download=True)) is C.FILE_TRANSFER
    assert classify("Dark mode", role="switch") is C.SECURITY_CHANGE
    assert (
        classify("Answer", kind=BrowserActionKind.TYPE, element=facts(field_kind="text"))
        is C.UNKNOWN
    )


def test_a_link_or_form_target_is_classified_by_its_path() -> None:
    to_checkout = facts(link_target=BrowserTargetFacts(same_origin=True, first_segment="checkout"))
    assert classify("Continue", element=to_checkout) is C.PAYMENT
    deep_billing = facts(
        form_target=BrowserTargetFacts(same_origin=True, first_segment="app", sensitive_path=True)
    )
    assert classify("Continue", element=deep_billing) is not C.ROUTINE
    inside = facts(
        link_target=BrowserTargetFacts(
            same_origin=True, first_segment="lesson", sensitive_path=False
        )
    )
    assert classify("Continue", element=inside) is C.ROUTINE


def test_every_path_segment_is_checked_for_sensitivity() -> None:
    sensitive = [
        "/lesson/settings",
        "/app/billing/x",
        "/Check%20out",
        "/admin",
        "/api/v1",
        "/OAuth/start",
        "/sso",
        "/auth",
        "/pricing",
        "/plus",
        "/inbox",
        "/compose",
        "/shop/items",
        "/lesson/subscriptions",
        "/%E2%82%AC",
        "/lesson/log-in",
    ]
    ordinary = ["/", "", "/lesson", "/lesson/unit-3", "/learn", "/lessons/12", "/practice-hub"]
    assert [path for path in sensitive if not path_is_sensitive(path)] == []
    assert [path for path in ordinary if path_is_sensitive(path)] == []
    assert segment_is_sensitive("Settings")
    assert not segment_is_sensitive("lesson")


def test_typed_text_shapes_are_refused() -> None:
    refused = [
        "a@b",
        "https://x.example",
        "go to www.example.com",
        "WWW.EXAMPLE.COM",
        "12345",
        "call 1234 now",
        "\uff11\uff12\uff13\uff14",
        "token: x",
        "password=hunter2",
        "\uff20home",
    ]
    allowed = ["hola", "el gato", "123", "12 34", "Je suis 1 chat", ""]
    assert [text for text in refused if typed_text_allowed(text)] == []
    assert [text for text in allowed if not typed_text_allowed(text)] == []


def test_task_grant_coverage_names_the_first_failing_rule() -> None:
    def typed(value: str) -> BrowserAction:
        return click(kind=BrowserActionKind.TYPE, value=value)

    text_field = facts(field_kind=BrowserFieldKind.TEXT)
    outside_form = BrowserTargetFacts(same_origin=True, first_segment="learn", sensitive_path=False)
    cases: dict[str, tuple[tuple[bool, str | None, C], tuple[bool, str | None]]] = {
        "no facts": (cover(element=None), (False, "facts_unavailable")),
        "another origin": (
            cover(page_url="https://example.com/lesson"),
            (False, "outside_origin"),
        ),
        "a page off the prefix": (cover(page_url=f"{ORIGIN}/learn"), (False, "outside_prefix")),
        "a neighbouring prefix": (
            cover(page_url=f"{ORIGIN}/lessons"),
            (False, "outside_prefix"),
        ),
        "a sensitive page segment": (
            cover(page_url=f"{ORIGIN}/lesson/settings"),
            (False, "sensitive_path"),
        ),
        "an unnamed element": (cover(labels=("", "  ")), (False, "unnamed_element")),
        "a purchase": (cover(labels=("Buy now",)), (False, "excluded.purchase")),
        "a hidden payment": (
            cover(element=facts(labels={BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"})),
            (False, "excluded.payment"),
        ),
        "typing into a number field": (
            cover(typed("12"), labels=("Age",), element=facts(field_kind=BrowserFieldKind.NUMBER)),
            (False, "field_not_covered"),
        ),
        "choosing in a text field": (
            cover(
                click(kind=BrowserActionKind.SELECT, value="gato"),
                labels=("Word",),
                element=text_field,
            ),
            (False, "field_not_covered"),
        ),
        "checking a button": (
            cover(click(kind=BrowserActionKind.CHECK)),
            (False, "field_not_covered"),
        ),
        "pressing in a URL field": (
            cover(
                click(kind=BrowserActionKind.PRESS, key=BrowserKey.ENTER),
                labels=("Site",),
                element=facts(field_kind=BrowserFieldKind.URL),
            ),
            (False, "field_not_covered"),
        ),
        "clicking an unrecognized field": (
            cover(labels=("Answer",), element=facts(field_kind=BrowserFieldKind.OTHER)),
            (False, "field_not_covered"),
        ),
        "257 typed characters": (
            cover(typed("a" * 257), labels=("Answer",), element=text_field),
            (False, "text_too_long"),
        ),
        "a typed address": (
            cover(typed("me@example.com"), labels=("Answer",), element=text_field),
            (False, "text_not_covered"),
        ),
        "a sensitive link": (
            cover(
                element=facts(
                    link_target=BrowserTargetFacts(
                        same_origin=True, first_segment="lesson", sensitive_path=True
                    )
                )
            ),
            (False, "link_outside_prefix"),
        ),
        "a link to another origin": (
            cover(
                element=facts(
                    link_target=BrowserTargetFacts(
                        same_origin=False, first_segment="lesson", sensitive_path=False
                    )
                )
            ),
            (False, "link_outside_prefix"),
        ),
        "a link off the prefix": (
            cover(element=facts(link_target=outside_form)),
            (False, "link_outside_prefix"),
        ),
        "a submit off the prefix": (
            cover(element=facts(form_target=outside_form)),
            (False, "form_outside_prefix"),
        ),
        "Enter in a form off the prefix": (
            cover(
                click(kind=BrowserActionKind.PRESS, key=BrowserKey.ENTER),
                labels=("Answer",),
                element=facts(field_kind=BrowserFieldKind.TEXT, form_target=outside_form),
            ),
            (False, "form_outside_prefix"),
        ),
        "a routine click": (cover(), (True, None)),
        "an answer tile": (cover(labels=("el gato",)), (True, None)),
        "typing an answer": (
            cover(typed("el gato"), labels=("Answer",), element=text_field),
            (True, None),
        ),
        "choosing an option": (
            cover(
                click(kind=BrowserActionKind.SELECT, value="gato"),
                labels=("Word",),
                element=facts(field_kind=BrowserFieldKind.CHOICE),
            ),
            (True, None),
        ),
        "checking a choice in a form off the prefix": (
            cover(
                click(kind=BrowserActionKind.CHECK),
                labels=("gato",),
                element=facts(field_kind=BrowserFieldKind.CHOICE, form_target=outside_form),
            ),
            (True, None),
        ),
        "Tab in a form off the prefix": (
            cover(
                click(kind=BrowserActionKind.PRESS, key=BrowserKey.TAB),
                labels=("Answer",),
                element=facts(field_kind=BrowserFieldKind.TEXT, form_target=outside_form),
            ),
            (True, None),
        ),
        "a scroll over an unnamed element": (
            cover(click(kind=BrowserActionKind.SCROLL, delta_y=400), labels=()),
            (True, None),
        ),
        "the prefix itself": (cover(page_url=f"{ORIGIN}/lesson"), (True, None)),
    }
    wrong = {name: found[:2] for name, (found, expected) in cases.items() if found[:2] != expected}
    assert wrong == {}
    assert cases["a purchase"][0][2] is C.PURCHASE
    assert cases["a routine click"][0][2] is C.ROUTINE
    assert cases["an answer tile"][0][2] is C.UNKNOWN


def test_every_form_submission_is_checked_against_the_prefix() -> None:
    """Rule 11: any click, and Enter or Space, can submit the element's form.

    A submit button keeps its form's target whatever its ARIA role, and Space
    on a focused submit button submits as a click does (review finding).
    """

    outside = BrowserTargetFacts(same_origin=True, first_segment="courses", sensitive_path=True)
    inside = BrowserTargetFacts(same_origin=True, first_segment="lesson", sensitive_path=False)

    def press(key: BrowserKey) -> BrowserAction:
        return click(kind=BrowserActionKind.PRESS, key=key)

    cases: dict[str, tuple[tuple[bool, str | None, C], tuple[bool, str | None]]] = {
        "Space on a submit button": (
            cover(press(BrowserKey.SPACE), labels=("Check",), element=facts(form_target=outside)),
            (False, "form_outside_prefix"),
        ),
        "Enter on a submit button": (
            cover(press(BrowserKey.ENTER), labels=("Check",), element=facts(form_target=outside)),
            (False, "form_outside_prefix"),
        ),
        "a click on a submit button with a choice role": (
            cover(
                labels=("Spanish",),
                role="radio",
                element=facts(field_kind=BrowserFieldKind.CHOICE, form_target=outside),
            ),
            (False, "form_outside_prefix"),
        ),
        "a click on a text field in a form off the prefix": (
            cover(
                labels=("Answer",),
                element=facts(field_kind=BrowserFieldKind.TEXT, form_target=outside),
            ),
            (False, "form_outside_prefix"),
        ),
        "Space on a submit button inside the prefix": (
            cover(press(BrowserKey.SPACE), labels=("Check",), element=facts(form_target=inside)),
            (True, None),
        ),
        "a choice-role submit button inside the prefix": (
            cover(
                labels=("Spanish",),
                role="radio",
                element=facts(field_kind=BrowserFieldKind.CHOICE, form_target=inside),
            ),
            (True, None),
        ),
        "an arrow key in a text field in a form off the prefix": (
            cover(
                press(BrowserKey.ARROW_LEFT),
                labels=("Answer",),
                element=facts(field_kind=BrowserFieldKind.TEXT, form_target=outside),
            ),
            (True, None),
        ),
        "Escape on a button in a form off the prefix": (
            cover(press(BrowserKey.ESCAPE), labels=("Check",), element=facts(form_target=outside)),
            (True, None),
        ),
    }
    wrong = {name: found[:2] for name, (found, expected) in cases.items() if found[:2] != expected}
    assert wrong == {}


def test_a_key_press_on_a_choice_control_is_not_covered() -> None:
    """Rule 7: an arrow key moves a radio group to an option nobody classified.

    Select and check classify the option they choose, so choice controls are
    covered through them and never through press (review finding).
    """

    choice = facts(field_kind=BrowserFieldKind.CHOICE)
    found = {
        key.value: cover(
            click(kind=BrowserActionKind.PRESS, key=key),
            labels=("Keep learning",),
            role="radio",
            element=choice,
        )[:2]
        for key in BrowserKey
    }
    assert found == {key.value: (False, "field_not_covered") for key in BrowserKey}
    assert cover(click(kind=BrowserActionKind.CHECK), labels=("Keep learning",), element=choice)[
        :2
    ] == (True, None)


def test_standing_ceiling_covers_only_routine_actions_on_its_origins() -> None:
    def ceiling(*labels: str, page_url: str = LESSON) -> tuple[bool, str | None]:
        coverage = standing_ceiling_coverage(
            action=click(),
            page_url=page_url,
            role="button",
            labels=labels,
            facts=None,
            option_texts=(),
            origins=(ORIGIN,),
        )
        return coverage.covered, coverage.reason

    assert ceiling("Continue") == (True, None)
    assert ceiling("Continue", page_url="https://example.com/lesson") == (False, "outside_origin")
    assert ceiling("Answer") == (False, "excluded.unknown")
    assert ceiling("Continue", "Delete") == (False, "excluded.destructive")


def test_dispatch_constraints_check_expiry_and_origins_before_coverage() -> None:
    task = BrowserDispatchConstraint(
        grant_kind="task",
        origins=(ORIGIN,),
        path_prefix="/lesson",
        not_after=NOW + timedelta(minutes=30),
        consequence_ceiling="unknown",
        max_text_characters=256,
    )
    standing = BrowserDispatchConstraint(
        grant_kind="standing",
        origins=(ORIGIN,),
        not_after=NOW + timedelta(days=1),
        consequence_ceiling="routine",
        max_text_characters=None,
    )

    def check(
        constraint: BrowserDispatchConstraint,
        *,
        page_url: str = LESSON,
        labels: tuple[str, ...] = ("Continue",),
        origins: tuple[str, ...] = (ORIGIN,),
        now_offset: timedelta = timedelta(),
    ) -> tuple[bool, str | None]:
        coverage = dispatch_constraint_coverage(
            constraint,
            action=click(),
            page_url=page_url,
            role="button",
            labels=labels,
            facts=facts(),
            option_texts=(),
            runtime_origins=origins,
            now=NOW + now_offset,
        )
        return coverage.covered, coverage.reason

    assert check(task) == (True, None)
    assert check(task, now_offset=timedelta(minutes=30)) == (False, "expired")
    assert check(task, origins=("https://example.com",)) == (False, "outside_origin")
    assert check(task, page_url=f"{ORIGIN}/learn") == (False, "outside_prefix")
    assert check(task, labels=("el gato",)) == (True, None)
    assert check(standing, page_url=f"{ORIGIN}/learn") == (True, None)
    assert check(standing, labels=("el gato",)) == (False, "excluded.unknown")


def test_scope_setting_refuses_a_sensitive_segment() -> None:
    for raw in (
        "https://site.example/settings",
        "https://site.example/billing",
        "https://site.example/admin",
        "https://site.example/checkout",
        "https://site.example/lesson,https://site.example/pricing",
    ):
        with pytest.raises(ValueError, match="entry"):
            parse_task_grant_scopes(raw)
    with pytest.raises(ValidationError):
        BrowserTaskGrantScope(origin=ORIGIN, path_prefix="/Settings")
    assert parse_task_grant_scopes("https://site.example/lesson")[0].path_prefix == "/lesson"


def test_offer_scope_is_the_one_configured_scope_containing_the_page() -> None:
    scopes = parse_task_grant_scopes("https://www.duolingo.com/lesson,https://example.org/practice")

    assert offer_scope("https://www.duolingo.com/lesson/abc", scopes) == scopes[0]
    assert offer_scope("https://example.org/practice", scopes) == scopes[1]
    assert offer_scope("https://www.duolingo.com/learn", scopes) is None
    assert offer_scope("https://duolingo.com/lesson", scopes) is None
    assert offer_scope("https://www.duolingo.com/lesson", ()) is None


@dataclass
class NamedElementSessions(FakeSessions):
    names: tuple[str, ...] = ("Continue", "Buy now", "Answer")

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
        del lease_ref
        return BrowserObservation(
            url=url,
            revision="revision-1",
            elements=tuple(
                BrowserElement(ref=f"revision-1:{n}", role="button", name=name)
                for n, name in enumerate(self.names)
            ),
        )


async def test_hosted_provider_classifies_through_the_shared_classifier() -> None:
    provider = ready_provider(NamedElementSessions())
    await provider.bind_execution(tool_context())
    await provider.navigate("https://example.org/lesson")

    found = [
        (
            await provider.action_context(
                BrowserAction(kind=BrowserActionKind.CLICK, expected_revision="revision-1", ref=ref)
            )
        ).consequence
        for ref in ("revision-1:0", "revision-1:1", "revision-1:2")
    ]

    assert found == [C.ROUTINE, C.PURCHASE, C.UNKNOWN]


EXCLUDED_WORDS = [entry for entries in VOCABULARY.values() for entry in entries]
LESSON_WORDS = [
    "el gato",
    "Check",
    "la casa",
    "hola",
    "\u00bfQu\u00e9?",
    "Continue",
    "Next",
    "",
    "42",
]


@settings(max_examples=200, deadline=None)
@given(
    labels=st.lists(st.sampled_from(LESSON_WORDS), max_size=4),
    excluded=st.sampled_from(EXCLUDED_WORDS),
    where=st.sampled_from(["new label", "appended", "prefixed", "dialog", "hidden source"]),
    kind=st.sampled_from([BrowserActionKind.CLICK, BrowserActionKind.PRESS]),
)
def test_adding_an_excluded_word_never_makes_an_action_covered(
    labels: list[str], excluded: str, where: str, kind: BrowserActionKind
) -> None:
    action = click(kind=kind, key=BrowserKey.ENTER) if kind is BrowserActionKind.PRESS else click()
    element = facts()
    grown = list(labels) or ["Continue"]
    if where == "new label":
        grown.append(excluded)
    elif where == "appended":
        grown[0] = f"{grown[0]} {excluded}"
    elif where == "prefixed":
        grown[-1] = f"{excluded} {grown[-1]}"
    elif where == "dialog":
        element = facts(context_name=excluded)
    else:
        element = facts(labels={BrowserLabelSource.ARIA_LABEL: excluded})
        grown.append(excluded)

    covered, _reason, consequence = cover(action, labels=tuple(grown), element=element)

    assert not covered
    assert consequence not in {C.ROUTINE, C.UNKNOWN}
