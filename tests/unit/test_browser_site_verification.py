"""A curated site's protected state and intended account are both required."""

from pathlib import Path

import pytest

from agent_core.browser_control_plane.sessions import DeviceSessionRejected
from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from agent_core.domain.browser import (
    BrowserAuthenticationStatus,
    BrowserObservation,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
)
from tests.contract.support import principal
from tests.contract.test_hosted_profile_session_service_contract import (
    PROFILE_ID,
    PROVIDER_REF,
    HandoffScenario,
    begin_device,
    device_handoff,
    provision,
    record_writes,
    services,
)


async def test_saved_session_with_wrong_account_is_refused_before_a_lease(tmp_path: Path) -> None:
    from datetime import timedelta

    from agent_core.domain.browser import BrowserProviderError
    from tests.contract.support import NOW
    from tests.contract.test_hosted_profile_session_service_contract import RUN_ID

    catalog = BrowserVerificationCatalog.model_validate(
        {
            "sites": [
                {
                    "id": "fixture-account",
                    "version": 1,
                    "profile_id": str(PROFILE_ID),
                    "origin": "https://example.org",
                    "protected_path": "/current",
                    "ready": {"kind": "text", "text": "Account ready"},
                    "account": {
                        "kind": "region",
                        "region_kind": "heading",
                        "text": "Account: Owner",
                    },
                }
            ]
        }
    )
    lifecycle, sessions, runtimes, _times = services(tmp_path, verification=catalog)
    await provision(lifecycle)
    with pytest.raises(BrowserProviderError, match="authentication_required"):
        await sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=1),
        )
    assert all(runtime.closed for runtime in runtimes)


@pytest.mark.parametrize("mode", ["device", "remote"])
@pytest.mark.parametrize("case", ["verified", "wrong_account", "truncated", "public", "no_success"])
async def test_curated_verification_requires_unique_complete_account_and_protected_state(
    tmp_path: Path,
    mode: str,
    case: str,
) -> None:
    catalog = BrowserVerificationCatalog.model_validate(
        {
            "sites": [
                {
                    "id": "fixture-account",
                    "version": 1,
                    "profile_id": str(PROFILE_ID),
                    "origin": "https://example.org",
                    "protected_path": "/current",
                    "ready": {"kind": "text", "text": "Account ready"},
                    "account": {
                        "kind": "region",
                        "region_kind": "heading",
                        "text": "Account: Owner",
                    },
                }
            ]
        }
    )
    observed = BrowserObservation(
        url="https://example.org/current",
        revision="fresh",
        text="Account ready" if case != "no_success" else "Loading",
        regions=(
            BrowserSemanticRegion(
                ref="account",
                kind="heading",
                text="Account: Other" if case == "wrong_account" else "Account: Owner",
                text_truncated=case == "truncated",
            ),
        ),
        region_coverage=BrowserRegionCoverage(scanned_nodes=5),
    )
    scenario = HandoffScenario(with_observation=observed)
    if case == "public":
        scenario.without_session = None
        scenario.without_observation = observed
    lifecycle, sessions, _runtimes, _times = services(
        tmp_path, scenario=scenario, verification=catalog
    )
    await provision(lifecycle)
    writes = record_writes(sessions)
    if mode == "device":
        ceremony_id, capability = await begin_device(sessions)
        if case == "verified":
            await sessions.accept_device_session(ceremony_id, capability, device_handoff())
        else:
            with pytest.raises(DeviceSessionRejected):
                await sessions.accept_device_session(ceremony_id, capability, device_handoff())
    else:
        ceremony = await sessions.begin_authentication(
            PROFILE_ID, principal(), PROVIDER_REF, login_url="https://example.org/login"
        )
        ceremony_id = ceremony.id
        await sessions.refresh_authentication(ceremony_id, principal())
    status = await sessions.authentication_status(ceremony_id, principal())
    assert (status.status is BrowserAuthenticationStatus.READY) is (case == "verified")
    assert len(writes) == int(case == "verified")
    if case == "verified":
        from datetime import timedelta

        from tests.contract.support import NOW
        from tests.contract.test_hosted_profile_session_service_contract import RUN_ID

        lease = await sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=1),
        )
        assert lease.lease_ref
        assert await sessions.authentication_status(ceremony_id, principal()) == status
        await sessions.close(lease.lease_ref)
