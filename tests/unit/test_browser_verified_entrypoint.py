"""A verified account dashboard survives bare-origin navigation and its retries."""

from datetime import timedelta
from pathlib import Path

import pytest

from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from agent_core.domain.browser import (
    BrowserObservation,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
)
from tests.contract.support import NOW, principal
from tests.contract.test_hosted_profile_session_service_contract import (
    PROFILE_ID,
    PROVIDER_REF,
    RUN_ID,
    HandoffScenario,
    provision,
    services,
)

ORIGIN = "https://example.org"
HOME = ORIGIN + "/MHO/Home"


def verified_catalog() -> BrowserVerificationCatalog:
    return BrowserVerificationCatalog.model_validate(
        {
            "sites": [
                {
                    "id": "fixture-patient-dashboard",
                    "version": 1,
                    "profile_id": str(PROFILE_ID),
                    "origin": ORIGIN,
                    "protected_path": "/MHO/Home",
                    "ready": {"kind": "text", "text": "Quick Actions"},
                    "account": {
                        "kind": "region",
                        "region_kind": "heading",
                        "text": "Fixture Patient",
                    },
                }
            ]
        }
    )


@pytest.mark.parametrize("requested", [ORIGIN, ORIGIN + "/"])
async def test_verified_entrypoint_survives_fresh_leases(tmp_path: Path, requested: str) -> None:
    observation = BrowserObservation(
        url=HOME,
        revision="verified",
        text="Quick Actions",
        regions=(BrowserSemanticRegion(ref="patient", kind="heading", text="Fixture Patient"),),
        region_coverage=BrowserRegionCoverage(scanned_nodes=5),
    )
    lifecycle, sessions, runtimes, _ = services(
        tmp_path,
        verification=verified_catalog(),
        scenario=HandoffScenario(
            without_session=None, without_observation=observation, with_observation=observation
        ),
    )
    await provision(lifecycle)
    for attempt in (1, 2):
        lease = await sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=attempt,
            deadline_at=NOW + timedelta(minutes=1),
        )
        try:
            assert runtimes[-1].evidence_urls == [HOME]
            # An observation cannot change how the profile's entry URL resolves.
            assert (await sessions.observe(lease.lease_ref)).url == HOME
            assert (await sessions.navigate(lease.lease_ref, requested)).url == HOME
            assert (await sessions.navigate(lease.lease_ref, requested)).url == HOME
        finally:
            await sessions.close(lease.lease_ref)


@pytest.mark.parametrize(
    "suffix", ["/records", "/?view=public", "/#public", "/MHO/Authentication/Login"]
)
async def test_explicit_navigation_is_not_rewritten(tmp_path: Path, suffix: str) -> None:
    observation = BrowserObservation(
        url=HOME,
        revision="verified",
        text="Quick Actions",
        regions=(BrowserSemanticRegion(ref="patient", kind="heading", text="Fixture Patient"),),
        region_coverage=BrowserRegionCoverage(scanned_nodes=5),
    )
    lifecycle, sessions, _, _ = services(
        tmp_path,
        verification=verified_catalog(),
        scenario=HandoffScenario(without_session=None, without_observation=observation),
    )
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=1),
    )
    try:
        assert (await sessions.navigate(lease.lease_ref, ORIGIN + suffix)).url == ORIGIN + suffix
        assert (await sessions.navigate(lease.lease_ref, ORIGIN)).url == HOME
    finally:
        await sessions.close(lease.lease_ref)


async def test_unconfigured_profile_keeps_bare_origin_navigation(tmp_path: Path) -> None:
    lifecycle, sessions, _, _ = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=1),
    )
    try:
        assert (await sessions.navigate(lease.lease_ref, ORIGIN)).url == ORIGIN
    finally:
        await sessions.close(lease.lease_ref)
