"""Website resources load without granting their origins navigation authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime


@dataclass
class DocumentSession:
    """Record decisions at Chromium's pre-dispatch redirect boundary."""

    commands: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    async def send(self, method: str, parameters: dict[str, str]) -> None:
        """Capture the continuation or refusal for a paused document request."""
        self.commands.append((method, parameters))


@pytest.mark.parametrize(
    ("url", "frame_id", "allowed"),
    [
        ("https://site.example/login", "main", True),
        ("https://static.other.example/redirect", "main", False),
        ("https://verify.other.example/challenge", "child", True),
        ("https://verify.other.example/challenge", "", False),
        ("http://verify.other.example/challenge", "child", False),
        ("https://metadata.internal/token", "child", False),
    ],
)
async def test_each_document_hop_is_checked_before_dispatch(
    url: str, frame_id: str, allowed: bool
) -> None:
    """Resource permission never grants a redirect permission, including on reuse."""
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.example",)
    runtime._main_frame_id = "main"
    session = DocumentSession()
    runtime._document_session = session  # type: ignore[assignment]
    resource = ResourceRoute("https://static.other.example/app.js")
    await runtime._route(resource)  # type: ignore[arg-type]
    assert resource.continued

    await runtime._guard_document_request(
        {"requestId": "redirect-hop", "frameId": frame_id, "request": {"url": url}}
    )

    parameters = {"requestId": "redirect-hop"}
    if not allowed:
        parameters["errorReason"] = "BlockedByClient"
    assert session.commands == [
        ("Fetch.continueRequest" if allowed else "Fetch.failRequest", parameters)
    ]
    assert runtime._disallowed_navigation is (not allowed and frame_id == "main")


@dataclass
class ResourceRoute:
    """Capture a browser request decision without performing network I/O."""

    url: str
    navigation: bool = False
    child_frame: bool = False
    continued: bool = False
    aborted: bool = False

    @property
    def request(self) -> SimpleNamespace:
        """Expose request metadata provided by the browser, not page content."""
        return SimpleNamespace(
            url=self.url,
            is_navigation_request=lambda: self.navigation,
            frame=SimpleNamespace(parent_frame=object() if self.child_frame else None),
        )

    async def continue_(self) -> None:
        """Record permission to send the request."""
        self.continued = True

    async def abort(self, _reason: str) -> None:
        """Record refusal before the request reaches the network."""
        self.aborted = True


@pytest.mark.parametrize(
    ("url", "navigation", "child_frame", "allowed"),
    [
        ("https://static.other.example/app.js", False, False, True),
        ("https://api.other.example/session", False, False, True),
        ("https://verify.other.example/challenge", True, True, True),
        ("https://static.other.example/account", True, False, False),
        ("https://site.example/login", True, False, True),
        ("http://static.other.example/app.js", False, False, False),
        ("https://127.0.0.1/app.js", False, False, False),
        ("https://metadata.internal/app.js", False, False, False),
        ("https://static.other.example:8443/app.js", False, False, False),
        ("https://user@static.other.example/app.js", False, False, False),
    ],
)
async def test_page_resource_permission_does_not_grant_navigation(
    url: str, navigation: bool, child_frame: bool, allowed: bool
) -> None:
    """Public assets and verification frames work with only the website origin."""
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.example",)
    route = ResourceRoute(url, navigation, child_frame)

    await runtime._route(route)  # type: ignore[arg-type]

    assert route.continued is allowed
    assert route.aborted is not allowed
    assert runtime._allowed_origins == ("https://site.example",)


def test_embedded_verification_is_not_a_refused_top_level_redirect() -> None:
    """A CAPTCHA frame must not poison the main-page navigation result."""
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.example",)
    route = ResourceRoute("https://verify.other.example/challenge", True, True)

    runtime._track_navigation(route.request)  # type: ignore[arg-type]

    assert runtime._disallowed_navigation is False
