"""Milestone 24 device-capability admission, identity, and exposure."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from agent_core.api.app import DeviceRegistrationRequest
from agent_core.application.device_management import _device_view, _registration_hash
from agent_core.domain.devices import (
    Device,
    DeviceCapability,
    DeviceKind,
    DeviceRegistration,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
    device_capability_issue,
)
from agent_core.domain.errors import DeviceValidationError

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002a0")
SMS_SEND = DeviceCapability.SMS_SEND.value


def request(**updates: object) -> DeviceRegistrationRequest:
    values: dict[str, object] = {
        "client_device_id": "installation-a",
        "name": "Owner phone",
        "kind": DeviceKind.MOBILE.value,
        "platform": "ios",
        "app_bundle_id": "com.veetbot.apple",
        "push_provider": PushProvider.APNS.value,
        "push_token": "capability-request-token",
        "push_environment": PushEnvironment.SANDBOX.value,
    }
    values.update(updates)
    return DeviceRegistrationRequest.model_validate(values)


def registration(**updates: object) -> DeviceRegistration:
    values: dict[str, object] = {
        "client_device_id": "installation-a",
        "name": "Owner phone",
        "kind": DeviceKind.MOBILE,
        "platform": "ios",
        "app_bundle_id": "com.veetbot.apple",
        "push_provider": PushProvider.APNS,
        "push_token": SecretStr("capability-hash-token"),
        "push_environment": PushEnvironment.SANDBOX,
    }
    values.update(updates)
    return DeviceRegistration.model_validate(values)


def test_registration_request_accepts_a_declared_capability() -> None:
    declared = request(capabilities=(SMS_SEND,)).registration()

    assert declared.capabilities == frozenset({SMS_SEND})
    assert request().registration().capabilities == frozenset()


def test_registration_request_refuses_an_unknown_capability() -> None:
    with pytest.raises(DeviceValidationError) as refusal:
        request(capabilities=("device.email.send",)).registration()

    assert refusal.value.reason == "device.capability_unknown"


def test_surface_registration_refuses_any_capability() -> None:
    surface = request(
        kind=DeviceKind.SURFACE.value,
        push_provider=None,
        push_token=None,
        push_environment=None,
        app_bundle_id=None,
        capabilities=(SMS_SEND,),
    )

    with pytest.raises(DeviceValidationError) as refusal:
        surface.registration()

    assert refusal.value.reason == "device.surface_capability_unsupported"


def test_registration_request_refuses_a_repeated_capability() -> None:
    """A duplicate is never admitted, whichever of the two bounds catches it.

    While the vocabulary has one member the request's ``max_length`` bound
    refuses the pair first; ``device.capability_duplicate`` in ``registration()``
    is the refusal once a second capability widens that bound.
    """

    with pytest.raises(ValidationError):
        request(capabilities=(SMS_SEND, SMS_SEND))

    duplicated = DeviceRegistrationRequest.model_construct(
        **request().model_dump() | {"capabilities": (SMS_SEND, SMS_SEND)}
    )
    with pytest.raises(DeviceValidationError) as refusal:
        duplicated.registration()

    assert refusal.value.reason == "device.capability_duplicate"


def test_capability_issue_passes_a_mobile_device_and_an_undeclared_surface() -> None:
    assert (
        device_capability_issue(kind=DeviceKind.MOBILE, capabilities=frozenset({SMS_SEND})) is None
    )
    assert device_capability_issue(kind=DeviceKind.SURFACE, capabilities=frozenset()) is None


def test_registration_hash_separates_declared_capabilities() -> None:
    plain = _registration_hash(registration())
    declaring = _registration_hash(registration(capabilities=frozenset({SMS_SEND})))

    assert plain != declaring
    assert declaring == _registration_hash(registration(capabilities=frozenset({SMS_SEND})))
    assert plain == _registration_hash(registration())


def test_device_view_exposes_declared_capabilities() -> None:
    device = Device(
        id=DEVICE_ID,
        tenant_id="tenant-a",
        principal_id="principal-a",
        client_device_id="installation-a",
        name="Owner phone",
        kind=DeviceKind.MOBILE,
        platform="ios",
        app_bundle_id="com.veetbot.apple",
        push_provider=PushProvider.APNS,
        push_token=SecretStr("capability-view-token"),
        push_environment=PushEnvironment.SANDBOX,
        push_token_updated_at=NOW,
        muted_kinds=frozenset(),
        capabilities=frozenset({SMS_SEND}),
        status=DeviceStatus.ACTIVE,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )

    view = _device_view(device)

    assert view.capabilities == frozenset({SMS_SEND})
    assert view.model_dump(mode="json")["capabilities"] == [SMS_SEND]
