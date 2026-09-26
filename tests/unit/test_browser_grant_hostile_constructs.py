"""More hostile page constructs against a grant-constrained act, in a real Chromium.

ADR-0129 widened: each page builds one construct a hostile site could use to
make a covered act do more than the classifier read. Under a task grant for
``/lesson`` the act must be refused before dispatch or have no effect beyond
the classified element: no document may load, and no form may submit, outside
the prefix or the origin, and no control whose labels name an excluded
consequence may change. A construct that is an accepted limit of ADR-0129 says
so and pins the behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from agent_core.adapters.browser import playwright as playwright_adapter
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserKey,
    BrowserLabelSource,
    BrowserObservation,
    BrowserProviderError,
)
from tests.real_browser_support import RealBrowserRuntime
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
OUTCOME_UNKNOWN = "tool.browser.outcome_unknown"
BLANK = "<!doctype html><title>Blank</title><p>Blank</p>"

Build = Callable[[BrowserObservation], BrowserAction]


async def _covered(runtime: Any, action: BrowserAction) -> BrowserObservation:
    observation: BrowserObservation = await runtime.act(
        action, constraint=lesson_constraint(), now=GRANT_NOW
    )
    return observation


async def _outcome(runtime: Any, action: BrowserAction) -> str:
    """``dispatched``, or the reason code the act failed with."""

    try:
        await _covered(runtime, action)
    except BrowserProviderError as error:
        return error.reason_code
    return "dispatched"


async def _refusals(runtime: Any, page: BrowserObservation, builds: list[Build]) -> list[str]:
    """Refuse each action in turn, observing again after each refusal."""

    codes = []
    for build in builds:
        codes.append((await _refused(runtime, build(page))).reason_code)
        page = await runtime.observe()
    return codes


async def _each(
    runtime: Any, visit: Callable[[str], Any], path: str, builds: list[Build]
) -> list[str]:
    """Each action's outcome on a fresh load of ``path``, so one that did act
    cannot change what the next one meets."""

    outcomes = []
    for build in builds:
        page = await visit(path)
        outcomes.append(await _outcome(runtime, build(page)))
    return outcomes


async def _page_value(runtime: Any, script: str) -> Any:
    return await runtime._current_page().evaluate(script)


async def _pause(runtime: Any, milliseconds: int = 300) -> None:
    await _page_value(runtime, f"new Promise(resolve => setTimeout(resolve, {milliseconds}))")


def _click_ref(page: BrowserObservation, ref: str) -> BrowserAction:
    return BrowserAction(kind=BrowserActionKind.CLICK, expected_revision=page.revision, ref=ref)


def _click(name: str, *, role: str | None = None) -> Build:
    return lambda page: _click_named(page, name, role=role)


def _key(name: str, key: str) -> Build:
    return lambda page: _press(page, name, key)


def _select(name: str, value: str) -> Build:
    return lambda page: BrowserAction(
        kind=BrowserActionKind.SELECT,
        expected_revision=page.revision,
        ref=_ref(page, name),
        value=value,
    )


def _type(page: BrowserObservation, name: str, value: str) -> BrowserAction:
    return BrowserAction(
        kind=BrowserActionKind.TYPE,
        expected_revision=page.revision,
        ref=_ref(page, name),
        value=value,
    )


def _ref_by_alt(runtime: Any, page: BrowserObservation, alt: str) -> str:
    """An element named only by alternative text, which its name leaves out."""

    facts = runtime.facts(page.revision)
    assert facts is not None
    ref: str = next(
        ref
        for ref, element in facts.elements.items()
        if element.labels.get(BrowserLabelSource.ALT, "").startswith(alt)
    )
    return ref


def _windows(runtime: RealBrowserRuntime) -> int:
    """How many pages the browser context holds; the runtime closes new windows."""

    context = runtime._context
    assert context is not None
    return len(context.pages)


# ---------------------------------------------------------------------------
# form= ownership, in the page and across shadow trees.

FORM_OWNERS = """<!doctype html><html><head><title>Lesson</title></head><body>
<form id="away" method="post" action="/courses/remove-course"></form>
<button type="submit" form="away">Continue</button>
<input type="text" form="away" aria-label="Answer">
<button type="submit" form="inner" onclick="window.clicks.push('next')">Next</button>
<div id="host"></div>
<script>
window.clicks = [];
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  '<form id="inner" method="post" action="/courses/remove-course"></form>'
  + '<button type="submit" form="inner">Check</button>'
  + '<input type="text" form="inner" aria-label="Word">';
</script>
</body></html>"""


async def test_a_form_attribute_owner_is_where_the_element_submits() -> None:
    """A control owned through ``form=`` submits to that form, in its own tree;
    a reference to a form in another tree owns nothing."""

    async with lesson_pages({"/lesson/1": FORM_OWNERS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusals = await _refusals(
            runtime,
            page,
            [_click("Continue"), _key("Answer", "Enter"), _click("Check"), _key("Word", "Enter")],
        )
        page = await runtime.observe()
        await _covered(runtime, _click_named(page, "Next"))
        clicks = await _page_value(runtime, "window.clicks")

    assert refusals == [GRANT_NOT_APPLICABLE] * 4
    assert clicks == ["next"]
    assert left == []


# ---------------------------------------------------------------------------
# formmethod, formtarget and <input type=image>.

SUBMITTERS = """<!doctype html><html><head><title>Lesson</title></head><body>
<form method="post" action="/courses/remove-course">
<input type="image" alt="Next" style="width:120px;height:40px">
</form>
<form method="get" action="/lesson/check">
<input type="image" alt="Buy 500 gems" style="width:120px;height:40px">
<input type="image" alt="Check" formaction="/courses/remove-course" style="width:120px;height:40px">
<button type="submit" formmethod="dialog" formaction="/courses/remove-course">Skip</button>
<button type="submit" formtarget="side" formaction="/courses/remove-course">Done</button>
<button type="submit" formtarget="_blank">Continue</button>
</form>
<iframe name="side" src="/lesson/blank" style="width:10px;height:10px"></iframe>
</body></html>"""


async def test_submitter_overrides_and_image_inputs_are_classified() -> None:
    """An image input submits like a button and is named by its alt text;
    ``formmethod`` and ``formtarget`` never hide a ``formaction``; a submission
    into a new window loads nothing."""

    pages = {"/lesson/1": SUBMITTERS, "/lesson/blank": BLANK}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusals = []
        for alt in ("Next", "Buy 500 gems", "Check"):
            action = _click_ref(page, _ref_by_alt(runtime, page, alt))
            refusals.append((await _refused(runtime, action)).reason_code)
            page = await runtime.observe()
        refusals += await _refusals(runtime, page, [_click("Skip"), _click("Done")])
        page = await runtime.observe()
        stayed = await _covered(runtime, _click_named(page, "Continue"))
        await _pause(runtime)
        windows = _windows(runtime)

    assert refusals == [GRANT_NOT_APPLICABLE] * 5
    assert stayed.url.endswith("/lesson/1")
    assert windows == 1
    assert left == []


# ---------------------------------------------------------------------------
# <dialog method=dialog>, forms inside dialogs, and <details>/<summary>.

DIALOGS = """<!doctype html><html><head><title>Lesson</title></head><body>
<dialog open aria-label="Buy 500 gems" style="position:static">
<button onclick="window.clicks.push('gems')">Continue</button></dialog>
<dialog open style="position:static"><h2>Start your free trial</h2>
<form method="dialog"><button value="yes">Keep going</button></form></dialog>
<dialog open style="position:static"><form method="post" action="/courses/remove-course">
<button>Done</button></form></dialog>
<dialog open id="leave" style="position:static"><form method="dialog">
<button value="next">Next</button></form></dialog>
<script>
window.clicks = [];
document.getElementById('leave').addEventListener('close', () => {
  location.href = '/courses/remove-course';
});
</script>
</body></html>"""


async def test_dialogs_name_their_controls_and_fence_their_close() -> None:
    """A dialog's name or heading classifies what is inside it; a form inside a
    dialog submits where its action says; closing one cannot navigate out."""

    async with lesson_pages({"/lesson/1": DIALOGS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusals = await _refusals(
            runtime, page, [_click("Continue"), _click("Keep going"), _click("Done")]
        )
        clicks = await _page_value(runtime, "window.clicks")
        page = await runtime.observe()
        closed = await _outcome(runtime, _click_named(page, "Next"))

    assert refusals == [GRANT_NOT_APPLICABLE] * 3
    assert clicks == []
    # The dialog closed, and its close handler's navigation never loaded.
    assert closed == OUTCOME_UNKNOWN
    assert left == []


DETAILS = """<!doctype html><html><head><title>Lesson</title></head><body>
<details><summary role="button" aria-label="Continue">Buy 500 gems</summary><p>Gems</p></details>
<form method="post" action="/courses/remove-course">
<details><summary role="button">Next</summary><p>More</p></details></form>
<details id="hint"><summary role="button">Show hint</summary>
<label><input type="checkbox" id="renew">Renew my subscription</label></details>
</body></html>"""


async def test_a_summary_is_classified_like_any_control() -> None:
    async with lesson_pages({"/lesson/1": DETAILS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusals = await _refusals(runtime, page, [_click("Continue"), _click("Next")])
        page = await runtime.observe()
        await _covered(runtime, _click_named(page, "Show hint"))
        state = await _page_value(
            runtime,
            "[document.getElementById('hint').open, document.getElementById('renew').checked]",
        )

    assert refusals == [GRANT_NOT_APPLICABLE] * 2
    assert state == [True, False]
    assert left == []


# ---------------------------------------------------------------------------
# Image maps.

PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
IMAGE_MAPS = f"""<!doctype html><html><head><title>Lesson</title></head><body>
<map name="away"><area shape="rect" coords="0,0,200,60" href="/courses/remove-course" alt="Go">
</map>
<map name="inside"><area shape="rect" coords="0,0,200,60" href="/lesson/2" alt="Go"></map>
<img role="button" alt="Continue" usemap="#away" src="{PIXEL}" width="200" height="60">
<div role="button" aria-label="Next" style="width:200px;height:60px">
<img usemap="#inside" src="{PIXEL}" width="200" height="60" alt=""></div>
<map name="named"><area role="link" shape="rect" coords="0,0,200,60"
 href="/courses/remove-course" alt="Skip" aria-label="Skip"></map>
<img usemap="#named" src="{PIXEL}" width="200" height="60" alt="">
</body></html>"""


async def test_an_image_map_is_a_target_outside_every_origin() -> None:
    """An image map sends a click to whichever area lies under it, so an image
    with a map, or an element holding one, is opaque; an area is a link."""

    async with lesson_pages({"/lesson/1": IMAGE_MAPS}) as (runtime, visit, left):
        outcomes = await _each(
            runtime,
            visit,
            "/lesson/1",
            [lambda page: _click_ref(page, _ref_by_alt(runtime, page, "Continue")), _click("Next")],
        )
        page = await visit("/lesson/1")
        if any(element.name == "Skip" for element in page.elements):
            outcomes.append(await _outcome(runtime, _click_named(page, "Skip")))

    assert set(outcomes) == {GRANT_NOT_APPLICABLE}
    assert left == []


# ---------------------------------------------------------------------------
# SVG links.

SVG_LINKS = """<!doctype html><html><head><title>Lesson</title></head><body>
<svg width="200" height="40"><a href="/courses/remove-course" aria-label="Next">
<rect width="200" height="40"></rect></a></svg>
<svg width="200" height="40"><a href="/lesson/2" aria-label="Continue">
<set attributeName="href" to="/courses/remove-course" begin="0s" fill="freeze"></set>
<rect width="200" height="40"></rect></a></svg>
<svg width="200" height="40"><defs><a id="hidden" href="/courses/remove-course">
<rect width="200" height="40"></rect></a></defs>
<use role="button" aria-label="Skip" href="#hidden"></use></svg>
<svg role="button" aria-label="Start" width="200" height="40"><use href="#hidden"></use></svg>
<svg width="200" height="40"><a href="/lesson/2" target="_blank" aria-label="Done">
<rect width="200" height="40"></rect></a></svg>
<svg width="0" height="0"><symbol id="tick"><path d="M0 0h10v10z"></path></symbol></svg>
<button type="button" aria-label="Check" onclick="window.clicks.push('check')">
<svg width="20" height="20"><use href="#tick"></use></svg> Check</button>
<script>window.clicks = [];</script>
</body></html>"""


async def test_svg_links_are_read_as_the_browser_follows_them() -> None:
    """An SVG link's target is its animated value, and a ``<use>`` instance
    of a link, which page script cannot see into, is opaque."""

    pages = {"/lesson/1": SVG_LINKS, "/lesson/2": BLANK}
    async with lesson_pages(pages) as (runtime, visit, left):
        outcomes = await _each(
            runtime,
            visit,
            "/lesson/1",
            [_click("Next"), _click("Continue"), _click("Skip"), _click("Start")],
        )
        page = await visit("/lesson/1")
        stayed = await _covered(runtime, _click_named(page, "Done"))
        await _pause(runtime)
        windows = _windows(runtime)
        # An icon drawn with <use> copies no link, so its button stays covered.
        page = await runtime.observe()
        await _covered(runtime, _click_named(page, "Check"))
        clicks = await _page_value(runtime, "window.clicks")

    assert outcomes == [GRANT_NOT_APPLICABLE] * 4
    assert clicks == ["check"]
    assert stayed.url.endswith("/lesson/1")
    assert windows == 1
    assert left == []


# ---------------------------------------------------------------------------
# Same-origin, srcdoc and sandboxed frames.

FRAMES = """<!doctype html><html><head><title>Lesson</title></head><body>
<iframe id="same" src="/lesson/frame" style="width:10px;height:10px"></iframe>
<iframe id="doc" style="width:10px;height:10px"
 srcdoc="<form method=post action=/courses/remove-course><input name=x></form>"></iframe>
<iframe id="box" sandbox="allow-scripts allow-forms allow-top-navigation"
 style="width:10px;height:10px" srcdoc="<script>
addEventListener('message', () => { top.location = '/courses/remove-course'; });
</script>"></iframe>
<iframe role="button" aria-label="Skip" style="width:300px;height:60px" srcdoc="<a
 href=/courses/remove-course target=_top style='display:block;height:60px'>Go</a>"></iframe>
<button type="button" id="next">Next</button>
<button type="button" id="continue">Continue</button>
<button type="button" id="done">Done</button>
<script>
const byId = id => document.getElementById(id);
byId('next').onclick = () => byId('same').contentDocument.forms[0].submit();
byId('continue').onclick = () => byId('doc').contentDocument.forms[0].requestSubmit();
byId('done').onclick = () => byId('box').contentWindow.postMessage('go', '*');
</script>
</body></html>"""
FRAME_FORM = """<!doctype html><html><body>
<form method="post" action="/courses/remove-course"><input name="x"></form></body></html>"""


async def test_no_frame_loads_a_document_outside_the_prefix() -> None:
    """Whatever frame the page drives from a covered click, same-origin,
    ``srcdoc`` or sandboxed, no document outside the prefix loads."""

    pages = {"/lesson/1": FRAMES, "/lesson/frame": FRAME_FORM}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        embedded = await _refused(runtime, _click_named(page, "Skip"))
        page = await runtime.observe()
        same = await _covered(runtime, _click_named(page, "Next"))
        srcdoc = await _covered(runtime, _click_named(same, "Continue"))
        sandboxed = await _outcome(runtime, _click_named(srcdoc, "Done"))

    assert embedded.reason_code == GRANT_NOT_APPLICABLE
    assert same.url.endswith("/lesson/1") and srcdoc.url.endswith("/lesson/1")
    # The sandboxed frame navigated the page itself, which was refused.
    assert sandboxed == OUTCOME_UNKNOWN
    assert left == []


# ---------------------------------------------------------------------------
# <meta http-equiv=refresh> inserted by a click.

REFRESH = """<!doctype html><html><head><title>Lesson</title></head><body>
<button type="button" onclick="refresh(0)">Next</button>
<button type="button" onclick="refresh(2.5)">Continue</button>
<script>
function refresh(seconds) {
  const meta = document.createElement('meta');
  meta.httpEquiv = 'refresh';
  meta.content = seconds + ';url=/courses/remove-course';
  document.head.append(meta);
}
</script>
</body></html>"""


async def test_a_refresh_a_click_inserts_is_fenced_while_the_act_settles() -> None:
    async with lesson_pages({"/lesson/1": REFRESH}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        outcome = await _outcome(runtime, _click_named(page, "Next"))

    assert outcome == OUTCOME_UNKNOWN
    assert left == []


async def test_a_delayed_refresh_after_the_act_settles_is_an_accepted_limit() -> None:
    """ADR-0129 Consequences: a navigation that starts after the act settles is
    not fenced. A refresh the click schedules for later is such a navigation;
    it stays on the grant's origin, since the origin guard still holds."""

    async with lesson_pages({"/lesson/1": REFRESH}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        await _covered(runtime, _click_named(page, "Continue"))
        during = list(left)
        await asyncio.sleep(3)

    assert during == []
    assert left == ["GET /courses/remove-course"]


# ---------------------------------------------------------------------------
# Form-associated custom elements.

CUSTOM_CONTROLS = """<!doctype html><html><head><title>Lesson</title></head><body>
<form id="away" method="post" action="/courses/remove-course">
<x-answer role="button">Continue</x-answer></form>
<x-answer role="button" form="away">Next</x-answer>
<div id="host"></div>
<script>
class Answer extends HTMLElement {
  static formAssociated = true;
  constructor() {
    super();
    this.internals = this.attachInternals();
    this.addEventListener('click', () => {
      if (this.internals.form) {
        this.internals.form.requestSubmit();
      }
    });
  }
}
customElements.define('x-answer', Answer);
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  '<form id="shadow-away" method="post" action="/courses/remove-course"></form>'
  + '<x-answer role="button" form="shadow-away">Skip</x-answer>';
</script>
</body></html>"""


async def test_a_form_associated_custom_element_submits_its_form() -> None:
    """A custom element with ElementInternals has a form owner, through its
    ancestors or its ``form`` attribute, and ``requestSubmit`` sends it."""

    async with lesson_pages({"/lesson/1": CUSTOM_CONTROLS}) as (runtime, visit, left):
        outcomes = await _each(
            runtime, visit, "/lesson/1", [_click("Continue"), _click("Next"), _click("Skip")]
        )

    assert outcomes == [GRANT_NOT_APPLICABLE] * 3
    assert left == []


# ---------------------------------------------------------------------------
# accesskey.

ACCESS_KEYS = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="text" aria-label="Answer">
<button accesskey="s" onclick="window.clicks.push('subscribe')">Subscribe</button>
<a href="/courses/remove-course" accesskey="n">Remove course</a>
<script>window.clicks = [];</script>
</body></html>"""


async def test_no_covered_key_reaches_an_access_key() -> None:
    """A covered key is one of a closed set with no modifier, and typed text
    sends no key events, so no access key fires."""

    async with lesson_pages({"/lesson/1": ACCESS_KEYS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        for key in BrowserKey:
            if key is BrowserKey.TAB:
                continue
            await _covered(runtime, _press(page, "Answer", key.value))
            page = await runtime.observe()
        for value in ("s", "n", "sn"):
            await _covered(runtime, _type(page, "Answer", value))
            page = await runtime.observe()
        clicks = await _page_value(runtime, "window.clicks")

    assert clicks == []
    assert left == []


# ---------------------------------------------------------------------------
# contenteditable.

EDITABLE = """<!doctype html><html><head><title>Lesson</title></head><body>
<form method="post" action="/courses/remove-course">
<div contenteditable="true" role="textbox" aria-label="Answer">hola</div></form>
<div contenteditable="true" role="textbox" aria-label="Word">Delete my account</div>
<div contenteditable="true" role="textbox" aria-label="Translation">hola</div>
<div contenteditable="true"><span role="button">Next</span>
<a href="/courses/remove-course">Remove course</a></div>
</body></html>"""


async def test_an_editable_region_is_classified_and_submits_nothing() -> None:
    """An editable region inside a form counts as a submission, its text is a
    label, an element inside one cannot take a key, and text is typed only into
    real fields."""

    async with lesson_pages({"/lesson/1": EDITABLE}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusals = await _refusals(
            runtime,
            page,
            [_key("Answer", "Enter"), _key("Word", "ArrowLeft"), _key("Next", "Enter")],
        )
        page = await runtime.observe()
        with pytest.raises(BrowserProviderError) as typed:
            await _covered(runtime, _type(page, "Translation", "adiós"))
        page = await runtime.observe()
        for key in ("ArrowLeft", "Enter"):
            await _covered(runtime, _press(page, "Translation", key))
            page = await runtime.observe()
        await _covered(runtime, _click_named(page, "Next"))

    assert refusals == [GRANT_NOT_APPLICABLE] * 3
    assert typed.value.reason_code == "tool.browser.action_not_allowed"
    assert left == []


# ---------------------------------------------------------------------------
# <select> and <option> labels.

OPTIONS = """<!doctype html><html><head><title>Lesson</title></head><body>
<select aria-label="Plan"><option value="keep">Keep learning</option>
<option value="es" aria-label="Buy 500 gems">Spanish</option></select>
<select aria-label="Course"><option value="keep">Keep learning</option>
<option value="fr" title="Start free trial">French</option></select>
<select aria-label="Tier"><option value="keep">Keep learning</option>
<optgroup label="Premium plans"><option value="it">Italian</option></optgroup></select>
<select aria-label="Level"><option value="keep">Keep learning</option>
<option value="de" label="German">Subscribe to Super</option></select>
<select aria-label="Pace"><option value="keep">Keep learning</option>
<option value="buy-gems" label=" Kept ">x</option><option value="kept">Kept</option></select>
<select aria-label="Unit">UNITS</select>
</body></html>""".replace(
    "UNITS",
    '<option value="keep">a</option>'
    + '<option value="u">a</option>' * 300
    + '<option value="deep" label="Buy 500 gems">u</option>',
)


async def test_every_label_of_the_option_a_selection_would_choose_is_classified() -> None:
    """Whichever option the selection would choose, by value or by label,
    every one of its labels and its group's label are classified."""

    async with lesson_pages({"/lesson/1": OPTIONS}) as (runtime, visit, _left):
        outcomes = await _each(
            runtime,
            visit,
            "/lesson/1",
            [
                _select("Plan", "Spanish"),
                _select("Course", "French"),
                _select("Tier", "Italian"),
                _select("Level", "German"),
                # Playwright matches a label with its white space collapsed.
                _select("Pace", "Kept"),
                # The option lies past the first 256.
                _select("Unit", "deep"),
            ],
        )
        page = await visit("/lesson/1")
        await _covered(runtime, _select("Plan", "Keep learning")(page))
        values = await _page_value(
            runtime, "Array.from(document.querySelectorAll('select')).map(select => select.value)"
        )

    assert outcomes == [GRANT_NOT_APPLICABLE] * 6
    assert values == ["keep"] * 6


# ---------------------------------------------------------------------------
# Overlays and pointer-events.

OVERLAYS = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="radio" name="plan" id="trial" aria-label="Start Super trial $12.99">
<button type="button" style="position:relative;display:block;width:240px;height:50px">
<span role="radio" aria-checked="false">Spanish</span>
<label for="trial" style="position:absolute;inset:0"></label></button>
<div style="position:relative;width:240px;height:50px">
<button type="button" style="width:100%;height:100%"
 onclick="window.clicks.push('french')">French</button>
<label for="trial" style="position:absolute;inset:0;opacity:0"></label></div>
<div style="position:relative;width:240px;height:50px">
<button type="button" style="width:100%;height:100%"
 onclick="window.clicks.push('german')">German</button></div>
<a href="/courses/remove-course" style="display:block;width:240px;height:50px;
 transform:translateY(-50px)">Away</a>
<div style="position:relative;width:240px;height:50px;margin-top:10px">
<form method="post" action="/courses/remove-course" style="position:absolute;inset:0;margin:0">
<button type="submit" aria-label="Go" style="width:100%;height:100%"></button></form>
<button type="button" style="position:absolute;inset:0;clip-path:inset(0 0 60% 0)"
 onclick="window.clicks.push('italian')">Italian</button></div>
<div style="position:relative;width:240px;height:50px;margin-top:10px">
<label for="trial" style="position:absolute;inset:0">Trial</label>
<button type="button" style="position:absolute;inset:0;pointer-events:none"
 onclick="window.clicks.push('portuguese')">Portuguese</button></div>
<script>window.clicks = [];</script>
</body></html>"""


@pytest.fixture
def short_action_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Playwright waits for a click point the element itself receives; the
    wait's bound only sets how long a test takes to see it fail."""

    monkeypatch.setattr(playwright_adapter, "ACTION_TIMEOUT_MILLISECONDS", 1_500)


@pytest.mark.usefixtures("short_action_timeout")
async def test_a_click_point_on_another_element_never_acts_there() -> None:
    """Whatever lies over the element's centre, whether a label inside the
    button around it, a transparent or transformed overlay, or what a clip-path
    or ``pointer-events: none`` lets through, the click never reaches it."""

    async with lesson_pages({"/lesson/1": OVERLAYS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        outcomes = {}
        for name, role in (
            ("Spanish", "radio"),
            ("French", None),
            ("German", None),
            ("Italian", None),
            ("Portuguese", None),
        ):
            outcomes[name] = await _outcome(runtime, _click_named(page, name, role=role))
            page = await runtime.observe()
        trial = await _page_value(runtime, "document.getElementById('trial').checked")
        clicks = await _page_value(runtime, "window.clicks")

    assert trial is False
    assert clicks == []
    assert set(outcomes.values()) <= {GRANT_NOT_APPLICABLE, OUTCOME_UNKNOWN}
    assert left == []


ORDINARY = """<!doctype html><html><head><title>Lesson</title></head><body>
<form method="get" action="/lesson/check">
<input type="text" name="answer" aria-label="Answer"><button type="submit">Check</button></form>
<label role="button" for="es">Spanish</label>
<input type="radio" name="language" id="es" aria-label="Spanish tile">
<button type="button" style="position:relative;display:block;width:240px;height:50px"
 onclick="window.clicks.push('tile')"><span role="radio" aria-checked="false">French</span>
<span style="position:absolute;inset:0;pointer-events:none"></span></button>
<script>window.clicks = [];</script>
</body></html>"""


async def test_the_click_guard_lets_covered_acts_reach_what_they_name() -> None:
    """Enter submits a form in the prefix through its default button, a label
    changes its own control, and a click passes through a decoration that
    takes no pointer events: the guard stops none of them."""

    pages = {"/lesson/1": ORDINARY, "/lesson/check": BLANK}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        page = await _covered(runtime, _click_named(page, "Spanish"))
        chosen = await _page_value(runtime, "document.getElementById('es').checked")
        page = await _covered(runtime, _click_named(page, "French", role="radio"))
        clicks = await _page_value(runtime, "window.clicks")
        page = await _covered(runtime, _type(page, "Answer", "hola"))
        checked = await _covered(runtime, _press(page, "Answer", "Enter"))

    assert chosen is True
    assert clicks == ["tile"]
    assert checked.url.endswith("/lesson/check?answer=hola")
    assert left == []


# ---------------------------------------------------------------------------
# The DOM changed between observation and dispatch.

SWAPPED = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="radio" name="plan" id="keep" aria-label="Keep learning">
<input type="radio" name="plan" id="trial" aria-label="Start Super trial $12.99">
<label role="button" id="stay" for="keep">Stay</label>
<a id="next" href="/lesson/2">Next</a>
<form id="here" method="get" action="/lesson/2"><button id="check">Check</button></form>
<form id="away" method="post" action="/courses/remove-course"></form>
<script>
window.swap = {
  label: () => { document.getElementById('stay').htmlFor = 'trial'; },
  href: () => { document.getElementById('next').href = '/courses/remove-course'; },
  action: () => { document.getElementById('here').action = '/courses/remove-course'; },
  form: () => { document.getElementById('away').append(document.getElementById('check')); },
};
</script>
</body></html>"""
SWAPPED_ON_PRESS = SWAPPED.replace(
    "</script>",
    """for (const [id, swap] of [['stay', 'label'], ['next', 'href'], ['check', 'action']]) {
  document.getElementById(id).addEventListener('mousedown', () => window.swap[swap]());
}
</script>""",
)
SWAP_TARGETS = {"label": "Stay", "href": "Next", "action": "Check", "form": "Check"}


@pytest.mark.parametrize("swap", sorted(SWAP_TARGETS))
async def test_a_change_after_observation_is_read_live(swap: str) -> None:
    pages = {"/lesson/1": SWAPPED, "/lesson/2": BLANK}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        await _page_value(runtime, f"window.swap.{swap}()")
        refusal = await _refused(runtime, _click_named(page, SWAP_TARGETS[swap]))
        trial = await _page_value(runtime, "document.getElementById('trial').checked")

    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert trial is False
    assert left == []


@pytest.mark.parametrize("swap", ["action", "href", "label"])
async def test_a_change_the_press_itself_makes_never_acts_outside(swap: str) -> None:
    """The page swaps a label's control, a link or a form action on
    ``mousedown``, after the live check."""

    pages = {"/lesson/1": SWAPPED_ON_PRESS, "/lesson/2": BLANK}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        outcome = await _outcome(runtime, _click_named(page, SWAP_TARGETS[swap]))
        trial = False
        if swap == "label":
            trial = await _page_value(runtime, "document.getElementById('trial').checked")

    # The click guard stops the label's click on another control; the fence
    # refuses the swapped link's and form's documents.
    assert outcome == OUTCOME_UNKNOWN
    assert trial is False
    assert left == []


# ---------------------------------------------------------------------------
# The element's own handlers act on another element.

HANDLERS = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="checkbox" id="renew" aria-label="Renew my subscription">
<form id="away" method="post" action="/courses/remove-course">
<button type="submit" id="go">Go</button></form>
<a id="leave" href="/courses/remove-course">Remove course</a>
<button type="button" onclick="document.getElementById('renew').click()">Next</button>
<button type="button" onclick="document.getElementById('away').requestSubmit()">Continue</button>
<button type="button" onclick="document.getElementById('away').submit()">Done</button>
<button type="button" onclick="document.getElementById('go').click()">Skip</button>
<button type="button" onclick="document.getElementById('leave').click()">Start</button>
</body></html>"""


async def test_a_handler_submitting_another_form_or_following_another_link_is_fenced() -> None:
    """requestSubmit, submit and click on another element, from the element's
    own click handler, never load a document outside the prefix."""

    async with lesson_pages({"/lesson/1": HANDLERS}) as (runtime, visit, left):
        outcomes = []
        for name in ("Continue", "Done", "Skip", "Start"):
            page = await visit("/lesson/1")
            outcomes.append(await _outcome(runtime, _click_named(page, name)))

    assert outcomes == [OUTCOME_UNKNOWN] * 4
    assert left == []


async def test_a_handler_clicking_another_check_box_is_page_script() -> None:
    """ADR-0129 Consequences: page script is not bounded by the classifier.
    ``element.click()`` from the element's own handler is page script, as is
    setting ``checked``; it changes only the page, and any request it causes
    is the page's own."""

    async with lesson_pages({"/lesson/1": HANDLERS}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        outcome = await _outcome(runtime, _click_named(page, "Next"))
        renew = await _page_value(runtime, "document.getElementById('renew').checked")

    assert (outcome, renew) == ("dispatched", True)
    assert left == []


# ---------------------------------------------------------------------------
# window.open and target=_blank.

WINDOWS = """<!doctype html><html><head><title>Lesson</title></head><body>
<a href="/courses/remove-course" target="_blank">Next</a>
<a href="/lesson/2" target="_blank">Continue</a>
<button type="button" onclick="window.open('/courses/remove-course')">Done</button>
<button type="button"
 onclick="window.open('/courses/remove-course', '_blank', 'noopener')">Skip</button>
<button type="button" onclick="const w = window.open('about:blank');
 w.document.write('<form method=post action=/courses/remove-course></form>');
 w.document.forms[0].submit()">Start</button>
<button type="button" onclick="window.open('/courses/remove-course', '_self')">Review</button>
</body></html>"""


async def test_no_new_window_loads_anything() -> None:
    """A new window is closed and loads nothing; a window the page names as
    itself is the page, and fenced."""

    pages = {"/lesson/1": WINDOWS, "/lesson/2": BLANK}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        refusal = await _refused(runtime, _click_named(page, "Next"))
        page = await runtime.observe()
        for name in ("Continue", "Done", "Skip", "Start"):
            page = await _covered(runtime, _click_named(page, name))
        await _pause(runtime)
        windows = _windows(runtime)
        itself = await _outcome(runtime, _click_named(page, "Review"))

    assert refusal.reason_code == GRANT_NOT_APPLICABLE
    assert windows == 1
    assert itself == OUTCOME_UNKNOWN
    assert left == []


# ---------------------------------------------------------------------------
# Excluded words in disguise.

DISGUISED = {
    # Zero-width characters inside the word.
    "zero width": "P\u200bay",
    "zero width joiner": "De\u200dle\u2060te",
    "soft hyphen": "Sub\u00adscribe",
    # A right-to-left override displays the reversed letters as the word.
    "override": "\u202eyaP\u202c now",
    "override in word": "D\u202eetele\u202c",
    "override phrase": "Keep \u202eebircsbuS\u202c going",
    # Letters from other scripts that look like Latin ones.
    "cyrillic": "D\u0435l\u0435t\u0435",
    "greek": "\u03a1\u03b1y",
    "mixed": "\u0405ubscrib\u0435",
}
DISGUISED_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
BUTTONS
<script>window.clicks = [];</script>
</body></html>""".replace(
    "BUTTONS",
    "\n".join(
        f'<button aria-label="Continue" onclick="window.clicks.push({index})">{text}</button>'
        for index, text in enumerate(DISGUISED.values())
    ),
)


async def test_excluded_words_in_disguise_are_still_excluded() -> None:
    """Zero-width characters, a right-to-left override and lookalike letters
    from other scripts never hide "Pay", "Delete" or "Subscribe"."""

    async with lesson_pages({"/lesson/1": DISGUISED_PAGE}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        refused = {}
        for index, disguise in enumerate(DISGUISED):
            button = [element for element in page.elements if element.role == "button"][index]
            refused[disguise] = await _outcome(runtime, _click_ref(page, button.ref))
            page = await runtime.observe()
        clicks = await _page_value(runtime, "window.clicks")

    assert refused == dict.fromkeys(DISGUISED, GRANT_NOT_APPLICABLE)
    assert clicks == []
