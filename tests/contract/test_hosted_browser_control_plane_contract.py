"""Composite hosted browser control-plane surface contract."""

from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane


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
    assert not public & {
        "complete_authentication",
        "export_material",
        "load_cookies",
        "storage_state",
    }
