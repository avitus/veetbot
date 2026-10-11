"""Account-menu qualification through the production bounded browser capture."""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from tests.unit.test_browser_account_control_verification import catalog
from tests.unit.test_browser_runtime_real_chromium import browsing, html


@pytest.mark.parametrize("case", ["valid", "wrong_account", "hidden", "editable", "truncated"])
async def test_account_menu_uses_visible_noneditable_complete_evidence(case: str) -> None:
    async def home(request: Request) -> Response:
        del request
        text = "Fixture Owner<br>@fixture_owner"
        attributes = 'aria-label="Account menu"'
        if case == "wrong_account":
            text = "Other @other"
        elif case == "hidden":
            text = "<span hidden>Fixture Owner @fixture_owner</span>"
        elif case == "editable":
            attributes += ' contenteditable="true"'
        elif case == "truncated":
            text += " x" * 600
        return html(
            f"<button {attributes}>{text}</button>"
            "<h1>Your Home Timeline</h1>"
            "<article>Unrelated feed mention: Fixture Owner @fixture_owner</article>"
        )

    async with browsing([Route("/home", home)]) as (site, runtime):
        data = catalog().model_dump(mode="json")
        data["sites"][0]["origin"] = site.origin
        definition = BrowserVerificationCatalog.model_validate(data).sites[0]
        observation = await runtime.navigate(site.url("/home"))
        assert definition.confirms(observation, runtime.facts(observation.revision)) is (
            case == "valid"
        )
