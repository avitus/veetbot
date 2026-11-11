"""Web-component regions remain bounded evidence with revision-bound focus."""

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


async def test_open_shadow_regions_are_discoverable_and_focusable() -> None:
    """A web component's form and nested status can be found without a selector."""

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button>Outside</button><div id="host"></div>',
            script="""
            const shadow = document.querySelector('#host').attachShadow({mode: 'open'});
            shadow.innerHTML = '<form><h2>Preferences</h2><button>Save settings</button>'
                + '<div id="nested"></div></form>';
            shadow.querySelector('#nested').attachShadow({mode: 'open'}).innerHTML =
                '<div role="status">Saved settings</div>';
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        forms = [region for region in initial.regions if region.kind == "form"]
        assert len(forms) == 1, "open-shadow form is absent from semantic regions"
        assert any(region.text == "Saved settings" for region in initial.regions)
        assert initial.region_coverage is not None
        assert initial.region_coverage.version == 2
        assert initial.region_coverage.scope == "main_document_and_open_shadow"
        focused = await runtime.expand(
            BrowserObservationExpansion(region_ref=forms[0].ref, expected_revision=initial.revision)
        )
        assert [element.name for element in focused.elements] == ["Save settings", "Saved settings"]
        assert "Outside" not in focused.text
        assert "Preferences" in focused.text and "Saved settings" in focused.text
        with pytest.raises(BrowserProviderError, match="element_not_found"):
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.CLICK,
                    expected_revision=focused.revision,
                    ref=focused.regions[0].ref,
                )
            )


async def test_shadow_regions_exclude_private_hosts_slots_frames_and_closed_roots() -> None:
    """Rendered slot text cannot bypass an excluded composed ancestor."""

    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="visible"></div><div id="closed"></div>'
            '<iframe srcdoc="<h2>FRAME_CANARY</h2>"></iframe>',
            script="""
            document.querySelector('#visible').attachShadow({mode:'open'}).innerHTML =
                '<div role="status">Public status</div><input value="INPUT_CANARY">'
                + '<div contenteditable><h2>EDITABLE_CANARY</h2></div>';
            document.querySelector('#closed').attachShadow({mode:'closed'}).innerHTML =
                '<h2>CLOSED_CANARY</h2>';
            for (const rule of ['hidden', 'aria-hidden="true"', 'style="opacity:0"',
                                'contenteditable', 'role="textbox"']) {
                const host = document.createElement('div');
                host.innerHTML = '<h2 slot="secret">SLOTTED_CANARY</h2>';
                host.attachShadow({mode:'open'}).innerHTML =
                    '<div ' + rule + '><slot name="secret"></slot></div>';
                document.body.append(host);
                const excluded = document.createElement('div');
                excluded.innerHTML = '<div ' + rule + ' id="private"></div>';
                excluded.firstChild.attachShadow({mode:'open'}).innerHTML =
                    '<div role="alert">HOST_CANARY</div>';
                document.body.append(excluded);
            }
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observed = await runtime.navigate(site.url("/"))
        assert [region.text for region in observed.regions] == ["Public status"]


@pytest.mark.parametrize("change", ["hidden", "aria", "opacity", "editable", "textbox", "detach"])
async def test_focus_rechecks_shadow_host_exclusion_and_discards_handles(change: str) -> None:
    """An old region handle cannot read through a host excluded after capture."""

    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="host"></div>',
            script="""
            document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
                '<form><h2>Private preferences</h2><button>Save</button></form>';
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observed = await runtime.navigate(site.url("/"))
        form = next(region for region in observed.regions if region.kind == "form")
        assert runtime._page is not None
        await runtime._page.locator("#host").evaluate(
            """(node, change) => {
            if (change === 'hidden') node.hidden = true;
            if (change === 'aria') node.setAttribute('aria-hidden', 'true');
            if (change === 'opacity') node.style.opacity = '0';
            if (change === 'editable') node.contentEditable = 'true';
            if (change === 'textbox') node.setAttribute('role', 'textbox');
            if (change === 'detach') node.remove();
        }""",
            change,
        )
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await runtime.expand(
                BrowserObservationExpansion(
                    region_ref=form.ref,
                    expected_revision=observed.revision,
                )
            )
        assert not runtime._regions and not runtime._elements


async def test_shadow_region_bounds_priority_and_summary_truncation() -> None:
    """Shadow descendants share the same limits as ordinary document nodes."""

    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="host"></div>',
            script="""
            document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
                '<h2>Heading</h2>'.repeat(40)
                + '<div role="alert">' + '🦉'.repeat(400) + '</div>'
                + '<div role="status">' + '<span></span>'.repeat(300)
                + 'Beyond summary</div>'
                + '<span></span>'.repeat(9000) + '<h2>Beyond scan</h2>';
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observed = await runtime.navigate(site.url("/"))
        assert len(observed.regions) == 32
        assert observed.region_coverage is not None
        assert observed.region_coverage.scanned_nodes == 8192
        assert observed.region_coverage.scan_limit_reached
        assert observed.region_coverage.omitted_regions == 10
        assert observed.regions[0].kind == "alert"
        assert observed.regions[0].text == "🦉" * 256
        assert observed.regions[0].text_truncated
        status = next(region for region in observed.regions if region.kind == "status")
        assert status.text_truncated and "Beyond" not in status.text
        assert all("Beyond scan" not in region.text for region in observed.regions)


async def test_slotted_regions_are_counted_once_and_ancestor_checks_are_bounded() -> None:
    """Light nodes are visited once; a deep composed ancestry consumes the shared ceiling."""

    async def home(request: Request) -> Response:
        del request
        return html(
            '<div id="host"><h2 slot="public">Visible once</h2></div>',
            script="""
            const shadow = document.querySelector('#host').attachShadow({mode:'open'});
            shadow.innerHTML = '<slot name="public"></slot>';
            let parent = shadow;
            for (let i = 0; i < 300; i++) {
                const child = document.createElement('div');
                parent.append(child); parent = child.attachShadow({mode:'open'});
            }
            parent.innerHTML = '<div role="status">Deep bounded summary</div>';
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observed = await runtime.navigate(site.url("/"))
        assert sum(region.text == "Visible once" for region in observed.regions) == 1
        status = next(region for region in observed.regions if region.kind == "status")
        assert status.text_truncated and not status.text
        assert observed.region_coverage is not None
        assert observed.region_coverage.scanned_nodes <= 8192
