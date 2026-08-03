"""Raw provider fixtures authored once for the shared model contract."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


class ScriptedRawSource:
    def __init__(self, streams: list[list[dict[str, Any]]]) -> None:
        self.streams = streams
        self.requests: list[dict[str, Any]] = []
        self.index = 0

    async def __call__(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(request)
        events = self.streams[min(self.index, len(self.streams) - 1)]
        self.index += 1
        for event in events:
            yield event


def openai_tool_events(arguments: str, *, call_id: str = "call-byte-1") -> list[dict[str, Any]]:
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": "math.calculate",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": arguments,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-tool",
                "model": "contract-model",
                "status": "completed",
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 2},
                    "output_tokens": 4,
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            },
        },
    ]


def openai_text_events(text: str = "391") -> list[dict[str, Any]]:
    return [
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "delta": text,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-text",
                "model": "contract-model",
                "status": "completed",
                "usage": {
                    "input_tokens": 12,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    ]


def anthropic_tool_events(arguments: str, *, call_id: str = "call-byte-1") -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg-tool",
                "model": "contract-model",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": call_id,
                "name": "math.calculate",
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": arguments},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]


def anthropic_text_events(text: str = "391") -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg-text",
                "model": "contract-model",
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]


def chat_tool_events(arguments: str, *, call_id: str = "call-byte-1") -> list[dict[str, Any]]:
    payload = (
        '<tool_call>{"id":"'
        + call_id
        + '","name":"math.calculate","arguments":'
        + json.dumps(arguments)
        + "}</tool_call>"
    )
    return [
        {
            "id": "chat-tool",
            "model": "contract-model",
            "choices": [{"delta": {"content": payload[:21]}, "finish_reason": None}],
        },
        {
            "id": "chat-tool",
            "model": "contract-model",
            "choices": [{"delta": {"content": payload[21:]}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
        {"type": "done"},
    ]


def chat_text_events(text: str = "391") -> list[dict[str, Any]]:
    return [
        {
            "id": "chat-text",
            "model": "contract-model",
            "choices": [
                {"delta": {"content": f"<think>computed</think>{text}"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        },
        {"type": "done"},
    ]
