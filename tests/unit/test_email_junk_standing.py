"""Owner-authorized standing work-inbox filtering; packets here are synthetic only."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.email_junk import (
    JUNK_STANDING_POLICY,
    EmailJunkMode,
    JunkAuthorization,
    JunkPilot,
    JunkStandingAuthorization,
)
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from agent_core.runtime.email_junk import EmailJunkFilter
from tests.gates.test_email_runtime_m26 import _assessment_turn
from tests.unit.test_config import base_environment
from tests.unit.test_email_junk_pilot import (
    pilot_decision,
    pilot_packet,
    pilot_settings,
    work_factory,
)


@pytest.mark.parametrize("mode", ["pilot", "owner_filter"])
def test_pilot_and_standing_modes_are_explicit_and_default_stays_off(mode: str) -> None:
    """Both approved modes are exposed explicitly without changing default activation."""
    assert load_settings(base_environment()).email_junk_filter_mode.value == "off"
    assert (
        load_settings(
            {**base_environment(), "AGENT_EMAIL_JUNK_FILTER_MODE": mode}
        ).email_junk_filter_mode.value
        == mode
    )


def standing_packet() -> dict[str, Any]:
    """Synthetic authorization well beyond the old seven-day lifetime."""
    initial = pilot_packet()
    return {
        "policy": JUNK_STANDING_POLICY,
        "model": initial["model"],
        "tenant_id": initial["tenant_id"],
        "principal_id": initial["principal_id"],
        "account_id": "work",
        "authorized_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
        "sample": "owner_reviewed_complete_pilot",
        "initial_observations": initial["observations"],
        "bypass_observations": [
            {
                "fingerprint": hashlib.sha256(f"synthetic-standing-{n}".encode()).hexdigest(),
                "category": "bulk",
                "label": "junk",
                "source": "owner_review",
            }
            for n in range(198)
        ],
    }


def standing_settings(tmp_path: Path) -> Any:
    """Reuse work-account fixtures, with a separate synthetic standing packet."""
    settings = pilot_settings(tmp_path)
    settings.email_junk_filter_evidence.write_text(json.dumps(standing_packet()))
    return replace(settings, email_junk_filter_mode=EmailJunkMode.OWNER_FILTER)


SETTINGS_FACTORIES = [
    pytest.param(pilot_settings, id="pilot"),
    pytest.param(standing_settings, id="standing"),
]


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "policy",
        "model",
        "owner",
        "tenant",
        "future",
        "few_bypasses",
        "false_positive",
        "duplicate",
        "keep_bypass",
        "few_initial",
        "initial_false_positive",
        "initial_duplicate",
        "no_legitimate_keeps",
        "few_initial_junk",
    ],
)
def test_standing_evidence_and_scope(mutation: str) -> None:
    """Standing permission requires both complete review cohorts, not an expired pilot."""
    data = standing_packet()
    if mutation in {"policy", "model"}:
        data[mutation] = "wrong"
    elif mutation in {"owner", "tenant"}:
        data["principal_id" if mutation == "owner" else "tenant_id"] = "other"
    elif mutation == "future":
        data["authorized_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif mutation == "few_bypasses":
        data["bypass_observations"].pop()
    elif mutation == "false_positive":
        data["bypass_observations"][0]["label"] = "keep"
    elif mutation == "duplicate":
        data["bypass_observations"][1] = data["bypass_observations"][0]
    elif mutation == "keep_bypass":
        data["bypass_observations"][0]["category"] = "keep"
    elif mutation == "few_initial":
        data["initial_observations"].pop()
    elif mutation == "initial_false_positive":
        data["initial_observations"][0]["label"] = "keep"
    elif mutation == "initial_duplicate":
        data["initial_observations"][1] = data["initial_observations"][0]
    elif mutation == "no_legitimate_keeps":
        for row in data["initial_observations"]:
            row["label"] = "junk"
    elif mutation == "few_initial_junk":
        for row in data["initial_observations"][:11]:
            row["category"] = "keep"
    packet = JunkStandingAuthorization.model_validate(data)
    owner = Principal(tenant_id="local", principal_id="local-user")
    assert packet.admitted(owner, "work", datetime.now(UTC)) is (mutation == "none")
    assert not packet.admitted(owner, "personal", datetime.now(UTC))


def test_standing_and_pilot_authorizations_are_not_interchangeable() -> None:
    """Neither mode silently upgrades or downgrades a different authorization type."""
    pilot = JunkPilot.model_validate(pilot_packet())
    standing = JunkStandingAuthorization.model_validate(standing_packet())
    cases: list[tuple[EmailJunkMode, JunkAuthorization | None]] = [
        (EmailJunkMode.PILOT, standing),
        (EmailJunkMode.OWNER_FILTER, pilot),
        (EmailJunkMode.OWNER_FILTER, None),
    ]
    for mode, packet in cases:
        filter_ = EmailJunkFilter(FakeJudgmentProvider([]), mode, pilot=packet)
        assert not filter_.enabled


@pytest.mark.parametrize("failure", ["missing", "invalid", "pilot", "owner"])
async def test_standing_invalid_evidence_makes_no_paid_calls(tmp_path: Path, failure: str) -> None:
    """Invalid standing evidence fails closed rather than running shadow classification."""
    settings = standing_settings(tmp_path)
    path = settings.email_junk_filter_evidence
    if failure == "missing":
        path.unlink()
    elif failure == "invalid":
        path.write_text("invalid")
    else:
        data = pilot_packet() if failure == "pilot" else standing_packet()
        if failure == "owner":
            data["principal_id"] = "other"
        path.write_text(json.dumps(data))
    judge = FakeJudgmentProvider([])
    async with build(
        settings=settings,
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.usage.model_calls == 1 and not judge.requests


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("settings_factory", SETTINGS_FACTORIES)
async def test_pilot_and_standing_use_reviewed_choice_and_preserve_keep(
    tmp_path: Path, settings_factory: Callable[[Path], Any], keep: bool
) -> None:
    """Only the scoped filter bypasses a valid raw bulk choice below the general cutoff."""
    judge = FakeJudgmentProvider([pilot_decision(keep=keep)])
    async with build(
        settings=settings_factory(tmp_path),
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert len(judge.requests) == 1
        assert run.usage.model_calls == int(keep)
        async with app.uow_factory() as uow:
            [assessment] = await uow.email.list(app.principal, "assessment")
            assert not await uow.email.list(app.principal, "draft")
        assert bool(assessment.payload.get("junk_bypassed")) is not keep


@pytest.mark.parametrize(
    ("settings_factory", "stop"),
    [
        pytest.param(pilot_settings, "expiry", id="pilot-expiry"),
        pytest.param(pilot_settings, "off", id="pilot-off"),
        pytest.param(pilot_settings, "evidence", id="pilot-evidence"),
        pytest.param(pilot_settings, "important", id="pilot-important"),
        pytest.param(standing_settings, "off", id="standing-off"),
        pytest.param(standing_settings, "evidence", id="standing-evidence"),
        pytest.param(standing_settings, "important", id="standing-important"),
    ],
)
async def test_stopping_pilot_or_standing_filter_restores_cached_bypass(
    tmp_path: Path, settings_factory: Callable[[Path], Any], stop: str
) -> None:
    """The bounded cached-mail refresh restores ordinary assessment after revocation."""
    from uuid import UUID

    from agent_core.adapters.determinism import FixedClock

    settings = settings_factory(tmp_path)
    now = datetime.now(UTC)
    if settings_factory is pilot_settings:
        data = pilot_packet(now)
        data["expires_at"] = (now + timedelta(minutes=2)).isoformat()
        settings.email_junk_filter_evidence.write_text(json.dumps(data))
    clock = FixedClock(now)
    judge = FakeJudgmentProvider([pilot_decision()])
    async with build(
        settings=settings,
        clock=clock,
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        async with app.uow_factory() as uow:
            [first] = await uow.email.list(app.principal, "assessment")
        assert first.payload["junk_bypassed"]
        if stop == "expiry":
            clock.advance(timedelta(minutes=2))
        elif stop == "off":
            object.__setattr__(settings, "email_junk_filter_mode", EmailJunkMode.OFF)
        elif stop == "evidence":
            settings.email_junk_filter_evidence.unlink()
        else:
            await app.services.email.feedback(
                app.principal, thread_id=UUID(first.key), target="thread", judgment="important"
            )
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.usage.model_calls == 1 and len(judge.requests) == 1
        async with app.uow_factory() as uow:
            [last] = await uow.email.list(app.principal, "assessment")
        assert not last.payload.get("junk_bypassed")


@pytest.mark.parametrize("settings_factory", SETTINGS_FACTORIES)
async def test_pilot_and_standing_full_daily_cap_avoids_provider_call(
    tmp_path: Path, settings_factory: Callable[[Path], Any]
) -> None:
    """An already full durable UTC-day allowance goes directly to normal assessment."""
    from agent_core.domain.email import EmailRecord

    judge = FakeJudgmentProvider([])
    async with build(
        settings=settings_factory(tmp_path),
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        now = app.clock.now()
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            await uow.email.put(
                EmailRecord(
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    kind="junk_pilot_day",
                    key=f"work:{now.date().isoformat()}",
                    revision=1,
                    payload={"count": 50},
                    created_at=now,
                    updated_at=now,
                ),
                expected_revision=0,
            )
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.usage.model_calls == 1 and judge.requests == []
