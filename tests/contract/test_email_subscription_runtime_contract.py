"""The subscription runtime boundary is inert when disabled and exact when enabled."""

from typing import Any
from uuid import uuid4

import pytest

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.runs import Run
from agent_core.ports.email import EmailSubscriptionRuntime
from tests.gates.test_email_m18 import _run
from tests.gates.test_email_unsubscribe_m31 import (
    Mailbox,
    Transport,
    _app,
    _block,
    _rows,
    _summary,
)


async def assert_email_subscription_runtime_contract(
    runtime: EmailSubscriptionRuntime,
    principal: Principal,
    summaries: list[dict[str, Any]],
    block: dict[str, Any],
    *,
    enabled: bool,
) -> None:
    run: Run = _run().model_copy(update={"id": uuid4()})
    observed = await runtime.observe(principal, "default", summaries)
    repeated = await runtime.observe(principal, "default", summaries)
    assert repeated == 0, "re-observing the same summaries must change nothing"
    pending = await runtime.unverified(principal, "default")
    if not enabled:
        assert (observed, pending) == (0, [])
        await runtime.sweep(principal, "default")
        await runtime.apply_verification(principal, "0" * 64, block)
        with pytest.raises(NotFoundError):
            await runtime.validate(principal, run, None)
        return
    assert observed == 1
    [(subscription_id, message_id)] = pending
    assert message_id == block["message_id"]
    await runtime.apply_verification(principal, subscription_id, block)
    assert await runtime.unverified(principal, "default") == []
    # A run that carries no owner consent validates to nothing and finishes quietly.
    with pytest.raises(ConflictError):
        await runtime.validate(principal, run, None)
    await runtime.finish(principal, run, None)


@pytest.mark.parametrize("enabled", [True, False])
async def test_shared_email_subscription_runtime_contract(enabled: bool) -> None:
    mailbox = Mailbox()
    summaries = [_summary("shop-0", "shop-m0", "News <news@shop.example.com>")]
    async with _app(mailbox, Transport(), enabled=enabled) as app:
        runtime: EmailSubscriptionRuntime = app.services.email.subscriptions
        await assert_email_subscription_runtime_contract(
            runtime, app.principal, summaries, _block("shop-m0"), enabled=enabled
        )
        if enabled:
            [row] = await _rows(app)
            assert row["verified"] and row["mechanism"] == "one_click"
