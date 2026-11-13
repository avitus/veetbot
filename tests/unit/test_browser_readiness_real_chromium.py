"""Readiness and content-free phase evidence on real polling pages."""

import asyncio
from time import monotonic

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.adapters.browser import playwright as playwright_adapter
from agent_core.adapters.browser.page_readiness import DOM_QUIET_SCRIPT
from agent_core.domain.browser import BrowserAction, BrowserActionKind
from agent_core.domain.browser_diagnostics import collect_browser_diagnostics
from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_covered_check_is_refused_without_an_effect_and_can_be_observed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A covered check must leave time for recovery instead of an uncertain timeout.

    A radio takes the same check path as a checkbox. The click bound's value is
    pinned by ``test_browser_check_dispatch``; here a shorter one only makes the
    covered check fail sooner.
    """
    from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserProviderError

    monkeypatch.setattr(playwright_adapter, "CLICK_TIMEOUT_MILLISECONDS", 1_500)
    input_type = "checkbox"
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            f'<label><input type="{input_type}" id="choice"> Include archived</label>'
            '<div id="overlay" style="position:fixed;inset:0;z-index:9">Loading</div>',
            script="document.querySelector('#choice').onchange = "
            "() => fetch('/effect', {method:'POST'});",
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with browsing([Route("/", home), Route("/effect", effect, methods=["POST"])]) as (
        site,
        runtime,
    ):
        first = await runtime.navigate(site.url("/"))
        target = next(element for element in first.elements if element.role == input_type)
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.CHECK, ref=target.ref, expected_revision=first.revision
                )
            )
        assert refused.value.reason_code == "tool.browser.element_not_found"
        assert not refused.value.retryable
        assert effects == []
        assert runtime._page is not None
        assert not await runtime._page.locator("#choice").is_checked()
        await runtime._page.locator("#overlay").evaluate("node => node.remove()")
        fresh = await runtime.observe()
        target = next(element for element in fresh.elements if element.role == input_type)
        checked = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CHECK, ref=target.ref, expected_revision=fresh.revision
            )
        )
        target = next(element for element in checked.elements if element.role == input_type)
        assert target.checked
        repeated = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CHECK, ref=target.ref, expected_revision=checked.revision
            )
        )
        assert next(element for element in repeated.elements if element.role == input_type).checked
        assert effects == [True]


async def test_background_polling_does_not_consume_the_whole_readiness_budget() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("<button>Ready</button>", script="setInterval(() => fetch('/poll'), 70)")

    async def poll(request: Request) -> Response:
        del request
        return Response("ok")

    async with browsing([Route("/", home), Route("/poll", poll)]) as (site, runtime):
        with collect_browser_diagnostics(clock=asyncio.get_running_loop().time) as collector:
            started = monotonic()
            observation = await runtime.navigate(site.url("/"))
            elapsed = monotonic() - started
        assert elapsed < 1.8, "background network activity exhausted the two-second settle budget"
        assert observation.model_dump().get("readiness") == "dom_quiet"
        assert [phase.phase for phase in collector.snapshot().phases] == [
            "readiness",
            "observation",
            "navigation",
        ]


async def test_continuously_changing_dom_reports_expired_readiness_bound() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<button>Changing</button>",
            script="""
            setInterval(() => {
                document.querySelector('button').textContent = String(Date.now());
            }, 50);
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        with collect_browser_diagnostics(clock=asyncio.get_running_loop().time) as collector:
            observation = await runtime.navigate(site.url("/"))
        assert observation.model_dump().get("readiness") == "bound_expired"
        readiness = next(p for p in collector.snapshot().phases if p.phase == "readiness")
        assert readiness.outcome == "bound_expired"


@pytest.mark.parametrize("shape", ["existing", "attached", "nested"])
async def test_changing_open_shadow_dom_cannot_report_quiet(shape: str) -> None:
    """Verify changing open shadow dom cannot report quiet."""

    async def home(request: Request) -> Response:
        """Serve the synthetic page used to observe browser behavior."""
        del request
        return html(
            '<div id="host"></div>',
            script="""
            function start() {
                let root = document.querySelector('#host').attachShadow({mode:'open'});
                if (NESTED) {
                    root.innerHTML = '<div></div>';
                    root = root.firstElementChild.attachShadow({mode:'open'});
                }
                root.innerHTML = '<button>Changing</button>';
                setInterval(() => {
                    root.querySelector('button').textContent = String(Date.now());
                }, 50);
            }
            setTimeout(start, DELAY);
            """.replace("NESTED", "true" if shape == "nested" else "false").replace(
                "DELAY", "250" if shape == "attached" else "0"
            ),
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        with collect_browser_diagnostics(clock=asyncio.get_running_loop().time) as collector:
            observed = await runtime.navigate(site.url("/"))
        assert observed.readiness == "bound_expired"
        assert next(p for p in collector.snapshot().phases if p.phase == "readiness").outcome == (
            "bound_expired"
        )


async def test_shadow_action_settles_after_final_update_without_repeating_effect() -> None:
    """Verify shadow action settles after final update without repeating effect."""
    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="send">Submit</button><div id="host"></div>',
            script="""
            document.querySelector('#send').onclick = () => {
                fetch('/effect', {method:'POST'});
                const root = document.querySelector('#host').attachShadow({mode:'open'});
                root.innerHTML = '<div role="status">Working</div>';
                let count = 0;
                const timer = setInterval(() => {
                    root.firstElementChild.textContent = ++count === 6 ? 'Saved' : String(count);
                    if (count === 6) clearInterval(timer);
                }, 100);
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
        result = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CLICK,
                ref=initial.elements[0].ref,
                expected_revision=initial.revision,
            )
        )
        assert result.readiness == "dom_quiet"
        assert any(region.text == "Saved" for region in result.regions)
        assert effects == [True]


@pytest.mark.parametrize("scope", ["closed", "frame", "detached"])
async def test_readiness_does_not_follow_out_of_scope_or_detached_roots(scope: str) -> None:
    """Verify readiness does not follow out of scope or detached roots."""

    async def home(request: Request) -> Response:
        """Serve the synthetic page used to observe browser behavior."""
        del request
        if scope == "frame":
            return html(
                '<button>Ready</button><iframe srcdoc="<p>Private frame</p>'
                "<script>setInterval(() => document.querySelector('p').textContent = "
                'String(Date.now()), 50)</script>"></iframe>'
            )
        return html(
            '<button>Ready</button><div id="host"></div>',
            script="""
            const host = document.querySelector('#host');
            const root = host.attachShadow({mode: MODE});
            root.innerHTML = '<p>Changing</p>';
            setInterval(() => root.firstElementChild.textContent = String(Date.now()), 50);
            if (DETACH) setTimeout(() => host.remove(), 250);
            """.replace("MODE", "'closed'" if scope == "closed" else "'open'").replace(
                "DETACH", "true" if scope == "detached" else "false"
            ),
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        result = await runtime.navigate(site.url("/"))
        assert result.readiness == "dom_quiet"
        assert result.text == "Ready"


@pytest.mark.parametrize(
    "case", ["quiet", "changing", "nodes", "roots", "root_limit", "total", "observer_error"]
)
async def test_readiness_limits_and_cleanup(case: str) -> None:
    """Verify readiness limits and cleanup."""

    async def home(request: Request) -> Response:
        del request
        return html('<div id="host"></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """kind => {
                const host = document.querySelector('#host');
                if (kind === 'nodes' || kind === 'total')
                    host.innerHTML = '<span></span>'.repeat(kind === 'nodes' ? 9000 : 6000);
                if (kind === 'roots' || kind === 'root_limit') {
                    for (let i = 0; i < (kind === 'roots' ? 65 : 64); i++) {
                        const child = document.createElement('div');
                        host.append(child); child.attachShadow({mode:'open'});
                    }
                }
                if (kind === 'changing' || kind === 'total')
                    setInterval(() => host.setAttribute('data-tick', String(Date.now())), 50);
                if (kind === 'observer_error') host.attachShadow({mode:'open'});
                const state = window.cleanup = {timers: new Set(), observers: []};
                const NativeObserver = window.MutationObserver;
                window.MutationObserver = class extends NativeObserver {
                    constructor(callback) { super(callback); state.observers.push(this); }
                    observe(...args) {
                        if (kind === 'observer_error' && args[0] instanceof ShadowRoot)
                            throw new Error('Subscription failed');
                        this.active = true; return super.observe(...args);
                    }
                    disconnect() { this.active = false; return super.disconnect(); }
                };
                const timeout = window.setTimeout, interval = window.setInterval;
                const clear = window.clearTimeout, clearRepeat = window.clearInterval;
                window.setTimeout = (callback, ms) => {
                    const id = timeout(() => { state.timers.delete(id); callback(); }, ms);
                    state.timers.add(id); return id;
                };
                window.setInterval = (callback, ms) => {
                    const id = interval(callback, ms); state.timers.add(id); return id;
                };
                window.clearTimeout = id => { state.timers.delete(id); clear(id); };
                window.clearInterval = id => { state.timers.delete(id); clearRepeat(id); };
            }""",
            case,
        )
        started = monotonic()
        quiet = await runtime._page.evaluate(
            DOM_QUIET_SCRIPT, [300, 5000 if case == "total" else 800]
        )
        assert quiet is (case in {"quiet", "root_limit"})
        assert monotonic() - started < 4, "discovery did not stop at its own resource bound"
        assert await runtime._page.evaluate(
            """() => ({timers: cleanup.timers.size,
                observers: cleanup.observers.filter(observer => observer.active).length})"""
        ) == {"timers": 0, "observers": 0}


async def test_delayed_action_postcondition_never_duplicates_the_server_effect() -> None:
    from agent_core.domain.browser import (
        BrowserAction,
        BrowserDispatchConstraint,
        BrowserObservation,
    )
    from agent_core.tools.browser_act import BrowserActTool
    from tests.contract.support import tool_context
    from tests.unit.test_browser_tools import FakeBrowserProvider

    effects = []

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="send">Submit</button>',
            script="""
            document.querySelector('#send').onclick = async () => {
                await fetch('/effect', {method: 'POST'});
                setTimeout(() => document.querySelector('#send').textContent = 'Finished', 850);
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

        class Provider(FakeBrowserProvider):
            def allows(self, url: str) -> bool:
                return url.startswith(site.origin + "/")

            async def observe(self) -> BrowserObservation:
                return await runtime.observe()

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                return await runtime.act(action, constraint=constraint)

        first = await runtime.navigate(site.url("/"))
        result = await BrowserActTool(Provider()).execute(
            {
                "kind": "click",
                "expected_revision": first.revision,
                "ref": first.elements[0].ref,
                "postcondition": {"role": "button", "name": "Finished", "timeout_ms": 2000},
            },
            tool_context(),
        )
        assert result.ok and result.structured is not None
        assert result.structured["condition"]["status"] == "satisfied"
        assert result.structured["condition"]["observations"] > 1
        assert effects == [True]


async def test_initial_data_finishes_before_dom_quiet_despite_later_polling() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<main>Loading</main>",
            script="""
            fetch('/data').then(r => r.text()).then(text => {
                document.querySelector('main').textContent = text;
            });
            setInterval(() => fetch('/poll'), 70);
            """,
        )

    async def data(request: Request) -> Response:
        del request
        await asyncio.sleep(0.6)
        return Response("Exercise loaded")

    async def poll(request: Request) -> Response:
        del request
        return Response("ok")

    async with browsing([Route("/", home), Route("/data", data), Route("/poll", poll)]) as (
        site,
        runtime,
    ):
        started = monotonic()
        observation = await runtime.navigate(site.url("/"))
        assert observation.text == "Exercise loaded"
        assert observation.readiness == "dom_quiet"
        assert monotonic() - started < 1.8
