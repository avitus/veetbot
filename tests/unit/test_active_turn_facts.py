"""ADR-0129 section 5: what the executor tells an authorizer about the turn.

The turn runs from the newest user message; its trust is that message's
trust, and its tools are the unwrapped names of every call since, including
the other calls of the current step (the loop appends a step's calls before
running them).
"""

from __future__ import annotations

import json
from typing import Any

from agent_core.domain.messages import (
    AssistantMessage,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
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

    assert facts.newest_user_trust is TrustLevel.USER
    assert facts.tool_names == frozenset({"browser.navigate", "browser.act"})


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


def result(index: int, trust: TrustLevel) -> ToolResultItem:
    return ToolResultItem(call_id=f"call-{index}", content=[TextPart(text="x")], trust=trust)


def test_owner_words_and_outside_content_span_the_whole_conversation() -> None:
    """ADR-0213: an email read in an earlier turn still taints a later owner turn."""
    facts = _active_turn_facts(
        checkpoint(
            user("Read my latest email."),
            call("mcp.gmail_read.get_message", {}, 0),
            result(0, TrustLevel.EXTERNAL_UNTRUSTED),
            user("from a device", TrustLevel.EXTERNAL_UNTRUSTED),
            user("Go ahead."),
        )
    )
    clean = _active_turn_facts(
        checkpoint(
            user("Call Riv at +1 (650) 555-0123."),
            call("memory.search", {}, 0),
            result(0, TrustLevel.MEMORY),
        )
    )

    assert facts.owner_texts == ("Read my latest email.", "Go ahead.")
    assert facts.conversation_untrusted is True
    assert clean.owner_texts == ("Call Riv at +1 (650) 555-0123.",)
    assert clean.conversation_untrusted is False


def test_an_answered_question_or_a_file_export_leaves_the_turn_browser_only() -> None:
    """ADR-0255: the platform's sign-in resumption, the owner's own answer and
    a file export bring nothing into the turn a page could be typed from."""
    facts = _active_turn_facts(
        checkpoint(
            user("Look up my records."),
            call("browser.observe", {}, 0),
            call("conversation.ask_user", {"question": "Please sign in."}, 1),
            result(1, TrustLevel.PLATFORM),
            call("conversation.ask_user", {"question": "Which year?"}, 2),
            result(2, TrustLevel.USER),
            call("artifact.export", {"content": "a", "filename": "a.csv"}, 3),
            result(3, TrustLevel.INTERNAL_TOOL),
            call("tool.call", {"name": "artifact.export", "arguments": {}}, 4),
            call("browser.act", {"kind": "click"}, 5),
        )
    )

    assert facts.tool_names == frozenset({"browser.observe", "browser.act"})


def test_a_pending_or_untrusted_answer_still_counts() -> None:
    """ADR-0255: only an answer from the owner or the platform is exempt."""
    pending = _active_turn_facts(
        checkpoint(
            user("Look up my records."),
            call("conversation.ask_user", {"question": "Which year?"}, 0),
            call("browser.act", {"kind": "click"}, 1),
        )
    )
    untrusted = _active_turn_facts(
        checkpoint(
            user("Look up my records."),
            call("conversation.ask_user", {"question": "Which year?"}, 0),
            result(0, TrustLevel.EXTERNAL_UNTRUSTED),
            call("browser.act", {"kind": "click"}, 1),
        )
    )

    assert pending.tool_names == frozenset({"conversation.ask_user", "browser.act"})
    assert untrusted.tool_names == frozenset({"conversation.ask_user", "browser.act"})
