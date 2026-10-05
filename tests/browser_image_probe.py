"""ADR-0146's measurement, run inside the built browser-profile service image.

``tests/security/test_browser_image_traffic.py`` starts this module in a
container from the image, under the production limits (ADR-0152). It drives
the image's own runtime headed on its own display, through the shared
harness's relay, which serves one synthetic site and refuses and records every
other request. It prints what the browser asked for as one JSON line.

The image has no test dependencies, so nothing here imports pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserObservation
from tests.real_browser_support import RealBrowserRuntime, local_https_site

# ADR-0146 measured for 7.4 minutes; every request it found began within three
# seconds of the launch or as a text field took focus. ADR-0145 measured with
# a refusing relay for two minutes.
IDLE_SECONDS = 120
# Whether a typed note fetched the dictionary varied between batches of runs,
# so the probe types one again after the idle and waits this long.
SETTLE_SECONDS = 10
# A sign-in form and an address form, whose fields the autofill query described.
_FORMS = (
    '<!doctype html><html lang="en"><head><title>Sign in</title></head><body>'
    '<form method="post" action="/">'
    '<input name="email" type="email" autocomplete="username" aria-label="Email">'
    '<input name="password" type="password" aria-label="Password">'
    "<button>Sign in</button></form>"
    '<form><input name="street" autocomplete="street-address" aria-label="Street address">'
    '<input name="city" autocomplete="address-level2" aria-label="City">'
    '<input name="card" autocomplete="cc-number" aria-label="Card number"></form>'
    "</body></html>"
)
# A lone text field. Chromium fetched the spelling dictionary for one, after a
# click into it and typing, but not for the fields of the page above
# (measured 2026-10-05).
_NOTE = (
    '<!doctype html><html lang="en"><head><title>Note</title></head><body>'
    '<input name="note" aria-label="Note"></body></html>'
)


def _action(
    observation: BrowserObservation, name: str, kind: BrowserActionKind, value: str | None = None
) -> BrowserAction:
    element = next(element for element in observation.elements if element.name == name)
    return BrowserAction(
        kind=kind, expected_revision=observation.revision, ref=element.ref, value=value
    )


async def measure(idle_seconds: float) -> dict[str, object]:
    """Type into the forms, submit, type a note, idle, type another; report requests."""
    page_loads: list[str] = []
    user_agents: set[str] = set()

    async def serve(request: Request) -> Response:
        page_loads.append(f"{request.method} {request.url.path}")
        user_agents.add(request.headers.get("user-agent", ""))
        return HTMLResponse(_NOTE if request.url.path == "/note" else _FORMS)

    app = Starlette(routes=[Route("/", serve, methods=["GET", "POST"]), Route("/note", serve)])
    async with local_https_site(app) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,), headed=True)
        try:
            page = await runtime.navigate(site.url("/"))
            for name, value in (("Email", "owner@example.test"), ("City", "Springfield")):
                page = await _click_and_type(runtime, page, name, value)
            await runtime.act(_action(page, "Sign in", BrowserActionKind.CLICK))
            note = await runtime.navigate(site.url("/note"))
            await _click_and_type(runtime, note, "Note", "Leave it by the door")
            await asyncio.sleep(idle_seconds)
            note = await runtime.navigate(site.url("/note"))
            await _click_and_type(runtime, note, "Note", "Ring the bell twice")
            await asyncio.sleep(SETTLE_SECONDS)
        finally:
            await runtime.close()
        return {
            "tunnelled": sorted(set(site.relay.tunnelled)),
            "refused": dict(sorted(Counter(site.relay.refused).items())),
            "page_loads": page_loads,
            "user_agents": sorted(user_agents),
        }


async def _click_and_type(
    runtime: RealBrowserRuntime, page: BrowserObservation, name: str, value: str
) -> BrowserObservation:
    # Typing alone, with no click into the field first, fetched no dictionary.
    page = await runtime.act(_action(page, name, BrowserActionKind.CLICK))
    return await runtime.act(_action(page, name, BrowserActionKind.TYPE, value))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(measure(arguments.idle_seconds)), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
