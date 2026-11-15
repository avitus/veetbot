"""Playwright runtime cases that need a real Chromium.

Rendering, transport, live element facts and grant refusals before dispatch.
The fake-page cases and the shared helpers stay in ``test_browser_playwright``,
which runs in the fast suite; only these join the browser partition.
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserProviderError,
    BrowserTargetFacts,
)
from tests.real_browser_support import RealBrowserRuntime, local_https_site, require_real_browser
from tests.unit.test_browser_playwright import (
    GRANT_NOW,
    _click_named,
    _press,
    _ref,
    _refused,
    lesson_constraint,
    lesson_pages,
)


async def test_headed_document_renders_compressed_responses() -> None:
    """The fulfillment path renders each wire encoding without decoding the body twice."""
    require_real_browser()
    html = b"<!doctype html><title>Compressed sign-in</title><p>Ready</p>"
    # The Brotli fixture encodes the same HTML without adding a test dependency.
    encoded = {
        "gzip": gzip.compress(html),
        "deflate": zlib.compress(html),
        "br": bytes.fromhex(
            "1b3b0000c4032e4bac7125001c72c0feed40712609051eb2123e41516c2c50ae966e"
            "85e5c1fd11e4dc3a914c"
        ),
    }

    async def document(request: Request) -> Response:
        """Serve an encoded document with its original wire content length."""
        encoding = request.path_params["encoding"]
        return Response(
            encoded[encoding], media_type="text/html", headers={"content-encoding": encoding}
        )

    app = Starlette(routes=[Route("/login/{encoding}", document)])
    async with local_https_site(app) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,), headed=True)
        try:
            for encoding in encoded:
                page = await runtime.navigate(site.url(f"/login/{encoding}"))
                assert (encoding, page.title, page.text) == (
                    encoding,
                    "Compressed sign-in",
                    "Ready",
                )
        finally:
            await runtime.close()


async def test_headed_popup_policy_preserves_sign_in_and_redirect_boundaries() -> None:
    """Real sign-in keeps cookies, storage, CSP and a single POST through the proxy."""
    require_real_browser()
    submissions: list[tuple[bytes, dict[str, str]]] = []
    blocked_images: list[str] = []

    async def login(request: Request) -> Response:
        """Issue separate cookies and a policy that must survive fulfillment."""
        del request
        response = HTMLResponse(
            """<!doctype html><title>Sign in</title>
            <script>localStorage.setItem('login_started', 'yes')</script>
            <img src="/blocked.png">
            <form action="/submit" method="post">
              <input name="answer" value="salve"><button>Sign in</button>
            </form>""",
            headers={"content-security-policy": "img-src 'none'"},
        )
        response.set_cookie("first", "one", secure=True, httponly=True)
        response.set_cookie("second", "two", secure=True, httponly=True)
        return response

    async def submit(request: Request) -> Response:
        """Record the mutation and set authentication state on its redirect."""
        submissions.append((await request.body(), request.cookies))
        response = RedirectResponse("/ready", status_code=303)
        response.set_cookie("signed_in", "yes", secure=True, httponly=True)
        return response

    async def ready(request: Request) -> Response:
        """Require the redirected browser to arrive authenticated with GET."""
        assert request.method == "GET"
        assert request.cookies["signed_in"] == "yes"
        return HTMLResponse('<title>Ready</title><a href="/leave">Leave</a>')

    async def leave(request: Request) -> Response:
        """Attempt an off-origin redirect that the HTTP client must not follow."""
        del request
        return RedirectResponse("https://outside.test/forbidden", status_code=302)

    async def image(request: Request) -> Response:
        """Record any resource request that the site's original CSP should block."""
        blocked_images.append(request.url.path)
        return Response(b"image")

    app = Starlette(
        routes=[
            Route("/login", login),
            Route("/submit", submit, methods=["POST"]),
            Route("/ready", ready),
            Route("/leave", leave),
            Route("/blocked.png", image),
        ]
    )
    async with local_https_site(app) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,), headed=True)
        try:
            await runtime.navigate(site.url("/login"))
            page = runtime._current_page()
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await page.get_by_role("button", name="Sign in").click()
            assert await page.title() == "Ready"
            assert await page.evaluate("localStorage.getItem('login_started')") == "yes"
            assert await page.evaluate("document.cookie") == ""
            assert submissions == [(b"answer=salve", {"first": "one", "second": "two"})]
            assert blocked_images == []
            with pytest.raises(BrowserProviderError) as refused:
                await runtime.navigate(site.url("/leave"))
            assert refused.value.reason_code == "tool.browser.url_disallowed"
            assert "outside.test:443" not in site.relay.refused
        finally:
            await runtime.close()


async def test_headed_transport_does_not_add_cookies_to_cross_site_post() -> None:
    """The HTTP client's jar must not undo Chromium's SameSite cookie exclusion."""
    require_real_browser()
    received: list[dict[str, str]] = []

    async def submit(request: Request) -> Response:
        """Record the cookies on a POST initiated from an opaque origin."""
        received.append(request.cookies)
        return HTMLResponse("<title>Received</title>")

    app = Starlette(routes=[Route("/submit", submit, methods=["POST"])])
    async with local_https_site(app) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,), headed=True)
        try:
            page = runtime._current_page()
            await page.context.add_cookies(
                [{"name": "strict", "value": "private", "url": site.origin, "sameSite": "Strict"}]
            )
            await page.set_content(
                f'<form action="{site.url("/submit")}" method="post"><button>Send</button></form>'
            )
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await page.get_by_role("button", name="Send").click()
            assert await page.title() == "Received"
            assert received == [{}]
        finally:
            await runtime.close()


async def test_saved_session_reopens_with_navigation_after_its_page_is_released() -> None:
    """A resumed browser keeps login state but has no page until it navigates."""
    require_real_browser()

    async def account(request: Request) -> Response:
        """Expose protected fixture content according to the restored session cookie."""

        text = "Account overview" if request.cookies.get("session") == "synthetic" else "Sign in"
        return HTMLResponse(f"<title>Account</title><main>{text}</main>")

    async with local_https_site(Starlette(routes=[Route("/account", account)])) as site:
        original = RealBrowserRuntime()
        await original.start(site.proxy_url, (site.origin,), headed=True)
        try:
            await original._current_page().context.add_cookies(
                [{"name": "session", "value": "synthetic", "url": site.origin}]
            )
            assert "Account overview" in (await original.navigate(site.url("/account"))).text
            saved = await original.storage_state()
        finally:
            await original.close()

        resumed = RealBrowserRuntime()
        await resumed.start(site.proxy_url, (site.origin,), storage_state=saved, headed=True)
        try:
            with pytest.raises(BrowserProviderError) as blank:
                await resumed.observe()
            assert blank.value.reason_code == "tool.browser.navigation_required"
            assert "Account overview" in (await resumed.navigate(site.url("/account"))).text
            assert "Account overview" in (await resumed.observe()).text
        finally:
            await resumed.close()


FACTS_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<input type="password" aria-label="Password">
<input autocomplete="cc-number" aria-label="Card number">
<input type="email" aria-label="Email">
<textarea aria-label="Answer"></textarea>
<a href="/settings">Settings</a>
<form action="/lesson/submit"><button formaction="/lesson/../checkout">Check</button></form>
<a href="/files/report.pdf" download>Report</a>
<dialog open aria-label="Try Super free"><button>Keep going</button></dialog>
<button aria-label="Continue">Pay $12.99</button>
<a href="/lesson/2">Next lesson</a>
<a href="#top">Top</a>
<a href="mailto:owner@example.org">Write</a>
<div role="textbox" contenteditable="true" aria-label="Story" style="min-height:20px"></div>
<input type="checkbox" aria-label="Sound">
<select aria-label="Word"><option>gato</option></select>
<div role="checkbox" aria-checked="false" aria-label="Hints">Hints</div>
<button aria-label="Next">Next $</button>
</body></html>"""


async def test_element_facts_classify_fields_links_forms_dialogs() -> None:
    """ADR-0129 section 4.5: facts are derived from live attributes, in a real Chromium."""

    require_real_browser()

    async def lesson(request: Request) -> Response:
        del request
        return HTMLResponse(FACTS_PAGE)

    async with local_https_site(Starlette(routes=[Route("/lesson/1", lesson)])) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            observation = await runtime.navigate(site.url("/lesson/1"))
            facts = runtime.facts(observation.revision)
            stale = runtime.facts("another-revision")
        finally:
            await runtime.close()

    assert facts is not None
    assert stale is None
    assert facts.revision == observation.revision
    by_name = {element.name: facts.elements[element.ref] for element in observation.elements}
    assert by_name["Password"].field_kind is BrowserFieldKind.PASSWORD
    assert by_name["Card number"].field_kind is BrowserFieldKind.PAYMENT
    assert by_name["Email"].field_kind is BrowserFieldKind.EMAIL
    assert by_name["Answer"].field_kind is BrowserFieldKind.MULTILINE
    # Kinds follow what the runtime acts on: it types only into an input or a
    # text area, selects only in a select and checks only a native check box.
    assert by_name["Story"].field_kind is BrowserFieldKind.EDITABLE
    assert by_name["Sound"].field_kind is BrowserFieldKind.CHOICE
    assert by_name["Word"].field_kind is BrowserFieldKind.SELECT
    assert by_name["Hints"].field_kind is BrowserFieldKind.CUSTOM_CHOICE
    assert by_name["Settings"].link_target == BrowserTargetFacts(
        same_origin=True, first_segment="settings", sensitive_path=True
    )
    assert by_name["Check"].form_target == BrowserTargetFacts(
        same_origin=True, first_segment="checkout", sensitive_path=True
    )
    assert by_name["Report"].download is True
    assert by_name["Keep going"].context_name == "Try Super free"
    assert by_name["Continue"].labels == {BrowserLabelSource.VISIBLE_TEXT: "Pay $12.99"}
    # A source that normalizes to the name but reads differently is carried.
    assert by_name["Next"].labels == {BrowserLabelSource.VISIBLE_TEXT: "Next $"}
    assert by_name["Hints"].labels == {}
    assert by_name["Next lesson"].link_target == BrowserTargetFacts(
        same_origin=True, first_segment="lesson", sensitive_path=False
    )
    assert by_name["Top"].link_target is None
    assert by_name["Write"].link_target is not None
    assert by_name["Write"].link_target.same_origin is False
    rendered = facts.model_dump_json()
    assert "/settings" not in rendered and "checkout" in rendered
    assert "report.pdf" not in rendered and "mailto" not in rendered


GRANT_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<button id="go" onclick="window.clicks.push('go')">Continue</button>
<button id="rename" onclick="window.clicks.push('rename')">Continue</button>
<button id="pay" aria-label="Continue" onclick="window.clicks.push('pay')">Pay $12.99</button>
<a id="leave" href="/settings" onclick="window.clicks.push('leave');return false">Continue</a>
<script>window.clicks = [];</script>
</body></html>"""


@asynccontextmanager
async def grant_page() -> AsyncIterator[tuple[RealBrowserRuntime, BrowserObservation]]:
    require_real_browser()

    async def lesson(request: Request) -> Response:
        del request
        return HTMLResponse(GRANT_PAGE)

    async with local_https_site(Starlette(routes=[Route("/lesson/1", lesson)])) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            yield runtime, await runtime.navigate(site.url("/lesson/1"))
        finally:
            await runtime.close()


def _click_on(observation: BrowserObservation, index: int) -> BrowserAction:
    element = observation.elements[index]
    return BrowserAction(
        kind=BrowserActionKind.CLICK, expected_revision=observation.revision, ref=element.ref
    )


async def _clicks(runtime: RealBrowserRuntime) -> list[str]:
    clicks: list[str] = await runtime._current_page().evaluate("window.clicks")
    return clicks


async def test_a_covered_click_dispatches_under_the_constraint() -> None:
    async with grant_page() as (runtime, observation):
        acted = await runtime.act(
            _click_on(observation, 0), constraint=lesson_constraint(), now=GRANT_NOW
        )
        clicks = await _clicks(runtime)

    assert clicks == ["go"]
    assert acted.revision != observation.revision


@pytest.mark.parametrize(
    ("setup", "index"),
    [
        ("history.pushState({}, '', '/settings')", 0),
        ("document.getElementById('rename').textContent = 'Buy now'", 1),
        (None, 2),
    ],
    ids=[
        "live_page_outside_prefix",
        "renamed_element",
        "hidden_label_source",
    ],
)
async def test_a_grant_refuses_before_dispatch(setup: str | None, index: int) -> None:
    """The live page is rechecked: a left prefix, a renamed control, or a hidden
    label source ("Continue" over "Pay") refuses. A sensitive link target is
    refused in ``test_refusal_forgets_the_revision``."""

    async with grant_page() as (runtime, observation):
        assert observation.elements[index].name == "Continue"
        if setup is not None:
            await runtime._current_page().evaluate(setup)
        refusal = await _refused(runtime, _click_on(observation, index))
        clicks = await _clicks(runtime)

    assert refusal.reason_code == "tool.browser.grant_not_applicable"
    assert clicks == []


async def test_refusal_forgets_the_revision() -> None:
    """ADR-0129 D16: after a refusal the model must observe again.

    The refused click is the "Continue" link to a sensitive path.
    """

    async with grant_page() as (runtime, observation):
        assert observation.elements[3].name == "Continue"
        refusal = await _refused(runtime, _click_on(observation, 3))
        with pytest.raises(BrowserProviderError) as stale:
            await runtime.act(_click_on(observation, 0))
        clicks = await _clicks(runtime)
        forgotten = runtime.facts(observation.revision)

    assert refusal.reason_code == "tool.browser.grant_not_applicable"
    assert stale.value.reason_code == "tool.browser.page_changed"
    assert clicks == []
    assert forgotten is None


SUBMITTING_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<form method="post" action="/courses/remove-course">
<button type="submit">Check</button>
<button type="submit" role="radio" aria-checked="false">Spanish</button>
</form>
</body></html>"""


async def test_space_and_choice_role_submissions_off_the_prefix_are_refused() -> None:
    """Rule 11: Space on a submit button, or a click on one with a choice role, submits."""

    async with lesson_pages({"/lesson/1": SUBMITTING_PAGE}) as (runtime, visit, left):
        page = await visit("/lesson/1")
        space = await _refused(runtime, _press(page, "Check", "Space"))
        page = await runtime.observe()
        choice = await _refused(runtime, _click_named(page, "Spanish"))

    assert space.reason_code == choice.reason_code == "tool.browser.grant_not_applicable"
    assert left == []


TARGETS_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<form method="post" action="/lesson/check">
<input type="text" aria-label="Answer">
<button type="submit" formaction="/settings/delete">Go</button>
</form>
<form method="post" action="/lesson/check">
<button type="submit" formaction="/courses/remove"><span role="radio">Spanish</span></button>
</form>
<form id="away" method="post" action="/courses/leave"></form>
<button type="submit" form="away" aria-label="Outer"><span role="radio">French</span></button>
<form id="quiz" method="post" action="/lesson/check">
<button id="quit" type="submit" formaction="/courses/quit">Quit</button>
</form>
<label role="button" for="quit">Keep going</label>
<form method="post" action="/courses/delete">
<input type="text" formaction="/lesson/check" aria-label="Word">
</form>
<a href="#top">Top</a>
<a href="">Here</a>
</body></html>"""
BASE_PAGE = """<!doctype html><html><head><title>Lesson</title>
<base href="/courses/remove-course"></head><body>
<a href="#next">Continue</a>
</body></html>"""


async def test_facts_record_the_target_the_browser_submits_to() -> None:
    """Section 4.5: the default button's formaction, the activated submitter's own
    target, and a fragment link under a ``<base>`` elsewhere (review finding)."""

    pages = {"/lesson/1": TARGETS_PAGE, "/lesson/2": BASE_PAGE}
    async with lesson_pages(pages) as (runtime, visit, _left):
        targets = await visit("/lesson/1")
        targets_facts = runtime.facts(targets.revision)
        based = await visit("/lesson/2")
        based_facts = runtime.facts(based.revision)

    assert targets_facts is not None and based_facts is not None

    def fact(observation: BrowserObservation, name: str, role: str | None = None) -> Any:
        facts = targets_facts if observation is targets else based_facts
        assert facts is not None
        return facts.elements[_ref(observation, name, role=role)]

    def target(first_segment: str, *, sensitive: bool) -> BrowserTargetFacts:
        return BrowserTargetFacts(
            same_origin=True, first_segment=first_segment, sensitive_path=sensitive
        )

    found = {
        "Answer": fact(targets, "Answer").form_target,
        "Spanish": fact(targets, "Spanish", "radio").form_target,
        "French": fact(targets, "French", "radio").form_target,
        "Keep going": fact(targets, "Keep going").form_target,
        "Word": fact(targets, "Word").form_target,
        "Top": fact(targets, "Top").link_target,
        "Here": fact(targets, "Here").link_target,
        "Continue": fact(based, "Continue").link_target,
    }
    assert found == {
        "Answer": target("settings", sensitive=True),
        "Spanish": target("courses", sensitive=True),
        "French": target("courses", sensitive=False),
        "Keep going": target("courses", sensitive=False),
        "Word": target("courses", sensitive=True),
        "Top": None,
        "Here": None,
        "Continue": target("courses", sensitive=True),
    }


async def test_enter_and_a_based_fragment_link_off_the_prefix_are_refused() -> None:
    """Enter submits through the default button; ``<base>`` moves a fragment link."""

    pages = {"/lesson/1": TARGETS_PAGE, "/lesson/2": BASE_PAGE}
    async with lesson_pages(pages) as (runtime, visit, left):
        page = await visit("/lesson/1")
        enter = await _refused(runtime, _press(page, "Answer", "Enter"))
        page = await visit("/lesson/2")
        link = await _refused(runtime, _click_named(page, "Continue"))

    assert enter.reason_code == link.reason_code == "tool.browser.grant_not_applicable"
    assert left == []


RADIO_PAGE = """<!doctype html><html><head><title>Lesson</title></head><body>
<label><input type="radio" name="plan" id="keep" checked> Keep learning</label>
<label><input type="radio" name="plan" id="trial"> Start Super trial $12.99</label>
</body></html>"""


async def test_an_arrow_key_on_a_radio_is_refused_before_dispatch() -> None:
    """Rule 7: ArrowDown would check the payment-labelled radio unclassified."""

    async with lesson_pages({"/lesson/1": RADIO_PAGE}) as (runtime, visit, _left):
        page = await visit("/lesson/1")
        assert page.elements[0].name == "Keep learning"
        refusal = await _refused(runtime, _press(page, "Keep learning", "ArrowDown", role="radio"))
        state = await runtime._current_page().evaluate(
            "[document.getElementById('keep').checked, document.getElementById('trial').checked]"
        )

    assert refusal.reason_code == "tool.browser.grant_not_applicable"
    assert state == [True, False]
