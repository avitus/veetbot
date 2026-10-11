"""Article evidence comes from bounded visible DOM reads, never site API data."""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser_extraction import BrowserExtractionField, BrowserExtractionRequest
from agent_core.tools.browser_observe import ArticleBrowserObserveTool
from tests.contract.support import tool_context
from tests.unit.test_browser_extraction_real_chromium import Provider
from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_article_extraction_pairs_visible_excerpt_with_its_timestamp_link() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("""<main><h1>Your Home Timeline</h1>
          <article><p>Tool testing is becoming a priority for agent teams.</p>
          <a href="/researcher/status/123"><time>2h</time></a></article>
          <article hidden>HIDDEN_CANARY</article>
          <article><input value="SECRET_CANARY"><p>Model serving costs are falling.</p>
          <a href="/engineer/status/456"><time>1h</time></a></article></main>""")

    async with browsing([Route("/home", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/home"))
        result = await ArticleBrowserObserveTool(Provider(runtime, site.origin)).execute(
            {
                "extract": {
                    "expected_revision": first.revision,
                    "kind": "articles",
                    "fields": [
                        {"name": "excerpt", "column": 0, "type": "string"},
                        {"name": "url", "column": 1, "type": "string"},
                    ],
                    "row_limit": 3,
                }
            },
            tool_context(),
        )
        assert result.ok, "visible article extraction is unavailable"
        assert result.structured is not None
        rows = result.structured["extraction"]["rows"]
        assert len(rows) == 2
        assert rows[0]["cells"][1]["value"] == site.url("/researcher/status/123")
        assert "Tool testing" in rows[0]["cells"][0]["value"]
        assert "CANARY" not in str(result.structured["extraction"])


@pytest.mark.parametrize(
    "content",
    [
        '<a href="https://elsewhere.test/author/status/1"><time>1h</time></a>',
        '<a href="/author/status/1?secret=CANARY"><time>1h</time></a>',
        '<a href="/author/status/1#CANARY"><time>1h</time></a>',
        '<a href="javascript:alert(1)"><time>1h</time></a>',
        '<a href="/author/status/1" hidden><time>1h</time></a>',
        '<div contenteditable><a href="/author/status/1"><time>1h</time></a></div>',
        '<a href="/author/status/1"><time>1h</time></a>'
        '<a href="/other/status/2"><time>2h</time></a>',
    ],
)
async def test_article_links_must_be_visible_unique_and_on_the_current_origin(content: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(f"<main><article><p>Visible excerpt</p>{content}</article></main>")

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        observed = await runtime.extract(
            BrowserExtractionRequest(
                expected_revision=first.revision,
                kind="articles",
                fields=[
                    BrowserExtractionField(name="excerpt", column=0, type="string"),
                    BrowserExtractionField(name="url", column=1, type="string"),
                ],
                row_limit=3,
            )
        )
        assert observed.extraction is not None
        assert len(observed.extraction.rows) == 1
        assert observed.extraction.rows[0].cells[1].status == "missing"
        assert not observed.extraction.rows[0].schema_valid


async def test_articles_exclude_offscreen_and_nested_posts_and_label_clipped_excerpts() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<main><article><p>" + "Long visible discussion. " * 20 + "</p>"
            '<a href="/author/status/1"><time>1h</time></a>'
            '<article>NESTED_CANARY<a href="/quoted/status/2"><time>2h</time></a></article>'
            '</article><div style="height:2000px"></div>'
            '<article>OFFSCREEN_CANARY<a href="/below/status/3"><time>3h</time></a>'
            "</article></main>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        observed = await runtime.extract(
            BrowserExtractionRequest(
                expected_revision=first.revision,
                kind="articles",
                fields=[
                    BrowserExtractionField(name="excerpt", column=0, type="string"),
                    BrowserExtractionField(name="url", column=1, type="string"),
                ],
                row_limit=3,
            )
        )
        extracted = observed.extraction
        assert extracted is not None and extracted.scope == "main_document_viewport_articles"
        assert len(extracted.rows) == 1
        assert extracted.rows[0].cells[0].status == "truncated"
        assert len(extracted.rows[0].cells[0].text) <= 256
        assert extracted.rows[0].cells[1].value == site.url("/author/status/1")
        assert "CANARY" not in str([row.model_dump() for row in extracted.rows])


async def test_incomplete_article_link_scan_cannot_claim_a_unique_permalink() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<main><article>Visible excerpt"
            '<a href="/author/status/1"><time>1h</time></a>'
            + "<span></span>" * 260
            + '<a href="/other/status/2"><time>2h</time></a></article></main>'
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        observed = await runtime.extract(
            BrowserExtractionRequest(
                expected_revision=first.revision,
                kind="articles",
                fields=[
                    BrowserExtractionField(name="url", column=1, type="string"),
                ],
                row_limit=3,
            )
        )
        assert observed.extraction is not None
        assert observed.extraction.rows[0].cells[0].status == "missing"


async def test_real_personal_feed_workflow_reads_scrolls_and_finishes_without_writes() -> None:
    import json

    from starlette.applications import Starlette

    from agent_core.domain.browser import (
        BrowserAction,
        BrowserDispatchConstraint,
        BrowserObservation,
    )
    from agent_core.domain.runs import RunStatus
    from tests.real_browser_support import (
        RealBrowserRuntime,
        local_https_site,
        require_real_browser,
    )
    from tests.unit.test_browser_composition import PROFILE_ID
    from tests.unit.test_browser_feed_workflow import INTENT, FeedProvider, feed_app

    require_real_browser()
    effects: list[bool] = []
    posts = "".join(
        f'<article style="height:260px"><p>Post {i}: '
        + (
            "Agent teams are improving tool reliability."
            if i % 2
            else "Inference serving costs are falling."
        )
        + f'</p><a href="/author/status/{100 + i}"><time>{i + 1}h</time></a>'
        "<button onclick=\"fetch('/effect', {method:'POST'})\">Like</button></article>"
        for i in range(16)
    )

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button aria-label="Account menu" style="position:fixed;bottom:5px">'
            "Fixture Owner @fixture</button><main><h1>Your Home Timeline</h1>"
            '<div id="posts">Loading</div></main>',
            script='setTimeout(()=>document.getElementById("posts").innerHTML='
            + json.dumps(posts)
            + ",1500);",
        )

    async def effect(request: Request) -> Response:
        del request
        effects.append(True)
        return Response("ok")

    async with local_https_site(
        Starlette(
            routes=[
                Route("/home", home),
                Route("/effect", effect, methods=["POST"]),
            ]
        ),
        host="x.com",
    ) as site:
        runtime = RealBrowserRuntime()
        await runtime.start(site.proxy_url, (site.origin,))

        class Provider(FeedProvider):
            async def navigate(self, url: str) -> BrowserObservation:
                self.navigations.append(url)
                return await runtime.navigate(url)

            async def observe(self) -> BrowserObservation:
                return await runtime.observe()

            async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
                self.reads += 1
                return await runtime.extract(request)

            async def act(
                self, action: BrowserAction, *, constraint: BrowserDispatchConstraint | None = None
            ) -> BrowserObservation:
                self.actions.append(action)
                return await runtime.act(action, constraint=constraint)

        provider = Provider()
        try:
            async with feed_app(provider, real_clock=True) as app:
                session = await app.services.sessions.create(
                    app.principal, "general", {}, browser_profile_id=PROFILE_ID
                )
                run_id = await app.runs.submit(INTENT, session.id)
                result = await app.runs.get(run_id)
                async with app.uow_factory() as uow:
                    checkpoint = await uow.checkpoints.latest(run_id)
                assert result.status is RunStatus.COMPLETED
                assert checkpoint is not None
                state = checkpoint.working_state["browser_workflow"]
                assert state["status"] == "verified", state.get("feed")
                assert len(state["feed"]["post_ids"]) >= 6
                assert 1 <= len(provider.actions) <= 3
                assert not await app.approvals.list_pending(run_id=run_id)
                assert effects == []
        finally:
            await runtime.close()
