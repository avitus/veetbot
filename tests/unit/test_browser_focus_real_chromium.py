"""Task-directed region reads and continuations through the real browser boundary."""

import json
from typing import Any, cast
from uuid import UUID

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.domain.browser import BrowserObservationExpansion, BrowserProviderError
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.tool_output import content_bytes
from tests.unit.test_browser_runtime_real_chromium import browsing, html


@pytest.mark.parametrize("budget", [1024, 4096])
async def test_focus_expands_omitted_text_and_controls_without_leaving_task_region(
    budget: int,
) -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<nav>" + "<button>Navigation</button>" * 270 + "</nav>"
            '<form id="task"><h2>Practice</h2><p>'
            + "雪 exercise " * 100
            + "</p>"
            + "".join(f"<button>Answer {i}</button>" for i in range(280))
            + "</form><footer><button>Footer</button></footer>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        initial_projection = browser_context_projection(
            initial.model_dump(mode="json"),
            budget=budget,
            reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
        )
        assert initial_projection is not None
        first_view = json.loads(next(p.text for p in initial_projection if isinstance(p, TextPart)))
        region = next(r for r in first_view["regions"] if r["kind"] == "form")
        focused = await runtime.expand(
            BrowserObservationExpansion.model_validate(
                {
                    "region_ref": region["ref"],
                    "expected_revision": first_view["revision"],
                }
            )
        )
        assert focused.elements[0].name == "Answer 0"
        assert "Navigation" not in focused.text and "Footer" not in focused.text
        expected = focused.text
        seen = ""
        for _ in range(150):
            projected = browser_context_projection(
                focused.model_dump(mode="json"),
                budget=budget,
                reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
            )
            assert projected is not None and len(content_bytes(projected)) <= budget
            payload = json.loads(next(p.text for p in projected if isinstance(p, TextPart)))
            assert payload["elements"], payload
            seen += payload["text"]
            if "next_text" not in payload:
                break
            assert payload["text"], "text continuation must make progress"
            focused = await runtime.expand(
                BrowserObservationExpansion.model_validate(payload["next_text"])
            )
        assert seen == expected
        more = await runtime.expand(BrowserObservationExpansion(after=focused.elements[-1].ref))
        assert more.elements[-1].name == "Answer 279"
        assert all("Answer" in e.name for e in more.elements)
        whole = await runtime.observe()
        assert whole.elements[0].name == "Navigation"


async def test_focus_rejects_stale_detached_and_changed_text_continuations() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<form id="task"><p>Hello 雪 world</p><button>Continue</button></form>')

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        request = {"region_ref": initial.regions[0].ref, "expected_revision": initial.revision}
        focused = await runtime.expand(BrowserObservationExpansion.model_validate(request))
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await runtime.expand(BrowserObservationExpansion.model_validate(request))
        assert runtime._page is not None
        await runtime._page.locator("p").evaluate("node => node.textContent = 'Changed'")
        focus = focused.model_dump()["focus"]
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await runtime.expand(
                BrowserObservationExpansion.model_validate(
                    {
                        "region_ref": focus["region_ref"],
                        "expected_revision": focused.revision,
                        "text_offset": 6,
                    }
                )
            )
        fresh = await runtime.observe()
        await runtime._page.locator("form").evaluate("node => node.remove()")
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await runtime.expand(
                BrowserObservationExpansion.model_validate(
                    {
                        "region_ref": fresh.regions[0].ref,
                        "expected_revision": fresh.revision,
                    }
                )
            )


async def test_active_dialog_controls_precede_navigation() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            "<nav>"
            + "<button>Navigation</button>" * 300
            + "</nav><dialog open><button>Continue task</button></dialog>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        observation = await runtime.navigate(site.url("/"))
        assert observation.elements[0].name == "Continue task"


async def test_passive_recaptcha_badge_does_not_interrupt_automation() -> None:
    async def home(request: Request) -> Response:
        del request
        return html(
            '<button>Continue</button><div class="grecaptcha-badge">'
            '<iframe title="reCAPTCHA"></iframe></div>'
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        await runtime.navigate(site.url("/"))
        await runtime.check_automation_ready()


@pytest.mark.parametrize(
    "challenge",
    [
        '<input type="password" value="SECRET_CANARY">',
        '<input autocomplete="one-time-code" value="SECRET_CANARY">',
        '<iframe title="CAPTCHA"></iframe>',
        '<div class="grecaptcha-badge"><input type="password" value="SECRET_CANARY"></div>',
        "<form>Use a passkey</form>",
    ],
)
async def test_hosted_authentication_interruption_discards_refs_before_another_action(
    challenge: str,
) -> None:
    from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
    from agent_core.domain.browser import BrowserAction, BrowserActionKind

    async def home(request: Request) -> Response:
        del request
        return html(
            '<button id="go" onclick="document.body.dataset.clicked=\'yes\'">Continue</button>'
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        hosted = HostedPlaywrightSessionRuntime(
            tenant_id="test",
            runtime=runtime,
            proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        )
        assert runtime._page is not None
        await runtime._page.evaluate(
            '(html) => document.body.insertAdjacentHTML("beforeend", html)', challenge
        )
        with pytest.raises(BrowserProviderError, match="needs_user"):
            await hosted.act(
                BrowserAction(
                    kind=BrowserActionKind.CLICK,
                    expected_revision=initial.revision,
                    ref=initial.elements[0].ref,
                )
            )
        assert await runtime._page.evaluate("document.body.dataset.clicked") is None
        assert runtime.facts(initial.revision) is None
        with pytest.raises(BrowserProviderError, match="needs_user"):
            await hosted.observe()


async def test_control_expansion_rechecks_focused_ancestor_privacy() -> None:
    async def home(request: Request) -> Response:
        del request
        return html('<div id="ancestor"><form><p>Text</p><button>Continue</button></form></div>')

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        focused = await runtime.expand(
            BrowserObservationExpansion.model_validate(
                {
                    "region_ref": initial.regions[0].ref,
                    "expected_revision": initial.revision,
                }
            )
        )
        assert runtime._page is not None
        await runtime._page.locator("#ancestor").evaluate("node => node.contentEditable = 'true'")
        with pytest.raises(BrowserProviderError, match="page_changed"):
            await runtime.expand(BrowserObservationExpansion(after=focused.elements[0].ref))


async def test_action_that_reaches_sign_in_returns_interruption_without_controls() -> None:
    from agent_core.browser_control_plane.runtime import HostedPlaywrightSessionRuntime
    from agent_core.domain.browser import BrowserAction, BrowserActionKind

    async def home(request: Request) -> Response:
        del request
        return html(
            "<button onclick=\"document.body.innerHTML='<form><input type=password>'\">"
            "<span>Continue</span></button>"
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        hosted = HostedPlaywrightSessionRuntime(
            tenant_id="test",
            runtime=runtime,
            proxy_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        )
        interrupted = await hosted.act(
            BrowserAction(
                kind=BrowserActionKind.CLICK,
                expected_revision=initial.revision,
                ref=initial.elements[0].ref,
            )
        )
        assert interrupted.interruption == "needs_user"
        assert not interrupted.elements and not interrupted.regions and not interrupted.text
        assert runtime.facts(interrupted.revision) is None


async def test_main_section_focus_keeps_open_shadow_text_and_never_exposes_editable_values() -> (
    None
):
    async def home(request: Request) -> Response:
        del request
        return html(
            '<nav>Other</nav><main><section><h2>Task</h2><div id="host"></div>'
            '<div role="textbox">ROLE_VALUE_CANARY</div>'
            '<input value="PRIVATE_CANARY"></section></main>',
            script="""
            document.querySelector('#host').attachShadow({mode:'open'}).innerHTML =
                '<p>Shadow instruction</p><button>Continue</button>';
        """,
        )

    async with browsing([Route("/", home)]) as (site, runtime):
        initial = await runtime.navigate(site.url("/"))
        assert {"main", "section"} <= {r.kind for r in initial.regions}
        section = next(r for r in initial.regions if r.kind == "section")
        focused = await runtime.expand(
            BrowserObservationExpansion.model_validate(
                {
                    "region_ref": section.ref,
                    "expected_revision": initial.revision,
                }
            )
        )
        assert "Shadow instruction" in focused.text
        assert "Other" not in focused.text and "CANARY" not in focused.model_dump_json()
        assert focused.elements[0].name == "Continue"


@pytest.mark.parametrize("budget", [1024, 4096])
async def test_focused_task_uses_only_model_visible_refs_and_records_one_server_effect(
    budget: int,
) -> None:
    from starlette.responses import JSONResponse

    from agent_core.domain.browser import BrowserAction, BrowserActionKind

    effects: list[bool] = []

    async def home(request: Request) -> Response:
        del request
        return html(
            "<nav>"
            + "<button>Navigation</button>" * 270
            + "</nav><form><p>Save this practice answer</p>"
            '<button type="button" id="save">Save answer</button></form>',
            script="""
            document.querySelector('#save').onclick = async () => {
                await fetch('/save', {method:'POST'});
                document.querySelector('form').innerHTML = '<p role="status">Saved</p>';
            };
        """,
        )

    async def save(request: Request) -> Response:
        del request
        effects.append(True)
        return JSONResponse({"saved": True})

    def model_view(observation: object) -> dict[str, Any]:
        from agent_core.domain.browser import BrowserObservation

        assert isinstance(observation, BrowserObservation)
        parts = browser_context_projection(
            observation.model_dump(mode="json"),
            budget=budget,
            reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
        )
        assert parts is not None and len(content_bytes(parts)) <= budget
        return cast(
            dict[str, Any], json.loads(next(p.text for p in parts if isinstance(p, TextPart)))
        )

    async with browsing([Route("/", home), Route("/save", save, methods=["POST"])]) as (
        site,
        runtime,
    ):
        view = model_view(await runtime.navigate(site.url("/")))
        region = next(r for r in view["regions"] if r["kind"] == "form")
        view = model_view(
            await runtime.expand(
                BrowserObservationExpansion.model_validate(
                    {
                        "region_ref": region["ref"],
                        "expected_revision": view["revision"],
                    }
                )
            )
        )
        target = next(e for e in view["elements"] if e["name"] == "Save answer")
        result = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CLICK, expected_revision=view["revision"], ref=target["ref"]
            )
        )
        assert effects == [True]
        assert any(region.text == "Saved" for region in result.regions)
