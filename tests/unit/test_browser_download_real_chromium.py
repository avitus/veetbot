"""A real Chromium saves a file only for an individually approved click (ADR-0294)."""

import asyncio

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import BrowserAction, BrowserActionKind
from agent_core.domain.browser_download import MAX_BROWSER_DOWNLOAD_BYTES
from tests.unit.test_browser_playwright import GRANT_NOW, lesson_constraint
from tests.unit.test_browser_runtime_real_chromium import browsing, html

CSV = b"Card,Transaction Date,Description,Amount\n1,01/02/2026,STREAMING CO,-15.49\n"


def _attachment(data: bytes, name: str) -> Response:
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _link_page(name: str = "Download") -> Response:
    return html(f'<a href="/file">{name}</a>')


def _click(revision: str, ref: str) -> BrowserAction:
    return BrowserAction(kind=BrowserActionKind.CLICK, expected_revision=revision, ref=ref)


async def test_an_approved_click_on_an_attachment_link_returns_the_file() -> None:
    async def home(request: Request) -> Response:
        del request
        return _link_page()

    async def file(request: Request) -> Response:
        del request
        return _attachment(CSV, "Activity.CSV")

    async with browsing([Route("/", home), Route("/file", file)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))

    assert after.download is not None
    assert after.download.status == "saved"
    assert after.download.filename == "Activity.CSV"
    assert after.download.media_type == "text/csv"
    assert after.download.text() == CSV.decode()


async def test_a_page_that_builds_its_file_slowly_still_reaches_the_approved_click() -> None:
    """A banking page fetches the data, then saves it from a script-made blob."""

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="go">Download</button>',
            script="""
            document.querySelector('#go').onclick = async () => {
                const body = await (await fetch('/data')).text();
                await new Promise(resolve => setTimeout(resolve, 3000));
                const link = document.createElement('a');
                link.href = URL.createObjectURL(new Blob([body], {type: 'text/csv'}));
                link.download = 'Activity.csv';
                document.body.appendChild(link);
                link.click();
            };
            """,
        )

    async def data(request: Request) -> Response:
        del request
        return Response(CSV, media_type="text/plain")

    async with browsing([Route("/", home), Route("/data", data)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))

    assert after.download is not None and after.download.status == "saved"
    assert after.download.text() == CSV.decode()


async def test_a_click_a_grant_authorized_never_saves_a_file() -> None:
    async def lesson(request: Request) -> Response:
        del request
        return html(
            '<button id="go">Continue</button>',
            script="""
            document.querySelector('#go').onclick = () => {
                const link = document.createElement('a');
                link.href = URL.createObjectURL(new Blob(['secret'], {type: 'text/csv'}));
                link.download = 'Activity.csv';
                document.body.appendChild(link);
                link.click();
            };
            """,
        )

    async with browsing([Route("/lesson", lesson)]) as (site, runtime):
        before = await runtime.navigate(site.url("/lesson"))
        constraint = lesson_constraint(origins=(site.origin,))
        after = await runtime.act(
            _click(before.revision, before.elements[0].ref), constraint=constraint, now=GRANT_NOW
        )

    assert after.download is not None
    assert after.download.status == "refused"
    assert after.download.reason == "needs_approval"
    assert after.download.data_base64 is None


async def test_a_binary_file_is_refused_and_named_as_unsupported() -> None:
    async def home(request: Request) -> Response:
        del request
        return _link_page()

    async def file(request: Request) -> Response:
        del request
        return _attachment(b"%PDF-1.7\x00\x01binary", "statement.pdf")

    async with browsing([Route("/", home), Route("/file", file)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))

    assert after.download is not None
    assert after.download.status == "refused"
    assert after.download.reason == "unsupported_type"


async def test_a_file_over_the_limit_is_refused_while_it_downloads() -> None:
    async def home(request: Request) -> Response:
        del request
        return _link_page()

    async def file(request: Request) -> Response:
        del request
        return _attachment(b"a,b\n" * (MAX_BROWSER_DOWNLOAD_BYTES // 2), "huge.csv")

    async with browsing([Route("/", home), Route("/file", file)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))

    assert after.download is not None
    assert after.download.status == "refused"
    assert after.download.reason == "too_large"


async def test_a_file_a_page_starts_on_its_own_is_cancelled_and_never_held() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="go">Next</button>',
            script="""
            const link = document.createElement('a');
            link.href = URL.createObjectURL(new Blob(['pushed'], {type: 'text/csv'}));
            link.download = 'pushed.csv';
            document.body.appendChild(link);
            link.click();
            """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        await asyncio.sleep(0.5)
        after = await runtime.act(_click(before.revision, before.elements[0].ref))

    assert before.download is None
    assert after.download is None


async def test_an_approved_click_without_a_file_reports_nothing() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<button id="go">Next</button>')

    async with browsing([Route("/", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))

    assert after.download is None
