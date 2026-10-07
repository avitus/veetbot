"""A curated site's protected state and intended account are both required."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.browser_control_plane.sessions import DeviceSessionRejected
from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from agent_core.domain.browser import (
    BrowserAuthenticationStatus,
    BrowserObservation,
    BrowserProviderError,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
)
from agent_core.domain.errors import ConflictError
from tests.contract.support import NOW, principal
from tests.contract.test_hosted_profile_session_service_contract import (
    PROFILE_ID,
    PROVIDER_REF,
    RUN_ID,
    HandoffScenario,
    begin_device,
    device_handoff,
    provision,
    record_writes,
    services,
)


@pytest.mark.parametrize("finish", ["cancel", "revoke", "expire"])
async def test_slow_lease_verification_does_not_block_other_profiles(
    tmp_path: Path,
    finish: str,
) -> None:
    """An unrelated live lease stays usable while a protected-page load is pending."""
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
    scenario = HandoffScenario(
        gate=asyncio.Event(),
        without_session=None,
        without_observation=BrowserObservation(
            url="https://example.org/current",
            revision="verified",
            text="Account ready",
            regions=(BrowserSemanticRegion(ref="account", kind="heading", text="Account: Owner"),),
            region_coverage=BrowserRegionCoverage(scanned_nodes=5),
        ),
    )
    lifecycle, sessions, runtimes, times = services(
        tmp_path, scenario=scenario, verification=catalog
    )
    await provision(lifecycle)
    other = UUID(int=99551)
    lifecycle._reference_factory = lambda: "opaque-other-session-reference-0000002"
    provisioned = await lifecycle.provision(other, principal(), ("https://example.org",))
    lease = await sessions.acquire(
        other,
        principal(),
        provisioned.provider_ref,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=1),
    )
    pending = asyncio.create_task(
        sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=1),
        )
    )
    try:
        await asyncio.wait_for(scenario.started.wait(), 1)
        observed = await asyncio.wait_for(sessions.observe(lease.lease_ref), 0.2)
        assert observed.revision == "verified"
        with pytest.raises(ConflictError):
            await asyncio.wait_for(
                sessions.acquire(
                    PROFILE_ID,
                    principal(),
                    PROVIDER_REF,
                    run_id=RUN_ID,
                    attempt_number=1,
                    deadline_at=NOW + timedelta(minutes=1),
                ),
                0.2,
            )
        with pytest.raises(ConflictError):
            await asyncio.wait_for(
                sessions.begin_authentication(
                    PROFILE_ID,
                    principal(),
                    PROVIDER_REF,
                    login_url="https://example.org/login",
                ),
                0.2,
            )
        if finish == "revoke":
            await asyncio.wait_for(lifecycle.revoke(PROFILE_ID, principal(), PROVIDER_REF), 0.2)
        elif finish == "expire":
            times[0] += timedelta(minutes=2)
        if finish != "cancel":
            assert scenario.gate is not None
            scenario.gate.set()
            with pytest.raises(BrowserProviderError, match="profile_unavailable"):
                await pending
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError, BrowserProviderError):
            await pending
        await sessions.close(lease.lease_ref)
    assert all(runtime.closed for runtime in runtimes)
    # No cancelled verification can leave an admission slot occupied.
    for number in range(3):

        def next_reference(number: int = number) -> str:
            return f"opaque-after-cancel-reference-{number:08d}"

        lifecycle._reference_factory = next_reference
        profile = UUID(int=99552 + number)
        provisioned = await lifecycle.provision(profile, principal(), ("https://example.org",))
        await sessions.acquire(
            profile,
            principal(),
            provisioned.provider_ref,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=times[0] + timedelta(minutes=1),
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
