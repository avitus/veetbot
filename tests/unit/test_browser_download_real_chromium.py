"""A real Chromium saves a file only for an individually approved click (ADR-0294)."""

import asyncio
import gzip
import time

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.adapters.browser.playwright import DOWNLOAD_START_SECONDS
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


async def test_any_act_a_grant_authorized_reports_the_file_it_cancelled() -> None:
    """A task grant also covers ticking a box, and a page may save a file on that."""

    async def lesson(request: Request) -> Response:
        del request
        return html(
            '<label><input id="go" type="checkbox">Show details</label>',
            script="""
            document.querySelector('#go').onchange = () => {
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
        after = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CHECK,
                expected_revision=before.revision,
                ref=before.elements[0].ref,
            ),
            constraint=lesson_constraint(origins=(site.origin,)),
            now=GRANT_NOW,
        )

    assert after.download is not None
    assert (after.download.status, after.download.reason) == ("refused", "needs_approval")


async def test_a_grant_authorized_click_reports_a_file_the_page_starts_late() -> None:
    """The model must learn the file was cancelled, not read the click as doing nothing.

    A grant never reaches a control named download or export; a CSV button it can.
    """

    async def lesson(request: Request) -> Response:
        del request
        return html(
            '<button id="go">Get CSV</button>',
            script="""
            document.querySelector('#go').onclick = () => setTimeout(() => {
                const link = document.createElement('a');
                link.href = URL.createObjectURL(new Blob(['secret'], {type: 'text/csv'}));
                link.download = 'Activity.csv';
                document.body.appendChild(link);
                link.click();
            }, 3000);
            """,
        )

    async with browsing([Route("/lesson", lesson)]) as (site, runtime):
        before = await runtime.navigate(site.url("/lesson"))
        after = await runtime.act(
            _click(before.revision, before.elements[0].ref),
            constraint=lesson_constraint(origins=(site.origin,)),
            now=GRANT_NOW,
        )

    assert after.download is not None
    assert (after.download.status, after.download.reason) == ("refused", "needs_approval")
    assert after.download.data_base64 is None


async def test_only_a_click_or_key_press_waits_for_a_file_a_control_names() -> None:
    """Ticking a box called CSV details is not asking for a file."""

    async def lesson(request: Request) -> Response:
        del request
        return html('<label><input id="go" type="checkbox">CSV details</label>')

    async with browsing([Route("/lesson", lesson)]) as (site, runtime):
        before = await runtime.navigate(site.url("/lesson"))
        started = time.monotonic()
        after = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CHECK,
                expected_revision=before.revision,
                ref=before.elements[0].ref,
            ),
            constraint=lesson_constraint(origins=(site.origin,)),
            now=GRANT_NOW,
        )

    assert time.monotonic() - started < DOWNLOAD_START_SECONDS
    assert after.download is None


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


@pytest.mark.parametrize("compressed", [False, True])
async def test_an_approved_csv_export_can_read_the_sites_fetch_without_a_native_download(
    compressed: bool,
) -> None:
    """A dashboard fetches a CSV on click but never creates a Chromium download."""

    async def home(request: Request) -> Response:
        return html(
            '<button id="go">Download</button>',
            script="""
            document.querySelector('#go').onclick = () => fetch('/activity.csv');
        """,
        )

    async def file(request: Request) -> Response:
        return Response(
            gzip.compress(CSV) if compressed else CSV,
            media_type="text/csv",
            headers={"Content-Encoding": "gzip"} if compressed else {},
        )

    async with browsing([Route("/", home), Route("/activity.csv", file)], headed=True) as (
        site,
        runtime,
    ):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))
    assert after.download is not None and after.download.status == "saved"
    assert after.download.text() == CSV.decode()


async def test_a_csv_fetch_on_an_ordinary_approved_click_is_not_an_export() -> None:
    async def home(request: Request) -> Response:
        return html(
            '<button id="go">Next</button>',
            script="""
            document.querySelector('#go').onclick = () => fetch('/activity.csv');
        """,
        )

    async def file(request: Request) -> Response:
        return Response(CSV, media_type="text/csv")

    async with browsing([Route("/", home), Route("/activity.csv", file)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))
    assert after.download is None


async def test_a_compressed_csv_over_the_decoded_byte_limit_is_never_saved() -> None:
    async def home(request: Request) -> Response:
        return html(
            '<button id="go">Download</button>', script="go.onclick = () => fetch('/large')"
        )

    async def file(request: Request) -> Response:
        return Response(
            gzip.compress(b"x" * (MAX_BROWSER_DOWNLOAD_BYTES + 1)),
            media_type="text/csv",
            headers={"Content-Encoding": "gzip"},
        )

    async with browsing([Route("/", home), Route("/large", file)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))
    assert after.download is not None and after.download.reason == "too_large"
    assert after.download.data_base64 is None


async def test_a_child_frames_csv_is_not_the_approved_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.browser import playwright as adapter

    monkeypatch.setattr(adapter, "DOWNLOAD_ACT_BUDGET_SECONDS", 2.0)
    fetched = asyncio.Event()

    async def home(request: Request) -> Response:
        return html(
            '<button id="go">Download</button><iframe src="/frame"></iframe>',
            script="go.onclick = () => frames[0].postMessage('fetch', '*')",
        )

    async def frame(request: Request) -> Response:
        return html("", script="onmessage = () => fetch('/foreign.csv')")

    async def file(request: Request) -> Response:
        fetched.set()
        return Response(CSV, media_type="text/csv")

    async with browsing(
        [Route("/", home), Route("/frame", frame), Route("/foreign.csv", file)]
    ) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))
    assert fetched.is_set()
    assert after.download is not None and after.download.reason == "timed_out"


async def test_an_export_whose_observer_cannot_start_says_so_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A local capture failure is told apart from a site that sent no file."""
    from playwright.async_api import Error as PlaywrightError

    from agent_core.adapters.browser import playwright as adapter
    from agent_core.adapters.browser.csv_export import CsvExportObserver

    monkeypatch.setattr(adapter, "DOWNLOAD_ACT_BUDGET_SECONDS", 2.0)

    async def refuse(self: object) -> None:
        raise PlaywrightError("Target page, context or browser has been closed")

    monkeypatch.setattr(CsvExportObserver, "start", refuse)

    async def home(request: Request) -> Response:
        return html('<button id="go">Download</button>', script="go.onclick = () => 0")

    caplog.set_level("WARNING", logger=adapter.__name__)
    async with browsing([Route("/", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        after = await runtime.act(_click(before.revision, before.elements[0].ref))
    assert after.download is not None and after.download.reason == "timed_out"
    records = [r for r in caplog.records if r.name == adapter.__name__]
    assert [r.getMessage() for r in records] == ["browser.csv_export.observer_unavailable"]
    assert "closed" not in caplog.text
