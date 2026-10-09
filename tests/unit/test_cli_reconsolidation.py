"""CLI owner controls use the same durable operation service as HTTP."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

import agent_core.cli.main as cli_main
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_operation_cases import committed
from tests.contract.reconsolidation_surface_cases import owner, service
from tests.contract.support import memory_uow_factory

runner = CliRunner()


@pytest.fixture
def operation_id(monkeypatch: pytest.MonkeyPatch) -> UUID:
    async def prepare() -> tuple[Factory, UUID]:
        _, factory = await memory_uow_factory()
        _, _, operation = await committed(cast(Factory, factory))
        return cast(Factory, factory), operation.id

    factory, operation_id = asyncio.run(prepare())

    @asynccontextmanager
    async def fake_build(*, storage: str) -> AsyncIterator[SimpleNamespace]:
        assert storage == "postgres"
        yield SimpleNamespace(
            principal=owner(), services=SimpleNamespace(reconsolidation=service(factory))
        )

    monkeypatch.setattr(cli_main, "build", fake_build)
    return operation_id


def test_cli_operation_list_get_undo_and_retry(operation_id: UUID) -> None:
    base = ["memory", "reconsolidations"]
    result = runner.invoke(cli_main.app, [*base, "list", "--ceiling", "internal"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["items"][0]["id"] == str(operation_id)
    detail = runner.invoke(cli_main.app, [*base, "get", str(operation_id), "--ceiling", "internal"])
    assert detail.exit_code == 0 and json.loads(detail.output)["state"] == "committed"
    undo = [
        *base,
        "undo",
        str(operation_id),
        "--ceiling",
        "internal",
        "--expected-revision",
        "1",
        "--idempotency-key",
        "cli-undo",
    ]
    for _ in range(2):
        result = runner.invoke(cli_main.app, undo)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["state"] == "undone"
    undo[-1] = "different-key"
    assert runner.invoke(cli_main.app, undo).exit_code == 1


def test_cli_operation_validation_and_absence(operation_id: UUID) -> None:
    base = ["memory", "reconsolidations"]
    for args in (
        ["list"],
        ["list", "--ceiling", "internal", "--kind", "wrong"],
        ["list", "--ceiling", "internal", "--cursor", "garbage"],
        ["get", str(UUID(int=999)), "--ceiling", "internal"],
        ["get", str(operation_id), "--ceiling", "public"],
        ["undo", str(operation_id), "--ceiling", "internal", "--expected-revision", "1"],
    ):
        result = runner.invoke(cli_main.app, [*base, *args])
        assert result.exit_code != 0
    # No validation failure above mutated the real operation.
    result = runner.invoke(cli_main.app, [*base, "get", str(operation_id), "--ceiling", "internal"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "committed"
