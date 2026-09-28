"""Growing tool history remains reusable while per-step context changes."""

import hashlib
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.context.builder import _RecallBundle
from agent_core.domain.context import WorkingState
from agent_core.domain.messages import (
    ModelCapabilities,
    ModelLimits,
    ModelPricing,
    ModelRequest,
    ProviderReasoningItem,
    ResolvedModel,
    TextPart,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import ProviderContinuation
from agent_core.domain.skills import LoadedSkillBody
from tests.contract.support import NOW, agent, principal, session
from tests.unit.test_history_cache_window import (
    _call,
    _checkpoint,
    _history,
    _result,
    _second_run,
    _stack,
)


def _wire(request: ModelRequest, model: ResolvedModel) -> list[dict[str, Any]]:
    # Cache annotations are outside the model-visible prefix. Normalize only
    # their presence, never message bytes, trust envelopes or reasoning items.
    payload = OpenAIResponsesProvider._request_payload(request, model)
    rows: list[dict[str, Any]] = payload["input"]
    for row in rows:
        for field in ("content", "output"):
            content = row.get(field)
            if isinstance(content, list):
                for block in content:
                    block.pop("prompt_cache_breakpoint", None)
                if len(content) == 1 and set(content[0]) == {"type", "text"}:
                    row[field] = content[0]["text"]
    return rows


@pytest.mark.parametrize("reasoning", [False, True])
@pytest.mark.parametrize("carried", [False, True])
@pytest.mark.parametrize("volatile", ["recall", "recall_shape", "state", "clock", "skill"])
async def test_active_history_survives_fresh_step_context(
    monkeypatch: pytest.MonkeyPatch, reasoning: bool, carried: bool, volatile: str
) -> None:
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
    cp = _checkpoint(
        active,
        [*(_history() if carried else []), UserMessage(content=[TextPart(text="Research it.")])],
    )
    tick = 0

    async def recall(*args: object) -> _RecallBundle:
        if volatile == "recall_shape":
            return _RecallBundle(
                base="New memory" if tick % 2 else None, delta="New delta" if tick % 3 else None
            )
        return (
            _RecallBundle(base=f'<memory as_of="{tick}">Current fact {tick}</memory>')
            if volatile == "recall"
            else _RecallBundle()
        )

    monkeypatch.setattr(builder, "_recall_once", recall)
    previous: list[dict[str, Any]] | None = None
    previous_boundary = 0
    for tick in range(1, 6):
        result = _result(f"call-{tick}", "canonical " * 1000, tick * 2 + 4)
        result.context_content = [TextPart(text=f"Page {tick}: " + "excerpt " * 200)]
        cp.conversation.extend([_call(f"call-{tick}", tick * 2 + 3), result])
        if reasoning:
            cp.provider_continuation = ProviderContinuation(
                provider="openai",
                opaque_items=[
                    ProviderReasoningItem(
                        provider="openai",
                        item_index=0,
                        provider_payload={
                            "type": "reasoning",
                            "id": f"opaque-{tick}",
                            "encrypted_content": f"opaque-{tick}",
                            "summary": [],
                        },
                    ).model_dump(mode="json")
                ],
            )
        if volatile == "state":
            cp.working_state = {
                "context": WorkingState(objective=f"Step {tick}").model_dump(mode="json")
            }
        elif volatile == "clock":
            assert isinstance(builder._clock, FixedClock)
            builder._clock.advance(timedelta(days=1))
        elif volatile == "skill":
            content = f"Loaded reference {tick}"
            cp.loaded_skills = [
                LoadedSkillBody(
                    name="reference",
                    revision=tick,
                    content=content,
                    tokens=20,
                    trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                )
            ]
        saved = cp.model_dump_json()
        assembled = await builder.assemble(active, cp, agent(), principal())
        assert cp.model_dump_json() == saved
        # Durable checkpoint replay must produce the same model request.
        replay = await builder.build(
            active, type(cp).model_validate_json(saved), agent(), principal()
        )
        assert replay == assembled.request
        assert assembled.pressure.fits
        assert assembled.pressure.yield_steps == ()
        wire = _wire(assembled.request, model)
        if previous is not None:
            assert wire[:previous_boundary] == previous[:previous_boundary], (
                "Fresh per-step context rewrote the cacheable active history"
            )
        # The reusable prefix reaches completed tool exchanges, not just the
        # initial instructions or the history carried from a previous run.
        stable_call = tick - 1 if reasoning else tick
        if stable_call:
            boundary = next(
                i + 1
                for i, row in enumerate(wire)
                if row.get("call_id") == f"call-{stable_call}" and "output" in row
            )
            assert boundary > previous_boundary
            assert assembled.request.cache_hints is not None
            assert any(
                h.through_input_item is not None and h.through_input_item >= boundary - 1
                for h in assembled.request.cache_hints.breakpoints
            )
            previous_boundary = boundary
        previous = wire
        text = str(wire)
        if volatile == "recall":
            assert f"Current fact {tick}" in text
            if tick > 1:
                assert f"Current fact {tick - 1}" not in text
        elif volatile == "recall_shape":
            assert ("New memory" in text) == bool(tick % 2)
            assert ("New delta" in text) == bool(tick % 3)
        elif volatile == "state":
            assert f"Step {tick}" in text
        elif volatile == "clock":
            assert builder._clock.now().date().isoformat() in text
        elif volatile == "skill":
            assert f"Loaded reference {tick}" in text
        if reasoning:
            assert [r["id"] for r in wire if r.get("type") == "reasoning"] == [f"opaque-{tick}"]
        # Canonical content and checkpoint state are never replaced by rendering.
        assert isinstance(result.content[0], TextPart)
        assert result.content[0].text == "canonical " * 1000


async def test_tool_loop_keeps_corrections_when_recall_yields() -> None:
    from tests.unit.test_context_recall_delta import (
        _builder,
        _correction_line,
        _memory_texts,
        _Retriever,
    )
    from tests.unit.test_context_recall_delta import (
        _checkpoint as recall_checkpoint,
    )

    cp = recall_checkpoint()
    cp.conversation.extend([_call("tool", 4), _result("tool", "Page", 5)])
    active = _second_run()
    roomy = await _builder(_Retriever()).assemble(active, cp, agent(), principal())
    tight = await _builder(_Retriever(), total_tokens=roomy.pressure.total_tokens - 1).assemble(
        active, cp, agent(), principal()
    )
    assert tight.pressure.fits
    assert tight.pressure.yield_steps == ("recall",)
    texts = _memory_texts(tight.request)
    assert len(texts) == 1
    assert _correction_line() in texts[0]
    result_index = next(
        i for i, item in enumerate(tight.request.conversation) if item.kind == "tool_result"
    )
    correction_index = next(
        i
        for i, item in enumerate(tight.request.conversation)
        if isinstance(item, UserMessage) and item.trust is TrustLevel.MEMORY
    )
    assert correction_index > result_index


async def test_supplemental_user_input_stays_after_fresh_context() -> None:
    planner, builder = await _stack()
    model = ResolvedModel(provider="openai", model="gpt-6-astra", resolved_at=NOW)
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    cp = _checkpoint(
        active,
        [
            UserMessage(content=[TextPart(text="Research")]),
            _call("tool", 4),
            _result("tool", "Page", 5),
        ],
    )
    before = await builder.build(active, cp, agent(), principal())
    cp.conversation.append(UserMessage(content=[TextPart(text="Use the corrected objective")]))
    cp.working_state = {
        "context": WorkingState(objective="Corrected objective").model_dump(mode="json")
    }
    after = await builder.build(active, cp, agent(), principal())
    a, b = _wire(before, model), _wire(after, model)
    boundary = next(i + 1 for i, row in enumerate(a) if "output" in row)
    assert a[:boundary] == b[:boundary]
    assert "Use the corrected objective" in str(b[-1])
    assert "Corrected objective" in str(b[boundary:-1])


async def test_fresh_context_follows_the_complete_anthropic_tool_batch() -> None:
    from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
    from tests.unit.test_history_cache_window import _claude, _continuation

    planner, builder = await _stack()
    model = _claude()
    await planner.plan(session(), agent(), principal(), model)
    active = _second_run()
    cp = _checkpoint(
        active,
        [
            UserMessage(content=[TextPart(text="Research")]),
            _call("first", 4),
            _call("second", 4).model_copy(update={"item_index": 1}),
            _result("first", "First page", 5),
            _result("second", "Second page", 6),
        ],
    )
    cp.provider_continuation = _continuation("signed-latest-turn")
    request = await builder.build(active, cp, agent(), principal())
    payload = AnthropicMessagesProvider._request_payload(request, model)[0]
    messages = payload["messages"]
    assistant_index = next(
        i
        for i, message in enumerate(messages)
        if any(block.get("type") == "tool_use" for block in message["content"])
    )
    assistant_blocks = messages[assistant_index]["content"]
    assert [b["type"] for b in assistant_blocks] == ["thinking", "tool_use", "tool_use"]
    assert assistant_blocks[0]["signature"] == "signed-latest-turn"
    following = messages[assistant_index + 1]["content"]
    assert [b["type"] for b in following[:2]] == ["tool_result", "tool_result"]
    assert [b["tool_use_id"] for b in following[:2]] == ["first", "second"]
    assert "Runtime metadata" in str(messages[assistant_index + 1 :])
