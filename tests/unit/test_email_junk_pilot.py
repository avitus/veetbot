"""The explicitly approved pilot stays owner-scoped, expiring and capped.

Every packet in this module is fabricated test data, never activation evidence.
"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.email_junk import JUNK_MODEL, JUNK_PILOT_POLICY, EmailJunkMode, JunkPilot
from agent_core.domain.judgment import ChoiceAnswer, JudgmentResult, NoulAnswer
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_runtime_m26 import _assessment_turn, _current_mail_factory
from tests.unit.test_config import base_environment
from tests.unit.test_email_junk_first_pass import decision, filter_settings


def test_explicit_pilot_mode_is_available_but_default_remains_off() -> None:
    """Expose the separately approved pilot without changing default activation."""
    assert load_settings(base_environment()).email_junk_filter_mode.value == "off"
    assert (
        load_settings(
            {**base_environment(), "AGENT_EMAIL_JUNK_FILTER_MODE": "pilot"}
        ).email_junk_filter_mode.value
        == "pilot"
    )


def pilot_packet(now: datetime | None = None) -> dict[str, Any]:
    """Create a clearly synthetic, selected sample with six deliberate missed junk cases."""
    now = now or datetime.now(UTC)
    return {
        "policy": JUNK_PILOT_POLICY,
        "model": JUNK_MODEL,
        "tenant_id": "local",
        "principal_id": "local-user",
        "account_id": "work",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "sample": "selected_live_inbox",
        "observations": [
            {
                "fingerprint": hashlib.sha256(f"synthetic-pilot-{n}".encode()).hexdigest(),
                "category": "bulk" if n < 30 else "keep",
                "label": "junk" if n < 36 else "keep",
                "source": "owner_review",
            }
            for n in range(43)
        ],
    }


def pilot_settings(tmp_path: Path) -> Any:
    """Bind synthetic work-account credentials and a private test-only pilot packet."""
    settings = filter_settings(tmp_path, "pilot", admitted=False)
    settings.email_junk_filter_evidence.write_text(json.dumps(pilot_packet()))
    return replace(
        settings,
        email_account_ids=("work",),
    )


async def work_factory() -> Any:
    """Reuse the governed mailbox transport for the explicitly named work account."""
    factory = await _current_mail_factory()

    def work(config: Any, credential: Any, environment: Any) -> Any:
        """Translate scripted server names while retaining the production work-account roster."""
        return factory(
            config.model_copy(
                update={"server_id": config.server_id.replace("gmail_work_", "gmail_")}
            ),
            credential,
            environment,
        )

    return work


def pilot_decision(*, keep: bool = False, topic: bool = False) -> JudgmentResult:
    """Use a valid raw choice below the general threshold and optionally a topic veto."""
    answers: dict[str, Any] = {
        "category": ChoiceAnswer(
            choice="keep" if keep else "bulk",
            probabilities={
                "bulk": 0.20 if keep else 0.60,
                "spam": 0.10,
                "keep": 0.70 if keep else 0.30,
            },
            confidence=0.7 if keep else 0.6,
        )
    }
    if topic:
        answers["protected_topic"] = NoulAnswer(probability=0.9)
    return decision().model_copy(update={"answers": answers})


@pytest.mark.parametrize("keep", [False, True])
async def test_pilot_uses_reviewed_choice_and_preserves_keep(tmp_path: Path, keep: bool) -> None:
    """Only the scoped pilot bypasses a valid raw bulk choice below the general cutoff."""
    judge = FakeJudgmentProvider([pilot_decision(keep=keep)])
    async with build(
        settings=pilot_settings(tmp_path),
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
    "mutation",
    [
        "policy",
        "model",
        "owner",
        "tenant",
        "expired",
        "future",
        "too_long",
        "empty_window",
        "duplicate",
        "false_positive",
        "too_small",
        "few_junk",
        "few_keep",
    ],
)
def test_pilot_rejects_out_of_scope_or_unreliable_evidence(mutation: str) -> None:
    """No scope, time or evidence exception admits an unreviewed production pilot."""
    now = datetime.now(UTC)
    data = pilot_packet(now)
    if mutation in {"policy", "model"}:
        data[mutation] = "wrong"
    elif mutation in {"owner", "tenant"}:
        data["principal_id" if mutation == "owner" else "tenant_id"] = "another"
    elif mutation == "expired":
        data["issued_at"] = (now - timedelta(days=7)).isoformat()
        data["expires_at"] = now.isoformat()
    elif mutation == "future":
        data["issued_at"] = (now + timedelta(seconds=1)).isoformat()
    elif mutation == "too_long":
        data["expires_at"] = (now + timedelta(days=7, seconds=1)).isoformat()
    elif mutation == "empty_window":
        data["expires_at"] = now.isoformat()
    elif mutation == "duplicate":
        data["observations"][1]["fingerprint"] = data["observations"][0]["fingerprint"]
    elif mutation == "false_positive":
        data["observations"][0]["label"] = "keep"
    elif mutation == "too_small":
        data["observations"].pop()
    elif mutation == "few_junk":
        for row in data["observations"][:11]:
            row["category"] = "keep"
    elif mutation == "few_keep":
        data["observations"][-1]["label"] = "junk"
    assert not JunkPilot.model_validate(data).admitted(
        Principal(tenant_id="local", principal_id="local-user"), "work", now
    )


@pytest.mark.parametrize("mutation", ["account", "sample", "source", "naive"])
def test_pilot_packet_rejects_unapproved_shapes(mutation: str) -> None:
    """The packet cannot claim random sampling, model labels or a different account."""
    data = pilot_packet()
    if mutation == "account":
        data["account_id"] = "personal"
    elif mutation == "sample":
        data["sample"] = "random"
    elif mutation == "source":
        data["observations"][0]["source"] = "model"
    else:
        data["issued_at"] = "2026-10-15T01:00:00"
    with pytest.raises(ValueError):
        JunkPilot.model_validate(data)


@pytest.mark.parametrize("failure", ["missing", "invalid", "expired", "owner", "account"])
async def test_invalid_pilot_stops_calls_and_retains_normal_assessment(
    tmp_path: Path, failure: str
) -> None:
    """Invalid pilot evidence disables egress, unlike the general filter's shadow fallback."""
    settings = pilot_settings(tmp_path)
    path = settings.email_junk_filter_evidence
    data = pilot_packet()
    if failure == "missing":
        path.unlink()
    elif failure == "invalid":
        path.write_text("invalid")
    elif failure == "expired":
        data["issued_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        data["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        path.write_text(json.dumps(data))
    elif failure == "owner":
        data["principal_id"] = "someone-else"
        path.write_text(json.dumps(data))
    else:
        settings = replace(settings, email_account_ids=())
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
        assert run.usage.model_calls == 1 and judge.requests == []


@pytest.mark.parametrize("stop", ["expiry", "off", "evidence", "important"])
async def test_stopping_pilot_restores_cached_bypass(tmp_path: Path, stop: str) -> None:
    """The bounded cached-mail refresh restores ordinary assessment after revocation."""
    from uuid import UUID

    from agent_core.adapters.determinism import FixedClock

    settings = pilot_settings(tmp_path)
    now = datetime.now(UTC)
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


async def test_expiry_during_provider_call_prevents_bypass(tmp_path: Path) -> None:
    """An in-flight paid classification cannot outlive its authorization window."""
    from agent_core.adapters.determinism import FixedClock

    now = datetime.now(UTC)
    settings = pilot_settings(tmp_path)
    data = pilot_packet(now)
    data["expires_at"] = (now + timedelta(seconds=1)).isoformat()
    settings.email_junk_filter_evidence.write_text(json.dumps(data))
    clock = FixedClock(now)

    class ExpiringJudge(FakeJudgmentProvider):
        async def judge(self, request: Any) -> JudgmentResult:
            """Advance the test clock across expiry after the request reached the provider."""
            clock.advance(timedelta(seconds=1))
            return await super().judge(request)

    judge = ExpiringJudge([pilot_decision()])
    async with build(
        settings=settings,
        clock=clock,
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.usage.model_calls == 1 and len(judge.requests) == 1
        async with app.uow_factory() as uow:
            assert not await uow.email.list(app.principal, "junk_pilot_day")


async def test_pilot_topic_veto_exception_does_not_change_general_filter(tmp_path: Path) -> None:
    """The reviewed raw choice is sufficient only for an active, scoped pilot."""
    from agent_core.domain.email_junk import JUNK_POLICY, junk_request, junk_result
    from agent_core.runtime.email_junk import EmailJunkFilter
    from tests.gates.test_email_spam_m31 import _seed

    async with build(settings=filter_settings(tmp_path)) as app:
        thread = await _seed(app)
        request = junk_request(thread, {"junk_protected_topics": ["board"]}, app.clock.now())
        assert request is not None
        receipt = {
            **junk_result(request, pilot_decision(topic=True)),
            "policy": JUNK_POLICY,
            "model": JUNK_MODEL,
        }
        pilot = JunkPilot.model_validate(pilot_packet())
        filter_ = EmailJunkFilter(FakeJudgmentProvider([]), EmailJunkMode.PILOT, pilot=pilot)
        assert filter_.bypass(
            receipt, principal=app.principal, account_id="work", now=app.clock.now()
        )
        assert not filter_.bypass(
            receipt, principal=app.principal, account_id="default", now=app.clock.now()
        )
        assert not EmailJunkFilter(None, EmailJunkMode.FILTER, admitted=True).bypass(receipt)
        for change in ({"status": "invalid"}, {"model": "wrong"}, {"raw_category": "keep"}):
            assert not filter_.bypass(
                {**receipt, **change},
                principal=app.principal,
                account_id="work",
                now=app.clock.now(),
            )


async def test_full_daily_cap_avoids_provider_call(tmp_path: Path) -> None:
    """An already full durable UTC-day allowance goes directly to normal assessment."""
    from agent_core.domain.email import EmailRecord

    judge = FakeJudgmentProvider([])
    async with build(
        settings=pilot_settings(tmp_path),
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


async def assert_durable_pilot_cap(app: Any) -> None:
    """Exercise simultaneous saves, replay, evidence changes and the next UTC day."""
    import asyncio
    from uuid import uuid4

    from agent_core.application.email import save_value
    from agent_core.application.email_junk import JunkPilotUnavailableError
    from agent_core.domain.email import EmailThread

    async with app.uow_factory() as uow:
        [source] = await uow.email.list(app.principal, "thread")
        [row] = await uow.email.list(app.principal, "assessment")
    original = EmailThread.model_validate(source.payload)
    assessment = row.payload
    assert assessment["junk_bypassed"]
    now = app.clock.now()
    pilot = JunkPilot.model_validate(pilot_packet(now))
    # A newly loaded/reformatted evidence packet must not reset the daily counter.
    threads = [
        original.model_copy(update={"id": uuid4(), "provider_thread_id": f"pilot-{n}"})
        for n in range(53)
    ]
    async with app.uow_factory() as uow, uow.email.lock(app.principal):
        for thread in threads:
            await save_value(uow.email, app.principal, "thread", str(thread.id), thread, now)
    results = await asyncio.gather(
        *[
            app.services.email.save_assessment(
                app.principal, t.id, t.revision, assessment, junk_pilot=pilot
            )
            for t in threads
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, EmailThread) for r in results) == 49
    assert sum(isinstance(r, JunkPilotUnavailableError) for r in results) == 4
    # Replaying an admitted conversation consumes no new slot, even on a later date.
    for _ in range(2):
        await app.services.email.save_assessment(
            app.principal, original.id, original.revision, assessment, junk_pilot=pilot
        )
    async with app.uow_factory() as uow:
        [day] = await uow.email.list(app.principal, "junk_pilot_day")
        assert day.payload == {"count": 50}
        assert len(await uow.email.list(app.principal, "junk_pilot_admission")) == 50
    from agent_core.adapters.determinism import FixedClock

    if isinstance(app.clock, FixedClock):
        app.clock.advance(timedelta(days=1))
        rejected = next(
            t
            for t, r in zip(threads, results, strict=True)
            if isinstance(r, JunkPilotUnavailableError)
        )
        await app.services.email.save_assessment(
            app.principal, rejected.id, rejected.revision, assessment, junk_pilot=pilot
        )
        await app.services.email.save_assessment(
            app.principal, original.id, original.revision, assessment, junk_pilot=pilot
        )
        async with app.uow_factory() as uow:
            days = await uow.email.list(app.principal, "junk_pilot_day")
        assert sorted(d.payload["count"] for d in days) == [1, 50]


async def test_daily_cap_is_atomic_and_cached_refresh_is_idempotent(tmp_path: Path) -> None:
    """Memory-adapter concurrency respects the same durable admission contract as PostgreSQL."""
    from agent_core.adapters.determinism import FixedClock

    settings = pilot_settings(tmp_path)
    async with build(
        settings=settings,
        clock=FixedClock(datetime.now(UTC)),
        judgment_provider_override=FakeJudgmentProvider([pilot_decision()]),
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await work_factory(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        assert (await app.runs.get(operation.run_id)).status is RunStatus.COMPLETED
        await assert_durable_pilot_cap(app)


@pytest.mark.parametrize("race", ["last_slot", "expiry_at_save"])
async def test_pilot_commit_races_resume_normal_assessment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    """Losing authority between a valid receipt and commit never leaves a bypass."""
    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.email import EmailRecord
    from agent_core.runtime.email_junk import EmailJunkFilter

    now = datetime.now(UTC)
    clock = FixedClock(now)
    settings = pilot_settings(tmp_path)
    data = pilot_packet(now)
    data["expires_at"] = (now + timedelta(seconds=2)).isoformat()
    settings.email_junk_filter_evidence.write_text(json.dumps(data))
    original = EmailJunkFilter.bypass

    def expire_after_decision(self: EmailJunkFilter, receipt: Any, **kwargs: Any) -> bool:
        """Move time only after the runtime approved its valid result, before saving it."""
        result = original(self, receipt, **kwargs)
        if result and race == "expiry_at_save":
            clock.advance(timedelta(seconds=2))
        return result

    monkeypatch.setattr(EmailJunkFilter, "bypass", expire_after_decision)
    judge = FakeJudgmentProvider([pilot_decision()])
    async with build(
        settings=settings,
        clock=clock,
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        original_judge = judge.judge

        async def fill_last_slot(request: Any) -> JudgmentResult:
            """Represent another transaction winning the quota during provider latency."""
            if race == "last_slot":
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
            return await original_judge(request)

        monkeypatch.setattr(judge, "judge", fill_last_slot)
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.usage.model_calls == 1 and len(judge.requests) == 1
        assert run.usage.cost >= decision().usage.cost
        async with app.uow_factory() as uow:
            [assessment] = await uow.email.list(app.principal, "assessment")
            assert not assessment.payload.get("junk_bypassed")
            assert not await uow.email.list(app.principal, "junk_pilot_admission")
