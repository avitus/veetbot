"""Bounded semantic page evidence, independently checked in Chromium."""

import json

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserObservationExpansion,
    BrowserProviderError,
)
from tests.unit.test_browser_runtime_real_chromium import browsing, html


async def test_visible_regions_prioritize_dialogs_and_exclude_hidden_or_editable_values() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("""
            <h1>Account overview</h1>
            <form><h2>Preferences</h2><label>Timezone</label>
              <input value="INPUT_CANARY"><textarea>TEXTAREA_CANARY</textarea>
              <select><option>SELECT_CANARY</option></select>
              <div contenteditable>EDITABLE_CANARY</div>
              <span hidden>HIDDEN_CANARY</span><span style="display:none">CSS_CANARY</span>
              <span aria-hidden="true">ARIA_CANARY</span>
            </form>
            <div role="status">Saved preferences</div>
            <div role="alert">Review your timezone</div>
            <dialog open><h2>Confirm changes</h2><button>Continue</button></dialog>
            <h2 hidden>HIDDEN_HEADING</h2><h2 style="visibility:hidden">INVISIBLE_HEADING</h2>
            <iframe srcdoc="<h2>FRAME_CANARY</h2>"></iframe>
            <div id="shadow-host"></div>
            <script>document.querySelector("#shadow-host").attachShadow({mode:"open"})
              .innerHTML = "<h2>SHADOW_CANARY</h2>";</script>
        """)

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        raw = observation.model_dump()
        assert raw.get("regions"), "bounded semantic regions are missing"
        regions = raw["regions"]
        assert [r["kind"] for r in regions[:4]] == ["dialog", "alert", "status", "form"]
        assert regions[0]["text"] == "Confirm changes Continue"
        serialized = json.dumps(regions)
        assert "CANARY" not in serialized and "HIDDEN_HEADING" not in serialized
        assert "INVISIBLE_HEADING" not in serialized
        assert all(r["ref"].startswith(observation.revision + ":region:") for r in regions)
        assert raw["region_coverage"]["scope"] == "main_document"
        assert raw["region_coverage"]["scan_limit_reached"] is False
        assert raw["region_coverage"]["omitted_regions"] == 0
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.CLICK,
                    expected_revision=observation.revision,
                    ref=regions[0]["ref"],
                )
            )
        assert refused.value.reason_code == "tool.browser.element_not_found"
        with pytest.raises(BrowserProviderError) as not_anchor:
            await runtime.expand(BrowserObservationExpansion(after=regions[0]["ref"]))
        assert not_anchor.value.reason_code == "tool.browser.page_changed"


async def test_region_count_and_summary_bounds_preserve_priority_and_disclose_omissions() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "".join(f"<h2>Section {i}</h2>" for i in range(40))
            + "<div role='alert'>"
            + "雪" * 2000
            + "</div>"
            + "<div role='status'>"
            + "<span></span>" * 300
            + "Beyond summary scan</div>"
            + "<button>Next</button>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        raw = observation.model_dump()
        assert raw.get("regions"), "bounded semantic regions are missing"
        assert len(raw["regions"]) == 32
        assert raw["region_coverage"]["omitted_regions"] == 10
        assert raw["regions"][0]["kind"] == "alert"
        assert len(raw["regions"][0]["text"]) == 512
        assert raw["regions"][0]["text_truncated"] is True
        status = next(r for r in raw["regions"] if r["kind"] == "status")
        assert status["text_truncated"] is True and "Beyond" not in status["text"]


async def test_region_document_scan_is_bounded_and_changed_evidence_gets_a_new_revision() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<h1>Before</h1><button>Update</button>"
            + "<span></span>" * 9000
            + "<h2>Beyond scan</h2>",
            script="""document.querySelector('button').onclick = () => {
                document.querySelector('h1').textContent = 'After';
            }""",
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        before = await runtime.navigate(site.url("/"))
        coverage = before.model_dump().get("region_coverage")
        assert coverage is not None, "region scan coverage is missing"
        assert coverage["scanned_nodes"] == 8192 and coverage["scan_limit_reached"] is True
        assert "Beyond scan" not in json.dumps(before.model_dump()["regions"])
        after = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CLICK,
                expected_revision=before.revision,
                ref=before.elements[0].ref,
            )
        )
        assert after.revision != before.revision
        assert after.model_dump()["regions"][0]["text"] == "After"
        assert after.model_dump()["regions"][0]["ref"] != before.model_dump()["regions"][0]["ref"]


async def test_region_text_bounds_never_split_a_multibyte_character() -> None:
    async def home(request: Request) -> Response:
        del request
        return html("<div role='alert'>X" + "🦉" * 400 + "</div>")

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        region = observation.model_dump()["regions"][0]
        assert region["text_truncated"] is True
        assert region["text"].encode("utf-8").decode("utf-8") == region["text"]
        assert region["text"] == ("X" + "🦉" * 400)[: len(region["text"])]
