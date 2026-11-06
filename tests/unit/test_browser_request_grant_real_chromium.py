"""A real Chromium honours a request grant's constraint (ADR-0301).

An act the owner allowed for the whole request may save a file and move to
another page of the login's site, whatever its path; the runtime still
refuses a control that names a payment on the live page.
"""

from datetime import timedelta
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserDispatchConstraint,
    BrowserProviderError,
)
from tests.unit.test_browser_download_real_chromium import CSV, _attachment
from tests.unit.test_browser_playwright import GRANT_NOW
from tests.unit.test_browser_runtime_real_chromium import browsing, html


def request_constraint(origin: str, **overrides: Any) -> BrowserDispatchConstraint:
    fields: dict[str, Any] = {
        "grant_kind": "request",
        "origins": (origin,),
        "path_prefix": None,
        "not_after": GRANT_NOW + timedelta(minutes=30),
        "consequence_ceiling": "unknown",
        "max_text_characters": 256,
    }
    fields.update(overrides)
    return BrowserDispatchConstraint.model_validate(fields)


def _click(revision: str, ref: str) -> BrowserAction:
    return BrowserAction(kind=BrowserActionKind.CLICK, expected_revision=revision, ref=ref)


async def test_a_request_authorized_click_saves_the_file() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<a href="/file">Download</a>')

    async def file(request: Request) -> Response:
        del request
        return _attachment(CSV, "Activity.CSV")

    async with browsing([Route("/web/auth/dashboard", home), Route("/file", file)]) as (
        site,
        runtime,
    ):
        before = await runtime.navigate(site.url("/web/auth/dashboard"))
        after = await runtime.act(
            _click(before.revision, before.elements[0].ref),
            constraint=request_constraint(site.origin),
            now=GRANT_NOW,
        )

    assert after.download is not None
    assert after.download.status == "saved"
    assert after.download.text() == CSV.decode()


async def test_a_request_authorized_link_may_open_another_authenticated_page() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<a href="/web/auth/statements">Statements and documents</a>')

    async def statements(request: Request) -> Response:
        del request
        return html("<h1>Statements</h1>")

    async with browsing(
        [Route("/web/auth/dashboard", home), Route("/web/auth/statements", statements)]
    ) as (site, runtime):
        before = await runtime.navigate(site.url("/web/auth/dashboard"))
        after = await runtime.act(
            _click(before.revision, before.elements[0].ref),
            constraint=request_constraint(site.origin),
            now=GRANT_NOW,
        )

    assert after.url.endswith("/web/auth/statements")


async def test_the_runtime_refuses_a_payment_control_under_a_request_grant() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<button id="pay">Make a payment</button>')

    async with browsing([Route("/web/auth/dashboard", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/web/auth/dashboard"))
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.act(
                _click(before.revision, before.elements[0].ref),
                constraint=request_constraint(site.origin),
                now=GRANT_NOW,
            )

    assert refused.value.reason_code == "tool.browser.grant_not_applicable"
