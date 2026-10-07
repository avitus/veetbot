"""Visible collection extraction checked against synthetic page evidence."""

from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.browser import BrowserObservation
from agent_core.tools.browser_observe import BrowserObserveTool
from tests.contract.support import tool_context
from tests.unit.test_browser_extraction import extraction_arguments
from tests.unit.test_browser_runtime_real_chromium import browsing, html
from tests.unit.test_browser_tools import FakeBrowserProvider


class Provider(FakeBrowserProvider):
    def __init__(self, runtime: PythonPlaywrightRuntime, origin: str) -> None:
        super().__init__()
        self.runtime = runtime
        self.origin = origin

    def allows(self, url: str) -> bool:
        return url.startswith(self.origin + "/")

    async def extract(self, request: Any) -> BrowserObservation:
        method = self.runtime.extract
        result: BrowserObservation = await method(request)
        return result


async def test_table_extraction_validates_values_and_never_infers_missing_cells() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("""<table><tr><th>Item</th><th>Quantity</th></tr>
          <tr><td>Apples</td><td>3</td></tr><tr><td>Pears</td><td>several</td></tr>
          <tr><td>Oranges</td></tr><tr hidden><td>HIDDEN_CANARY</td><td>99</td></tr>
          <tr><td><input value="INPUT_CANARY">Visible only</td><td>4</td></tr>
          </table><button>Next</button>""")

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        arguments = extraction_arguments(first.revision)
        arguments["extract"]["fields"].append({"name": "quantity", "column": 1, "type": "integer"})
        result = await BrowserObserveTool(Provider(runtime, site.origin)).execute(
            arguments, tool_context()
        )
        assert result.ok, "typed table extraction is missing"
        assert result.structured is not None
        data = result.structured["extraction"]
        assert data["status"] == "extracted" and data["rows"][0]["schema_valid"] is False
        apple, pear, orange, visible = data["rows"][1:]
        assert apple["cells"][1]["value"] == 3 and apple["schema_valid"] is True
        assert pear["cells"][1]["status"] == "invalid"
        assert orange["cells"][1]["status"] == "missing"
        assert visible["cells"][0]["value"] == "Visible only"
        assert "CANARY" not in str(data)
        assert result.structured["revision"] != first.revision
        assert all(row["ref"].startswith(result.structured["revision"]) for row in data["rows"])


@pytest.mark.parametrize("kind", ["list", "form"])
async def test_lists_and_form_metadata_are_bounded_visible_reads(kind: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html("""<ul><li>One</li><li>Two</li><li hidden>HIDDEN_CANARY</li></ul>
          <form><label>Nickname<input value="VALUE_CANARY"></label>
          <label>Password<input type="password" value="PASSWORD_CANARY"></label>
          <label>Notes<textarea>TEXTAREA_CANARY</textarea></label>
          <label>Color<select><option>OPTION_CANARY</option></select></label>
          <label>Enabled<input type="checkbox" checked></label></form>""")

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        result = await BrowserObserveTool(Provider(runtime, site.origin)).execute(
            extraction_arguments(first.revision, kind=kind), tool_context()
        )
        assert result.ok, "typed list/form extraction is missing"
        assert result.structured is not None
        rows = result.structured["extraction"]["rows"]
        assert [row["cells"][0]["value"] for row in rows] == (
            ["One", "Two"]
            if kind == "list"
            else ["Nickname", "Password", "Notes", "Color", "Enabled"]
        )
        assert "CANARY" not in str(result.structured["extraction"])


async def test_extraction_rejects_stale_and_foreign_revisions_and_does_not_send_actions() -> None:
    from agent_core.domain.browser import (
        BrowserAction,
        BrowserActionKind,
        BrowserObservationExpansion,
        BrowserProviderError,
    )

    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            "<table><tr><td>One</td></tr></table><button>Write</button>",
            script=(
                "document.querySelector('button').onclick = () => fetch('/effect', {method:'POST'})"
            ),
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
        tool = BrowserObserveTool(Provider(runtime, site.origin))
        wrong = await tool.execute(extraction_arguments("foreign-revision"), tool_context())
        assert (
            not wrong.ok
            and wrong.failure is not None
            and wrong.failure.reason_code.endswith("page_changed")
        )
        result = await tool.execute(extraction_arguments(first.revision), tool_context())
        assert result.ok and result.structured is not None
        stale = await tool.execute(extraction_arguments(first.revision), tool_context())
        assert (
            not stale.ok
            and stale.failure is not None
            and stale.failure.reason_code.endswith("page_changed")
        )
        row_ref = result.structured["extraction"]["rows"][0]["ref"]
        with pytest.raises(BrowserProviderError, match="element_not_found"):
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.CLICK,
                    ref=row_ref,
                    expected_revision=result.structured["revision"],
                )
            )
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await runtime.expand(BrowserObservationExpansion(after=row_ref))
        assert effects == []


@pytest.mark.parametrize(
    "fixture", ["rows", "long_cell", "span", "source_bound", "row_bound", "nested"]
)
async def test_extraction_reports_bounds_and_unsupported_structure(fixture: str) -> None:
    pages = {
        "rows": "<ul>" + "<li>Item</li>" * 60 + "</ul>",
        "long_cell": "<ul><li>X" + "🦉" * 400 + "</li></ul>",
        "span": "<table><tr><td colspan=2>Merged</td></tr></table>",
        "source_bound": "<span></span>" * 9000 + "<ul><li>Unseen</li></ul>",
        "row_bound": "<ul>" + "<span></span>" * 5000 + "<li>Unseen</li></ul>",
        "nested": "<ul><li>Outer<ul><li>NESTED_CANARY</li></ul></li></ul>",
    }

    async def home(request: Request) -> Response:
        del request
        return html(pages[fixture])

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        args = extraction_arguments(first.revision, kind="table" if fixture == "span" else "list")
        result = await BrowserObserveTool(Provider(runtime, site.origin)).execute(
            args, tool_context()
        )
        assert result.ok and result.structured is not None
        data = result.structured["extraction"]
        if fixture == "rows":
            assert len(data["rows"]) == 20 and data["omitted_rows"] == 40
        elif fixture == "long_cell":
            cell = data["rows"][0]["cells"][0]
            assert cell["status"] == "truncated" and cell["value"] is None
            assert cell["text"] == ("X" + "🦉" * 400)[: len(cell["text"])]
            assert data["rows"][0]["schema_valid"] is False
        elif fixture == "span":
            assert data["status"] == "unsupported_structure" and data["rows"] == []
        elif fixture == "source_bound":
            assert data["status"] == "not_found" and data["source_scan_limit_reached"]
            assert data["source_nodes"] == 8192
        elif fixture == "row_bound":
            assert data["rows"] == [] and data["row_scan_limit_reached"]
            assert data["row_nodes"] == 4096
        else:
            assert [r["cells"][0]["text"] for r in data["rows"]] == ["Outer"]
            assert "CANARY" not in str(data)


@pytest.mark.parametrize("failure", ["error", "cancel", "navigation"])
async def test_extraction_capture_failure_invalidates_all_action_references(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from playwright.async_api import Error as PlaywrightError

    from agent_core.adapters.browser.extraction import EXTRACTION_SCRIPT
    from agent_core.domain.browser import BrowserProviderError
    from agent_core.domain.browser_extraction import BrowserExtractionRequest

    async def home(request: Request) -> Response:
        del request
        return html("<ul><li>One</li></ul><button>Next</button>")

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        page = runtime._current_page()
        original = page.evaluate
        started = asyncio.Event()

        async def evaluate(expression: str, arg: Any = None) -> Any:
            if expression == EXTRACTION_SCRIPT:
                started.set()
                if failure == "error":
                    raise PlaywrightError("synthetic extraction failure")
                if failure == "cancel":
                    await asyncio.Future()
                runtime._main_frame_navigations += 1
            return await original(expression, arg)

        monkeypatch.setattr(page, "evaluate", evaluate)
        request = BrowserExtractionRequest.model_validate(
            extraction_arguments(first.revision)["extract"]
        )
        task = asyncio.create_task(runtime.extract(request))
        await asyncio.wait_for(started.wait(), 2)
        if failure == "cancel":
            task.cancel()
        with pytest.raises((PlaywrightError, asyncio.CancelledError, BrowserProviderError)):
            await task
        assert runtime._revision is None and runtime._elements == {} and runtime._facts is None
        assert runtime._continuation is None and runtime._element_offsets == {}


async def test_form_label_evidence_excludes_hidden_ancestors_and_reports_clipped_ids() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<div aria-hidden="true"><span id="hidden">LABEL_CANARY</span></div>'
            '<span id="visible">Name</span><form>'
            '<input aria-labelledby="hidden"><input aria-labelledby="visible'
            + " " * 1100
            + 'hidden"></form>'
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        first = await runtime.navigate(site.url("/"))
        result = await BrowserObserveTool(Provider(runtime, site.origin)).execute(
            extraction_arguments(first.revision, kind="form"), tool_context()
        )
        assert result.ok and result.structured is not None
        data = result.structured["extraction"]
        assert "CANARY" not in str(data)
        assert data["rows"][0]["cells"][0]["status"] == "missing"
        assert data["rows"][1]["cells"][0]["status"] == "truncated"


async def test_explicit_aria_collections_and_form_states_are_extracted() -> None:
    from agent_core.domain.browser_extraction import BrowserExtractionRequest

    async def home(request: Request) -> Response:
        del request
        return html("""<div role="table"><div role="row"><span role="cell">8</span></div></div>
          <div role="list"><div role="listitem">Choice</div></div>
          <div role="form"><div role="checkbox" aria-label="Enabled" aria-checked="true"
           aria-disabled="true" aria-required="true">Enabled</div></div>""")

    async with browsing([Route("/", home)]) as (site, runtime):
        page = await runtime.navigate(site.url("/"))
        for kind, expected in (("table", "8"), ("list", "Choice"), ("form", "Enabled")):
            args = extraction_arguments(page.revision, kind=kind)["extract"]
            if kind == "form":
                args["fields"].extend(
                    {"name": name, "column": column, "type": "boolean"}
                    for column, name in ((2, "disabled"), (3, "checked"), (4, "required"))
                )
            page = await runtime.extract(BrowserExtractionRequest.model_validate(args))
            assert page.extraction is not None
            row = page.extraction.rows[0]
            assert row.schema_valid and row.cells[0].value == expected
            if kind == "form":
                assert [cell.value for cell in row.cells[1:]] == [True, True, True]
