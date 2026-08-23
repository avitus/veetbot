"""Executable adapter-contract coverage for notification ports."""

from __future__ import annotations

import inspect
from collections.abc import Callable

from agent_core.adapters.apns import APNsPushTransport
from agent_core.adapters.persistence.notifications import (
    InMemoryDeviceRegistrationIdempotencyRepository,
    InMemoryDeviceRegistry,
    InMemoryNotificationOutbox,
    PostgresDeviceRegistrationIdempotencyRepository,
    PostgresDeviceRegistry,
    PostgresNotificationOutbox,
)
from agent_core.adapters.push import FakePushTransport
from agent_core.ports.devices import (
    DeviceRegistrationIdempotencyRepository,
    DeviceRegistry,
)
from agent_core.ports.notifications import NotificationOutbox, PushTransport
from tests.contract import (
    test_device_registration_idempotency_repository_contract as device_idempotency_contract,
)
from tests.contract import test_device_registry_contract as device_contract
from tests.contract import test_notification_outbox_contract as outbox_contract
from tests.contract import test_push_transport_contract as push_contract
from tests.integration import test_notification_persistence_m12 as postgres_contract


def _protocol_methods(protocol: type[object]) -> set[str]:
    return {
        name
        for name, member in vars(protocol).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def test_notification_ports_have_executable_contracts_for_every_adapter() -> None:
    manifests: tuple[
        tuple[type[object], tuple[type[object], ...], tuple[Callable[..., object], ...]], ...
    ] = (
        (
            DeviceRegistry,
            (InMemoryDeviceRegistry, PostgresDeviceRegistry),
            (
                device_contract.test_device_registration_is_idempotent_and_principal_scoped,
                device_contract.test_device_tokens_move_and_lifecycle_removes_targets,
                device_contract.test_device_listing_is_stable,
                postgres_contract.test_postgres_device_registration_is_idempotent_and_principal_scoped,
                postgres_contract.test_postgres_live_push_token_moves_to_new_installation,
                postgres_contract.test_postgres_device_listing_uses_stable_cursor,
            ),
        ),
        (
            DeviceRegistrationIdempotencyRepository,
            (
                InMemoryDeviceRegistrationIdempotencyRepository,
                PostgresDeviceRegistrationIdempotencyRepository,
            ),
            (
                device_idempotency_contract.test_in_memory_device_registration_idempotency_repository_satisfies_contract,
                postgres_contract.test_postgres_device_registration_idempotency_satisfies_shared_contract,
            ),
        ),
        (
            NotificationOutbox,
            (InMemoryNotificationOutbox, PostgresNotificationOutbox),
            (
                outbox_contract.test_notification_enqueue_deduplicates_and_lists_by_principal,
                outbox_contract.test_notification_claim_settle_and_pagination,
                outbox_contract.test_notification_delivery_attempt_is_unique,
                postgres_contract.test_postgres_notification_outbox_satisfies_shared_contracts,
            ),
        ),
        (
            PushTransport,
            (FakePushTransport, APNsPushTransport),
            (
                push_contract.test_fake_push_transport_satisfies_shared_contract,
                push_contract.test_apns_push_transport_satisfies_shared_contract,
            ),
        ),
    )

    for protocol, adapters, contract_tests in manifests:
        methods = _protocol_methods(protocol)
        assert methods
        for adapter in adapters:
            assert methods <= set(vars(adapter)), f"{adapter.__name__} misses {methods}"
        assert contract_tests
        assert all(inspect.iscoroutinefunction(test) for test in contract_tests)
