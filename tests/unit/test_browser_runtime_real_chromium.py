"""The Playwright runtime on real lesson-like pages, in a real Chromium (ADR-0130).

Each page is served by the shared local HTTPS harness, so the production
runtime's origin guard, CDP document guard and egress routing all run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserElement,
    BrowserInteractiveEvent,
    BrowserObservation,
    BrowserProviderError,
)
from tests import real_browser_support
from tests.real_browser_support import (
    REQUIRE_REAL_BROWSER,
    LocalHttpsSite,
    RealBrowserRuntime,
    local_https_site,
    require_real_browser,
)


def html(body: str, *, script: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><title>Lesson</title></head>"
        f"<body>{body}<script>{script}</script></body></html>"
    )


@asynccontextmanager
async def browsing(
    routes: list[Route],
    *,
    headed: bool = False,
) -> AsyncIterator[tuple[LocalHttpsSite, RealBrowserRuntime]]:
    require_real_browser()
    async with local_https_site(Starlette(routes=routes)) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,), headed=headed)
        try:
            yield site, runtime
        finally:
            await runtime.close()


def test_a_required_real_browser_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The required delivery lane fails explicitly when Chromium is unavailable."""

    monkeypatch.setattr(real_browser_support, "chromium_installed", lambda: False)
    monkeypatch.setenv(REQUIRE_REAL_BROWSER, "1")
    with pytest.raises(pytest.fail.Exception):
        require_real_browser()
    monkeypatch.delenv(REQUIRE_REAL_BROWSER)
    with pytest.raises(pytest.skip.Exception):
        require_real_browser()


async def test_the_harness_serves_the_site_through_the_production_guards() -> None:
    """A document on the synthetic origin passes the runtime's guards; nothing escapes."""

    async def home(request: Request) -> Response:
        del request
        return html('<h1>Home</h1><img src="https://tracker.test/pixel.png" alt="">')

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.navigate("https://elsewhere.test/")

    assert observation.url == site.url("/")
    assert "Home" in observation.text
    assert refused.value.reason_code == "tool.browser.url_disallowed"
    assert f"{site.host}:443" in site.relay.tunnelled
    assert "elsewhere.test:443" not in site.relay.tunnelled
    assert set(site.relay.refused) <= {"tracker.test:443"}


async def test_a_headed_browser_asks_its_proxy_only_for_what_the_page_loads() -> None:
    """Full Chromium contacts its vendor's services on its own; the launch stops it (ADR-0146)."""

    async def sign_in(request: Request) -> Response:
        del request
        return html(
            '<form method="post" action="/session">'
            '<label>Email <input name="email" type="email" autocomplete="username"></label>'
            '<label>Password <input name="password" type="password"></label>'
            '<label>Street address <input name="street" autocomplete="street-address"></label>'
            '<label>Card number <input name="card" autocomplete="cc-number"></label>'
            "<button>Sign in</button></form>"
        )

    require_real_browser()
    async with local_https_site(Starlette(routes=[Route("/", sign_in)])) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,), headed=True)
        try:
            observation = await runtime.navigate(site.url("/"))
            # The time, search, form and messaging requests all began within
            # three seconds of the launch.
            await asyncio.sleep(5)
        finally:
            await runtime.close()

    assert "Sign in" in observation.text
    assert f"{site.host}:443" in site.relay.tunnelled
    assert site.relay.refused_page_targets() == set()


async def test_the_production_runtime_still_refuses_the_harness_certificate() -> None:
    """Only the test subclass trusts the throwaway certificate."""

    require_real_browser()

    async def home(request: Request) -> Response:
        del request
        return html("<h1>Home</h1>")

    async with local_https_site(Starlette(routes=[Route("/", home)])) as site:
        runtime = PythonPlaywrightRuntime()
        await runtime.start(site.proxy_url, (site.origin,))
        try:
            with pytest.raises(BrowserProviderError) as raised:
                await runtime.navigate(site.url("/"))
        finally:
            await runtime.close()

    assert raised.value.reason_code == "tool.browser.provider_unavailable"


async def test_verification_accepts_a_document_loading_within_its_thirty_second_budget() -> None:
    """A slow subresource must not spend an undocumented twenty-second sub-budget."""

    async def members(request: Request) -> Response:
        del request
        return html('<h1>Your lessons</h1><img src="/slow.svg" alt="">')

    async def slow_image(request: Request) -> Response:
        del request
        await asyncio.sleep(20.5)
        return Response(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
            media_type="image/svg+xml",
        )

    async with browsing([Route("/learn", members), Route("/slow.svg", slow_image)]) as (
        site,
        runtime,
    ):
        async with asyncio.timeout(30):
            evidence = await runtime.load_page_evidence(site.url("/learn"))

    assert evidence.on_allowed_origin
    assert evidence.path == "/learn"
    assert not evidence.challenge_visible


async def _exercise_api(request: Request) -> Response:
    del request
    await asyncio.sleep(0.3)
    return JSONResponse({"title": "Exercise 1"})


async def _late_page(request: Request) -> Response:
    del request
    return html(
        '<main id="main">Loading</main>',
        script=(
            "fetch('/api/exercise').then(r=>r.json()).then(d=>{"
            "document.getElementById('main').innerHTML="
            "'<h1>'+d.title+'</h1><button>Check</button>';});"
        ),
    )


async def _exercise_page(request: Request) -> Response:
    del request
    return html(
        '<main id="main"><h1>Exercise 1</h1><button id="check">Check</button></main>',
        script=(
            "document.getElementById('check').addEventListener('click',()=>{"
            "const m=document.getElementById('main');m.textContent='Checking';"
            "setTimeout(()=>{m.innerHTML='<p>Correct!</p><button id=\"next\">Continue</button>';"
            "document.getElementById('next').addEventListener('click',()=>{"
            "m.innerHTML='<h1>Exercise 2</h1>';});},250);});"
        ),
    )


async def _crowded_page(request: Request) -> Response:
    del request
    hidden = "".join(
        f'<button style="display:none">Hidden {index}</button>' for index in range(300)
    )
    visible = "".join(f"<button>Answer {index}</button>" for index in range(5))
    return html(hidden + visible)


async def _contents_page(request: Request) -> Response:
    del request
    return html(
        '<div role="button" style="display:contents"><span>Contents choice</span></div>'
        '<a href="/x" style="display:contents"><span>Contents link</span></a>'
        '<div role="button" style="display:contents"></div>'
        '<button style="visibility:collapse">Collapsed</button>'
        "<button>Plain</button>"
    )


async def _collapsed_page(request: Request) -> Response:
    del request
    collapsed = "".join(
        f'<button style="visibility:collapse">Collapsed {index}</button>' for index in range(300)
    )
    return html(collapsed + "<button>Answer</button>")


async def _restless_page(request: Request) -> Response:
    del request
    return html(
        '<p id="clock">0</p><button>Stay</button>',
        script=(
            "let n=0;setInterval(()=>{"
            "document.getElementById('clock').textContent=String(++n);},100);"
        ),
    )


async def _fleeting_page(request: Request) -> Response:
    """A page that removes one control as soon as the runtime reads it, the way
    a live timeline replaces the nodes it renders."""
    del request
    return html(
        '<button id="fleeting">Fleeting</button><button>Stay</button>',
        script=(
            "const read=Element.prototype.getAttribute;"
            "Element.prototype.getAttribute=function(name){"
            "if(this.id==='fleeting'){queueMicrotask(()=>this.remove());}"
            "return read.call(this,name);};"
        ),
    )


async def _linking_page(request: Request) -> Response:
    del request
    return html('<h1>Page one</h1><a href="/two">Next</a>')


async def _second_document(request: Request) -> Response:
    del request
    await asyncio.sleep(0.3)
    return html("<h1>Page two</h1><button>Onward</button>")


LESSON_ROUTES = [
    Route("/late", _late_page),
    Route("/api/exercise", _exercise_api),
    Route("/exercise", _exercise_page),
    Route("/crowded", _crowded_page),
    Route("/contents", _contents_page),
    Route("/collapsed", _collapsed_page),
    Route("/restless", _restless_page),
    Route("/fleeting", _fleeting_page),
    Route("/one", _linking_page),
    Route("/two", _second_document),
]


def _element(observation: BrowserObservation, name: str) -> BrowserElement:
    return next(element for element in observation.elements if element.name == name)


def _click(observation: BrowserObservation, name: str) -> BrowserAction:
    return BrowserAction(
        kind=BrowserActionKind.CLICK,
        expected_revision=observation.revision,
        ref=_element(observation, name).ref,
    )


async def test_navigation_waits_for_the_exercise() -> None:
    """ADR-0130 decision 5: navigate returns the page once it settles."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        observation = await runtime.navigate(site.url("/late"))

    assert "Exercise 1" in observation.text
    assert "Loading" not in observation.text
    assert [element.name for element in observation.elements] == ["Check"]


async def test_hidden_controls_never_take_an_element_slot_in_chromium() -> None:
    """ADR-0130 decision 8: the 256 slots go to visible elements, in document order."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        observation = await runtime.navigate(site.url("/crowded"))

    assert [element.name for element in observation.elements] == [
        f"Answer {index}" for index in range(5)
    ]


async def test_the_visibility_scan_keeps_display_contents_controls() -> None:
    """The one-call scan matches Playwright's is_visible (review finding): a
    display:contents control is visible when one of its children is."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        contents = await runtime.navigate(site.url("/contents"))

    assert [(element.role, element.name) for element in contents.elements] == [
        ("button", "Contents choice"),
        ("link", "Contents link"),
        ("button", "Plain"),
    ]


async def test_collapsed_controls_never_take_an_element_slot() -> None:
    """visibility:collapse is hidden to is_visible, so the scan drops it first."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        collapsed = await runtime.navigate(site.url("/collapsed"))

    assert [element.name for element in collapsed.elements] == ["Answer"]


async def test_action_returns_the_page_after_it_settles() -> None:
    """ADR-0130 decision 6: act returns the settled page, not the first repaint."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        page = await runtime.navigate(site.url("/exercise"))
        checked = await runtime.act(_click(page, "Check"))

    assert "Correct!" in checked.text
    assert "Checking" not in checked.text
    assert checked.revision != page.revision


async def test_act_can_follow_act_on_the_returned_revision() -> None:
    """The returned revision and refs are live: no observe between two acts."""

    async with browsing(LESSON_ROUTES) as (site, runtime):
        page = await runtime.navigate(site.url("/exercise"))
        checked = await runtime.act(_click(page, "Check"))
        continued = await runtime.act(_click(checked, "Continue"))

    assert "Exercise 2" in continued.text


async def test_a_page_that_never_settles_is_observed_at_the_bound() -> None:
    async with browsing(LESSON_ROUTES) as (site, runtime):
        started = asyncio.get_running_loop().time()
        observation = await runtime.navigate(site.url("/restless"))
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 3
    assert [element.name for element in observation.elements] == ["Stay"]


async def test_a_control_removed_while_it_is_read_takes_no_slot_in_chromium() -> None:
    async with browsing(LESSON_ROUTES) as (site, runtime):
        observation = await runtime.navigate(site.url("/fleeting"))

    assert [element.name for element in observation.elements] == ["Stay"]


async def test_a_link_that_loads_a_new_document_settles_on_it() -> None:
    async with browsing(LESSON_ROUTES) as (site, runtime):
        page = await runtime.navigate(site.url("/one"))
        followed = await runtime.act(_click(page, "Next"))

    assert followed.url == site.url("/two")
    assert "Page two" in followed.text
    assert [element.name for element in followed.elements] == ["Onward"]


async def _framed_sign_in(request: Request) -> Response:
    """The page Chase serves: its whole sign-in form lives in a same-site frame."""
    del request
    return html("<h1>Sign in</h1><iframe id='logonbox' src='/logon-form' width='400'></iframe>")


async def _logon_form(request: Request) -> Response:
    del request
    return html("<form><input name='user'><input type='password' name='secret'></form>")


async def _framed_code(request: Request) -> Response:
    del request
    return html("<iframe src='/code-form' width='400'></iframe>")


async def _code_form(request: Request) -> Response:
    del request
    return html("<input autocomplete='one-time-code' name='code'>")


async def _sectioned_code(request: Request) -> Response:
    """`autocomplete` is a case-insensitive token list, not one exact value."""
    del request
    return html("<input autocomplete='section-login ONE-TIME-CODE' name='code'>")


async def _framed_sectioned_code(request: Request) -> Response:
    del request
    return html("<iframe src='/sectioned-code' width='400'></iframe>")


async def _hidden_frames(request: Request) -> Response:
    """Signed-in pages keep invisible frames, such as silent token refreshers."""
    del request
    return html(
        "<h1>Accounts</h1><iframe src='/logon-form' style='display:none'></iframe>"
        "<iframe src='/logon-form' width='0' height='0' style='border:0'></iframe>"
    )


async def _frame_field_hidden(request: Request) -> Response:
    del request
    return html("<input type='password' name='secret' style='display:none'>")


async def _frame_with_hidden_field(request: Request) -> Response:
    del request
    return html("<h1>Accounts</h1><iframe src='/frame-field-hidden' width='400'></iframe>")


async def _visible_inner_frame(request: Request) -> Response:
    del request
    return html("<iframe src='/logon-form' width='400' style='visibility:visible'></iframe>")


async def _hidden_outer_frame(request: Request) -> Response:
    """A frame reports its own page's visibility, so the hidden owner is one frame up."""
    del request
    return html(
        "<h1>Accounts</h1>"
        "<iframe src='/visible-inner-frame' width='400' style='visibility:hidden'></iframe>"
    )


async def _accounts(request: Request) -> Response:
    del request
    response = html("<h1>Accounts</h1>")
    response.set_cookie("session", "synthetic", secure=True, httponly=True)
    return response


FRAMED_SIGN_IN_ROUTES = [
    Route("/framed-sign-in", _framed_sign_in),
    Route("/logon-form", _logon_form),
    Route("/framed-code", _framed_code),
    Route("/code-form", _code_form),
    Route("/sectioned-code", _sectioned_code),
    Route("/framed-sectioned-code", _framed_sectioned_code),
    Route("/visible-inner-frame", _visible_inner_frame),
    Route("/hidden-outer-frame", _hidden_outer_frame),
    Route("/hidden-frames", _hidden_frames),
    Route("/frame-field-hidden", _frame_field_hidden),
    Route("/frame-with-hidden-field", _frame_with_hidden_field),
    Route("/accounts", _accounts),
]


async def test_a_remote_sign_in_through_a_frame_reaches_ready() -> None:
    """ADR-0288: Chase's password field is in a frame, so its sign-in could never finish."""

    async with browsing(FRAMED_SIGN_IN_ROUTES) as (site, runtime):
        await runtime.navigate(site.url("/framed-sign-in"))
        assert await runtime.authentication_status() is BrowserAuthenticationStatus.NEEDS_USER

        await runtime.interactive_event(
            BrowserInteractiveEvent(kind="text", text="synthetic-entry")
        )
        await runtime.navigate(site.url("/accounts"))

        assert await runtime.authentication_status() is BrowserAuthenticationStatus.READY


async def test_a_one_time_code_field_in_a_frame_still_needs_the_user() -> None:
    async with browsing(FRAMED_SIGN_IN_ROUTES) as (site, runtime):
        await runtime.navigate(site.url("/framed-code"))

        assert await runtime.authentication_status() is BrowserAuthenticationStatus.NEEDS_USER


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/sectioned-code", id="in-the-page"),
        pytest.param("/framed-sectioned-code", id="in-a-frame"),
    ],
)
async def test_a_one_time_code_token_among_others_is_a_challenge(path: str) -> None:
    async with browsing(FRAMED_SIGN_IN_ROUTES) as (site, runtime):
        await runtime.navigate(site.url(path))

        assert await runtime.authentication_status() is BrowserAuthenticationStatus.NEEDS_USER
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.check_automation_ready()

    assert refused.value.reason_code == "tool.browser.needs_user"


async def test_verification_sees_a_sign_in_form_in_a_frame() -> None:
    """Without this the signed-out control looks signed in and nothing can verify."""

    async with browsing(FRAMED_SIGN_IN_ROUTES) as (site, runtime):
        evidence = await runtime.load_page_evidence(site.url("/framed-sign-in"))

    assert evidence.challenge_visible


@pytest.mark.parametrize("operation", ["verify", "automate"])
async def test_visible_security_verification_is_a_challenge(operation: str) -> None:
    """The LinkedIn run returned a security interstitial as a successful observation."""

    async def challenge(request: Request) -> Response:
        del request
        return html(
            "<main><h1>site.test</h1><h2>Performing security verification</h2>"
            "<p>This website uses a security service to protect against malicious bots.</p>"
            "</main>"
        )

    async with browsing([Route("/", challenge)]) as (site, runtime):
        if operation == "verify":
            evidence = await runtime.load_page_evidence(site.url("/"))
            assert evidence.challenge_visible
        else:
            # Readiness must stop before an ordinary observation reaches the model.
            await runtime._current_page().goto(site.url("/"))
            with pytest.raises(BrowserProviderError) as refused:
                await runtime.check_automation_ready()
            assert refused.value.reason_code == "tool.browser.needs_user"


async def test_hidden_or_discussed_security_verification_is_not_a_challenge() -> None:
    async def article(request: Request) -> Response:
        del request
        return html(
            "<h1>How security checks work</h1>"
            "<p>The screen may say Performing security verification while it loads.</p>"
            "<h2 hidden>Performing security verification</h2>"
            "<a hidden href='/login'>Sign in as Example User</a>"
            "<p>Choose Sign in as Example User when prompted.</p>"
            "<a href='/account'>Signed in as Example User</a>"
        )

    async with browsing([Route("/", article)]) as (site, runtime):
        evidence = await runtime.load_page_evidence(site.url("/"))
        assert not evidence.challenge_visible
        await runtime.check_automation_ready()


@pytest.mark.parametrize("operation", ["verify", "automate"])
async def test_a_remembered_account_prompt_is_not_a_signed_in_page(operation: str) -> None:
    async def prompt(request: Request) -> Response:
        del request
        return html("<h1>Welcome</h1><a href='/login'>Icon label<br>Sign in as Example User</a>")

    async with browsing([Route("/", prompt)]) as (site, runtime):
        if operation == "verify":
            evidence = await runtime.load_page_evidence(site.url("/"))
            assert evidence.challenge_visible
        else:
            await runtime._current_page().goto(site.url("/"))
            with pytest.raises(BrowserProviderError) as refused:
                await runtime.check_automation_ready()
            assert refused.value.reason_code == "tool.browser.needs_user"


async def test_a_signed_out_page_that_draws_its_form_late_still_reads_as_signed_out() -> None:
    """Chase draws its framed form after its requests go quiet; the control must see it."""

    async def late(request: Request) -> Response:
        del request
        return html(
            "<p>Loading</p>",
            script="""
            setTimeout(() => { location.hash = '#/logon'; }, 1000);
            setTimeout(() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<input type="password">';
                document.body.appendChild(frame);
            }, 3000);
            """,
        )

    async with browsing([Route("/late", late)]) as (site, runtime):
        without_waiting = await runtime.load_page_evidence(site.url("/late"))
        waiting = await runtime.load_page_evidence(
            site.url("/late"), challenge_deadline=asyncio.get_running_loop().time() + 10
        )

    assert not without_waiting.challenge_visible
    assert waiting.challenge_visible


async def test_a_signed_out_page_that_moves_deeper_before_its_form_still_reads_as_signed_out() -> (
    None
):
    """A move inside the confirmed page is not leaving it, so the control keeps looking."""

    async def late(request: Request) -> Response:
        del request
        return html(
            "<p>Loading</p>",
            script="""
            setTimeout(() => history.pushState({}, '', '/late/sign-in'), 1000);
            setTimeout(() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<input type="password">';
                document.body.appendChild(frame);
            }, 3000);
            """,
        )

    async with browsing([Route("/late", late)]) as (site, runtime):
        evidence = await runtime.load_page_evidence(
            site.url("/late"), challenge_deadline=asyncio.get_running_loop().time() + 10
        )

    assert evidence.path == "/late/sign-in"
    assert evidence.challenge_visible


async def test_a_second_look_sees_a_form_the_load_finished_too_early_for() -> None:
    async def late(request: Request) -> Response:
        del request
        return html(
            "<p>Loading</p>",
            script="""
            setTimeout(() => {
                const frame = document.createElement('iframe');
                frame.srcdoc = '<input type="password">';
                document.body.appendChild(frame);
            }, 3000);
            """,
        )

    async def members(request: Request) -> Response:
        del request
        return html("<p>Your account</p>")

    async with browsing([Route("/late", late), Route("/members", members)]) as (site, runtime):
        loaded = await runtime.load_page_evidence(site.url("/late"))
        appeared = await runtime.look_again_by(
            site.url("/late"),
            observed_seconds=20,
            deadline=asyncio.get_running_loop().time() + 10,
        )
        await runtime.load_page_evidence(site.url("/members"))
        stayed_signed_in = await runtime.look_again_by(
            site.url("/members"),
            observed_seconds=2,
            deadline=asyncio.get_running_loop().time() + 10,
        )

    assert not loaded.challenge_visible
    assert appeared.challenge_visible
    assert not stayed_signed_in.challenge_visible
    assert stayed_signed_in.path == "/members"
    assert 2 <= stayed_signed_in.observed_seconds < 6


async def test_a_lease_stops_at_a_sign_in_form_in_a_frame() -> None:
    """A signed-out lease must pause for the owner instead of reading the sign-in page."""

    async with browsing(FRAMED_SIGN_IN_ROUTES) as (site, runtime):
        await runtime.navigate(site.url("/framed-sign-in"))

        with pytest.raises(BrowserProviderError) as refused:
            await runtime.check_automation_ready()

    assert refused.value.reason_code == "tool.browser.needs_user"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/hidden-frames", id="invisible-frames"),
        pytest.param("/frame-with-hidden-field", id="hidden-field-in-a-visible-frame"),
        pytest.param("/hidden-outer-frame", id="visible-frame-in-a-hidden-frame"),
    ],
)
async def test_an_invisible_sign_in_field_in_a_frame_is_not_a_challenge(path: str) -> None:
    """A signed-in page with such a frame must not read as signed out."""

    async with browsing(FRAMED_SIGN_IN_ROUTES) as (site, runtime):
        evidence = await runtime.load_page_evidence(site.url(path))
        await runtime.check_automation_ready()
        status = await runtime.authentication_status()

    assert not evidence.challenge_visible
    assert status is BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED


async def test_navigation_progress_is_visible_while_chromium_waits_for_the_page() -> None:
    """Prove real Chromium reports navigation before a blocked page response completes."""
    from agent_core.domain.browser_diagnostics import BrowserProgress, collect_browser_diagnostics

    requested = asyncio.Event()
    release = asyncio.Event()
    updates: list[BrowserProgress] = []

    async def page(_request: Request) -> HTMLResponse:
        requested.set()
        await release.wait()
        return html("<h1>Ready</h1>")

    async with browsing([Route("/", page)]) as (site, runtime):
        with collect_browser_diagnostics(
            clock=asyncio.get_running_loop().time, on_progress=updates.append
        ):
            task = asyncio.create_task(runtime.navigate(site.origin + "/"))
            try:
                async with asyncio.timeout(5):
                    await requested.wait()
                assert not task.done()
                assert any(update.phase == "navigation" for update in updates)
                assert site.origin not in str(updates)
            finally:
                release.set()
                observation = await task
        assert "Ready" in observation.text
