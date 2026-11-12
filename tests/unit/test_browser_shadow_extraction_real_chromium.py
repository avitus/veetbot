"""Bounded collection evidence from open-root web components."""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.adapters.browser.extraction import EXTRACTION_SCRIPT
from agent_core.tools.browser_observe import ArticleBrowserObserveTool, ShadowBrowserObserveTool
from tests.contract.support import tool_context
from tests.unit.test_browser_extraction import extraction_arguments
from tests.unit.test_browser_extraction_real_chromium import Provider
from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_open_shadow_collection_is_discovered_by_the_bounded_reader() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="host"></div>',
            script="document.querySelector('#host').attachShadow({mode:'open'}).innerHTML = "
            "'<table><tr><td>Receipt</td></tr></table>';",
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        request = extraction_arguments(before.revision)["extract"]
        request["scope"] = "main_document_and_open_shadow"
        result = await runtime._page.evaluate(EXTRACTION_SCRIPT, request)
        assert result["status"] == "extracted"
        assert result["rows"][0][0]["text"] == "Receipt"


@pytest.mark.parametrize("role", ["textbox", "searchbox", "spinbutton"])
async def test_form_metadata_does_not_descend_into_a_shadow_editor(role: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            f'<form><div role="{role}" aria-label="Editor" id="editor"></div></form>',
            script="document.querySelector('#editor').attachShadow({mode:'open'}).innerHTML = "
            "'<input aria-label=EDITOR_CANARY value=VALUE_CANARY>';",
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        result = await ShadowBrowserObserveTool(Provider(runtime, site.origin)).execute(
            extraction_arguments(before.revision, kind="form"), tool_context()
        )
        assert result.ok and result.structured is not None
        data = result.structured["extraction"]
        assert [row["cells"][0]["value"] for row in data["rows"]] == ["Editor"]
        assert "CANARY" not in str(data)


@pytest.mark.parametrize("kind", ["table", "list", "form"])
@pytest.mark.parametrize("own_root", [False, True])
async def test_nested_collections_and_cells_share_the_new_scope(kind: str, own_root: bool) -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<span id="label">WRONG_ROOT_CANARY</span><div id="host"></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """({kind, ownRoot}) => {
                const outer = document.querySelector('#host').attachShadow({mode:'open'});
                const collection = document.createElement('div');
                collection.setAttribute('role', kind); outer.append(collection);
                const rows = ownRoot ? collection.attachShadow({mode:'open'}) : collection;
                if (kind === 'form') {
                    rows.innerHTML = '<span id="label">Receipt</span>'
                        + '<input aria-labelledby="label" value="VALUE_CANARY">';
                } else {
                    rows.innerHTML = kind === 'table' ? '<div role="row"></div>'
                        : '<div role="listitem"></div>';
                    const rowRoot = rows.firstChild.attachShadow({mode:'open'});
                    rowRoot.innerHTML = kind === 'table'
                        ? '<div role="cell"></div>' : '<span></span>';
                    rowRoot.firstChild.attachShadow({mode:'open'}).innerHTML =
                        'Receipt<input value="VALUE_CANARY">'
                        + '<span hidden>HIDDEN_CANARY</span>';
                }
            }""",
            {"kind": kind, "ownRoot": own_root},
        )
        before = await runtime.observe()
        tool = ShadowBrowserObserveTool(Provider(runtime, site.origin))
        result = await tool.execute(
            extraction_arguments(before.revision, kind=kind), tool_context()
        )
        assert result.ok and result.structured is not None
        data = result.structured["extraction"]
        assert data["version"] == 2 and data["scope"] == "main_document_and_open_shadow"
        assert data["rows"][0]["cells"][0]["value"] == "Receipt"
        assert "CANARY" not in str(data)


@pytest.mark.parametrize(
    "rule",
    ["hidden", 'aria-hidden="true"', 'style="opacity:0"', "contenteditable", 'role="textbox"'],
)
@pytest.mark.parametrize("slotted", [False, True])
async def test_excluded_hosts_and_slot_ancestors_never_supply_collections(
    rule: str, slotted: bool
) -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<div id="host"></div><ul><li>Visible</li></ul>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            """({rule, slotted}) => {
                const host = document.querySelector('#host');
                const root = host.attachShadow({mode:'open'});
                root.innerHTML = '<div ' + rule + '></div>';
                if (slotted) {
                    root.firstChild.innerHTML = '<slot></slot>';
                    host.innerHTML = '<ul><li>SECRET_CANARY</li></ul>';
                } else root.firstChild.attachShadow({mode:'open'}).innerHTML =
                    '<ul><li>SECRET_CANARY</li></ul>';
            }""",
            {"rule": rule, "slotted": slotted},
        )
        before = await runtime.observe()
        result = await ShadowBrowserObserveTool(Provider(runtime, site.origin)).execute(
            extraction_arguments(before.revision, kind="list"), tool_context()
        )
        assert result.ok and result.structured is not None
        data = result.structured["extraction"]
        assert data["rows"][0]["cells"][0]["value"] == "Visible"
        assert "CANARY" not in str(data)


async def test_pinned_extraction_keeps_its_collection_index_and_closed_roots_stay_excluded() -> (
    None
):
    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="closed"></div><div id="open"></div><ul><li>Document</li></ul>',
            script="""
            document.querySelector('#closed').attachShadow({mode:'closed'}).innerHTML =
                '<ul><li>CLOSED_CANARY</li></ul>';
            document.querySelector('#open').attachShadow({mode:'open'}).innerHTML =
                '<ul><li>Shadow</li></ul>';
            """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        for cls, value, version in (
            (ArticleBrowserObserveTool, "Document", 1),
            (ShadowBrowserObserveTool, "Shadow", 2),
        ):
            before = await runtime.observe()
            result = await cls(Provider(runtime, site.origin)).execute(
                extraction_arguments(before.revision, kind="list"), tool_context()
            )
            assert result.ok and result.structured is not None
            data = result.structured["extraction"]
            assert data["version"] == version
            assert data["rows"][0]["cells"][0]["value"] == value
            assert "CANARY" not in str(data)


@pytest.mark.parametrize("case", ["source", "rows", "cell", "span", "nested", "slot"])
async def test_shadow_extraction_bounds_and_subtree_limits(case: str) -> None:
    markup = {
        "source": "<span></span>" * 9000 + "<ul><li>Unseen</li></ul>",
        "rows": "<ul>" + "<span></span>" * 5000 + "<li>Unseen</li></ul>",
        "cell": "<ul><li>" + "<span></span>" * 300 + "Unseen</li></ul>",
        "span": '<table><tr><td colspan="2">Merged</td></tr></table>',
        "nested": "<ul><li>Outer<ul><li>NESTED_CANARY</li></ul></li></ul>",
        "slot": "<ul><slot></slot></ul>",
    }[case]

    async def home(request: Request) -> Response:
        del request
        return html('<div id="host"><li>SLOTTED_CANARY</li></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        assert runtime._page is not None
        await runtime._page.evaluate(
            "markup => document.querySelector('#host').attachShadow({mode:'open'})"
            ".innerHTML = markup",
            markup,
        )
        before = await runtime.observe()
        result = await ShadowBrowserObserveTool(Provider(runtime, site.origin)).execute(
            extraction_arguments(before.revision, kind="table" if case == "span" else "list"),
            tool_context(),
        )
        assert result.ok and result.structured is not None
        data = result.structured["extraction"]
        assert "CANARY" not in str(data)
        if case == "source":
            assert data["status"] == "not_found" and data["source_scan_limit_reached"]
        elif case == "rows":
            assert not data["rows"] and data["row_scan_limit_reached"]
        elif case == "cell":
            assert data["rows"][0]["cells"][0]["status"] == "truncated"
        elif case == "span":
            assert data["status"] == "unsupported_structure" and not data["rows"]
        elif case == "nested":
            assert data["rows"][0]["cells"][0]["value"] == "Outer"
        else:
            assert not data["rows"]
