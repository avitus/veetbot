"""Composite hosted browser control-plane surface contract."""

import json
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationMode,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserSnapshot,
)
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000e1")
CEREMONY_ID = UUID("00000000-0000-0000-0000-0000000000e2")


def test_hosted_browser_control_plane_has_no_material_or_caller_success_surface() -> None:
    public = {name for name in dir(HostedBrowserSessionControlPlane) if not name.startswith("_")}

    assert {
        "acquire",
        "navigate",
        "observe",
        "act",
        "close",
        "begin_authentication",
        "authentication_status",
        "cancel_authentication",
    } <= public
    # ADR-0128: the handoff goes from the owner's client to the isolated
    # service alone; orchestration can neither send nor import a session.
    assert not public & {
        "complete_authentication",
        "export_material",
        "load_cookies",
        "storage_state",
        "handoff",
        "accept_device_session",
        "import_session",
    }


@pytest.mark.parametrize("mode", [None, *BrowserAuthenticationMode])
async def test_hosted_client_sends_mode_only_for_device(
    mode: BrowserAuthenticationMode | None,
) -> None:
    """ADR-0128 section 2.4: remote begins stay byte-compatible with an older service."""

    bodies: list[dict[str, object]] = []

    def service(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        suffix = "/handoff" if mode is BrowserAuthenticationMode.DEVICE else ""
        return httpx.Response(
            201,
            json={
                "id": str(CEREMONY_ID),
                "profile_id": str(PROFILE_ID),
                "status": "authentication_required",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                "launch_url": (
                    f"https://browser.example.test/authentication/{CEREMONY_ID}{suffix}"
                    "#capability=one-time"
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        control_plane = HostedBrowserSessionControlPlane(
            base_url="https://browser.example.test",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=client,
        )
        if mode is None:
            view = await control_plane.begin_authentication(
                PROFILE_ID, principal(), "opaque/ref", login_url="https://example.org/"
            )
        else:
            view = await control_plane.begin_authentication(
                PROFILE_ID, principal(), "opaque/ref", login_url="https://example.org/", mode=mode
            )

    assert view.id == CEREMONY_ID
    (body,) = bodies
    if mode is BrowserAuthenticationMode.DEVICE:
        assert body.get("mode") == "device"
    else:
        assert "mode" not in body
    assert {key for key in body if key != "mode"} == {
        "profile_id",
        "provider_ref",
        "login_url",
        "tenant_id",
        "principal_id",
    }


LEASE_REF = "lease-reference-" + "0" * 32


def _page(revision: str) -> dict[str, object]:
    return {
        "url": "https://www.example.org/lesson",
        "title": "Lesson",
        "revision": revision,
        "text": "Exercise 1",
        "elements": [{"ref": f"{revision}:0", "role": "button", "name": "Continue"}],
    }


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (None, None),
        (
            {"revision": "r-1", "elements": {"r-1:0": {"field_kind": "none"}}},
            BrowserObservationFacts(
                revision="r-1",
                elements={"r-1:0": BrowserElementFacts(field_kind=BrowserFieldKind.NONE)},
            ),
        ),
        # Facts for another revision, or malformed facts, give no coverage.
        ({"revision": "r-0", "elements": {}}, None),
        ({"revision": "r-1", "elements": {"r-1:0": {"field_kind": "rocket"}}}, None),
    ],
)
async def test_old_and_new_session_response_shapes_parse(
    facts: dict[str, object] | None, expected: BrowserObservationFacts | None
) -> None:
    """ADR-0129 D26: facts are an optional sibling of the observation's fields."""

    def service(request: httpx.Request) -> httpx.Response:
        del request
        body = _page("r-1")
        if facts is not None:
            body["facts"] = facts
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        control_plane = HostedBrowserSessionControlPlane(
            base_url="https://browser.example.test",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=client,
        )
        pages = [
            await control_plane.navigate(LEASE_REF, "https://www.example.org/lesson"),
            await control_plane.observe(LEASE_REF),
            await control_plane.act(
                LEASE_REF,
                BrowserAction(kind=BrowserActionKind.CLICK, expected_revision="r-1", ref="r-1:0"),
                sequence=1,
            ),
        ]

    for page in pages:
        # Still an observation to every caller that knows nothing of facts.
        assert isinstance(page, BrowserObservation)
        assert page.snapshot() == BrowserSnapshot(
            observation=BrowserObservation.model_validate(_page("r-1")), facts=expected
        )
