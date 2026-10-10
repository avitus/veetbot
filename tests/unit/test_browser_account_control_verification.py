"""Operator-owned account labels must identify the observed account control."""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.browser_control_plane.configuration import load_profile_service_settings
from agent_core.browser_control_plane.models import ProfileStoreIntegrityError
from agent_core.browser_control_plane.sessions import DeviceSessionRejected
from agent_core.browser_control_plane.verification import BrowserVerificationCatalog
from agent_core.domain.browser import (
    BrowserAuthenticationStatus,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserProviderError,
)
from tests.contract.support import NOW, principal
from tests.contract.test_hosted_profile_session_service_contract import (
    PROFILE_ID,
    PROVIDER_REF,
    RUN_ID,
    FakeSessionRuntime,
    HandoffScenario,
    begin_device,
    device_handoff,
    provision,
    record_writes,
    services,
)
from tests.contract.test_profile_service_configuration_contract import (
    environment,
    private_file,
)


def catalog() -> BrowserVerificationCatalog:
    return BrowserVerificationCatalog.model_validate(
        {
            "sites": [
                {
                    "id": "fixture-account-menu",
                    "version": 1,
                    "profile_id": str(PROFILE_ID),
                    "origin": "https://example.org",
                    "protected_path": "/home",
                    "ready": {"kind": "text", "text": "Your Home Timeline"},
                    "account": {
                        "kind": "control",
                        "role": "button",
                        "name": "Account menu",
                        "text": "Fixture Owner @fixture_owner",
                    },
                }
            ]
        }
    )


def observed() -> BrowserObservation:
    return BrowserObservation(
        url="https://example.org/home",
        revision="fresh",
        text="Your Home Timeline\nFixture Owner @fixture_owner",
        elements=(BrowserElement(ref="menu", role="button", name="Account menu"),),
    )


def private_facts() -> BrowserObservationFacts:
    return BrowserObservationFacts(
        revision="fresh",
        elements={
            "menu": BrowserElementFacts(
                field_kind=BrowserFieldKind.NONE,
                labels={BrowserLabelSource.VISIBLE_TEXT: "Fixture Owner\n@fixture_owner"},
            )
        },
    )


def test_exact_account_menu_uses_private_visible_label() -> None:
    definition = catalog().sites[0]
    assert definition.confirms(observed(), private_facts())
    assert "fixture_owner" not in repr(definition)


@pytest.mark.parametrize(
    "change",
    [
        {"role": "textbox"},
        {"text": " "},
        {"name": " "},
        {"text": "a" * 257},
        {"script": "return true"},
        {"selector": "#account"},
    ],
)
def test_control_definition_is_bounded_and_declarative(change: dict[str, str]) -> None:
    data = catalog().model_dump(mode="json")
    data["sites"][0]["account"].update(change)
    with pytest.raises(ValidationError):
        BrowserVerificationCatalog.model_validate(data)


def test_profile_service_loads_control_catalog_only_from_private_file(tmp_path: Path) -> None:
    values = environment(tmp_path)
    path = tmp_path / "verification.json"
    private_file(path, catalog().model_dump_json())
    values["BROWSER_PROFILE_VERIFICATION_FILE"] = str(path)
    assert load_profile_service_settings(values).verification == catalog()
    path.chmod(0o644)
    with pytest.raises(ProfileStoreIntegrityError):
        load_profile_service_settings(values)


def test_label_equal_to_accessible_name_uses_runtime_omission_contract() -> None:
    data = catalog().model_dump(mode="json")
    data["sites"][0]["account"]["name"] = "Fixture Owner @fixture_owner"
    definition = BrowserVerificationCatalog.model_validate(data).sites[0]
    observation = observed().model_copy(
        update={
            "elements": (
                BrowserElement(ref="menu", role="button", name="Fixture Owner @fixture_owner"),
            )
        }
    )
    facts = private_facts().model_copy(
        update={
            "elements": {
                "menu": BrowserElementFacts(field_kind=BrowserFieldKind.NONE),
            }
        }
    )
    assert definition.confirms(observation, facts)


@pytest.mark.parametrize(
    "case",
    [
        "missing_facts",
        "missing_control_facts",
        "stale",
        "truncated",
        "editable",
        "wrong_account",
        "duplicate",
        "wrong_role",
        "wrong_name",
        "missing_control",
        "wrong_page",
        "not_ready",
        "interrupted",
        "missing_label",
    ],
)
def test_incomplete_or_unrelated_account_evidence_cannot_verify(case: str) -> None:
    observation = observed()
    complete = private_facts()
    facts: BrowserObservationFacts | None = complete
    element = complete.elements["menu"]
    if case == "missing_facts":
        facts = None
    elif case == "missing_control_facts":
        facts = complete.model_copy(update={"elements": {}})
    elif case == "stale":
        facts = complete.model_copy(update={"revision": "old"})
    elif case in {"truncated", "editable", "wrong_account", "missing_label"}:
        changes_by_case: dict[str, dict[str, object]] = {
            "truncated": {"labels_truncated": True},
            "editable": {"field_kind": BrowserFieldKind.EDITABLE},
            "wrong_account": {"labels": {BrowserLabelSource.VISIBLE_TEXT: "Other @other"}},
            "missing_label": {"labels": {}},
        }
        facts = complete.model_copy(
            update={
                "elements": {"menu": element.model_copy(update=changes_by_case[case])},
            }
        )
    elif case == "duplicate":
        observation = observation.model_copy(
            update={
                "elements": (
                    *observation.elements,
                    observation.elements[0].model_copy(update={"ref": "other"}),
                )
            }
        )
    elif case in {"wrong_role", "wrong_name"}:
        change = {"role": "textbox"} if case == "wrong_role" else {"name": "Post"}
        observation = observation.model_copy(
            update={"elements": (observation.elements[0].model_copy(update=change),)}
        )
    elif case == "missing_control":
        observation = observation.model_copy(update={"elements": ()})
    elif case == "wrong_page":
        observation = observation.model_copy(update={"url": "https://example.org/public"})
    elif case == "not_ready":
        observation = observation.model_copy(update={"text": "Loading"})
    else:
        observation = observation.model_copy(update={"interruption": "needs_user"})
    assert not catalog().sites[0].confirms(observation, facts)


@pytest.mark.parametrize("mode", ["device", "remote", "lease"])
@pytest.mark.parametrize("case", ["valid", "wrong_account", "missing_facts"])
async def test_service_checks_control_evidence_on_every_entry_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    case: str,
) -> None:
    active_case = "valid" if mode == "lease" else case

    def facts(self: FakeSessionRuntime, revision: str) -> BrowserObservationFacts | None:
        del self
        if active_case == "missing_facts":
            return None
        value = private_facts().model_copy(update={"revision": revision})
        if active_case == "wrong_account":
            return value.model_copy(
                update={
                    "elements": {
                        "menu": BrowserElementFacts(
                            field_kind=BrowserFieldKind.NONE,
                            labels={BrowserLabelSource.VISIBLE_TEXT: "Other @other"},
                        )
                    }
                }
            )
        return value

    monkeypatch.setattr(FakeSessionRuntime, "facts", facts)
    lifecycle, sessions, runtimes, _times = services(
        tmp_path,
        scenario=HandoffScenario(with_observation=observed()),
        verification=catalog(),
    )
    await provision(lifecycle)
    if mode == "lease":
        ceremony_id, capability = await begin_device(sessions)
        await sessions.accept_device_session(ceremony_id, capability, device_handoff())
        active_case = case
    writes = record_writes(sessions)
    valid = case == "valid"
    if mode == "lease":

        async def acquire() -> None:
            lease = await sessions.acquire(
                PROFILE_ID,
                principal(),
                PROVIDER_REF,
                run_id=RUN_ID,
                attempt_number=1,
                deadline_at=NOW + timedelta(minutes=1),
            )
            await sessions.close(lease.lease_ref)

        if valid:
            await acquire()
        else:
            with pytest.raises(BrowserProviderError, match="authentication_required"):
                await acquire()
        assert all(runtime.closed for runtime in runtimes)
        # Closing a valid lease reseals its session; a rejected lease writes nothing.
        assert len(writes) == int(valid)
        assert all(not runtime.actions for runtime in runtimes)
        return
    if mode == "device":
        ceremony_id, capability = await begin_device(sessions)
        if valid:
            await sessions.accept_device_session(ceremony_id, capability, device_handoff())
        else:
            with pytest.raises(DeviceSessionRejected):
                await sessions.accept_device_session(ceremony_id, capability, device_handoff())
    else:
        ceremony = await sessions.begin_authentication(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            login_url="https://example.org/login",
        )
        ceremony_id = ceremony.id
        await sessions.refresh_authentication(ceremony_id, principal())
    status = await sessions.authentication_status(ceremony_id, principal())
    assert (status.status is BrowserAuthenticationStatus.READY) is valid
    assert len(writes) == int(valid)


@pytest.mark.parametrize("mode", ["device", "remote"])
async def test_account_menu_on_public_page_cannot_confirm_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(FakeSessionRuntime, "facts", lambda self, revision: private_facts())
    lifecycle, sessions, _runtimes, _times = services(
        tmp_path,
        verification=catalog(),
        scenario=HandoffScenario(
            with_observation=observed(),
            without_observation=observed(),
            without_session=None,
        ),
    )
    await provision(lifecycle)
    writes = record_writes(sessions)
    if mode == "device":
        ceremony_id, capability = await begin_device(sessions)
        with pytest.raises(DeviceSessionRejected):
            await sessions.accept_device_session(ceremony_id, capability, device_handoff())
    else:
        ceremony = await sessions.begin_authentication(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            login_url="https://example.org/login",
        )
        ceremony_id = ceremony.id
        await sessions.refresh_authentication(ceremony_id, principal())
    status = await sessions.authentication_status(ceremony_id, principal())
    assert status.status is not BrowserAuthenticationStatus.READY
    assert not writes
