"""A grant-constrained act on a hostile page, in a real Chromium (ADR-0129).

Each page reproduces a bypass a security review found. Under a task grant for
``/lesson``, the act must either be refused before dispatch or have no native
effect on anything but the element the classifier read: no request may leave
the prefix, and no other control may change.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserProviderError
from tests.unit.test_browser_playwright import (
    GRANT_NOW,
    _click_named,
    _press,
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


# The covered field hands focus away as soon as it takes it.
FOCUS_HANDOFF = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="text" id="answer" aria-label="Answer">
<form method="post" action="/courses/remove-course">
<input type="text" id="other" aria-label="Word"><button type="submit" id="check">Check</button>
</form>
<script>
document.getElementById('answer').addEventListener('focus', () => {
  setTimeout(() => document.getElementById(window.handoff).focus(), 0);
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
        clicks = await _page_value(runtime, "window.clicks")

    assert clicks == ["next", "later"]
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
