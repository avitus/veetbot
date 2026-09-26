"""The deferred tool index renders names, parameters and one sentence (ADR-0123)."""

from agent_core.adapters.determinism import FixedClock
from agent_core.context.rendering import (
    DEFERRED_INDEX_DISCOVERED_HEADING,
    DEFERRED_INDEX_NOTE,
    build_prefix,
    deferred_index_items,
    prefix_bytes,
)
from agent_core.domain.messages import ConversationItem, SystemMessage, TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolSource, ToolSpec
from agent_core.tools.current_time import CurrentTimeTool
from tests.contract.support import NOW, agent


def _discovered(description: str) -> ToolSpec:
    return CurrentTimeTool(FixedClock(NOW)).spec.model_copy(
        update={
            "name": "mcp.mail_read.search_threads",
            "source": ToolSource.MCP,
            "server_id": "mail_read",
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        }
    )


def _text(item: ConversationItem) -> str:
    assert isinstance(item, (SystemMessage, UserMessage))
    [part] = item.content
    assert isinstance(part, TextPart)
    return part.text


def test_an_empty_index_changes_no_prefix_byte() -> None:
    assert deferred_index_items(()) == []
    assert prefix_bytes(build_prefix(agent(), []), []) == prefix_bytes(
        build_prefix(agent(), [], deferred_tools=()), [], ()
    )


def test_builtin_entries_are_trusted_and_discovered_entries_are_data() -> None:
    builtin = CurrentTimeTool(FixedClock(NOW)).spec
    discovered = _discovered("Search threads in the mailbox. Returns thread ids and snippets.")

    system, data = deferred_index_items([builtin, discovered])

    assert isinstance(system, SystemMessage)
    assert system.trust is TrustLevel.TRUSTED_CONFIGURATION
    lines = _text(system).splitlines()
    assert lines[0] == DEFERRED_INDEX_NOTE
    assert lines[1].startswith("- system.current_time(")
    assert isinstance(data, UserMessage)
    assert data.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert _text(data).splitlines() == [
        DEFERRED_INDEX_DISCOVERED_HEADING,
        "- mcp.mail_read.search_threads(query, limit?): Search threads in the mailbox.",
    ]
    # The discovered entry reaches the model only inside an untrusted envelope.
    rendered = build_prefix(agent(), [], deferred_tools=[builtin, discovered])
    assert '<untrusted trust="external_untrusted"' in _text(rendered[-1])


def jsonb_key_order(value: object) -> object:
    """Reorder object keys the way PostgreSQL jsonb stores them: shorter keys first, then bytes."""

    if isinstance(value, dict):
        return {
            key: jsonb_key_order(value[key])
            for key in sorted(value, key=lambda key: (len(key.encode()), key.encode()))
        }
    if isinstance(value, list):
        return [jsonb_key_order(item) for item in value]
    return value


def test_an_entry_renders_the_same_bytes_from_a_plan_reloaded_out_of_jsonb() -> None:
    """A plan's prefix re-renders from its persisted specs, and jsonb keeps no key order."""
    declared = _discovered("Send a message.").model_copy(
        update={
            "name": "mcp.mail_send.send_message",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "to": {"type": "array", "items": {"type": "string"}},
                    "body": {"type": "string"},
                    "cc": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["to", "subject", "body"],
            },
        }
    )
    reloaded = declared.model_copy(update={"input_schema": jsonb_key_order(declared.input_schema)})

    assert prefix_bytes(
        build_prefix(agent(), [], deferred_tools=[reloaded]), [], [reloaded]
    ) == prefix_bytes(build_prefix(agent(), [], deferred_tools=[declared]), [], [declared])
    [_system, data] = deferred_index_items([reloaded])
    assert _text(data).splitlines()[1] == (
        "- mcp.mail_send.send_message(to, subject, body, cc?): Send a message."
    )


def test_a_long_summary_is_cut_to_one_bounded_sentence() -> None:
    [_system, data] = deferred_index_items([_discovered("word " * 80)])

    entry = _text(data).splitlines()[1]
    summary = entry.split("): ", 1)[1]
    assert len(summary) == 200
    assert summary.endswith("…")
