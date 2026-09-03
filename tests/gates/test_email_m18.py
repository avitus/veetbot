"""Milestone 18 first-class Gmail integration hard gates."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import webbrowser
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from mcp.types import CallToolResult
from pydantic import SecretStr

import gmail_mcp.__main__ as gmail_main
import gmail_mcp.client as gmail_client_module
from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.bootstrap import build
from agent_core.config import (
    AuthMode,
    ConfigurationError,
    DeploymentMode,
    SandboxMechanism,
    Settings,
    load_settings,
)
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.credentials import CredentialRef, SecretValue
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.notifications import NotificationKind
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    LoadedRuleset,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind, RunLimits, RunStatus
from agent_core.domain.schedules import OnceCadence, ScheduleDefinition
from agent_core.mcp.configuration import (
    build_stdio_environment,
    email_server_configs,
    validate_mcp_config,
)
from agent_core.mcp.mapping import map_discovered_tools
from agent_core.policy.engine import evaluate_deterministic
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.scheduling.worker import ScheduleWorker
from gmail_mcp.bootstrap import bootstrap_credentials
from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import (
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_ENDPOINT,
    OUTPUT_MAXIMUM_BYTES,
    ROSTERS,
)
from gmail_mcp.errors import GmailError
from gmail_mcp.server import create_server
from scripts.architecture_checks import architecture_errors
from tests.contract.support import tool_context

ROOT = Path(__file__).resolve().parents[2]


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _thread() -> dict[str, object]:
    return {
        "id": "thread-1",
        "messages": [
            {
                "id": "message-1",
                "threadId": "thread-1",
                "labelIds": ["INBOX", "UNREAD"],
                "snippet": "A bounded snippet",
                "internalDate": "1700000000000",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "Sender <sender@example.test>"},
                        {"name": "To", "value": "owner@example.test"},
                        {"name": "Subject", "value": "Contract subject"},
                        {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _encoded("plain body"), "size": 10},
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "document.pdf",
                            "body": {"attachmentId": "attachment-1", "size": 123},
                        },
                    ],
                },
            }
        ],
    }


class _FakeGmail:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(
                200,
                json={"access_token": "provider-access-token", "expires_in": 3600},
            )
        assert request.headers.get("Authorization") == "Bearer provider-access-token"
        path = request.url.path
        if request.method == "GET" and path.endswith("/threads"):
            return httpx.Response(
                200,
                json={"threads": [{"id": "thread-1"}], "nextPageToken": "next-page"},
            )
        if request.method == "GET" and path.endswith("/threads/thread-1"):
            return httpx.Response(200, json=_thread())
        if request.method == "GET" and path.endswith("/labels"):
            return httpx.Response(
                200,
                json={"labels": [{"id": "INBOX", "name": "Inbox", "type": "system"}]},
            )
        if request.method == "POST" and path.endswith("/drafts"):
            return httpx.Response(
                200,
                json={"id": "draft-1", "message": {"id": "message-draft", "threadId": "thread-1"}},
            )
        if request.method == "POST" and path.endswith("/modify"):
            return httpx.Response(200, json={"id": path.split("/")[-2], "labelIds": ["STARRED"]})
        if request.method == "POST" and path.endswith(("/trash", "/untrash")):
            return httpx.Response(200, json={"id": "thread-1", "labelIds": ["TRASH"]})
        if request.method == "POST" and path.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "sent-1", "threadId": "thread-1"})
        return httpx.Response(404, json={"error": {"message": "must not cross"}})


def _credential(mode: str) -> GmailCredential:
    return GmailCredential.parse(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "scope": GOOGLE_SCOPES[mode],
            }
        ),
        expected_scope=GOOGLE_SCOPES[mode],
    )


async def _client(mode: str, fake: _FakeGmail) -> GmailClient:
    transport = httpx.MockTransport(fake)
    return GmailClient(
        _credential(mode),
        http_client=httpx.AsyncClient(transport=transport, follow_redirects=False),
    )


def test_gmail_package_is_import_isolated_from_agent_core() -> None:
    """The first-party MCP server is a sibling, never a core dependency."""

    package = ROOT / "src" / "gmail_mcp"
    assert package.is_dir(), "Milestone 18 gmail_mcp package has not been implemented"
    errors = architecture_errors(ROOT)
    assert not [error for error in errors if "gmail_mcp" in error]


@pytest.mark.parametrize("mode", ["read", "write", "send"])
async def test_gmail_modes_pass_the_shared_contract(mode: str) -> None:
    fake = _FakeGmail()
    client = await _client(mode, fake)
    server = create_server(mode, client)
    tools = {tool.name for tool in await server.list_tools()}
    assert tools == set(ROSTERS[mode])

    if mode == "read":
        search = await server.call_tool(
            "search_threads",
            {"query": "newer_than:1d", "max_results": 5},
        )
        thread = await server.call_tool("get_thread", {"thread_id": "thread-1"})
        labels = await server.call_tool("list_labels", {})
        assert isinstance(search, CallToolResult)
        assert isinstance(thread, CallToolResult)
        assert isinstance(labels, CallToolResult)
        rendered = "\n".join(
            str(item) for result in (search, thread, labels) for item in result.content
        )
        assert "thread-1" in rendered
        assert "plain body" in rendered
        assert "document.pdf" in rendered
        assert "attachment-1" not in rendered
    elif mode == "write":
        draft = await server.call_tool(
            "create_draft",
            {"to": "recipient@example.test", "subject": "Draft", "body": "Body"},
        )
        modified = await server.call_tool(
            "modify_labels",
            {"thread_ids": ["thread-1"], "add_label_ids": ["STARRED"]},
        )
        trashed = await server.call_tool("trash_thread", {"thread_id": "thread-1"})
        restored = await server.call_tool("untrash_thread", {"thread_id": "thread-1"})
        assert isinstance(draft, CallToolResult)
        assert isinstance(modified, CallToolResult)
        assert isinstance(trashed, CallToolResult)
        assert isinstance(restored, CallToolResult)
        assert all(result.is_error is False for result in (draft, modified, trashed, restored))
    else:
        sent = await server.call_tool(
            "send_message",
            {"to": "recipient@example.test", "subject": "Reply", "body": "Approved"},
        )
        assert isinstance(sent, CallToolResult)
        assert sent.is_error is False
        invalid = await server.call_tool(
            "send_message",
            {
                "to": "recipient@example.test\nBcc: injected@example.test",
                "subject": "Rejected",
                "body": "Must not dispatch",
            },
        )
        assert isinstance(invalid, CallToolResult)
        assert invalid.is_error is True
        assert [item.text for item in invalid.content if hasattr(item, "text")] == [
            "gmail.arguments_invalid"
        ]

    assert all(
        request.url.host in {"gmail.googleapis.com", "oauth2.googleapis.com"}
        for request in fake.requests
    )
    await client.close()


@pytest.mark.parametrize("mode", ["read", "write", "send"])
async def test_each_mode_advertises_only_its_declared_roster(mode: str) -> None:
    fake = _FakeGmail()
    client = await _client(mode, fake)
    server = create_server(mode, client)
    names = tuple(sorted(tool.name for tool in await server.list_tools()))
    assert names == tuple(sorted(ROSTERS[mode]))
    assert not any("delete" in name for name in names)
    await client.close()


async def test_search_threads_fanout_is_bounded_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGmail()
    client = await _client("read", fake)
    active = 0
    peak = 0

    async def request(
        _method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, Any]:
        nonlocal active, peak
        if path == "/threads":
            return {"threads": [{"id": f"thread-{index}"} for index in range(6)]}
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        thread_id = path.rsplit("/", 1)[-1]
        messages = _thread()["messages"]
        assert isinstance(messages, list)
        message = messages[0]
        assert isinstance(message, dict)
        return {"messages": [{**message, "threadId": thread_id}]}

    monkeypatch.setattr(client, "_request", request)
    result = await client.search_threads("newer_than:1d", 6)

    assert peak == 5
    assert [thread["thread_id"] for thread in result["threads"]] == [
        f"thread-{index}" for index in range(6)
    ]


async def test_search_threads_fanout_has_one_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGmail()
    client = await _client("read", fake)

    async def request(
        _method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, Any]:
        if path == "/threads":
            return {"threads": [{"id": "thread-1"}]}
        await asyncio.sleep(1)
        return _thread()

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(
        gmail_client_module,
        "_THREAD_FANOUT_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    with pytest.raises(GmailError, match=r"gmail\.provider_unavailable"):
        await client.search_threads("newer_than:1d", 1)


async def test_partial_label_batch_reports_unknown_outcome() -> None:
    class _PartialLabelFailure(_FakeGmail):
        def __init__(self) -> None:
            super().__init__()
            self.modify_calls = 0

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/modify"):
                self.requests.append(request)
                self.modify_calls += 1
                if self.modify_calls == 2:
                    return httpx.Response(400, json={"error": {"message": "must not cross"}})
                return httpx.Response(200, json={"id": request.url.path.split("/")[-2]})
            return await super().__call__(request)

    fake = _PartialLabelFailure()
    client = await _client("write", fake)
    server = create_server("write", client)

    result = await server.call_tool(
        "modify_labels",
        {
            "thread_ids": ["thread-1", "thread-2"],
            "add_label_ids": ["STARRED"],
        },
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert [item.text for item in result.content if hasattr(item, "text")] == [
        "gmail.outcome_unknown"
    ]
    assert result.structured_content == {"effect_status": "unknown"}


def test_loopback_authorization_ignores_stray_requests_until_matching_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers: list[Any] = []

    class _Server:
        def __init__(self, _address: object, callback: Any) -> None:
            self.callback = callback
            self.timeout: float | None = None
            self.requests = 0
            servers.append(self)

        def handle_request(self) -> None:
            paths = (
                "/?state=wrong&code=stray",
                "/?state=expected-state&code=accepted-code",
            )
            request = type("Request", (), {})()
            request.path = paths[self.requests]
            request.wfile = io.BytesIO()
            request.send_response = lambda _status: None
            request.send_header = lambda *_args: None
            request.end_headers = lambda: None
            self.requests += 1
            self.callback.do_GET(request)

        def server_close(self) -> None:
            return

    monkeypatch.setattr(gmail_main, "HTTPServer", _Server)
    monkeypatch.setattr(webbrowser, "open", lambda *_args, **_kwargs: True)
    authorization_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?state=expected-state&"
        "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765"
    )

    assert gmail_main._authorize_via_loopback("read", authorization_url) == "accepted-code"
    assert len(servers) == 1
    assert servers[0].requests == 2


def _discovery(mode: str) -> MCPDiscovery:
    schemas: dict[str, dict[str, object]] = {
        "create_draft": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
                "thread_id": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        }
    }
    return MCPDiscovery(
        tools=tuple(
            MCPRemoteTool(
                name=name,
                description=f"Gmail {name}",
                input_schema=schemas.get(
                    name,
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
            )
            for name in ROSTERS[mode]
        )
    )


def _base_environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://localhost/email-m18",
        "DEPLOYMENT_MODE": "development",
        "AUTH_MODE": "dev",
        "SANDBOX_MECHANISM": "fake",
    }


def _credential_files(tmp_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for mode, variable in (
        ("read", "GMAIL_READ_CREDENTIAL_FILE"),
        ("write", "GMAIL_WRITE_CREDENTIAL_FILE"),
        ("send", "GMAIL_SEND_CREDENTIAL_FILE"),
    ):
        path = tmp_path / f"gmail-{mode}.json"
        path.write_text(_credential(mode).as_json(), encoding="utf-8")
        path.chmod(0o600)
        result[variable] = str(path)
    return result


def _account_credential_files(tmp_path: Path, account_id: str) -> dict[str, str]:
    directory = tmp_path / account_id
    directory.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for mode in ("read", "write", "send"):
        document = json.loads(_credential(mode).as_json())
        document["account_id"] = account_id
        path = directory / f"gmail-{mode}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        result[f"{mode}_credential_file"] = str(path)
    return result


def _accounts_manifest(
    tmp_path: Path,
    *,
    account_ids: tuple[str, ...] = ("personal", "work"),
    default_account: str = "personal",
) -> Path:
    manifest = {
        "version": 1,
        "default_account": default_account,
        "accounts": [
            {
                "account_id": account_id,
                **_account_credential_files(tmp_path, account_id),
            }
            for account_id in account_ids
        ],
    }
    path = tmp_path / "gmail-accounts.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


async def test_named_accounts_compose_isolated_server_triplets(tmp_path: Path) -> None:
    manifest = _accounts_manifest(tmp_path)
    settings = load_settings(
        {
            **_base_environment(),
            "AGENT_EMAIL_ENABLED": "1",
            "GMAIL_ACCOUNTS_FILE": str(manifest),
        }
    )

    assert settings.email_account_ids == ("personal", "work")
    assert set(settings.credentials) == {
        "gmail_read",
        "gmail_write",
        "gmail_send",
        "gmail_work_read",
        "gmail_work_write",
        "gmail_work_send",
    }
    rows = email_server_configs(
        "local",
        enabled=settings.email_enabled,
        account_ids=settings.email_account_ids,
    )
    assert {row.server_id for row in rows} == set(settings.credentials)
    assert len(rows) == 6
    for row in rows:
        assert row.credential_ref == row.server_id
        assert row.required_scopes == {f"mcp.{row.server_id}.use"}
        if row.server_id in {"gmail_read", "gmail_write", "gmail_send"}:
            assert "--account-id personal" in row.endpoint
        else:
            assert "--account-id work" in row.endpoint
        mode = row.server_id.rsplit("_", maxsplit=1)[-1]
        report = map_discovered_tools(row, _discovery(mode).tools)
        assert {mapped.remote_name for mapped in report.accepted} == set(ROSTERS[mode])
        validate_mcp_config(row, destination_allowed=lambda _url: True)

    scripts = {
        row.server_id: ScriptedMCPServer(
            name=row.server_id,
            discovery=_discovery(row.server_id.rsplit("_", maxsplit=1)[-1]),
        )
        for row in rows
    }
    async with build(
        settings=settings,
        sequential_ids=True,
        mcp_client_factory=ScriptedMCPClientFactory(scripts),
    ) as composition:
        async with composition.uow_factory() as uow:
            stored = await uow.mcp_servers.list_enabled("local")
        assert {row.server_id for row in stored} == set(settings.credentials)
        assert {
            scope for scope in composition.principal.scopes if scope.startswith("mcp.gmail_")
        } == {f"mcp.{server_id}.use" for server_id in settings.credentials}


@hypothesis_settings(max_examples=24)
@given(
    account_ids=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    credential=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=64,
    ),
)
def test_named_account_credentials_cross_only_their_server_environment(
    account_ids: list[str],
    credential: str,
) -> None:
    rows = email_server_configs("tenant-email", account_ids=tuple(account_ids))
    assert len(rows) == len(account_ids) * 3
    assert len({row.server_id for row in rows}) == len(rows)
    assert len({row.credential_ref for row in rows}) == len(rows)
    for row in rows:
        opaque = f"ACCOUNT::{row.server_id}::{credential}::END"
        environment = build_stdio_environment(row, SecretValue(opaque))
        assert environment["GMAIL_MCP_CREDENTIAL"] == opaque
        assert [value for value in environment.values() if value == opaque] == [opaque]
        assert opaque not in row.endpoint
        assert opaque not in repr(row)


async def test_account_bound_bootstrap_tags_and_validates_credentials(tmp_path: Path) -> None:
    bootstrap_arguments = gmail_main._parser().parse_args(["bootstrap", "--account-id", "work"])
    assert bootstrap_arguments.bootstrap_account_id == "work"
    server_arguments = gmail_main._parser().parse_args(["--mode", "read", "--account-id", "work"])
    assert server_arguments.account_id == "work"

    def authorize(mode: str, _url: str) -> str:
        return f"authorization-code-{mode}"

    async def token_exchange(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        mode = form["code"][0].removeprefix("authorization-code-")
        return httpx.Response(
            200,
            json={
                "refresh_token": f"refresh-{mode}",
                "scope": GOOGLE_SCOPES[mode],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(token_exchange))
    oauth_client_secret = "-".join(("oauth", "client", "secret"))
    paths = await bootstrap_credentials(
        client_id="oauth-client-id",
        client_secret=oauth_client_secret,
        account_id="work",
        output_directory=tmp_path,
        authorize=authorize,
        http_client=client,
    )

    for mode, path in zip(("read", "write", "send"), paths, strict=True):
        parsed = GmailCredential.parse(
            path.read_text(),
            expected_scope=GOOGLE_SCOPES[mode],
            expected_account_id="work",
        )
        assert parsed.account_id == "work"
        with pytest.raises(GmailError, match=r"gmail\.credential_rejected"):
            GmailCredential.parse(
                path.read_text(),
                expected_scope=GOOGLE_SCOPES[mode],
                expected_account_id="personal",
            )

    legacy = _credential("read").as_json()
    GmailCredential.parse(legacy, expected_scope=GOOGLE_SCOPES["read"])
    with pytest.raises(GmailError, match=r"gmail\.credential_rejected"):
        GmailCredential.parse(
            legacy,
            expected_scope=GOOGLE_SCOPES["read"],
            expected_account_id="work",
        )


def test_multi_account_manifest_rejects_every_invalid_boundary(tmp_path: Path) -> None:
    def assert_rejected(manifest: object, *, extra_values: dict[str, str] | None = None) -> None:
        path = tmp_path / "candidate.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_settings(
                {
                    **_base_environment(),
                    "AGENT_EMAIL_ENABLED": "1",
                    "GMAIL_ACCOUNTS_FILE": str(path),
                    **(extra_values or {}),
                }
            )

    valid_path = _accounts_manifest(tmp_path)
    valid = json.loads(valid_path.read_text())
    invalid_documents: list[object] = [
        {**valid, "version": 2},
        {**valid, "unknown": True},
        {**valid, "accounts": []},
        {
            **valid,
            "accounts": [
                {
                    "account_id": f"account{index}",
                    **_account_credential_files(tmp_path, f"a{index}"),
                }
                for index in range(9)
            ],
            "default_account": "account0",
        },
        {**valid, "accounts": [valid["accounts"][0], valid["accounts"][0]]},
        {**valid, "default_account": "missing"},
        {
            **valid,
            "accounts": [{**valid["accounts"][0], "account_id": "Work"}],
            "default_account": "Work",
        },
        {
            **valid,
            "accounts": [{**valid["accounts"][0], "unknown": "field"}],
        },
        {
            **valid,
            "accounts": [{**valid["accounts"][0], "read_credential_file": "relative.json"}],
        },
        {
            **valid,
            "accounts": [
                {
                    key: value
                    for key, value in valid["accounts"][0].items()
                    if key != "send_credential_file"
                }
            ],
        },
    ]
    for document in invalid_documents:
        assert_rejected(document)

    insecure = Path(valid["accounts"][0]["read_credential_file"])
    insecure.chmod(0o644)
    assert_rejected(valid)
    insecure.chmod(0o600)

    assert_rejected(
        valid,
        extra_values={"GMAIL_READ_CREDENTIAL_FILE": valid["accounts"][0]["read_credential_file"]},
    )

    with pytest.raises(ConfigurationError):
        load_settings(
            {
                **_base_environment(),
                "GMAIL_ACCOUNTS_FILE": str(valid_path),
            }
        )

    eight_root = tmp_path / "eight"
    eight_path = _accounts_manifest(
        eight_root,
        account_ids=tuple(f"account{index}" for index in range(8)),
        default_account="account0",
    )
    eight_settings = load_settings(
        {
            **_base_environment(),
            "AGENT_EMAIL_ENABLED": "1",
            "GMAIL_ACCOUNTS_FILE": str(eight_path),
        }
    )
    assert len(eight_settings.email_account_ids) == 8
    assert len(eight_settings.credentials) == 24

    eight_path.chmod(0o666)
    with pytest.raises(ConfigurationError):
        load_settings(
            {
                **_base_environment(),
                "AGENT_EMAIL_ENABLED": "1",
                "GMAIL_ACCOUNTS_FILE": str(eight_path),
            }
        )


def test_registered_gmail_specs_have_exact_classification_and_trust() -> None:
    expected = {
        "gmail_read": (SideEffectClass.NETWORK_READ, RiskLevel.LOW, IdempotencyClass.READ_ONLY),
        "gmail_write": (
            SideEffectClass.EXTERNAL_WRITE,
            RiskLevel.MEDIUM,
            IdempotencyClass.NON_IDEMPOTENT,
        ),
        "gmail_send": (
            SideEffectClass.EXTERNAL_MESSAGE,
            RiskLevel.HIGH,
            IdempotencyClass.NON_IDEMPOTENT,
        ),
    }
    configs = email_server_configs("tenant-email")
    assert len(configs) == 3
    for config in configs:
        mode = config.server_id.removeprefix("gmail_")
        report = map_discovered_tools(config, _discovery(mode).tools)
        assert report.accepted
        for mapped in report.accepted:
            assert (
                mapped.spec.side_effect,
                mapped.spec.risk,
                mapped.spec.idempotency,
            ) == expected[config.server_id]
            assert mapped.spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
            assert mapped.spec.source.value == "mcp"
            assert mapped.spec.required_scopes == {f"mcp.{config.server_id}.use"}


def _ruleset() -> LoadedRuleset:
    return DEFAULT_RULESET.model_copy(deep=True)


def _action(
    *,
    server_id: str,
    side_effect: SideEffectClass,
    idempotency: IdempotencyClass,
    origin: TrustLevel = TrustLevel.USER,
) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=1),
        tenant_id="tenant-email",
        session_id=UUID(int=2),
        run_id=UUID(int=3),
        step_number=1,
        name=f"mcp.{server_id}.fixture",
        version="v1",
        summary="email action",
        side_effect=side_effect,
        risk=RiskLevel.LOW,
        idempotency=idempotency,
        required_scopes={f"mcp.{server_id}.use"},
        arguments={},
        normalized_arguments_hash="a" * 64,
        origin_trust=origin,
        target=ExecutionTarget(
            kind="mcp",
            isolated=False,
            network_enabled=False,
            server_id=server_id,
        ),
        evaluated_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


def _principal() -> Principal:
    return Principal(
        tenant_id="tenant-email",
        principal_id="owner",
        roles={"user"},
        scopes={"mcp.gmail_read.use", "mcp.gmail_write.use", "mcp.gmail_send.use"},
    )


def _run() -> Run:
    return Run(
        id=UUID(int=3),
        tenant_id="tenant-email",
        session_id=UUID(int=2),
        agent_id=UUID(int=4),
        agent_version="v1",
        status=RunStatus.RUNNING,
        kind=RunKind.INTERACTIVE,
        limits=RunLimits(max_steps=10, max_model_calls=10, max_tool_calls=10),
        principal_scopes={
            "mcp.gmail_read.use",
            "mcp.gmail_write.use",
            "mcp.gmail_send.use",
        },
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
        updated_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


def test_default_policy_allows_reads_and_requires_write_and_send_approval() -> None:
    decisions = {
        "read": evaluate_deterministic(
            _action(
                server_id="gmail_read",
                side_effect=SideEffectClass.NETWORK_READ,
                idempotency=IdempotencyClass.READ_ONLY,
            ),
            _principal(),
            _run(),
            _ruleset(),
        ).decision,
        "write": evaluate_deterministic(
            _action(
                server_id="gmail_write",
                side_effect=SideEffectClass.EXTERNAL_WRITE,
                idempotency=IdempotencyClass.NON_IDEMPOTENT,
            ),
            _principal(),
            _run(),
            _ruleset(),
        ).decision,
        "send": evaluate_deterministic(
            _action(
                server_id="gmail_send",
                side_effect=SideEffectClass.EXTERNAL_MESSAGE,
                idempotency=IdempotencyClass.NON_IDEMPOTENT,
            ),
            _principal(),
            _run(),
            _ruleset(),
        ).decision,
    }
    assert decisions == {
        "read": PolicyDecisionType.ALLOW,
        "write": PolicyDecisionType.REQUIRE_APPROVAL,
        "send": PolicyDecisionType.REQUIRE_APPROVAL,
    }

    named_scopes = {
        "mcp.gmail_work_read.use",
        "mcp.gmail_work_write.use",
        "mcp.gmail_work_send.use",
    }
    named_principal = _principal().model_copy(
        update={"scopes": {*_principal().scopes, *named_scopes}}, deep=True
    )
    named_run = _run().model_copy(
        update={"principal_scopes": {*_run().principal_scopes, *named_scopes}}, deep=True
    )
    assert (
        evaluate_deterministic(
            _action(
                server_id="gmail_work_read",
                side_effect=SideEffectClass.NETWORK_READ,
                idempotency=IdempotencyClass.READ_ONLY,
            ),
            named_principal,
            named_run,
            _ruleset(),
        ).decision
        is PolicyDecisionType.ALLOW
    )
    for server_id, side_effect in (
        ("gmail_work_write", SideEffectClass.EXTERNAL_WRITE),
        ("gmail_work_send", SideEffectClass.EXTERNAL_MESSAGE),
    ):
        assert (
            evaluate_deterministic(
                _action(
                    server_id=server_id,
                    side_effect=side_effect,
                    idempotency=IdempotencyClass.NON_IDEMPOTENT,
                ),
                named_principal,
                named_run,
                _ruleset(),
            ).decision
            is PolicyDecisionType.REQUIRE_APPROVAL
        )


def test_untrusted_mail_can_never_plain_allow_a_send() -> None:
    permissive = _ruleset()
    rules = tuple(
        rule.model_copy(update={"decision": PolicyDecisionType.ALLOW})
        if rule.side_effect is SideEffectClass.EXTERNAL_MESSAGE
        else rule
        for rule in permissive.rules
    )
    permissive = permissive.model_copy(update={"rules": rules})
    decision = evaluate_deterministic(
        _action(
            server_id="gmail_send",
            side_effect=SideEffectClass.EXTERNAL_MESSAGE,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            origin=TrustLevel.EXTERNAL_UNTRUSTED,
        ),
        _principal(),
        _run(),
        permissive,
    )
    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


def test_named_account_policy_profile_cannot_autoapprove_mutations() -> None:
    permissive = _ruleset()
    permissive = permissive.model_copy(
        update={
            "rules": tuple(
                rule.model_copy(update={"decision": PolicyDecisionType.ALLOW})
                if rule.side_effect
                in {SideEffectClass.EXTERNAL_WRITE, SideEffectClass.EXTERNAL_MESSAGE}
                else rule
                for rule in permissive.rules
            )
        }
    )
    scopes = {
        "mcp.gmail_work_write.use",
        "mcp.gmail_work_send.use",
    }
    principal = _principal().model_copy(update={"scopes": scopes}, deep=True)
    run = _run().model_copy(update={"principal_scopes": scopes}, deep=True)
    for server_id, side_effect in (
        ("gmail_work_write", SideEffectClass.EXTERNAL_WRITE),
        ("gmail_work_send", SideEffectClass.EXTERNAL_MESSAGE),
    ):
        decision = evaluate_deterministic(
            _action(
                server_id=server_id,
                side_effect=side_effect,
                idempotency=IdempotencyClass.NON_IDEMPOTENT,
            ),
            principal,
            run,
            permissive,
        )
        assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


@hypothesis_settings(max_examples=24)
@given(
    mode=st.sampled_from(("read", "write", "send")),
    credential=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=96,
    ),
)
def test_email_credentials_cross_only_the_constructed_declared_environment(
    mode: str,
    credential: str,
) -> None:
    configs = {config.server_id: config for config in email_server_configs("tenant-email")}
    config = configs[f"gmail_{mode}"]
    opaque_credential = f"M18_CREDENTIAL::{credential}::END"
    environment = build_stdio_environment(
        config,
        SecretValue(opaque_credential),
        synthesized={"HARMLESS_RUNTIME_VALUE": "present"},
    )
    assert environment["HARMLESS_RUNTIME_VALUE"] == "present"
    assert environment["GMAIL_MCP_CREDENTIAL"] == opaque_credential
    assert [name for name, value in environment.items() if value == opaque_credential] == [
        "GMAIL_MCP_CREDENTIAL"
    ]
    assert opaque_credential not in config.endpoint
    assert opaque_credential not in repr(config)


async def test_email_is_default_off_and_grants_nothing(tmp_path: Path) -> None:
    settings = load_settings(_base_environment())
    assert settings.email_enabled is False
    assert not any(name.startswith("gmail_") for name in settings.credentials)
    async with build(settings=settings, sequential_ids=True) as composition:
        assert not any(scope.startswith("mcp.gmail_") for scope in composition.principal.scopes)
        assert not email_server_configs("local", enabled=settings.email_enabled)


def test_email_scope_confinement_rejects_any_nonexact_scope() -> None:
    config = email_server_configs("tenant-email")[0]
    validate_mcp_config(config, destination_allowed=lambda _url: True)
    with pytest.raises(ValueError, match="exactly its use scope"):
        validate_mcp_config(
            config.model_copy(update={"required_scopes": {"mcp.gmail_read.admin"}}),
            destination_allowed=lambda _url: True,
        )


def test_enabled_email_loads_three_private_credentials_and_rows(tmp_path: Path) -> None:
    values = {
        **_base_environment(),
        "AGENT_EMAIL_ENABLED": "1",
        **_credential_files(tmp_path),
    }
    settings = load_settings(values)
    assert settings.email_enabled is True
    assert set(settings.credentials) == {"gmail_read", "gmail_write", "gmail_send"}
    rows = email_server_configs("local", enabled=settings.email_enabled)
    assert len(rows) == 3
    assert all(row.endpoint.startswith(f"{sys.executable} -m gmail_mcp --mode ") for row in rows)
    assert {scope for row in rows for scope in row.required_scopes} == {
        "mcp.gmail_read.use",
        "mcp.gmail_write.use",
        "mcp.gmail_send.use",
    }
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in tmp_path.glob("*.json"))


class _FailingGmail:
    def __init__(self, *, status: int | None = None, timeout: bool = False) -> None:
        self.status = status
        self.timeout = timeout
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(
                200,
                json={"access_token": "provider-access-token", "expires_in": 3600},
            )
        if self.timeout:
            raise httpx.ReadTimeout("raw provider timeout must not cross", request=request)
        return httpx.Response(
            self.status or 500,
            headers={"WWW-Authenticate": "Bearer raw-provider-diagnostic"},
            json={"error": {"message": "raw provider error must not cross"}},
        )


async def _assert_provider_failure_is_stable_and_classification_aware(
    status: int,
    read_code: str,
    write_code: str,
) -> None:
    read_fake = _FailingGmail(status=status)
    read = await _client("read", read_fake)  # type: ignore[arg-type]
    with pytest.raises(Exception) as read_failure:
        await read.list_labels()
    assert str(read_failure.value) == read_code

    write_fake = _FailingGmail(status=status)
    write = await _client("write", write_fake)  # type: ignore[arg-type]
    with pytest.raises(Exception) as write_failure:
        await write.create_draft("recipient@example.test", "subject", "body")
    assert str(write_failure.value) == write_code
    rendered = f"{read_failure.value!r} {write_failure.value!r}"
    assert "raw provider" not in rendered
    write_requests = [
        request for request in write_fake.requests if request.url.host == "gmail.googleapis.com"
    ]
    assert len(write_requests) == 1


async def _assert_lost_mutating_response_is_unknown_and_never_retried() -> None:
    fake = _FailingGmail(timeout=True)
    client = await _client("send", fake)  # type: ignore[arg-type]
    with pytest.raises(Exception, match=r"gmail\.outcome_unknown"):
        await client.send_message("recipient@example.test", "subject", "body")
    gmail_requests = [
        request for request in fake.requests if request.url.host == "gmail.googleapis.com"
    ]
    assert len(gmail_requests) == 1


async def _assert_oversized_thread_body_is_truncated_within_the_output_budget() -> None:
    large_body = "mail-body-" * 180_000

    class _LargeThread(_FakeGmail):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/threads/thread-1"):
                self.requests.append(request)
                payload = _thread()
                messages = payload["messages"]
                assert isinstance(messages, list)
                message = messages[0]
                assert isinstance(message, dict)
                mime_payload = message["payload"]
                assert isinstance(mime_payload, dict)
                parts = mime_payload["parts"]
                assert isinstance(parts, list)
                part = parts[0]
                assert isinstance(part, dict)
                part["body"] = {"data": _encoded(large_body), "size": len(large_body)}
                return httpx.Response(200, json=payload)
            return await super().__call__(request)

    fake = _LargeThread()
    client = await _client("read", fake)
    result = await client.get_thread("thread-1")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= OUTPUT_MAXIMUM_BYTES
    messages = result["messages"]
    assert isinstance(messages, list) and messages
    assert len(messages[0]["body"]) < len(large_body)


async def test_token_and_raw_upstream_text_never_cross_the_mcp_result() -> None:
    class _RejectedCredential:
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": "Bearer provider-secret-diagnostic"},
                json={"error": "provider-secret-error"},
            )

    transport = httpx.MockTransport(_RejectedCredential())
    client = GmailClient(
        _credential("read"),
        http_client=httpx.AsyncClient(transport=transport, follow_redirects=False),
    )
    result = await create_server("read", client).call_tool("list_labels", {})
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    rendered = " ".join(str(item) for item in result.content)
    assert "gmail.credential_rejected" in rendered
    assert "provider-secret" not in rendered
    assert "refresh-token" not in rendered


def _email_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/email-m18",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={
            f"gmail_{mode}": SecretStr(_credential(mode).as_json())
            for mode in ("read", "write", "send")
        },
        interpolation={"OPENAI_MODEL": ""},
        email_enabled=True,
    )


async def _assert_platform_maps_postdispatch_nonidempotent_failure_to_uncertain() -> None:
    scripts = {
        "gmail_read": ScriptedMCPServer(
            name="gmail_read",
            discovery=_discovery("read"),
            responses=(
                ScriptedMCPResponse(
                    name="list_labels",
                    result=MCPCallResult(
                        content=("gmail.provider_unavailable",),
                        is_error=True,
                    ),
                ),
            ),
        ),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("normalized.server_failure",),
                        is_error=True,
                    ),
                ),
                ScriptedMCPResponse(name="create_draft", outcome="disconnect"),
            ),
        ),
        "gmail_send": ScriptedMCPServer(
            name="gmail_send",
            discovery=_discovery("send"),
            responses=(
                ScriptedMCPResponse(
                    name="send_message",
                    result=MCPCallResult(
                        content=("gmail.provider_unavailable",),
                        structured={"effect_status": "not_applied"},
                        is_error=True,
                    ),
                ),
                ScriptedMCPResponse(
                    name="send_message",
                    result=MCPCallResult(
                        content=("gmail.provider_unavailable",),
                        is_error=True,
                    ),
                ),
            ),
        ),
    }
    factory = ScriptedMCPClientFactory(scripts)
    async with build(
        settings=_email_settings(),
        sequential_ids=True,
        mcp_client_factory=factory,
    ) as composition:
        session_id = await composition.sessions.create()
        context = replace(
            tool_context(),
            session_id=session_id,
            tenant_id="local",
            principal=composition.principal,
        )
        configs = {config.server_id: config for config in email_server_configs("local")}
        read_spec = next(
            mapped.spec
            for mapped in map_discovered_tools(
                configs["gmail_read"], _discovery("read").tools
            ).accepted
            if mapped.remote_name == "list_labels"
        )
        write_spec = next(
            mapped.spec
            for mapped in map_discovered_tools(
                configs["gmail_write"], _discovery("write").tools
            ).accepted
            if mapped.remote_name == "create_draft"
        )
        send_spec = next(
            mapped.spec
            for mapped in map_discovered_tools(
                configs["gmail_send"], _discovery("send").tools
            ).accepted
            if mapped.remote_name == "send_message"
        )
        read_result = await composition.mcp.call_tool(context, read_spec, "list_labels", {})
        write_result = await composition.mcp.call_tool(context, write_spec, "create_draft", {})
        conditional_result = await composition.mcp.call_tool(
            context,
            write_spec.model_copy(
                update={"idempotency": IdempotencyClass.CONDITIONALLY_IDEMPOTENT}
            ),
            "create_draft",
            {},
        )
        safe_send_result = await composition.mcp.call_tool(
            context,
            send_spec,
            "send_message",
            {},
        )
        idempotent_send_result = await composition.mcp.call_tool(
            context,
            send_spec.model_copy(update={"idempotency": IdempotencyClass.IDEMPOTENT}),
            "send_message",
            {},
        )
    assert read_result.failure is not None and read_result.failure.retryable is True
    assert write_result.failure is not None
    assert write_result.failure.reason_code == "tool.outcome_unknown"
    assert write_result.failure.retryable is False
    assert conditional_result.failure is not None
    assert conditional_result.failure.reason_code == "tool.outcome_unknown"
    assert conditional_result.failure.retryable is False
    assert safe_send_result.failure is not None
    assert safe_send_result.failure.reason_code == "tool.server_error"
    assert safe_send_result.failure.retryable is True
    assert idempotent_send_result.failure is not None
    assert idempotent_send_result.failure.reason_code == "tool.server_error"
    assert idempotent_send_result.failure.retryable is True

    repeated_arguments = {
        "to": "recipient@example.test",
        "subject": "One effect only",
        "body": "Do not retry an uncertain draft.",
    }
    repeated_scripts = {
        "gmail_read": ScriptedMCPServer(name="gmail_read", discovery=_discovery("read")),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.outcome_unknown",),
                        structured={"effect_status": "unknown"},
                        is_error=True,
                    ),
                ),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    repeated_factory = ScriptedMCPClientFactory(repeated_scripts)
    repeated_script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="mcp.gmail_write.create_draft",
                        arguments=repeated_arguments,
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="mcp.gmail_write.create_draft",
                        arguments=repeated_arguments,
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="The uncertain draft was not proposed again.",
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    async with build(
        settings=_email_settings(),
        script=repeated_script,
        sequential_ids=True,
        mcp_client_factory=repeated_factory,
    ) as composition:
        run_id = await composition.runs.submit("Create one draft only.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        terminal = await asyncio.wait_for(composition.runs.wait_terminal(run_id), timeout=2.0)
    assert terminal.status is RunStatus.COMPLETED
    write_client = next(client for client in repeated_factory.created if client.call_count)
    assert write_client.call_count == 1


class _RotatingGmailCredentials:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def resolve(self, reference: CredentialRef) -> SecretValue:
        count = self.calls.get(reference.name, 0)
        self.calls[reference.name] = count + 1
        mode = reference.name.removeprefix("gmail_")
        credential = _credential(mode)
        replacement = credential.__class__(
            client_id=credential.client_id,
            client_secret=credential.client_secret,
            refresh_token=f"refresh-token-{count}",
            scope=credential.scope,
        )
        return SecretValue(replacement.as_json())


class _UnavailableGmailRefresh:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def resolve(self, reference: CredentialRef) -> SecretValue:
        count = self.calls.get(reference.name, 0)
        self.calls[reference.name] = count + 1
        if count > 0:
            raise PermissionError("synthetic credential refresh failure")
        mode = reference.name.removeprefix("gmail_")
        return SecretValue(_credential(mode).as_json())


class _StableGmailCredentials:
    async def resolve(self, reference: CredentialRef) -> SecretValue:
        mode = reference.name.removeprefix("gmail_")
        return SecretValue(_credential(mode).as_json())


async def _assert_predispatch_credential_rejection_reauthenticates_once() -> None:
    scripts = {
        "gmail_read": ScriptedMCPServer(name="gmail_read", discovery=_discovery("read")),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        structured={"effect_status": "not_applied"},
                        is_error=True,
                    ),
                ),
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(content=("draft-created",)),
                ),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    factory = ScriptedMCPClientFactory(scripts)
    credentials = _RotatingGmailCredentials()
    async with build(
        settings=_email_settings(),
        sequential_ids=True,
        mcp_client_factory=factory,
        credential_resolver=credentials,
    ) as composition:
        session_id = await composition.sessions.create()
        context = replace(
            tool_context(),
            session_id=session_id,
            tenant_id="local",
            principal=composition.principal,
        )
        config = next(
            row for row in email_server_configs("local") if row.server_id == "gmail_write"
        )
        spec = next(
            mapped.spec
            for mapped in map_discovered_tools(config, _discovery("write").tools).accepted
            if mapped.remote_name == "create_draft"
        )
        result = await composition.mcp.call_tool(context, spec, "create_draft", {})
    assert result.ok is True
    write_client = next(client for client in factory.created if client.call_count)
    assert write_client.reauthentication_count == 1
    assert write_client.call_count == 2

    uncertain_retry_scripts = {
        "gmail_read": ScriptedMCPServer(name="gmail_read", discovery=_discovery("read")),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        structured={"effect_status": "not_applied"},
                        is_error=True,
                    ),
                ),
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        structured={"effect_status": "unknown"},
                        is_error=True,
                    ),
                ),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    uncertain_retry_factory = ScriptedMCPClientFactory(uncertain_retry_scripts)
    async with build(
        settings=_email_settings(),
        sequential_ids=True,
        mcp_client_factory=uncertain_retry_factory,
        credential_resolver=_RotatingGmailCredentials(),
    ) as composition:
        session_id = await composition.sessions.create()
        context = replace(
            tool_context(),
            session_id=session_id,
            tenant_id="local",
            principal=composition.principal,
        )
        config = next(
            row for row in email_server_configs("local") if row.server_id == "gmail_write"
        )
        spec = next(
            mapped.spec
            for mapped in map_discovered_tools(config, _discovery("write").tools).accepted
            if mapped.remote_name == "create_draft"
        )
        uncertain_retry_result = await composition.mcp.call_tool(context, spec, "create_draft", {})
    assert uncertain_retry_result.failure is not None
    assert uncertain_retry_result.failure.reason_code == "tool.outcome_unknown"
    assert uncertain_retry_result.failure.retryable is False

    prior_reauthentication_scripts = {
        "gmail_read": ScriptedMCPServer(name="gmail_read", discovery=_discovery("read")),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        structured={"effect_status": "not_applied"},
                        is_error=True,
                    ),
                ),
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(content=("draft-created",)),
                ),
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        structured={"effect_status": "unknown"},
                        is_error=True,
                    ),
                ),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    prior_reauthentication_factory = ScriptedMCPClientFactory(prior_reauthentication_scripts)
    async with build(
        settings=_email_settings(),
        sequential_ids=True,
        mcp_client_factory=prior_reauthentication_factory,
        credential_resolver=_RotatingGmailCredentials(),
    ) as composition:
        session_id = await composition.sessions.create()
        context = replace(
            tool_context(),
            session_id=session_id,
            tenant_id="local",
            principal=composition.principal,
        )
        config = next(
            row for row in email_server_configs("local") if row.server_id == "gmail_write"
        )
        spec = next(
            mapped.spec
            for mapped in map_discovered_tools(config, _discovery("write").tools).accepted
            if mapped.remote_name == "create_draft"
        )
        safe_rejection = await composition.mcp.call_tool(context, spec, "create_draft", {})
        ambiguous_after_reauthentication = await composition.mcp.call_tool(
            context, spec, "create_draft", {}
        )
    assert safe_rejection.ok is True
    assert ambiguous_after_reauthentication.failure is not None
    assert ambiguous_after_reauthentication.failure.reason_code == "tool.outcome_unknown"
    assert ambiguous_after_reauthentication.failure.retryable is False

    disconnected_scripts = {
        "gmail_read": ScriptedMCPServer(name="gmail_read", discovery=_discovery("read")),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        structured={"effect_status": "not_applied"},
                        is_error=True,
                    ),
                ),
                ScriptedMCPResponse(name="create_draft", outcome="disconnect"),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    disconnected_factory = ScriptedMCPClientFactory(disconnected_scripts)
    async with build(
        settings=_email_settings(),
        sequential_ids=True,
        mcp_client_factory=disconnected_factory,
        credential_resolver=_RotatingGmailCredentials(),
    ) as composition:
        session_id = await composition.sessions.create()
        context = replace(
            tool_context(),
            session_id=session_id,
            tenant_id="local",
            principal=composition.principal,
        )
        config = next(
            row for row in email_server_configs("local") if row.server_id == "gmail_write"
        )
        spec = next(
            mapped.spec
            for mapped in map_discovered_tools(config, _discovery("write").tools).accepted
            if mapped.remote_name == "create_draft"
        )
        result = await composition.mcp.call_tool(context, spec, "create_draft", {})
    assert result.failure is not None
    assert result.failure.reason_code == "tool.outcome_unknown"
    assert result.failure.retryable is False

    ambiguous_scripts = {
        "gmail_read": ScriptedMCPServer(name="gmail_read", discovery=_discovery("read")),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=("gmail.credential_rejected",),
                        is_error=True,
                    ),
                ),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    for resolver in (_StableGmailCredentials(), _UnavailableGmailRefresh()):
        ambiguous_factory = ScriptedMCPClientFactory(ambiguous_scripts)
        async with build(
            settings=_email_settings(),
            sequential_ids=True,
            mcp_client_factory=ambiguous_factory,
            credential_resolver=resolver,
        ) as composition:
            session_id = await composition.sessions.create()
            context = replace(
                tool_context(),
                session_id=session_id,
                tenant_id="local",
                principal=composition.principal,
            )
            config = next(
                row for row in email_server_configs("local") if row.server_id == "gmail_write"
            )
            spec = next(
                mapped.spec
                for mapped in map_discovered_tools(config, _discovery("write").tools).accepted
                if mapped.remote_name == "create_draft"
            )
            ambiguous_result = await composition.mcp.call_tool(context, spec, "create_draft", {})
        assert ambiguous_result.failure is not None
        assert ambiguous_result.failure.reason_code == "tool.outcome_unknown"
        assert ambiguous_result.failure.retryable is False


async def test_failure_taxonomy_is_bounded_and_nonidempotent_safe() -> None:
    for status, read_code, write_code in (
        (429, "gmail.rate_limited", "gmail.outcome_unknown"),
        (503, "gmail.provider_unavailable", "gmail.outcome_unknown"),
        (302, "gmail.provider_rejected", "gmail.provider_rejected"),
    ):
        await _assert_provider_failure_is_stable_and_classification_aware(
            status,
            read_code,
            write_code,
        )
    await _assert_lost_mutating_response_is_unknown_and_never_retried()
    await _assert_oversized_thread_body_is_truncated_within_the_output_budget()
    await _assert_platform_maps_postdispatch_nonidempotent_failure_to_uncertain()
    await _assert_predispatch_credential_rejection_reauthenticates_once()


async def test_bootstrap_consent_writes_exact_private_scope_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_authorizations: list[tuple[str, str]] = []
    observed_token_requests: list[httpx.Request] = []

    def authorize(mode: str, url: str) -> str:
        observed_authorizations.append((mode, url))
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
            "https://accounts.google.com/o/oauth2/v2/auth"
        )
        assert query["scope"] == [GOOGLE_SCOPES[mode]]
        assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
        assert query["code_challenge_method"] == ["S256"]
        assert query["access_type"] == ["offline"]
        return f"authorization-code-{mode}"

    async def token_exchange(request: httpx.Request) -> httpx.Response:
        observed_token_requests.append(request)
        form = parse_qs(request.content.decode())
        code = form["code"][0]
        mode = code.removeprefix("authorization-code-")
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{mode}",
                "refresh_token": f"refresh-{mode}",
                "expires_in": 3600,
                "scope": GOOGLE_SCOPES[mode],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(token_exchange),
        follow_redirects=False,
    )
    bootstrap_secret = "-".join(("oauth", "client", "secret"))
    paths = await bootstrap_credentials(
        client_id="oauth-client-id",
        client_secret=bootstrap_secret,
        output_directory=tmp_path,
        authorize=authorize,
        http_client=client,
    )
    assert tuple(path.name for path in paths) == (
        "gmail-read.json",
        "gmail-write.json",
        "gmail-send.json",
    )
    assert [mode for mode, _url in observed_authorizations] == ["read", "write", "send"]
    assert len(observed_token_requests) == 3
    assert all(
        request.url == httpx.URL(GOOGLE_TOKEN_ENDPOINT) for request in observed_token_requests
    )
    for mode, path in zip(("read", "write", "send"), paths, strict=True):
        assert path.stat().st_mode & 0o777 == 0o600
        parsed = GmailCredential.parse(path.read_text(), expected_scope=GOOGLE_SCOPES[mode])
        assert parsed.refresh_token == f"refresh-{mode}"

    settings = load_settings(
        {
            **_base_environment(),
            "AGENT_EMAIL_ENABLED": "1",
            "GMAIL_READ_CREDENTIAL_FILE": str(paths[0]),
            "GMAIL_WRITE_CREDENTIAL_FILE": str(paths[1]),
            "GMAIL_SEND_CREDENTIAL_FILE": str(paths[2]),
        }
    )
    assert set(settings.credentials) == {"gmail_read", "gmail_write", "gmail_send"}
    output = capsys.readouterr().out
    assert "refresh-read" not in output
    assert "access-read" not in output
    assert bootstrap_secret not in output
    assert all(str(path) in output for path in paths)

    real_fdopen = os.fdopen
    fdopen_calls = 0

    def fail_second_publish(*args: Any, **kwargs: Any) -> Any:
        nonlocal fdopen_calls
        fdopen_calls += 1
        if fdopen_calls == 2:
            raise OSError("synthetic publish failure")
        return real_fdopen(*args, **kwargs)

    monkeypatch.setattr(os, "fdopen", fail_second_publish)
    failed_directory = tmp_path / "failed-publish"
    with pytest.raises(OSError, match="synthetic publish failure"):
        await bootstrap_credentials(
            client_id="oauth-client-id",
            client_secret=bootstrap_secret,
            output_directory=failed_directory,
            authorize=authorize,
            http_client=client,
        )
    monkeypatch.setattr(os, "fdopen", real_fdopen)
    assert list(failed_directory.iterdir()) == []

    class _WriteFailure:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> _WriteFailure:
            self.stream.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.stream.__exit__(*args)

        def write(self, _value: bytes) -> int:
            raise OSError("synthetic write failure")

        def flush(self) -> None:
            self.stream.flush()

        def fileno(self) -> int:
            return int(self.stream.fileno())

    def fail_during_write(*args: Any, **kwargs: Any) -> _WriteFailure:
        return _WriteFailure(real_fdopen(*args, **kwargs))

    manual_closes: list[int] = []
    monkeypatch.setattr(os, "fdopen", fail_during_write)
    monkeypatch.setattr(os, "close", manual_closes.append)
    write_failed_directory = tmp_path / "write-failed-publish"
    with pytest.raises(OSError, match="synthetic write failure"):
        await bootstrap_credentials(
            client_id="oauth-client-id",
            client_secret=bootstrap_secret,
            output_directory=write_failed_directory,
            authorize=authorize,
            http_client=client,
        )
    assert manual_closes == []
    assert list(write_failed_directory.iterdir()) == []


async def test_daily_triage_recipe_reads_then_parks_write_and_reports_outcome() -> None:
    now = datetime(2026, 8, 26, 16, tzinfo=UTC)
    settings = replace(
        _email_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    scripts = {
        "gmail_read": ScriptedMCPServer(
            name="gmail_read",
            discovery=_discovery("read"),
            responses=(
                ScriptedMCPResponse(
                    name="list_labels",
                    result=MCPCallResult(
                        content=('{"labels":[{"id":"INBOX","name":"Inbox"}]}',),
                        structured={"labels": [{"id": "INBOX", "name": "Inbox"}]},
                    ),
                ),
            ),
        ),
        "gmail_write": ScriptedMCPServer(
            name="gmail_write",
            discovery=_discovery("write"),
            responses=(
                ScriptedMCPResponse(
                    name="create_draft",
                    result=MCPCallResult(
                        content=('{"draft_id":"draft-1"}',),
                        structured={"draft_id": "draft-1"},
                    ),
                ),
            ),
        ),
        "gmail_send": ScriptedMCPServer(name="gmail_send", discovery=_discovery("send")),
    }
    model = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="mcp.gmail_read.list_labels", arguments={})],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="mcp.gmail_write.create_draft",
                        arguments={
                            "to": "recipient@example.test",
                            "subject": "Scheduled draft",
                            "body": "Draft body stays out of notifications",
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Triage complete.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=settings,
        script=model,
        fixed_clock_at=now,
        sequential_ids=True,
        mcp_client_factory=ScriptedMCPClientFactory(scripts),
    ) as composition:
        default_agent = composition.sessions._default_agent
        record = await composition.schedules.create(
            composition.principal,
            ScheduleDefinition(
                title="Daily inbox triage",
                instruction=(
                    "Search the inbox for messages newer than one day; summarize what matters; "
                    "propose labels and drafts where needed."
                ),
                agent_id=default_agent.id,
                agent_version=default_agent.version,
                policy_profile="default",
                requested_scopes=frozenset({"mcp.gmail_read.use", "mcp.gmail_write.use"}),
                limits=RunLimits(
                    max_steps=8,
                    max_model_calls=8,
                    max_tool_calls=8,
                    max_cost=Decimal("1"),
                ),
                run_timeout_seconds=300,
                cadence=OnceCadence(at=now),
                misfire_grace_seconds=60,
                max_consecutive_failures=3,
            ),
            "email-triage-recipe",
        )
        schedule_worker = composition.schedule_worker_factory()
        assert isinstance(schedule_worker, ScheduleWorker)
        assert await schedule_worker.run_once() == 1
        occurrences = await composition.schedules.list_occurrences(
            composition.principal,
            record.schedule.id,
            limit=10,
            cursor=None,
        )
        [occurrence] = occurrences.items
        assert occurrence.run_id is not None
        await composition.executor.execute(occurrence.run_id)

        parked = await composition.runs.get(occurrence.run_id)
        assert parked.status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await composition.approvals.list_pending(run_id=parked.id)
        async with composition.uow_factory() as uow:
            pending_notifications = await uow.notification_outbox.list(
                composition.principal,
                limit=100,
            )
        approval_notifications = [
            row for row in pending_notifications if row.kind is NotificationKind.APPROVAL_REQUESTED
        ]
        assert len(approval_notifications) == 1
        serialized = approval_notifications[0].payload.model_dump_json()
        assert "Scheduled draft" not in serialized
        assert "Draft body" not in serialized

        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        resumed = await composition.runs.get(parked.id)
        assert resumed.status is RunStatus.COMPLETED
        async with composition.uow_factory() as uow:
            all_notifications = await uow.notification_outbox.list(
                composition.principal,
                limit=100,
            )
        assert any(row.kind is NotificationKind.SCHEDULE_RUN_FINISHED for row in all_notifications)
