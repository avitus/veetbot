"""Bounded readable snapshots from actual rendered pages."""

import pytest
from playwright.async_api import Locator
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_readable_text_excludes_hidden_and_editable_subtrees() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("""<h1>Preferences</h1><label>Name<input value="INPUT_CANARY"></label>
          <textarea>TEXTAREA_CANARY</textarea><select><option>SELECT_CANARY</option></select>
          <div contenteditable>EDITABLE_CANARY</div><div role="textbox">CUSTOM_CANARY</div>
          <div hidden>HIDDEN_CANARY</div><div aria-hidden="true">ARIA_CANARY</div>
          <div style="opacity:0">OPACITY_CANARY</div><div style="display:none">CSS_CANARY</div>
          <script>const unused = 'SCRIPT_CANARY';</script><style>/* STYLE_CANARY */</style>
          <iframe srcdoc="<p>FRAME_CANARY</p>"></iframe><p>Visible status</p>""")

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert "CANARY" not in observation.text
        assert "Preferences" in observation.text and "Name" in observation.text
        assert "Visible status" in observation.text
        coverage = observation.model_dump().get("text_coverage")
        assert coverage is not None, "readable text coverage is missing"
        assert coverage["scope"] == "main_document_and_open_shadow"
        assert not coverage["node_limit_reached"] and not coverage["text_limit_reached"]


async def test_shadow_and_slotted_text_is_read_once_without_whole_body_inner_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0
    original = Locator.inner_text

    async def counted(locator: Locator, *args: object, **kwargs: object) -> str:
        nonlocal reads
        reads += 1
        return await original(locator, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Locator, "inner_text", counted)

    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="host"><span>Slotted answer</span></div><div id="closed"></div>',
            script="""document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
              '<p>Shadow question</p><slot>FALLBACK_CANARY</slot><input value="VALUE_CANARY">';
            document.querySelector('#closed').attachShadow({mode:'closed'}).innerHTML =
              '<p>CLOSED_CANARY</p>';""",
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert "Shadow question" in observation.text
        assert observation.text.count("Slotted answer") == 1
        assert "CANARY" not in observation.text
        assert reads == 0, "collector still materializes whole rendered-page text"


async def test_readable_text_preserves_inline_and_block_boundaries() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<p><span>Inter</span><b>national</b>   space<br>Next line</p>"
            "<p>Second paragraph</p><table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>C</td><td>D</td></tr></table>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert observation.text == "International space\nNext line\nSecond paragraph\nA B\nC D"


@pytest.mark.parametrize("wrapper", ['aria-hidden="true"', 'style="opacity:0"', "contenteditable"])
async def test_slotted_text_obeys_its_shadow_ancestors(wrapper: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<p>Visible</p><div id="host"><span>SLOT_CANARY</span></div>',
            script="document.querySelector('#host').attachShadow({mode:'open'}).innerHTML="
            + repr(f"<div {wrapper}><slot></slot></div>")
            + ";",
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert observation.text == "Visible"


@pytest.mark.parametrize(
    "value,limited,expected",
    [
        ("x" * 262144, False, "x" * 262144),
        ("x" * 262145, True, "x" * 262144),
        ("🦉" * 90000, True, "🦉" * 65536),
        ("x" * 262143 + "🦉", True, "x" * 262143),
    ],
    ids=["exact", "units", "utf8", "surrogate"],
)
async def test_readable_text_bounds_work_before_transfer(
    value: str,
    limited: bool,
    expected: str,
) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(f'<p style="overflow-wrap:anywhere">{value}</p>')

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert observation.text == expected
        assert len(observation.text.encode()) <= 262144
        assert observation.text_coverage is not None
        assert observation.text_coverage.scanned_text_characters <= 262144
        assert observation.text_coverage.text_limit_reached is limited
        assert not observation.text_coverage.node_limit_reached


@pytest.mark.parametrize("shape", ["wide", "deep", "slotted"])
async def test_readable_text_stops_at_node_bound_without_recursing(shape: str) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<p>Start</p><div id="container"></div><p>UNREAD_CANARY</p>',
            script="""
          let parent = document.querySelector('#container');
          if (SLOTTED) parent.attachShadow({mode:'open'}).innerHTML = '<slot></slot>';
          for (let i=0; i<9000; i++) {
              const node = document.createElement('span');
              parent.appendChild(node);
              if (DEEP) parent = node;
          }""".replace("DEEP", "true" if shape == "deep" else "false").replace(
                "SLOTTED", "true" if shape == "slotted" else "false"
            ),
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert observation.text == "Start"
        assert observation.text_coverage is not None
        assert observation.text_coverage.scanned_nodes == 8192
        assert observation.text_coverage.node_limit_reached
