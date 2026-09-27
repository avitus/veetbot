"""Chat cache identity and the actual Responses wire prefix."""

import pytest

from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.domain.messages import ResolvedModel, TextPart, UserMessage
from tests.contract.support import NOW, agent, principal, run, session
from tests.unit.test_history_cache_window import _checkpoint, _stack


async def test_chat_payload_has_a_session_cache_key() -> None:
    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = run()
    request = await builder.build(
        active,
        _checkpoint(active, [UserMessage(content=[TextPart(text="Plan the trip.")])]),
        agent(),
        principal(),
    )
    payload = OpenAIResponsesProvider._request_payload(request, model)
    assert payload.get("prompt_cache_key"), "Chat has no per-session cache identity on the wire"


def test_openai_accounts_for_cache_writes_at_the_profile_price() -> None:
    from decimal import Decimal

    from agent_core.domain.messages import ModelPricing

    model = ResolvedModel(
        provider="openai",
        model="gpt-6-astra",
        resolved_at=NOW,
        pricing=ModelPricing(
            input_per_mtok=Decimal("10"),
            cached_input_per_mtok=Decimal("1"),
            cache_write_per_mtok=Decimal("12.5"),
            output_per_mtok=Decimal("50"),
        ),
    )
    usage = OpenAIResponsesProvider._usage(
        {
            "usage": {
                "input_tokens": 10000,
                "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 6000, "cache_write_tokens": 3000},
            }
        },
        model,
    )
    assert usage.cache_write_input_tokens == 3000
    assert usage.cost == Decimal("0.0585")


async def test_openai_writes_the_stable_history_before_volatile_context() -> None:
    from decimal import Decimal

    from agent_core.domain.messages import ModelCapabilities, ModelLimits, ModelPricing
    from tests.unit.test_history_cache_window import _history, _second_run

    planner, builder = await _stack()
    model = ResolvedModel(
        provider="openai",
        model="gpt-6-astra",
        resolved_at=NOW,
        capabilities=ModelCapabilities(explicit_cache_control=True),
        limits=ModelLimits(max_cache_breakpoints=3),
        pricing=ModelPricing(cache_write_per_mtok=Decimal("12.5")),
    )
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    req = await builder.build(
        active,
        _checkpoint(
            active,
            [
                *_history(),
                UserMessage(content=[TextPart(text="Continue the plan")], source_event_sequence=3),
            ],
        ),
        agent(),
        principal(),
    )
    payload = OpenAIResponsesProvider._request_payload(req, model)
    history_end = int(req.metadata["region_a_items"])
    content = payload["input"][history_end]["content"]
    assert isinstance(content, list), "The reusable history has no explicit cache write"
    assert content[-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}


async def test_session_identity_survives_steps_runs_and_epoch_rotation() -> None:
    from uuid import uuid4

    from agent_core.domain.cache import session_cache_key

    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = run()
    cp = _checkpoint(active, [UserMessage(content=[TextPart(text="Continue")])])
    first = await builder.build(active, cp, agent(), principal())
    active = active.model_copy(update={"id": uuid4(), "step_count": 3})
    await planner.rotate(active.session_id, "explicit_refresh")
    next_run = await builder.build(active, cp, agent(), principal())
    assert first.cache_hints is not None
    assert next_run.cache_hints is not None
    key = first.cache_hints.session_key
    assert key is not None
    assert key == next_run.cache_hints.session_key
    assert key != session_cache_key(active.tenant_id, uuid4())
    assert key != session_cache_key("another-tenant", active.session_id)
    other_session = session().model_copy(update={"id": uuid4()})
    async with planner._uow_factory() as uow:
        await uow.sessions.create(other_session)
    await planner.plan(other_session, agent(), principal(), model)
    other_run = active.model_copy(update={"session_id": other_session.id})
    other_request = await builder.build(other_run, cp, agent(), principal())
    assert other_request.cache_hints is not None
    assert other_request.cache_hints.session_key != key
    assert str(active.session_id) not in key
    assert active.tenant_id not in key
    assert OpenAIResponsesProvider._request_payload(next_run, model)["prompt_cache_key"] == key


async def test_wire_prefix_is_stable_until_the_replaced_reasoning_item() -> None:
    import json

    from agent_core.domain.messages import ProviderReasoningItem
    from agent_core.domain.runs import ProviderContinuation
    from tests.unit.test_history_cache_window import (
        _call,
        _history,
        _result,
        _second_run,
        _this_run,
    )

    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    cp = _checkpoint(active, [*_history(), *_this_run()])

    def continuation(label: str) -> ProviderContinuation:
        return ProviderContinuation(
            provider="openai",
            opaque_items=[
                ProviderReasoningItem(
                    provider="openai",
                    item_index=0,
                    provider_payload={
                        "type": "reasoning",
                        "id": label,
                        "encrypted_content": label,
                        "summary": [],
                    },
                ).model_dump()
            ],
        )

    cp.provider_continuation = continuation("opaque-1")
    first = await builder.assemble(active, cp, agent(), principal())
    cp.conversation.extend([_call("call-2", 8), _result("call-2", "page two", 9)])
    cp.provider_continuation = continuation("opaque-2")
    second = await builder.assemble(active, cp, agent(), principal())
    before = OpenAIResponsesProvider._request_payload(first.request, model)
    after = OpenAIResponsesProvider._request_payload(second.request, model)
    index = next(i for i, item in enumerate(before["input"]) if item.get("type") == "reasoning")
    assert before["input"][:index] == after["input"][:index]
    assert before["tools"] == after["tools"]
    assert first.pressure.history_cut == second.pressure.history_cut == 0
    assert first.pressure.yield_steps == second.pressure.yield_steps == ()
    a, b = (json.dumps(p["input"], separators=(",", ":")).encode() for p in (before, after))
    offset = next(i for i, pair in enumerate(zip(a, b, strict=False)) if pair[0] != pair[1])
    assert a[offset:].startswith(b'id":"opaque-1')
    assert b[offset:].startswith(b'type":"function_call')
    print(f"First wire difference: byte {offset}, input item {index}, replaced reasoning")


async def test_volatile_state_and_clock_leave_carried_history_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from agent_core.context.builder import _RecallBundle
    from agent_core.domain.context import WorkingState
    from tests.unit.test_history_cache_window import _history, _second_run

    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    cp = _checkpoint(
        active,
        [*_history(), UserMessage(content=[TextPart(text="Continue")], source_event_sequence=3)],
    )

    async def recall(*args: object) -> _RecallBundle:
        return _RecallBundle(base='<memory as_of="today">A fact</memory>')

    monkeypatch.setattr(builder, "_recall_once", recall)
    first = await builder.build(active, cp, agent(), principal())

    async def changed_recall(*args: object) -> _RecallBundle:
        return _RecallBundle(base='<memory as_of="tomorrow">A corrected fact</memory>')

    monkeypatch.setattr(builder, "_recall_once", changed_recall)
    from agent_core.adapters.determinism import FixedClock

    assert isinstance(builder._clock, FixedClock)
    builder._clock.advance(timedelta(days=1))
    cp.working_state = {"context": WorkingState(objective="Updated goal").model_dump(mode="json")}
    second = await builder.build(active, cp, agent(), principal())
    history_end = int(first.metadata["region_a_items"]) + len(_history())
    a = OpenAIResponsesProvider._request_payload(first, model)["input"]
    b = OpenAIResponsesProvider._request_payload(second, model)["input"]
    assert a[:history_end] == b[:history_end]
    assert a[history_end:] != b[history_end:]
    assert first.metadata["prefix_sha256"] == second.metadata["prefix_sha256"]


async def test_tool_pressure_can_rewrite_history_and_is_reported() -> None:
    from tests.unit.test_history_cache_window import _call, _result, _second_run

    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    cp = _checkpoint(
        active,
        [
            _call("old", 1),
            _result("old", "small result", 2),
            UserMessage(content=[TextPart(text="Continue")], source_event_sequence=3),
        ],
    )
    first = await builder.assemble(active, cp, agent(), principal())
    cp.conversation.extend([_call("new", 4), _result("new", "A large tool output. " * 3000, 5)])
    second = await builder.assemble(active, cp, agent(), principal())
    a = OpenAIResponsesProvider._request_payload(first.request, model)["input"]
    b = OpenAIResponsesProvider._request_payload(second.request, model)["input"]
    old = next(i for i, x in enumerate(a) if x.get("call_id") == "old" and "output" in x)
    assert a[:old] == b[:old]
    assert "truncated" in b[old]["output"]
    assert "tool_results" in second.pressure.yield_steps
    # This documented pressure response must not be disabled to win cache hits.
    assert first.request.metadata["prefix_sha256"] == second.request.metadata["prefix_sha256"]


async def test_builder_offers_input_boundary_for_implicit_cache_lookback() -> None:
    from tests.unit.test_history_cache_window import _history, _second_run

    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    req = await builder.build(
        active,
        _checkpoint(
            active,
            [
                *_history(),
                UserMessage(content=[TextPart(text="Continue")], source_event_sequence=3),
            ],
        ),
        agent(),
        principal(),
    )
    assert req.cache_hints is not None
    history = next(h for h in req.cache_hints.breakpoints if h.boundary == "after_history_prefix")
    assert getattr(history, "through_input_item", None) == int(req.metadata["region_a_items"])


async def test_openai_records_sent_and_dropped_hints_with_or_without_support() -> None:
    from decimal import Decimal

    from agent_core.domain.messages import ModelCapabilities, ModelPricing
    from agent_core.model.streaming import collect_turn
    from tests.contract.model_fixtures import ScriptedRawSource, openai_text_events
    from tests.contract.test_model_gateway_contract import ATTEMPT
    from tests.unit.test_history_cache_window import _limited, _tool_loop_request

    request = _tool_loop_request()
    from agent_core.domain.messages import ProviderReasoningItem

    for item in request.conversation:
        if isinstance(item, ProviderReasoningItem):
            item.provider = "openai"
            item.provider_payload = {
                "type": "reasoning",
                "encrypted_content": "opaque",
                "summary": [],
            }
    for budget, priced, expected in [
        (3, True, (3, 1)),
        (0, True, (0, 4)),
        (3, False, (0, 4)),
        (1, True, (1, 3)),
    ]:
        model = _limited(budget).model_copy(
            update={
                "provider": "openai",
                "capabilities": ModelCapabilities(explicit_cache_control=True),
                "pricing": ModelPricing(cache_write_per_mtok=Decimal("12.5") if priced else None),
            }
        )
        provider = OpenAIResponsesProvider(event_source=ScriptedRawSource([openai_text_events()]))
        turn = await collect_turn(provider.stream(request, model, ATTEMPT))
        assert turn.provider_metadata is not None
        assert (
            turn.provider_metadata.cache_breakpoints_sent,
            turn.provider_metadata.cache_breakpoints_dropped,
        ) == expected
        payload = OpenAIResponsesProvider._request_payload(request, model)
        assert "prompt_cache_retention" not in payload


def test_other_adapters_ignore_session_identity() -> None:
    from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
    from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
    from agent_core.domain.cache import session_cache_key
    from agent_core.domain.messages import CacheHints, ModelRequest
    from tests.contract.test_model_gateway_contract import resolved

    req = ModelRequest(
        model_policy="test", tools=[], conversation=[UserMessage(content=[TextPart(text="Hello")])]
    )
    adapters: list[tuple[type[AnthropicMessagesProvider] | type[ChatCompletionsProvider], str]] = [
        (AnthropicMessagesProvider, "anthropic"),
        (ChatCompletionsProvider, "chat_completions"),
    ]
    for adapter, name in adapters:
        before = adapter._request_payload(req, resolved(name))[0]
        req.cache_hints = CacheHints(
            session_key=session_cache_key(run().tenant_id, run().session_id)
        )
        after = adapter._request_payload(req, resolved(name))[0]
        assert before == after
        assert "prompt_cache_key" not in after
        req.cache_hints = None


def test_old_cache_hints_remain_readable_and_reject_raw_session_keys() -> None:
    import pytest
    from pydantic import ValidationError

    from agent_core.domain.messages import CacheHints

    assert CacheHints.model_validate({"breakpoints": []}).session_key is None
    with pytest.raises(ValidationError):
        CacheHints(session_key=str(session().id))
