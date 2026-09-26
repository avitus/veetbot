"""ADR-0129 section 5: what the executor tells an authorizer about the turn.

The turn runs from the newest user message; its trust is that message's
trust, and its tools are the unwrapped names of every call since, including
the other calls of the current step (the loop appends a step's calls before
running them).
"""

from __future__ import annotations

import json
from typing import Any

from agent_core.domain.messages import AssistantMessage, TextPart, ToolCallItem, UserMessage
from agent_core.domain.policies import AuthorizationTurn, TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.tools.executor import _active_turn_facts
from tests.contract.support import NOW, RUN_ID


def call(name: str, arguments: dict[str, Any], index: int) -> ToolCallItem:
    return ToolCallItem(
        call_id=f"call-{index}",
        item_index=index,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


def user(text: str, trust: TrustLevel = TrustLevel.USER) -> UserMessage:
    return UserMessage(content=[TextPart(text=text)], trust=trust, principal_id=None)


def checkpoint(*items: Any) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        conversation=list(items),
        created_at=NOW,
    )


def test_the_turn_starts_at_the_newest_user_message() -> None:
    facts = _active_turn_facts(
        checkpoint(
            user("earlier"),
            call("email.search", {"query": "x"}, 0),
            user("do the lesson"),
            AssistantMessage(content=[TextPart(text="ok")]),
            call("browser.navigate", {"url": "https://www.example.org/lesson"}, 1),
            call("browser.act", {"kind": "click"}, 2),
        )
    )

    assert facts == AuthorizationTurn(
        newest_user_trust=TrustLevel.USER,
        tool_names=frozenset({"browser.navigate", "browser.act"}),
    )


def test_a_deferred_call_counts_as_the_tool_it_names() -> None:
    facts = _active_turn_facts(
        checkpoint(
            user("do the lesson"),
            call("tool.call", {"name": "email.search", "arguments": {"query": "x"}}, 0),
            call("tool.call", {"arguments": {}}, 1),
            call("browser.act", {"kind": "click"}, 2),
        )
    )

    assert facts.tool_names == frozenset({"email.search", "tool.call", "browser.act"})


def test_an_untrusted_or_missing_user_message_is_recorded() -> None:
    ingested = _active_turn_facts(
        checkpoint(user("from a device", TrustLevel.EXTERNAL_UNTRUSTED), call("browser.act", {}, 0))
    )
    none = _active_turn_facts(checkpoint(call("browser.act", {}, 0)))

    assert ingested.newest_user_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert none.newest_user_trust is TrustLevel.EXTERNAL_UNTRUSTED
