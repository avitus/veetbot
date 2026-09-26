"""A grant-constrained act on a hostile page, in a real Chromium (ADR-0129).

Each page reproduces a bypass a security review found. Under a task grant for
``/lesson``, the act must either be refused before dispatch or have no native
effect on anything but the element the classifier read: no request may leave
the prefix, and no other control may change.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserElementFacts,
    BrowserProviderError,
    BrowserTargetFacts,
)
from tests.unit.test_browser_playwright import (
    GRANT_NOW,
    _click_named,
    _press,
    _ref,
    _refused,
    lesson_constraint,
    lesson_pages,
)

GRANT_NOT_APPLICABLE = "tool.browser.grant_not_applicable"


async def _covered(runtime: Any, action: BrowserAction) -> None:
    await runtime.act(action, constraint=lesson_constraint(), now=GRANT_NOW)


async def _page_value(runtime: Any, script: str) -> Any:
    return await runtime._current_page().evaluate(script)


# Tab on a covered field moves focus to a submit button outside the prefix.
FOCUS_SUBMIT = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="text" aria-label="Answer">
<form method="post" action="/courses/remove-course"><button type="submit">Check</button></form>
<div role="button">Next</div>
</body></html>"""


async def test_a_key_press_on_an_element_that_cannot_take_focus_is_refused() -> None:
    """Enter on a ``div`` would reach the focused off-prefix submit button."""

    async with lesson_pages({"/lesson/1": FOCUS_SUBMIT}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        await _covered(runtime, _press(page, "Answer", "Tab"))
        page = await runtime.observe()
        focused = await _page_value(runtime, "document.activeElement.textContent")
        refusal = await _refused(runtime, _press(page, "Next", "Enter"))

    assert focused == "Check"
    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert left == []


FOCUS_RADIO = """<!doctype html><html><head><title>Lesson</title></head><body>
<label><input type="radio" name="plan" id="keep"> Keep learning</label>
<label><input type="radio" name="plan" id="trial"> Start Super trial $12.99</label>
<div role="button">Next</div>
</body></html>"""


async def test_an_arrow_key_cannot_reach_a_radio_that_kept_focus() -> None:
    """ArrowDown on a ``div`` would move the focused radio group to the trial."""

    async with lesson_pages({"/lesson/1": FOCUS_RADIO}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        keep = next(element.ref for element in page.elements if element.role == "radio")
        await _covered(
            runtime,
            BrowserAction(kind=BrowserActionKind.CLICK, expected_revision=page.revision, ref=keep),
        )
        page = await runtime.observe()
        refusal = await _refused(runtime, _press(page, "Next", "ArrowDown"))
        state = await _page_value(
            runtime,
            "[document.getElementById('keep').checked, document.getElementById('trial').checked]",
        )

    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert state == [True, False]


# The covered field hands focus away as soon as it takes it. The handoff is a
# microtask, so it always runs after the focus check that focused the field and
# before the next key or text arrives. A timer would race the keyboard: when
# the key or text arrives first, it goes to the covered field and the handoff
# is never exercised.
FOCUS_HANDOFF = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="text" id="answer" aria-label="Answer">
<form method="post" action="/courses/remove-course">
<input type="text" id="other" aria-label="Word"><button type="submit" id="check">Check</button>
</form>
<script>
document.getElementById('answer').addEventListener('focus', () => {
  queueMicrotask(() => document.getElementById(window.handoff).focus());
});
window.handoff = 'check';
</script>
</body></html>"""


async def test_a_key_the_page_redirects_to_another_element_never_acts_there() -> None:
    """Focus moves after the check: the key reaches the submit button only to be
    stopped, and the act reports its outcome as unknown."""

    async with lesson_pages({"/lesson/1": FOCUS_HANDOFF}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        with pytest.raises(BrowserProviderError) as raised:
            await _covered(runtime, _press(page, "Answer", "Enter"))
        await _page_value(runtime, "new Promise(resolve => setTimeout(resolve, 300))")

    assert raised.value.reason_code == "tool.browser.outcome_unknown"
    assert left == []


async def test_text_the_page_redirects_to_another_field_is_never_typed_there() -> None:
    async with lesson_pages({"/lesson/1": FOCUS_HANDOFF}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        await _page_value(runtime, "window.handoff = 'other'")
        answer = next(element.ref for element in page.elements if element.name == "Answer")
        with pytest.raises(BrowserProviderError) as raised:
            await _covered(
                runtime,
                BrowserAction(
                    kind=BrowserActionKind.TYPE,
                    expected_revision=page.revision,
                    ref=answer,
                    value="hola",
                ),
            )
        other = await _page_value(runtime, "document.getElementById('other').value")

    assert raised.value.reason_code == "tool.browser.outcome_unknown"
    assert other == ""


KEYS_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="text" aria-label="Answer">
<button type="button" onclick="window.clicks.push('next')">Next</button>
<button type="button" onclick="window.clicks.push('later')">Later</button>
<div id="host"></div>
<script>
window.clicks = [];
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  '<input type="text" aria-label="Word">';
</script>
</body></html>"""


async def test_covered_keys_still_reach_the_element_they_name() -> None:
    """A focusable target, in the page or in an open shadow root, takes its key;
    Tab's focus move is the key's own effect, not a redirected key."""

    async with lesson_pages({"/lesson/1": KEYS_PAGE}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        await _covered(runtime, _press(page, "Next", "Enter"))
        page = await runtime.observe()
        await _covered(runtime, _press(page, "Answer", "Tab"))
        page = await runtime.observe()
        await _covered(runtime, _press(page, "Word", "ArrowLeft"))
        page = await runtime.observe()
        await _covered(runtime, _click_named(page, "Later"))
        page = await runtime.observe()
        for name, value in (("Answer", "hola"), ("Word", "adiós")):
            await _covered(
                runtime,
                BrowserAction(
                    kind=BrowserActionKind.TYPE,
                    expected_revision=page.revision,
                    ref=_ref(page, name),
                    value=value,
                ),
            )
            page = await runtime.observe()
        clicks = await _page_value(runtime, "window.clicks")
        typed = await _page_value(
            runtime,
            "[document.querySelector('input').value,"
            " document.getElementById('host').shadowRoot.querySelector('input').value]",
        )

    assert clicks == ["next", "later"]
    assert typed == ["hola", "adiós"]
    assert left == []


SCRIPT_LINKS = """<!doctype html><html><head><title>Lesson</title></head><body>
<a href="javascript:location='/settings/delete'">Continue</a>
<a href="JavaScript:void(0)" onclick="location='/courses/remove-course'">Next</a>
</body></html>"""


async def test_a_script_link_is_a_target_outside_the_prefix() -> None:
    """A ``javascript:`` URL runs script that can go anywhere, so it never stays."""

    async with lesson_pages({"/lesson/1": SCRIPT_LINKS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        script = await _refused(runtime, _click_named(page, "Continue"))
        page = await runtime.observe()
        mixed_case = await _refused(runtime, _press(page, "Next", "Enter"))

    assert script.reason_code == mixed_case.reason_code == GRANT_NOT_APPLICABLE
    assert left == []


# A form inside an open shadow root submits Enter through its default button.
SHADOW_DEFAULT = """<!doctype html><html><head><title>Lesson</title></head><body>
<div id="host"></div>
<script>
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  '<form method="post" action="/lesson/check">'
  + '<input type="text" aria-label="Answer">'
  + '<button type="submit" formaction="/settings/delete">Go</button></form>';
</script>
</body></html>"""
# A shadow-root span inside a light-DOM submit button activates the button.
SHADOW_IN_BUTTON = """<!doctype html><html><head><title>Lesson</title></head><body>
<form method="post" action="/courses/remove-course">
<button type="submit" aria-label="Remove course"><x-label id="label"></x-label></button>
</form>
<script>
document.getElementById('label').attachShadow({mode: 'open'}).innerHTML =
  '<span role="button">Continue</span>';
</script>
</body></html>"""
# A light-DOM span slotted into a shadow submit button, and a light-DOM link
# slotted inside a shadow control.
SLOTTED = """<!doctype html><html><head><title>Lesson</title></head><body>
<div id="submitter"><span role="button">Continue</span></div>
<div id="wrapper">
<a href="/courses/remove-course" style="display:block;width:200px;height:40px"></a>
</div>
<script>
document.getElementById('submitter').attachShadow({mode: 'open'}).innerHTML =
  '<form method="post" action="/courses/remove-course">'
  + '<button type="submit" style="padding:20px"><slot></slot></button></form>';
document.getElementById('wrapper').attachShadow({mode: 'open'}).innerHTML =
  '<div role="button" style="position:relative">Next<slot></slot></div>';
</script>
</body></html>"""


async def test_facts_follow_the_flat_tree_through_shadow_roots_and_slots() -> None:
    pages = {"/lesson/1": SHADOW_DEFAULT, "/lesson/2": SHADOW_IN_BUTTON}
    async with lesson_pages(pages) as (runtime, visit, _left):

        async def facts(path: str, name: str) -> BrowserElementFacts:
            page = await visit(path)
            observed = runtime.facts(page.revision)
            assert observed is not None
            return observed.elements[_ref(page, name)]

        answer = await facts("/lesson/1", "Answer")
        inner = await facts("/lesson/2", "Continue")

    removed = BrowserTargetFacts(same_origin=True, first_segment="courses", sensitive_path=True)
    assert answer.form_target == BrowserTargetFacts(
        same_origin=True, first_segment="settings", sensitive_path=True
    )
    assert inner.form_target == removed


async def test_shadow_and_slotted_submissions_off_the_prefix_are_refused() -> None:
    pages = {"/lesson/1": SHADOW_DEFAULT, "/lesson/2": SHADOW_IN_BUTTON, "/lesson/3": SLOTTED}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        await _page_value(
            runtime,
            "document.getElementById('host').shadowRoot.querySelector('input').value = 'hola'",
        )
        default = await _refused(runtime, _press(page, "Answer", "Enter"))
        page = await visit("/lesson/2")
        inner = await _refused(runtime, _click_named(page, "Continue"))
        page = await visit("/lesson/3")
        slotted = await _refused(runtime, _click_named(page, "Continue"))
        page = await runtime.observe()
        slotted_link = await _refused(runtime, _click_named(page, "Next"))

    refusals = {default.reason_code, inner.reason_code, slotted.reason_code}
    assert refusals | {slotted_link.reason_code} == {GRANT_NOT_APPLICABLE}
    assert left == []


# The click lands on whatever lies at the element's center, which may be a
# descendant with a target of its own, or a document the page embeds.
DESCENDANTS = """<!doctype html><html><head><title>Lesson</title></head><body>
<div role="button" style="position:relative;width:200px;height:60px">Continue
<a href="/courses/remove-course" style="position:absolute;inset:0"></a></div>
<div role="button" style="position:relative;width:200px;height:60px">Next
<form method="post" action="/courses/remove-course" style="position:absolute;inset:0;margin:0">
<button type="submit" aria-label="Go" style="width:100%;height:100%;opacity:0.01"></button>
</form></div>
<iframe role="button" aria-label="Skip" src="/lesson/frame"
 style="width:300px;height:120px"></iframe>
<svg width="200" height="60" xmlns:xlink="http://www.w3.org/1999/xlink">
<a xlink:href="/courses/remove-course" aria-label="Start"><rect width="200" height="60"></rect></a>
</svg>
</body></html>"""
FRAME = """<!doctype html><html><body style="margin:0">
<form method="post" action="/courses/remove-course">
<button type="submit" style="width:300px;height:120px">Go</button></form>
</body></html>"""


async def test_descendant_embedded_and_svg_targets_off_the_prefix_are_refused() -> None:
    pages = {"/lesson/1": DESCENDANTS, "/lesson/frame": FRAME}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusals = []
        for name in ("Continue", "Next", "Skip", "Start"):
            refusals.append((await _refused(runtime, _click_named(page, name))).reason_code)
            page = await runtime.observe()

    assert refusals == [GRANT_NOT_APPLICABLE] * 4
    assert left == []


# aria-labelledby names an element in the same tree, here a shadow root.
SHADOW_LABELLEDBY = """<!doctype html><html><head><title>Lesson</title></head><body>
<div id="host"></div>
<script>
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  '<span id="name" hidden>Buy 500 gems</span>'
  + '<button aria-labelledby="name" onclick="window.clicks.push(1)">Continue</button>';
window.clicks = [];
</script>
</body></html>"""


async def test_a_shadow_root_label_reference_is_read_in_its_own_tree() -> None:
    async with lesson_pages({"/lesson/1": SHADOW_LABELLEDBY}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        refusal = await _refused(runtime, _click_named(page, "Continue"))
        clicks = await _page_value(runtime, "window.clicks")

    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert clicks == []


# innerText leaves out shadow content, which is what these buttons show.
SHADOW_TEXT = """<!doctype html><html><head><title>Lesson</title></head><body>
<button aria-label="Continue" onclick="window.clicks.push('gems')">
<x-text id="gems"></x-text></button>
<button onclick="window.clicks.push('next')"><x-text id="next"></x-text></button>
<script>
window.clicks = [];
document.getElementById('gems').attachShadow({mode: 'open'}).innerHTML = '<b>Buy 500 gems</b>';
document.getElementById('next').attachShadow({mode: 'open'}).innerHTML =
  '<style>b { order: 1; }</style><b>Next</b>';
</script>
</body></html>"""


async def test_text_a_shadow_root_renders_is_visible_text() -> None:
    """Its rendered text is classified; its style sheet is not text."""

    async with lesson_pages({"/lesson/1": SHADOW_TEXT}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        refusal = await _refused(runtime, _click_named(page, "Continue"))
        page = await runtime.observe()
        unnamed = next(element.ref for element in page.elements if element.name == "")
        await _covered(
            runtime,
            BrowserAction(
                kind=BrowserActionKind.CLICK, expected_revision=page.revision, ref=unnamed
            ),
        )
        clicks = await _page_value(runtime, "window.clicks")

    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert clicks == ["next"]


# What no page script can see: a closed shadow root, declared in the HTML,
# wraps a light-DOM span in a submit button of its own form.
CLOSED_SHADOW = """<!doctype html><html><head><title>Lesson</title></head><body>
<div id="host"><template shadowrootmode="closed">
<form method="post" action="/courses/remove-course">
<button type="submit" style="padding:20px"><slot></slot></button></form>
</template><span role="button">Continue</span></div>
</body></html>"""
CLOSED_FRAME_TARGET = """<!doctype html><html><head><title>Lesson</title></head><body>
<iframe name="quiet" src="/lesson/blank" style="width:10px;height:10px"></iframe>
<div id="host"><template shadowrootmode="closed">
<form method="post" action="/courses/remove-course" target="quiet">
<button type="submit" style="padding:20px"><slot></slot></button></form>
</template><span role="button">Next</span></div>
</body></html>"""


async def test_no_document_outside_the_prefix_loads_during_a_granted_act() -> None:
    """The runtime refuses every document load outside the prefix while a
    granted act runs and settles, in the page or in any frame."""

    pages = {
        "/lesson/1": CLOSED_SHADOW,
        "/lesson/2": CLOSED_FRAME_TARGET,
        "/lesson/blank": "<!doctype html><title>Blank</title>",
    }
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        with pytest.raises(BrowserProviderError) as refused_page:
            await _covered(runtime, _click_named(page, "Continue"))
        page = await visit("/lesson/2")
        framed = await runtime.act(
            _click_named(page, "Next"), constraint=lesson_constraint(), now=GRANT_NOW
        )

    # The page's own document was refused, so the page is the browser's error
    # page and the act's outcome is unknown; a frame's refusal leaves the page.
    assert refused_page.value.reason_code == "tool.browser.outcome_unknown"
    assert framed.url.endswith("/lesson/2")
    assert left == []


PING = """<!doctype html><html><head><title>Lesson</title></head><body>
<a href="/lesson/2" ping="/courses/remove-course">Next</a>
</body></html>"""


async def test_a_covered_link_sends_no_hyperlink_auditing_ping() -> None:
    pages = {"/lesson/1": PING, "/lesson/2": "<!doctype html><title>Two</title><p>Two</p>"}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        arrived = await runtime.act(
            _click_named(page, "Next"), constraint=lesson_constraint(), now=GRANT_NOW
        )
        await _page_value(runtime, "new Promise(resolve => setTimeout(resolve, 300))")

    assert arrived.url.endswith("/lesson/2")
    assert left == []


# A click on a label, or on an element holding a check box, changes a control
# whose own labels say what it is.
TOGGLES = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="radio" name="plan" id="keep" aria-label="Keep learning">
<input type="radio" name="plan" id="trial" aria-label="Start Super trial $12.99">
<label role="button" for="trial">Keep going</label>
<label role="button" id="wrapped"><input type="checkbox" id="renew"
 aria-label="Renew my subscription">Continue</label>
<div role="button" style="position:relative;width:200px;height:60px">Next
<input type="checkbox" id="auto" aria-label="Auto-renew membership"
 style="position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0.01"></div>
<label role="button" for="keep">Stay</label>
</body></html>"""


async def test_a_control_a_click_would_change_is_classified_by_its_own_labels() -> None:
    async with lesson_pages({"/lesson/1": TOGGLES}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        refusals = []
        for name in ("Keep going", "Continue", "Next"):
            refusals.append((await _refused(runtime, _click_named(page, name))).reason_code)
            page = await runtime.observe()
        await _covered(runtime, _click_named(page, "Stay"))
        state = await _page_value(
            runtime,
            "['keep', 'trial', 'renew', 'auto'].map(id => document.getElementById(id).checked)",
        )

    assert refusals == [GRANT_NOT_APPLICABLE] * 3
    assert state == [True, False, False, False]


# An excluded word past the 1,024 characters the runtime reads.
PADDED = """<!doctype html><html><head><title>Lesson</title></head><body>
<button aria-label="Continue PADDING Buy 500 gems" onclick="window.clicks.push(1)">Continue</button>
<script>window.clicks = [];</script>
</body></html>""".replace("PADDING", "and " * 300)


async def test_a_label_too_long_to_read_whole_is_refused() -> None:
    async with lesson_pages({"/lesson/1": PADDED}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        name = next(element.name for element in page.elements if element.role == "button")
        refusal = await _refused(runtime, _click_named(page, name))
        clicks = await _page_value(runtime, "window.clicks")

    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert clicks == []
