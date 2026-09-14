"""Typed call-source selectors for the shared correspondence erasure graph."""

import json
from typing import Any

MARKER = "[call source erased]"


def erase_derived(value: Any) -> Any:
    """Remove generated content while keeping audit identities and typed envelopes."""
    if isinstance(value, list):
        return [erase_derived(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "raw_arguments":
            result[key] = "{}"
        elif key in {"arguments", "normalized_arguments", "structured_result", "data"}:
            result[key] = {} if item is not None else None
        elif key in {"content", "text", "final_message", "summary", "reason", "message", "error"}:
            if isinstance(item, list):
                result[key] = [{"type": "text", "text": MARKER}]
            elif isinstance(item, dict):
                result[key] = erase_derived(item)
            else:
                result[key] = MARKER if item is not None else None
        else:
            result[key] = erase_derived(item)
    return result


def read_server(metadata: dict[str, Any], account_id: str) -> str:
    return "bland_read"


def source_tool(name: object, server: str | None) -> bool:
    return (
        server == "bland_read"
        and isinstance(name, str)
        and name
        in {
            "mcp.bland_read.get_call",
            "mcp.bland_read.list_calls",
        }
    )


def erase_result(
    value: Any,
    thread_id: str,
    message_ids: frozenset[str],
    inherited_thread: str | None = None,
    *,
    account_id: str | None = None,
) -> Any:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if not isinstance(decoded, (dict, list)):
            return value
        erased = erase_result(decoded, thread_id, message_ids)
        return json.dumps(erased, ensure_ascii=False) if erased != decoded else value
    if isinstance(value, list):
        return [erase_result(item, thread_id, message_ids) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("source") == "bland" and isinstance(value.get("call_id"), str):
        if value["call_id"] != thread_id:
            return value
        return {"source": "bland", "call_id": thread_id, "erased": True, "provider_deleted": False}
    return {key: erase_result(item, thread_id, message_ids) for key, item in value.items()}


def source_present(
    value: Any,
    thread_id: str,
    message_ids: frozenset[str],
    inherited: str | None = None,
    *,
    account_id: str | None = None,
) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    if isinstance(value, list):
        return any(source_present(item, thread_id, message_ids) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("source") == "bland" and isinstance(value.get("call_id"), str):
        return bool(value["call_id"] == thread_id)
    return any(source_present(item, thread_id, message_ids) for item in value.values())
