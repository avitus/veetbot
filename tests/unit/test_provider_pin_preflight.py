"""Registry compatibility must be checked against saved identities, not policy."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
import yaml

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.config import PACKAGE_ROOT
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from scripts import check_provider_pins as preflight
from scripts.check_provider_pins import incompatible_provider_pins
from tests.contract.support import NOW, RUN_ID
from tests.integration.m2_support import memory_settings


def router(overlay: Path | None = None) -> StaticModelRouter:
    return StaticModelRouter(
        ProviderRegistry.load(
            PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=overlay
        ),
        FixedClock(NOW),
    )


@pytest.mark.parametrize("change", ["cache", "pricing", "policy", "catalog", "disabled"])
async def test_active_pins_reject_every_kind_of_registry_drift(tmp_path: Path, change: str) -> None:
    before = router()
    pin = before.pin(RUN_ID, await before.resolve("astra", tenant_id="owner"))
    document = "providers/openai.yaml"
    if change in {"policy", "disabled"}:
        document = "policies.yaml"
    elif change == "catalog":
        document = "catalog.yaml"
    value = yaml.safe_load((PACKAGE_ROOT / "models" / document).read_text())
    if change == "cache":
        value["capabilities"]["explicit_cache_control"] = False
        value["limits"]["max_cache_breakpoints"] = 0
    elif change == "pricing":
        value["models"][0]["pricing"]["input_per_mtok"] = "99"
    elif change == "policy":
        value["model_policies"]["astra"] = value["model_policies"]["fable"]
    elif change == "disabled":
        value["enabled_profiles"].remove("openai")
        for name in ["astra", "balanced"]:
            value["model_policies"][name] = value["model_policies"]["fable"]
    else:
        value["entries"]["open-local-8b"]["limits"]["context_window_tokens"] = 65536
    path = tmp_path / "models" / document
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(value))
    assert await incompatible_provider_pins(router(tmp_path), [(RUN_ID, pin.model_dump())]) == [
        RUN_ID
    ]


async def test_unchanged_registry_and_empty_database_are_compatible() -> None:
    candidate = router()
    pin = candidate.pin(RUN_ID, await candidate.resolve("astra", tenant_id="owner"))
    assert (
        await incompatible_provider_pins(candidate, [(RUN_ID, pin.model_dump(mode="json"))]) == []
    )
    assert await incompatible_provider_pins(candidate, []) == []


@pytest.mark.parametrize("fault", ["malformed", "model", "provider", "run_id"])
async def test_invalid_stored_identity_fails_closed(fault: str) -> None:
    candidate = router()
    pin = candidate.pin(RUN_ID, await candidate.resolve("astra", tenant_id="owner"))
    value = pin.model_dump(mode="json")
    if fault == "malformed":
        value = {"untrusted": "do not print stored content"}
    else:
        value[fault] = str(UUID(int=404)) if fault == "run_id" else "missing"
    assert await incompatible_provider_pins(candidate, [(RUN_ID, value)]) == [RUN_ID]


@pytest.mark.parametrize("failure", [OSError, TimeoutError, RuntimeError])
async def test_probe_errors_block_deployment_without_printing_sensitive_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: type[Exception]
) -> None:
    monkeypatch.setattr(preflight, "load_settings", lambda _: memory_settings())

    async def unavailable(_: str) -> list[tuple[UUID, object]]:
        raise failure("private connection or configuration content")

    monkeypatch.setattr(preflight, "inspect_provider_pins", unavailable)
    assert await preflight._run() == 1
    output = capsys.readouterr()
    assert "could not complete" in output.err
    assert failure.__name__ in output.err
    assert "private connection" not in output.err


@pytest.mark.parametrize("compatible", [False, True])
async def test_preflight_exit_code_and_operator_remediation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], compatible: bool
) -> None:
    monkeypatch.setattr(preflight, "load_settings", lambda _: memory_settings())
    candidate = router()
    pin = candidate.pin(RUN_ID, await candidate.resolve("astra", tenant_id="owner"))
    if not compatible:
        pin.registry_version = "unavailable"

    async def inventory(_: str) -> list[tuple[UUID, object]]:
        return [(RUN_ID, pin.model_dump(mode="json"))]

    monkeypatch.setattr(preflight, "inspect_provider_pins", inventory)
    assert await preflight._run() == (0 if compatible else 1)
    output = capsys.readouterr()
    if compatible:
        assert "OK:" in output.out
    else:
        assert str(RUN_ID) in output.err
        assert "finish or cancel" in output.err
