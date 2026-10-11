"""A grant-constrained act on a hostile page, in a real Chromium (ADR-0129).

Each page reproduces a bypass a security review found. Under a task grant for
``/lesson``, the act must either be refused before dispatch or have no native
effect on anything but the element the classifier read: no request may leave
the prefix, and no other control may change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserCoverage,
    BrowserElementFacts,
    BrowserObservation,
    BrowserProviderError,
    BrowserSnapshot,
    BrowserTargetFacts,
    browser_origin,
)
from agent_core.domain.browser_act_views import describe_browser_action, task_grant_view_coverage
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
ACTION_NOT_ALLOWED = "tool.browser.action_not_allowed"
ELEMENT_NOT_FOUND = "tool.browser.element_not_found"
DISPATCHED = "dispatched"


async def _covered(runtime: Any, action: BrowserAction) -> None:
    await runtime.act(action, constraint=lesson_constraint(), now=GRANT_NOW)


async def _page_value(runtime: Any, script: str) -> Any:
    return await runtime._current_page().evaluate(script)


def _worker_coverage(
    runtime: Any, page: BrowserObservation, action: BrowserAction
) -> BrowserCoverage:
    """The worker's decision, from the observation and its facts, made before
    the authorizer consumes a grant use."""

    view = describe_browser_action(
        action, BrowserSnapshot(observation=page, facts=runtime.facts(page.revision))
    )
    assert view.element is not None
    return task_grant_view_coverage(
        view, action, origin=browser_origin(page.url), path_prefix="/lesson"
    )


async def _worker_and_runtime(
    runtime: Any, choose: Callable[[BrowserObservation], BrowserAction]
) -> tuple[bool, str]:
    """Observe, then ask the worker and the runtime about the same act on the
    same, unchanged page: whether the worker covers it, and whether the runtime
    dispatched it under the grant or which refusal it gave."""

    page = await runtime.observe()
    action = choose(page)
    covered = _worker_coverage(runtime, page, action).covered
    try:
        await _covered(runtime, action)
    except BrowserProviderError as error:
        return covered, error.reason_code
    return covered, DISPATCHED


def _acting(
    kind: BrowserActionKind, name: str, **fields: Any
) -> Callable[[BrowserObservation], BrowserAction]:
    def choose(page: BrowserObservation) -> BrowserAction:
        return BrowserAction(
            kind=kind, expected_revision=page.revision, ref=_ref(page, name), **fields
        )

    return choose


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
        assert page.elements[0].name == "Buy 500 gems"
        refusal = await _refused(runtime, _click_named(page, "Buy 500 gems"))
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


LEAVING = """<!doctype html><html><head><title>Lesson</title></head><body>
<button type="button" onclick="location.href = '/courses/remove-course'">Continue</button>
<button type="button" onclick="window.clicks.push('next')">Next</button>
<script>window.clicks = [];</script>
</body></html>"""


async def test_an_act_that_ends_early_leaves_the_fence_refusal_behind() -> None:
    """A granted act whose page document the fence refused is cancelled while
    it settles. The refusal was that act's; the next act reports what it did."""

    async with lesson_pages({"/lesson/1": LEAVING}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        granted = asyncio.create_task(_covered(runtime, _click_named(page, "Continue")))
        async with asyncio.timeout(10):
            while not runtime._current_page().url.startswith("chrome-error:"):
                await asyncio.sleep(0.01)
        granted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await granted
        page = await visit("/lesson/1")
        after = await runtime.act(_click_named(page, "Next"))
        clicks = await _page_value(runtime, "window.clicks")

    assert after.url.endswith("/lesson/1")
    assert clicks == ["next"]
    assert left == []


# "Continue" sits under a transparent cover, so Playwright keeps waiting for
# its click point to reach it; "Next" is uncovered.
COVERED = """<!doctype html><html><head><title>Lesson</title></head><body>
<div style="position:relative;width:200px;height:40px">
<button type="button" style="width:200px;height:40px"
 onclick="window.clicks.push('continue')">Continue</button>
<div style="position:absolute;inset:0"></div></div>
<button type="button" onclick="window.clicks.push('next')">Next</button>
<script>window.clicks = [];</script>
</body></html>"""


async def test_an_act_cancelled_while_it_waits_leaves_no_guard_on_the_page() -> None:
    """A granted click still waiting for its element is cancelled. Its click
    guard goes with it, so the next act's click reaches the page."""

    async with lesson_pages({"/lesson/1": COVERED}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        granted = asyncio.create_task(_covered(runtime, _click_named(page, "Continue")))
        await asyncio.sleep(1.5)
        assert not granted.done()
        granted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await granted
        page = await runtime.observe()
        await runtime.act(_click_named(page, "Next"))
        clicks = await _page_value(runtime, "window.clicks")

    assert clicks == ["next"]
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


# An excluded word past the 256 characters element facts carry of a label
# source, but inside the 1,024 the runtime reads.
LONG_TAIL = """<!doctype html><html><head><title>Lesson</title></head><body>
<button aria-label="Continue" onclick="window.clicks.push(1)">Continue PADDING Pay $12.99</button>
<script>window.clicks = [];</script>
</body></html>""".replace("PADDING", "and " * 70)


@pytest.mark.parametrize("html", [PADDED, LONG_TAIL], ids=["past_the_live_read", "past_the_facts"])
async def test_the_worker_covers_no_label_it_cannot_read_whole(html: str) -> None:
    """The worker decides from the observation's facts before a use is
    consumed. A label source the facts could not carry whole may hide an
    excluded word the runtime reads, so the runtime would refuse an act the
    worker paid for, and the model, told to observe again, would pay again.
    The worker refuses it first, and the runtime still refuses it."""

    async with lesson_pages({"/lesson/1": html}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        name = next(element.name for element in page.elements if element.role == "button")
        action = _click_named(page, name)
        worker = _worker_coverage(runtime, page, action)
        refusal = await _refused(runtime, action)
        clicks = await _page_value(runtime, "window.clicks")

    assert (worker.covered, worker.reason) == (False, "facts_unavailable")
    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert clicks == []


# The worker must be at least as strict as the runtime on a page that has not
# changed: every act the runtime would refuse, the worker declines before a
# use is consumed, and the act goes to the ordinary approval card (ADR-0129).
CLICK = BrowserActionKind.CLICK

# A label source reads as the name once normalized, but not as displayed: a
# currency symbol, or a right-to-left override that shows "buy now".
SAME_WHEN_NORMALIZED = """<!doctype html><html><head><title>Lesson</title></head><body>
<button aria-label="Continue" onclick="window.clicks.push('pay')">Continue $</button>
<button aria-label="won yub" onclick="window.clicks.push('buy')">\u202ewon yub</button>
<button aria-label="Next" onclick="window.clicks.push('next')">Next</button>
<script>window.clicks = [];</script>
</body></html>"""


async def test_the_worker_classifies_every_label_source_the_runtime_does() -> None:
    """A label source that normalizes to the name may still read differently."""

    async with lesson_pages({"/lesson/1": SAME_WHEN_NORMALIZED}) as (runtime, visit, _left):
        await visit("/lesson/1")
        decisions = {
            name: await _worker_and_runtime(runtime, _acting(CLICK, name))
            for name in ("Continue", "won yub", "Next")
        }
        clicks = await _page_value(runtime, "window.clicks")

    assert decisions == {
        "Continue": (False, GRANT_NOT_APPLICABLE),
        "won yub": (False, GRANT_NOT_APPLICABLE),
        "Next": (True, DISPATCHED),
    }
    assert clicks == ["next"]


DISABLED = """<!doctype html><html><head><title>Lesson</title></head><body>
<button disabled>Next</button>
<div role="button" tabindex="0" aria-disabled="true">Skip</div>
<button onclick="window.clicks.push('continue')">Continue</button>
<script>window.clicks = [];</script>
</body></html>"""


async def test_the_worker_covers_no_disabled_element() -> None:
    """The runtime acts on no disabled element, natively or through ARIA."""

    async with lesson_pages({"/lesson/1": DISABLED}) as (runtime, visit, _left):
        await visit("/lesson/1")
        decisions = {
            name: await _worker_and_runtime(runtime, _acting(CLICK, name))
            for name in ("Next", "Skip", "Continue")
        }
        clicks = await _page_value(runtime, "window.clicks")

    assert decisions == {
        "Next": (False, ELEMENT_NOT_FOUND),
        "Skip": (False, ELEMENT_NOT_FOUND),
        "Continue": (True, DISPATCHED),
    }
    assert clicks == ["continue"]


EDITABLE = """<!doctype html><html><head><title>Lesson</title></head><body>
<div role="textbox" contenteditable="true" aria-label="Answer" style="min-height:40px"></div>
<textarea aria-label="Notes"></textarea>
</body></html>"""


async def test_the_worker_covers_no_typing_into_an_editable_region() -> None:
    """The runtime types only into an input or a text area; keys still reach
    an editable region."""

    async with lesson_pages({"/lesson/1": EDITABLE}) as (runtime, visit, _left):
        await visit("/lesson/1")
        typed = BrowserActionKind.TYPE
        decisions = {
            "type into the region": await _worker_and_runtime(
                runtime, _acting(typed, "Answer", value="hola")
            ),
            "press in the region": await _worker_and_runtime(
                runtime,
                lambda page: _press(page, "Answer", "ArrowLeft"),
            ),
            "type into the text area": await _worker_and_runtime(
                runtime, _acting(typed, "Notes", value="hola")
            ),
        }
        notes = await _page_value(runtime, "document.querySelector('textarea').value")

    assert decisions == {
        "type into the region": (False, ACTION_NOT_ALLOWED),
        "press in the region": (True, DISPATCHED),
        "type into the text area": (True, DISPATCHED),
    }
    assert notes == "hola"


CHOICES = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="checkbox" aria-label="Sound">
<select aria-label="Word"><option>gato</option><option>perro</option></select>
<div role="checkbox" aria-checked="false" tabindex="0"
 onclick="window.clicks.push('hints')">Hints</div>
<script>window.clicks = [];</script>
</body></html>"""


async def test_the_worker_selects_only_in_a_select_and_checks_only_a_check_box() -> None:
    """The runtime selects only in a ``select`` and checks only a native check
    box or radio; a click still reaches a choice the page built itself."""

    select, check = BrowserActionKind.SELECT, BrowserActionKind.CHECK
    async with lesson_pages({"/lesson/1": CHOICES}) as (runtime, visit, _left):
        await visit("/lesson/1")
        decisions = {
            "select in a check box": await _worker_and_runtime(
                runtime, _acting(select, "Sound", value="gato")
            ),
            "check a select": await _worker_and_runtime(runtime, _acting(check, "Word")),
            "check a built choice": await _worker_and_runtime(runtime, _acting(check, "Hints")),
            "select in a built choice": await _worker_and_runtime(
                runtime, _acting(select, "Hints", value="gato")
            ),
            "click a built choice": await _worker_and_runtime(runtime, _acting(CLICK, "Hints")),
            "check the check box": await _worker_and_runtime(runtime, _acting(check, "Sound")),
            "select in the select": await _worker_and_runtime(
                runtime, _acting(select, "Word", value="perro")
            ),
        }
        state = await _page_value(
            runtime,
            "[document.querySelector('input').checked, document.querySelector('select').value,"
            " window.clicks]",
        )

    assert decisions == {
        "select in a check box": (False, ACTION_NOT_ALLOWED),
        "check a select": (False, ACTION_NOT_ALLOWED),
        "check a built choice": (False, ACTION_NOT_ALLOWED),
        "select in a built choice": (False, ACTION_NOT_ALLOWED),
        "click a built choice": (True, DISPATCHED),
        "check the check box": (True, DISPATCHED),
        "select in the select": (True, DISPATCHED),
    }
    assert state == [True, "perro", ["hints"]]


@pytest.mark.parametrize("changed", [False, True])
async def test_owner_follow_constraint_rechecks_live_x_target(changed: bool) -> None:
    from datetime import timedelta

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    from agent_core.domain.browser import BrowserDispatchConstraint
    from tests.real_browser_support import (
        RealBrowserRuntime,
        local_https_site,
        require_real_browser,
    )

    require_real_browser()

    async def account(request: Request) -> HTMLResponse:
        del request
        return HTMLResponse("""<!doctype html><title>Account</title>
            <button aria-label="Follow @mitsuhiko" onclick="window.effects++;
            this.setAttribute('aria-label','Following @mitsuhiko');
            this.textContent='Following'">Follow</button>
            <script>window.effects=0;</script>""")

    async with local_https_site(
        Starlette(routes=[Route("/mitsuhiko", account)]), host="x.com"
    ) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            page = await runtime.navigate(site.url("/mitsuhiko"))
            target = next(
                element for element in page.elements if element.name == "Follow @mitsuhiko"
            )
            action = BrowserAction(
                kind=BrowserActionKind.CLICK, expected_revision=page.revision, ref=target.ref
            )
            constraint = BrowserDispatchConstraint(
                grant_kind="follow",
                origins=("https://x.com",),
                path_prefix="/mitsuhiko",
                follow_handle="mitsuhiko",
                not_after=GRANT_NOW + timedelta(minutes=1),
                consequence_ceiling="unknown",
                max_text_characters=None,
            )
            if changed:
                await runtime._current_page().evaluate(
                    "document.querySelector('button').setAttribute("
                    "'aria-label','Follow @someone_else')"
                )
                with pytest.raises(BrowserProviderError):
                    await runtime.act(action, constraint=constraint, now=GRANT_NOW)
            else:
                result = await runtime.act(action, constraint=constraint, now=GRANT_NOW)
                assert any(element.name == "Following @mitsuhiko" for element in result.elements)
            assert await runtime._current_page().evaluate("window.effects") == (0 if changed else 1)
        finally:
            await runtime.close()
