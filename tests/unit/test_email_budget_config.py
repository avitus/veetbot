"""Operator email allowances propagate to atomic application admission."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from agent_core.api import create_app
from agent_core.application.email import save_value
from agent_core.bootstrap import build
from agent_core.config import ConfigurationError, load_config_document, load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailBudgetLimits, EmailTask
from agent_core.domain.runs import RunLimits
from tests.integration.m2_support import memory_settings
from tests.unit.test_config import base_environment


def write_email_overlay(tmp_path: Path, document: str) -> None:
    """Install a minimal operator overlay in the isolated test directory."""
    path = tmp_path / "runtime" / "limits.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(document, encoding="utf-8")


def test_email_aggregate_defaults_are_versioned_configuration() -> None:
    """Keep the approved default allowances in versioned configuration."""
    settings = load_settings(base_environment())
    document = load_config_document(settings, "runtime/limits.yaml")
    assert document.get("email") == {"daily_cost": 20, "monthly_cost": 200}


@pytest.mark.parametrize("key", ["daily_cost", "monthly_cost"])
@pytest.mark.parametrize("value", ["0", "-1", ".nan", ".inf", "-.inf", "true", "null", '"25"'])
def test_email_aggregate_configuration_rejects_invalid_amounts(
    tmp_path: Path, key: str, value: str
) -> None:
    """Reject invalid allowances before composing an application."""
    write_email_overlay(tmp_path, f"email:\n  {key}: {value}\n")
    with pytest.raises(ConfigurationError, match=rf"email\.{key}"):
        load_settings({**base_environment(), "AGENT_CONFIG_DIR": str(tmp_path)})


@pytest.mark.parametrize("key", ["daily_cost", "monthly_cost"])
@pytest.mark.parametrize("amount", ["0.001", "0.01", "NaN", "Infinity", "-Infinity"])
def test_email_budget_domain_requires_finite_allowances_of_at_least_one_cent(
    key: str, amount: str
) -> None:
    """Direct domain construction enforces the same minimum as configuration."""
    values = {"daily_cost": Decimal("20"), "monthly_cost": Decimal("200")}
    values[key] = Decimal(amount)
    if amount == "0.01":
        limits = EmailBudgetLimits.model_validate(values)
        assert getattr(limits, key) == Decimal("0.01")
    else:
        with pytest.raises(ValidationError, match=key):
            EmailBudgetLimits.model_validate(values)


@pytest.mark.parametrize(
    ("daily", "monthly", "cost", "age", "per_run", "expected"),
    [
        ("40.25", "400.25", "39.25", 0, "1", 200),
        ("40.25", "400.25", "39.26", 0, "1", 402),
        ("40.25", "400.25", "399.25", 2, "1", 200),
        ("40.25", "400.25", "399.26", 2, "1", 402),
        ("20.5", "200", "20.25", 0, "0.25", 200),
        ("0.5", "200", "0", 0, "1", 402),
    ],
)
async def test_configured_email_ceilings_govern_refresh_admission(
    tmp_path: Path,
    daily: str,
    monthly: str,
    cost: str,
    age: int,
    per_run: str,
    expected: int,
) -> None:
    """Exercise configured aggregate boundaries and inherited slice limits over HTTP."""
    write_email_overlay(tmp_path, f"email:\n  daily_cost: {daily}\n  monthly_cost: {monthly}\n")
    settings = replace(memory_settings(), config_dir=tmp_path, email_mode_enabled=True)
    principal = Principal(
        tenant_id="local",
        principal_id="owner",
        scopes={"email.read", "email.write", "run.write", "session.write", "mcp.gmail_read.use"},
    )
    async with build(
        settings=settings,
        storage="memory",
        principal=principal,
        limits=RunLimits(max_cost=Decimal(per_run)),
    ) as composition:
        service = composition.services.email
        service.account_ids = ("work",)
        service.account_servers = {"work": {"read": "gmail_read", "send": "gmail_send"}}

        async def leave_queued(run_id: UUID) -> None:
            """Preserve the admitted reservation without starting provider work."""
            pass

        service.dispatch = leave_queued
        prior = EmailTask(
            id=uuid4(),
            run_id=uuid4(),
            session_id=uuid4(),
            kind="draft",
            account_ids=["personal"],
            created_at=service.clock.now() - timedelta(days=age),
            reservation=Decimal(cost),
            settled_cost=Decimal(cost),
        )
        async with composition.uow_factory() as uow, uow.email.lock(principal):
            await save_value(
                uow.email, principal, "task", str(prior.run_id), prior, service.clock.now()
            )
        app = create_app(
            composition.services,
            composition.settings,
            principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client:
            response = await client.post("/v1/email/refresh")
        assert response.status_code == expected, response.text
        if expected == 200:
            operation = response.json()
            task = await service.get_task(principal, UUID(operation["run_id"]))
            assert task is not None
            assert task.reservation == Decimal(per_run)
            async with composition.uow_factory() as uow:
                run = await uow.runs.get(task.run_id, principal)
            assert run.limits.max_cost == Decimal(per_run)
