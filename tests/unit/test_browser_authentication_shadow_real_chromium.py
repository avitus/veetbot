"""Open-shadow challenges use the existing human-only authentication interruption."""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.adapters.browser.page_authentication import AUTHENTICATION_SCRIPT
from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserProviderError
from tests.unit.test_browser_runtime_real_chromium import browsing, html


@pytest.mark.parametrize(
    "markup",
    [
        "<button><span hidden>Sign in as Alice</span>Continue</button>",
        "<h2><span contenteditable>Performing security verification</span>Welcome</h2>",
        '<div style="opacity:0"><h2>Performing security verification</h2></div>',
        '<button style="opacity:0">Sign in as Alice</button>',
    ],
)
async def test_hidden_or_editable_named_challenge_text_is_not_evidence(markup: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(markup)

    async with browsing([Route("/", home)]) as (site, runtime):
        page = runtime._current_page()
        await page.goto(site.url("/"))
        assert not await page.evaluate(AUTHENTICATION_SCRIPT)
        assert not await runtime._sign_in_challenge_visible(page)
        assert not runtime._security_challenge_seen


@pytest.mark.parametrize(
    "challenge",
    [
        '<input type="password" value="SECRET_CANARY">',
        '<input autocomplete="one-time-code" value="SECRET_CANARY">',
        '<iframe title="CAPTCHA"></iframe>',
        '<div id="captcha">Verify</div>',
        "<form>Use a passkey</form>",
        '<div role="dialog">Verification code</div>',
        "<button>Two-factor verification</button>",
        '<div role="form">Consent required</div>',
        "Use a passkey",
    ],
)
async def test_shadow_challenge_interrupts_before_dispatch_and_discards_references(
    challenge: str,
) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<form><button type="button" '
            "onclick=\"document.body.dataset.clicked='yes'\">Continue</button></form>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        assert runtime._elements and runtime._regions
        hosted = HostedPlaywrightSessionRuntime(
            tenant_id="test",
            runtime=runtime,
            proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        )
        assert runtime._page is not None
        await runtime._page.evaluate(
            """challenge => {
                const host = document.createElement('div'); document.body.append(host);
                host.setAttribute('role', 'form');
                const outer = host.attachShadow({mode:'open'});
                outer.innerHTML = '<div></div>';
                outer.firstElementChild.attachShadow({mode:'open'}).innerHTML = challenge;
            }""",
            challenge,
        )
        with pytest.raises(BrowserProviderError, match="needs_user"):
            await hosted.act(
                BrowserAction(
                    kind=BrowserActionKind.CLICK,
                    expected_revision=initial.revision,
                    ref=initial.elements[0].ref,
                )
            )
        assert await runtime._page.evaluate("document.body.dataset.clicked") is None
        assert not runtime._elements and not runtime._regions
        assert runtime.facts(initial.revision) is None
        with pytest.raises(BrowserProviderError, match="needs_user"):
            await hosted.observe()


@pytest.mark.parametrize("slotted", [False, True])
@pytest.mark.parametrize("rule", ["hidden", 'aria-hidden="true"', 'style="opacity:0"'])
async def test_excluded_composed_ancestors_do_not_trigger_sign_in(rule: str, slotted: bool) -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<button>Continue</button><div id="host"></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """({rule, slotted}) => {
                const host = document.querySelector('#host');
                const shadow = host.attachShadow({mode:'open'});
                if (slotted) {
                    shadow.innerHTML = '<div ' + rule + '><slot></slot></div>';
                    host.innerHTML = '<form><input type="password">Use a passkey</form>';
                } else {
                    shadow.innerHTML = '<div ' + rule + '></div>';
                    shadow.firstElementChild.attachShadow({mode:'open'}).innerHTML =
                        '<form><input type="password">Use a passkey</form>';
                }
            }""",
            {"rule": rule, "slotted": slotted},
        )
        await runtime.check_automation_ready()
        assert runtime.facts(initial.revision) is not None


@pytest.mark.parametrize("credential", [False, True])
async def test_shadow_passive_badge_exception_never_hides_a_credential(credential: bool) -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<button>Continue</button><div class="grecaptcha-badge" id="badge"></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """credential => {
                const root = document.querySelector('#badge').attachShadow({mode:'open'});
                root.innerHTML = credential ? '<input type="password">'
                    : '<div class="captcha-widget"><iframe title="CAPTCHA"></iframe></div>';
                if (credential) Object.defineProperty(root.firstElementChild, 'value', {
                    get() { throw new Error('Credential value must not be read'); }
                });
            }""",
            credential,
        )
        if credential:
            with pytest.raises(BrowserProviderError, match="needs_user"):
                await runtime.check_automation_ready()
        else:
            await runtime.check_automation_ready()


@pytest.mark.parametrize("editor", ["contenteditable", 'role="textbox"'])
async def test_editable_shadow_prompt_values_are_not_read(editor: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<div id="host"></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """editor => {
                const root = document.querySelector('#host').attachShadow({mode:'open'});
                root.innerHTML = '<form><div ' + editor + '>Use a passkey</div></form>';
                Object.defineProperty(root.querySelector('div').firstChild, 'nodeValue', {
                    get() { throw new Error('Editable prompt value must not be read'); }
                });
            }""",
            editor,
        )
        await runtime.check_automation_ready()


@pytest.mark.parametrize("case", ["closed", "outside_prompt", "scan_bound", "long_main"])
async def test_bounded_negative_detection_does_not_claim_positive_authentication(case: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<div id="host"></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """kind => {
                const root = document.querySelector('#host').attachShadow({
                    mode: kind === 'closed' ? 'closed' : 'open'
                });
                if (kind === 'long_main') {
                    document.body.innerHTML = '<p>x</p>'.repeat(9000) + '<input type="password">';
                    return;
                }
                root.innerHTML = kind === 'outside_prompt' ? '<p>Use a passkey</p>'
                    : (kind === 'scan_bound' ? '<span></span>'.repeat(9000) : '')
                        + '<input type="password">';
            }""",
            case,
        )
        assert await runtime._page.evaluate(AUTHENTICATION_SCRIPT) is False
        if case in {"scan_bound", "long_main"}:
            with pytest.raises(BrowserProviderError, match="needs_user"):
                await runtime.check_automation_ready()


async def test_shadow_challenge_after_dispatch_returns_no_evidence_and_one_effect() -> None:
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="send">Continue</button><div id="host"></div>',
            script="""
            document.querySelector('#send').onclick = async () => {
                await fetch('/effect', {method:'POST'});
                document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
                    '<form>Verification code<input autocomplete="one-time-code"'
                    + ' value="SECRET_CANARY"></form>';
            };
            """,
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing([Route("/", home), Route("/effect", effect, methods=["POST"])]) as (
        site,
        runtime,
    ):
        initial = await runtime.navigate(site.url("/"))
        hosted = HostedPlaywrightSessionRuntime(
            tenant_id="test",
            runtime=runtime,
            proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        )
        result = await hosted.act(
            BrowserAction(
                kind=BrowserActionKind.CLICK,
                ref=initial.elements[0].ref,
                expected_revision=initial.revision,
            )
        )
        assert result.interruption == "needs_user"
        assert not result.text and not result.elements and not result.regions
        assert "SECRET_CANARY" not in result.model_dump_json()
        assert not runtime._elements and runtime.facts(result.revision) is None
        with pytest.raises(BrowserProviderError, match="needs_user"):
            await hosted.observe()
        assert effects == [True]


async def test_visible_slotted_prompt_uses_composed_form_context() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="host"><span slot="login">Use a passkey</span></div>',
            script="""
            document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
                '<form><slot name="login"></slot></form>';
            """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        with pytest.raises(BrowserProviderError, match="needs_user"):
            await runtime.check_automation_ready()
