"""web.fetch_json returns the named fields of one public JSON document (ADR-0256).

The Cockpit research run of 2026-10-26 read about sixty GitHub compare answers
with web.fetch to learn two numbers from each: every answer was the whole
document, about ten kilobytes, and together they filled the tool-result budget
the run then lost to truncation. The same read through web.fetch_json returns
the two numbers.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
)
from agent_core.domain.policies import IdempotencyClass, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolInvocationStatus, ToolResult
from agent_core.tools.registry import RegisteredTool, StaticToolRegistry
from agent_core.tools.web_fetch_json import WebFetchJsonTool
from tests.contract.support import tool_context
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FakeWebProvider

COMPARE = {
    "status": "ahead",
    "ahead_by": 3,
    "behind_by": 0,
    "commits": [{"sha": "4f7cd8a"}, {"sha": "ea47c6c"}, {"sha": "020531e"}],
    "files": [{"filename": "x" * 200} for _ in range(40)],
}
BRANCHES = [{"name": "main", "protected": True}, {"name": "dev"}]


async def _fetch(content: str, fields: list[str]) -> ToolResult:
    tool = WebFetchJsonTool(FakeWebProvider(page_content=content))
    return await tool.execute(
        {"url": "https://api.github.com/repos/o/r/compare/main...dev", "fields": fields},
        tool_context(),
    )


def _answer(result: ToolResult) -> dict[str, Any]:
    assert result.ok, result.failure
    [part] = result.content
    assert isinstance(part, TextPart)
    provider, _, body = part.text.partition("\n\n")
    assert provider == "Provider: fake-web"
    return cast(dict[str, Any], json.loads(body))


def test_the_tool_is_a_bounded_read_only_external_capability() -> None:
    spec = WebFetchJsonTool.spec
    assert spec.name == "web.fetch_json"
    assert spec.side_effect is SideEffectClass.NETWORK_READ
    assert spec.idempotency is IdempotencyClass.READ_ONLY
    assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert spec.target_kind == "web_provider"
    assert spec.input_schema["required"] == ["url", "fields"]
    # Its index entry is the first sentence, capped at 200 characters.
    assert len(spec.description.split(". ", 1)[0]) < 200
    StaticToolRegistry().register(WebFetchJsonTool(FakeWebProvider()))


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(COMPARE, indent=2),
        "```json\n" + json.dumps(COMPARE, indent=2) + "\n```",
        "```JSON\n" + json.dumps(COMPARE, indent=2) + "\n```",
    ],
    ids=["raw", "markdown-fenced", "uppercase-fence-label"],
)
async def test_it_returns_only_the_named_fields(content: str) -> None:
    result = await _fetch(content, ["ahead_by", "behind_by", "commits.0.sha", "merge_base"])

    answer = _answer(result)
    assert answer["values"] == {"ahead_by": 3, "behind_by": 0, "commits.0.sha": "4f7cd8a"}
    assert answer["missing"] == ["merge_base"]
    assert result.structured is not None
    assert result.structured["values"] == answer["values"]
    # About ten kilobytes of document become one short answer.
    assert len(content) > 8_000
    assert len(cast(TextPart, result.content[0]).text) < 300


async def test_a_star_segment_maps_every_element() -> None:
    answer = _answer(await _fetch(json.dumps(BRANCHES), ["*.name", "*.protected", "5.name"]))

    assert answer["values"] == {"*.name": ["main", "dev"], "*.protected": [True, None]}
    assert answer["missing"] == ["5.name"]


async def test_a_page_that_is_not_json_fails_and_names_web_fetch() -> None:
    result = await _fetch("# A rendered page\n\nNot JSON.", ["ahead_by"])

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.web.json_invalid"
    assert result.failure.retryable is False


@pytest.mark.parametrize(
    "content",
    [
        '{"ahead_by": NaN}',
        '{"ahead_by": -Infinity}',
        '{"ahead_by": 1e400}',
        "[" * 100_000 + "]" * 100_000,
        # Within the C parser's limit, past Python's own recursion limit.
        "[" * 2_000 + "]" * 2_000,
    ],
    ids=["nan", "infinity", "overflowing-number", "deeply-nested", "nested-past-python"],
)
async def test_a_document_outside_strict_json_fails_as_invalid(content: str) -> None:
    # Python's parser accepts NaN and Infinity and turns an overflowing number
    # into Infinity, which no JSON store accepts; it raises RecursionError,
    # not ValueError, on deep nesting.
    result = await _fetch(content, ["0"] if content.startswith("[") else ["ahead_by"])

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.web.json_invalid"


async def test_a_lone_surrogate_becomes_the_replacement_character() -> None:
    # JSON escapes can spell a lone surrogate, which UTF-8 cannot encode.
    document = r'{"name": "a\ud800b", "nested": {"k\udfff": ["\ud83d"]}}'

    result = await _fetch(document, ["name", "nested"])

    answer = _answer(result)
    assert answer["values"] == {"name": "a\ufffdb", "nested": {"k\ufffd": ["\ufffd"]}}
    assert result.structured is not None
    assert result.structured["values"] == answer["values"]
    cast(TextPart, result.content[0]).text.encode("utf-8")


async def test_a_selection_past_the_output_limit_asks_for_narrower_fields() -> None:
    document = [{"name": "n" * 1_000} for _ in range(200)]

    result = await _fetch(json.dumps(document), ["*.name"])

    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.web.json_selection_too_large"


async def test_a_private_url_is_refused_before_the_provider() -> None:
    provider = FakeWebProvider(page_content="{}")
    result = await WebFetchJsonTool(provider).execute(
        {"url": "https://localhost/api", "fields": ["a"]}, tool_context()
    )

    assert result.failure is not None
    assert result.failure.reason_code == "tool.web.url_disallowed"
    assert provider.fetches == []


async def test_the_default_agent_defers_it_and_policy_allows_it() -> None:
    provider = FakeWebProvider(page_content=json.dumps(COMPARE))
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="tool.call",
                        arguments={
                            "name": "web.fetch_json",
                            "arguments": {
                                "url": "https://api.github.com/repos/o/r/compare/main...dev",
                                "fields": ["ahead_by", "behind_by"],
                            },
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="dev is three commits ahead.", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})

    async with build(
        settings=settings, script=script, sequential_ids=True, web_fetch_provider_override=provider
    ) as composition:
        registered = cast(RegisteredTool, composition.tool_pipeline._registry.get("web.fetch_json"))
        run_id = await composition.runs.submit("How far ahead of main is dev?")
        run = await composition.runs.wait_terminal(run_id)
        plan = await composition.executor._context_planner.current(run.session_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

    assert isinstance(registered.implementation, WebFetchJsonTool)
    # It shares web.fetch's provider route, so it adds no egress of its own.
    assert registered.implementation._provider is provider
    assert plan is not None
    assert "web.fetch_json" in plan.deferred_tool_names
    assert "web.fetch_json" not in plan.tool_names
    [invocation] = invocations
    assert invocation.tool_name == "web.fetch_json"
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert run.final_message == "dev is three commits ahead."
