"""Keep bounded discovery from consuming the calls needed for requested files."""

import base64
import hashlib
import json
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import MCPCallResult, MCPServerConfig
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_attachment_save_adr0296 import CONTENT
from tests.gates.test_email_learning_m26 import observation, prepare
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _mailbox_factory


@pytest.mark.parametrize(
    "mode", ["broad", "select_unread", "repeat_search", "overflow", "forged", "incomplete_source"]
)
async def test_broad_search_leaves_room_for_the_complete_document_set(mode: str) -> None:
    documents = [
        CONTENT + f"\n% document {i}\n".encode() for i in range(8 if mode == "overflow" else 4)
    ]
    source = observation(body="The current board meeting materials are attached.")
    source["thread_id"] = "materials"
    source["messages"][0].update(id="materials-message", thread_id="materials")
    source["messages"][0]["attachments"] = [
        {"filename": f"board-{i}.pdf", "mime_type": "application/pdf", "size": len(data)}
        for i, data in enumerate(documents)
    ]
    base = await _mailbox_factory([])
    reads: list[tuple[str, dict[str, object]]] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call(name: str, arguments: dict[str, object]) -> MCPCallResult:
            reads.append((name, arguments))
            value: dict[str, Any]
            if name == "get_profile":
                value = {"email_address": "owner@example.com"}
            elif name == "search_threads":
                value = {
                    "threads": [
                        {"thread_id": "materials", "subject": "Current board meeting"},
                        *(
                            {"thread_id": f"other-{i}", "subject": "Unrelated meeting"}
                            for i in range(4)
                        ),
                    ]
                }
                if mode == "select_unread":
                    value["threads"].insert(0, value["threads"].pop(1))
            elif name == "get_thread_page":
                value = (
                    source
                    if arguments["thread_id"] == "materials"
                    else observation(body="Unrelated agenda")
                )
                value = {**value, "thread_id": arguments["thread_id"]}
                if arguments["thread_id"] != "materials":
                    value["messages"][0].update(
                        id=f"{arguments['thread_id']}-message", thread_id=arguments["thread_id"]
                    )
                    if mode == "incomplete_source":
                        page = int(str(arguments.get("page_token") or 0))
                        value["next_page_token"] = str(page + 1)
                        value["messages"][0]["id"] = f"paged-message-{page}"
                    if mode == "select_unread":
                        value["messages"][0]["attachments"] = [
                            {
                                "filename": "obsolete.pdf",
                                "mime_type": "application/pdf",
                                "size": 100,
                            }
                        ]
            elif name == "stage_attachment":
                index = cast(int, arguments["attachment_index"])
                value = {
                    "thread_id": "materials",
                    "message_id": "materials-message",
                    "filename": f"board-{index}.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(documents[index]),
                    "data_base64": base64.b64encode(documents[index]).decode(),
                }
            else:
                raise AssertionError(f"Drafting must not perform {name}")
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        client.call_tool = call  # type: ignore[method-assign]
        return client

    script = FakeModelScript(turns=[])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=factory,
    ) as app:
        service = await prepare(app)
        stored = await service.import_thread(app.principal, "work", source, uuid4())
        request = observation(body="Please send the board materials.")
        request["thread_id"] = "request"
        request["messages"][0].update(id="request-message", thread_id="request")
        thread = await service.import_thread(app.principal, "work", request, uuid4())
        identifiers = [
            hashlib.sha256(f"work:{stored.id}:materials-message:{i}".encode()).hexdigest()[:20]
            for i in range(len(documents))
        ]
        script.turns.extend(
            [
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "mail_queries": [
                                {
                                    "account_id": "work",
                                    "query": "board",
                                    "reason": "Find requested materials",
                                }
                            ],
                            "requires_attachments": True,
                        }
                    )
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "body": "The board materials are attached.",
                            "attachment_ids": identifiers,
                            "requires_attachments": True,
                        }
                    )
                ),
            ]
        )
        if mode in {"select_unread", "repeat_search", "forged", "incomplete_source"}:
            continuation: dict[str, Any]
            if mode == "repeat_search":
                continuation = {
                    "attachment_ids": identifiers,
                    "mail_queries": [{"account_id": "work", "query": "board", "reason": "Confirm"}],
                }
            elif mode == "incomplete_source":
                continuation = {
                    "source_ids": [hashlib.sha256(b"work:other-0").hexdigest()[:20]],
                    "attachment_ids": identifiers,
                }
            else:
                continuation = {
                    "source_ids": [
                        hashlib.sha256(b"work:materials").hexdigest()[:20]
                        if mode == "select_unread"
                        else "unobserved-source"
                    ]
                }
            script.turns.insert(1, ScriptedTurn(text=json.dumps(continuation)))
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        run = await app.runs.get(operation.run_id)
        if mode in {"overflow", "forged", "incomplete_source"}:
            assert run.status is RunStatus.FAILED
            assert run.failure is not None
            assert run.failure.error_class == "DraftPreparationError"
            expected = {
                "overflow": "complete attachment set",
                "forged": "observed search results",
                "incomplete_source": "source remains incomplete",
            }[mode]
            assert expected in run.failure.message
            assert not any(name == "stage_attachment" for name, _ in reads)
            assert (await service.thread(app.principal, thread.id))["draft"] is None
            return
        assert run.status is RunStatus.COMPLETED, run.failure
        detail = await service.thread(app.principal, thread.id)
        files = cast(dict[str, Any], detail)["draft"]["attachments"]
        assert {f["sha256"] for f in files} == {
            hashlib.sha256(data).hexdigest() for data in documents
        }
        assert len(reads) <= 10
        source_reads = [args["thread_id"] for name, args in reads if name == "get_thread_page"]
        if mode == "broad":
            assert source_reads == ["materials"]
        elif mode == "select_unread":
            assert source_reads == ["other-0", "materials"]
        else:
            assert len(source_reads) == 4
            assert sum(name == "search_threads" for name, _ in reads) == 1
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        assert "reads_remaining" in provider.requests[-1].model_dump_json()
        assert "unread_sources" in provider.requests[-1].model_dump_json()


async def test_reserve_covers_verifying_the_mailbox_of_selected_files() -> None:
    """Optional discovery leaves the profile read that staging the files needs."""
    documents = [CONTENT + f"\n% document {i}\n".encode() for i in range(8)]
    request = observation(body="Please send these files back to me.")
    request["thread_id"] = "request"
    request["messages"][0].update(id="request-message", thread_id="request")
    request["messages"][0]["attachments"] = [
        {"filename": f"board-{i}.pdf", "mime_type": "application/pdf", "size": len(data)}
        for i, data in enumerate(documents)
    ]
    base = await _mailbox_factory([])
    reads: list[str] = []

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)

        async def call(name: str, arguments: dict[str, object]) -> MCPCallResult:
            reads.append(name)
            value: dict[str, Any]
            if name == "get_profile":
                value = {"email_address": "owner@example.com"}
            elif name == "stage_attachment":
                index = cast(int, arguments["attachment_index"])
                value = {
                    "thread_id": "request",
                    "message_id": "request-message",
                    "filename": f"board-{index}.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(documents[index]),
                    "data_base64": base64.b64encode(documents[index]).decode(),
                }
            else:
                raise AssertionError(f"Drafting must not perform {name}")
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        client.call_tool = call  # type: ignore[method-assign]
        return client

    script = FakeModelScript(turns=[])
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        script=script,
        mcp_client_factory=factory,
    ) as app:
        service = await prepare(app)
        thread = await service.import_thread(app.principal, "work", request, uuid4())
        identifiers = [
            hashlib.sha256(f"work:{thread.id}:request-message:{i}".encode()).hexdigest()[:20]
            for i in range(len(documents))
        ]
        script.turns.extend(
            [
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "attachment_ids": identifiers,
                            "memory_queries": ["board preferences", "prior board replies"],
                        }
                    )
                ),
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "body": "The files are attached.",
                            "attachment_ids": identifiers,
                            "requires_attachments": True,
                        }
                    )
                ),
            ]
        )
        operation = await service.submit_task(app.principal, kind="draft", thread_id=thread.id)
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure and run.failure.message
        detail = await service.thread(app.principal, thread.id)
        files = cast(dict[str, Any], detail)["draft"]["attachments"]
        assert {f["sha256"] for f in files} == {
            hashlib.sha256(data).hexdigest() for data in documents
        }
        assert reads.count("get_profile") == 1 and reads.count("stage_attachment") == 8
