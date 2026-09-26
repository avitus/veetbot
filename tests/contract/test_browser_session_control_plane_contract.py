"""Shared hosted browser session control-plane contract."""

from pathlib import Path

from tests.contract import test_profile_service_http_contract as wire_contract


async def test_hosted_browser_session_control_plane_contract(tmp_path: Path) -> None:
    """The client and isolated service agree on every lease/data-plane operation."""

    await wire_contract.test_profile_service_data_plane_and_authentication_are_wire_compatible(
        tmp_path
    )


async def test_facts_are_an_optional_sibling_of_the_observation(tmp_path: Path) -> None:
    """ADR-0129 D26: an older worker still parses every data-plane response."""

    await wire_contract.test_facts_are_an_optional_sibling_of_the_observation(tmp_path)
