"""The isolated service refuses a grant-constrained act before dispatch (ADR-0129 R2).

A constraint only narrows what an act may do. The service checks its expiry
and its origins against the lease inside the lease lock, after the sequence
check, and hands it to the runtime for the live recheck. Every refusal leaves
the action sequence where it was.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserDispatchConstraint,
    BrowserProviderError,
)
from agent_core.domain.credentials import SecretValue
from tests.contract.support import NOW, principal
from tests.contract.test_hosted_profile_session_service_contract import (
    PROFILE_ID,
    PROVIDER_REF,
    RUN_ID,
    FakeSessionRuntime,
    provision,
    services,
)

SERVICE_AUTH = "synthetic-dispatch-constraint-auth"
CLICK = BrowserAction(
    kind=BrowserActionKind.CLICK, expected_revision="revision-1", ref="revision-1:0"
)


def task_constraint(**overrides: Any) -> BrowserDispatchConstraint:
    fields: dict[str, Any] = {
        "grant_kind": "task",
        "origins": ("https://example.org",),
        "path_prefix": "/lesson",
        "not_after": NOW + timedelta(minutes=10),
        "consequence_ceiling": "unknown",
        "max_text_characters": 256,
    }
    fields.update(overrides)
    return BrowserDispatchConstraint.model_validate(fields)


async def leased(
    tmp_path: Path,
) -> tuple[HostedProfileSessionService, FakeSessionRuntime, str]:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=10),
    )
    return sessions, runtimes[0], lease.lease_ref


async def test_expired_constraint_is_refused_before_the_runtime_acts(tmp_path: Path) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)

    with pytest.raises(BrowserProviderError) as raised:
        await sessions.act(lease_ref, CLICK, sequence=1, constraint=task_constraint(not_after=NOW))

    assert raised.value.reason_code == "tool.browser.grant_not_applicable"
    assert raised.value.retryable is False
    assert runtime.actions == []
    assert runtime.constrained == []


@pytest.mark.parametrize(
    "constraint",
    [
        pytest.param(task_constraint(origins=("https://other.example",)), id="task-elsewhere"),
        pytest.param(
            BrowserDispatchConstraint(
                grant_kind="standing",
                origins=("https://example.org", "https://other.example"),
                not_after=NOW + timedelta(minutes=10),
                consequence_ceiling="routine",
                max_text_characters=None,
            ),
            id="standing-one-origin-elsewhere",
        ),
    ],
)
async def test_constraint_origins_outside_the_lease_are_refused(
    tmp_path: Path, constraint: BrowserDispatchConstraint
) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)

    with pytest.raises(BrowserProviderError) as raised:
        await sessions.act(lease_ref, CLICK, sequence=1, constraint=constraint)

    assert raised.value.reason_code == "tool.browser.grant_not_applicable"
    assert runtime.actions == []


async def test_no_constraint_behaves_as_before(tmp_path: Path) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)

    acted = await sessions.act(lease_ref, CLICK, sequence=1)

    assert acted.revision == "revision-2"
    assert runtime.actions == [CLICK]
    assert runtime.constrained == []


async def test_a_constraint_reaches_the_runtime_with_the_service_clock(tmp_path: Path) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)
    constraint = task_constraint()

    await sessions.act(lease_ref, CLICK, sequence=1, constraint=constraint)

    assert runtime.constrained == [(constraint, NOW)]
    assert runtime.actions == [CLICK]


async def test_refusal_leaves_the_sequence_unchanged(tmp_path: Path) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)

    with pytest.raises(BrowserProviderError):
        await sessions.act(lease_ref, CLICK, sequence=1, constraint=task_constraint(not_after=NOW))
    acted = await sessions.act(lease_ref, CLICK, sequence=1)
    reattached = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=10),
    )

    assert acted.revision == "revision-2"
    assert runtime.actions == [CLICK]
    assert reattached.sequence == 1


def _app(sessions: HostedProfileSessionService) -> httpx.AsyncClient:
    lifecycle = HostedProfileLifecycleService(
        sessions._store,  # noqa: SLF001 - the lifecycle shares the service's store
        reference_factory=lambda: PROVIDER_REF,
        invalidate_profile=sessions.invalidate_profile,
    )
    app = create_profile_service_app(lifecycle, SecretValue(SERVICE_AUTH), sessions=sessions)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://service.test"
    )


def _act_request(
    lease_ref: str, *, sequence: int, constraint: dict[str, Any] | None, auth: str = SERVICE_AUTH
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "lease_ref": lease_ref,
        "action": CLICK.model_dump(mode="json", exclude_none=True),
        "sequence": sequence,
    }
    if constraint is not None:
        body["constraint"] = constraint
    digest = hashlib.sha256(lease_ref.encode()).hexdigest()[:24]
    return {
        "url": "/v1/browser-sessions:act",
        "headers": {
            "Authorization": f"Bearer {auth}",
            "Idempotency-Key": f"browser-session:{digest}:act:{sequence}",
        },
        "json": body,
    }


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param({**task_constraint().model_dump(mode="json"), "grant_kind": "standing"}),
        pytest.param({**task_constraint().model_dump(mode="json"), "path_prefix": "/a/b"}),
        pytest.param({**task_constraint().model_dump(mode="json"), "extra": True}),
        pytest.param(
            {**task_constraint().model_dump(mode="json"), "not_after": "2026-07-25T13:10:00"}
        ),
    ],
)
async def test_malformed_constraint_is_400_and_dispatches_nothing(
    tmp_path: Path, malformed: dict[str, Any]
) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)
    async with _app(sessions) as http:
        rejected = await http.post(**_act_request(lease_ref, sequence=1, constraint=malformed))
        accepted = await http.post(**_act_request(lease_ref, sequence=1, constraint=None))

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "invalid_request"
    assert accepted.status_code == 200
    assert runtime.actions == [CLICK]


async def test_replay_of_an_applied_sequence_is_409_with_or_without_a_constraint(
    tmp_path: Path,
) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)
    constraint = task_constraint().model_dump(mode="json")
    async with _app(sessions) as http:
        applied = await http.post(**_act_request(lease_ref, sequence=1, constraint=constraint))
        replayed = await http.post(**_act_request(lease_ref, sequence=1, constraint=constraint))
        bare = await http.post(**_act_request(lease_ref, sequence=1, constraint=None))

    assert applied.status_code == 200
    assert [replayed.status_code, bare.status_code] == [409, 409]
    assert replayed.json()["error"]["code"] == "conflict"
    assert runtime.actions == [CLICK]


async def test_a_refusal_is_a_409_with_the_grant_code(tmp_path: Path) -> None:
    sessions, runtime, lease_ref = await leased(tmp_path)
    expired = task_constraint(not_after=NOW).model_dump(mode="json")
    async with _app(sessions) as http:
        refused = await http.post(**_act_request(lease_ref, sequence=1, constraint=expired))

    assert refused.status_code == 409
    assert refused.json() == {
        "error": {
            "code": "tool.browser.grant_not_applicable",
            "message": "browser operation rejected",
        }
    }
    assert runtime.actions == []


async def test_constraint_on_a_foreign_lease_or_wrong_credential_is_refused_and_dispatches_nothing(
    tmp_path: Path,
) -> None:
    """Build plan N15: a constraint never widens who may act on a lease."""

    sessions, runtime, _lease_ref = await leased(tmp_path)
    constraint = task_constraint().model_dump(mode="json")
    async with _app(sessions) as http:
        foreign = await http.post(**_act_request("f" * 43, sequence=1, constraint=constraint))
        unauthenticated = await http.post(
            **_act_request(_lease_ref, sequence=1, constraint=constraint, auth="w" * 36)
        )

    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "tool.browser.profile_unavailable"
    assert unauthenticated.status_code == 401
    assert runtime.actions == []
    assert runtime.constrained == []
