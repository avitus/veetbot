"""Milestone 34 owner records gates."""

from zoneinfo import ZoneInfo

import pytest

from agent_core.application.records import RecordsService
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import FakeModelScript
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.tools.messages import TOOL_MESSAGES
from agent_core.tools.records import (
    RecordsAppendTool,
    RecordsCreateSeriesTool,
    RecordsDeleteTool,
    RecordsListTool,
    RecordsQueryTool,
)
from agent_core.tools.registry import BUILTIN_DOMAINS, validate_registration
from tests.contract.support import memory_uow_factory

TOOL_NAMES = (
    "records.list",
    "records.create_series",
    "records.append",
    "records.query",
    "records.delete",
)
REASON_CODES = (
    "series_unknown",
    "series_exists",
    "component_mismatch",
    "value_out_of_range",
    "observed_in_future",
    "series_limit",
    "entry_limit",
    "window_too_large",
    "secret_detected",
    "name_rejected",
)


def _environment(flag: str | None) -> dict[str, str]:
    values = {
        "DATABASE_URL": "postgresql+asyncpg://localhost/records-m34",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "SANDBOX_MECHANISM": "fake",
    }
    if flag is not None:
        values["AGENT_RECORDS_ENABLED"] = flag
    return values


async def _registered(flag: str | None) -> set[str]:
    async with build(
        settings=load_settings(_environment(flag)), script=FakeModelScript(turns=[])
    ) as composition:
        found: set[str] = set()
        for name in TOOL_NAMES:
            try:
                composition.tool_pipeline._registry.get(name)
            except NotFoundError:
                continue
            found.add(name)
        return found


async def test_registration_under_flag() -> None:
    clock, factory = await memory_uow_factory()
    service = RecordsService(factory, clock)
    zone = ZoneInfo("UTC")
    tools = [
        RecordsListTool(service, zone),
        RecordsCreateSeriesTool(service, zone),
        RecordsAppendTool(service, zone),
        RecordsQueryTool(service, zone),
        RecordsDeleteTool(service, zone),
    ]
    assert [tool.spec.name for tool in tools] == list(TOOL_NAMES)
    for tool in tools:
        assert validate_registration(tool.spec) is tool.spec
    assert "records" in BUILTIN_DOMAINS
    assert {"records.read", "records.write"} <= PLATFORM_SCOPES
    for code in REASON_CODES:
        assert f"records.{code}" in TOOL_MESSAGES
    assert await _registered(None) == set()
    assert await _registered("0") == set()
    assert await _registered("1") == set(TOOL_NAMES)


@pytest.mark.parametrize("value", ["1", "0"])
def test_flag_parses(value: str) -> None:
    assert load_settings(_environment(value)).records_enabled is (value == "1")
