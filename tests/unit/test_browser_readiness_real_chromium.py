"""Readiness and content-free phase evidence on real polling pages."""

import asyncio
from time import monotonic

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser_diagnostics import collect_browser_diagnostics
from tests.unit.test_browser_runtime_real_chromium import browsing, html


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
