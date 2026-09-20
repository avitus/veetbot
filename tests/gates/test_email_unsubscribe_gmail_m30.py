"""Milestone 30 Gmail read contract: the bulk block and closed unsubscribe evidence."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any, cast

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from agent_core.domain.mcp import MCPRemoteTool
from agent_core.mcp.configuration import email_server_configs
from agent_core.mcp.mapping import map_discovered_tools
from gmail_mcp.client import GmailClient
from gmail_mcp.constants import GOOGLE_TOKEN_ENDPOINT, OUTPUT_MAXIMUM_BYTES, ROSTERS
from gmail_mcp.server import create_server
from tests.gates.test_email_m18 import _credential

type Header = tuple[str, str]

GOOGLE_HOSTS = {"gmail.googleapis.com", "oauth2.googleapis.com"}
APPLICATION_ONLY = "veetbot/application-only"
MODEL_VISIBLE_READ_TOOLS = {"search_threads", "get_thread", "list_labels"}
SEARCH_HEADERS = [
    "From",
    "Subject",
    "Date",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "List-Id",
]
EVIDENCE_HEADERS = [
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "List-Id",
    "From",
    "Authentication-Results",
    "DKIM-Signature",
]
LU = "list-unsubscribe"
LUP = "list-unsubscribe-post"
MARKER = "List-Unsubscribe=One-Click"
HTTPS = "https://unsubscribe.example.test/u/recipient-token?list=7"
MAILTO = "mailto:unsub@lists.example.test?subject=unsubscribe"
MAILTO_FIELDS = {"to": "unsub@lists.example.test", "subject": "unsubscribe", "body": ""}
SENDER = "news.example.test"
UNRELATED = "relay.example.test"
NEVER_PASSING = "cdn.example.test"
FROM = f"Example News <news@{SENDER}>"
DATE = "Mon, 14 Sep 2026 09:00:00 +0000"
NO_BULK = {"message_id": "", "from": "", "date": "", "list_id": "", "unsubscribe": "none"}


class Mailbox:
    """A fake Gmail that answers `format=metadata` with only the requested headers."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, int] = {}
        self.overrides: dict[str, dict[str, Any]] = {}

    def add(
        self,
        thread_id: str,
        message_id: str,
        headers: list[Header],
        *,
        labels: tuple[str, ...] = ("INBOX",),
    ) -> None:
        self.threads.setdefault(thread_id, []).append(
            {
                "id": message_id,
                "threadId": thread_id,
                "historyId": "4200",
                "internalDate": "1789376400000",
                "labelIds": list(labels),
                "snippet": f"snippet of {message_id}",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": name, "value": value} for name, value in headers],
                },
            }
        )

    @staticmethod
    def _projected(message: dict[str, Any], request: httpx.Request) -> dict[str, Any]:
        if request.url.params.get("format") != "metadata":
            return message
        wanted = {name.casefold() for name in request.url.params.get_list("metadataHeaders")}
        headers = [
            item for item in message["payload"]["headers"] if item["name"].casefold() in wanted
        ]
        return {**message, "payload": {**message["payload"], "headers": headers}}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": "private-access", "expires_in": 3600})
        assert request.method == "GET", "the read contract never mutates the mailbox"
        path = request.url.path
        resource = path.rsplit("/", 1)[-1]
        status = self.statuses.get(resource, 200)
        if status != 200:
            return httpx.Response(
                status,
                headers={"Location": "https://attacker.example.test/collect"},
                json={"error": "private upstream text"},
            )
        if path.endswith("/threads"):
            return httpx.Response(200, json={"threads": [{"id": item} for item in self.threads]})
        if "/threads/" in path and resource in self.threads:
            messages = [self._projected(item, request) for item in self.threads[resource]]
            return httpx.Response(
                200, json={"id": resource, "historyId": "4200", "messages": messages}
            )
        if "/messages/" in path:
            for message in (item for group in self.threads.values() for item in group):
                if message["id"] == resource:
                    body = {**self._projected(message, request), **self.overrides.get(resource, {})}
                    return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "private upstream text"})


def _server(mailbox: Mailbox) -> MCPServer:
    transport = httpx.MockTransport(mailbox)
    return create_server(
        "read",
        GmailClient(
            _credential("read"),
            http_client=httpx.AsyncClient(transport=transport, follow_redirects=False),
        ),
    )


async def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> CallToolResult:
    assert name in {tool.name for tool in await server.list_tools()}, (
        f"missing documented Gmail tool {name}"
    )
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    return result


async def _invoke(mailbox: Mailbox, name: str, arguments: dict[str, Any]) -> CallToolResult:
    return await _call(_server(mailbox), name, arguments)


def _texts(result: CallToolResult) -> list[str]:
    return [item.text for item in result.content if isinstance(item, TextContent)]


def _output(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is False, _texts(result)
    assert result.structured_content is not None
    return cast(dict[str, Any], result.structured_content)


def _dkim(domain: str, result: str = "pass", *, style: str = "i") -> str:
    identity = {
        "i": f"header.i=@{domain}",
        "i_local": f"header.i=bounces@{domain}",
        "d": f"header.d={domain}",
        "both": f"header.d={domain} header.i=@{domain}",
    }[style]
    return f"dkim={result} {identity} header.s=s1 header.b=AbCdEfGh"


def _verdict(*results: str, authserv: str = "mx.google.com", fold: bool = True) -> str:
    """One Authentication-Results value in the shape Gmail prepends to received mail."""
    return (";\r\n       " if fold else "; ").join(
        [
            authserv,
            *results,
            f"spf=pass (google.com: domain of bounce@{SENDER} designates 192.0.2.1 as"
            f" permitted sender) smtp.mailfrom=bounce@{SENDER}",
            f"dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from={SENDER}",
        ]
    )


def _signature(domain: str, *signed: str, fold: bool = True, title: bool = False) -> str:
    names = ["from", "subject", "date", *signed]
    if title:
        names = [name.title() for name in names]
    gap = "\r\n        " if fold else " "
    listed = (f":{gap}" if fold else ":").join(names)
    return (
        f"v=1; a=rsa-sha256; c=relaxed/relaxed;{gap}d={domain}; s=s1;{gap}"
        f"h={listed};{gap}bh=Ym9keSBoYXNo;{gap}b=AbCdEfGhc2lnbmF0dXJl"
    )


def _message(
    *,
    unsubscribe: str | None = f"<{HTTPS}>, <{MAILTO}>",
    post: str | None = MARKER,
    verdicts: tuple[str, ...] | None = None,
    signatures: tuple[str, ...] | None = None,
    list_id: str | None = f"Example News <{SENDER}>",
    sender: str = FROM,
) -> list[Header]:
    """An authenticated one-click bulk message, top of message first, unless overridden."""
    verdicts = (_verdict(_dkim(SENDER)),) if verdicts is None else verdicts
    signatures = (_signature(SENDER, LU, LUP),) if signatures is None else signatures
    headers: list[Header] = [("Delivered-To", "owner@example.test")]
    headers += [("Authentication-Results", value) for value in verdicts]
    headers += [("DKIM-Signature", value) for value in signatures]
    headers += [("From", sender), ("Subject", "Weekly news"), ("Date", DATE)]
    for name, value in (
        ("List-Id", list_id),
        ("List-Unsubscribe", unsubscribe),
        ("List-Unsubscribe-Post", post),
    ):
        if value is not None:
            headers.append((name, value))
    return headers


def _block(**changes: Any) -> dict[str, Any]:
    """The closed block `_message()` yields; an unexpected key fails equality."""
    return {
        "schema_version": 1,
        "message_id": "message-1",
        "thread_id": "thread-1",
        "history_id": "4200",
        "from": FROM,
        "list_id": SENDER,
        "offered": "one_click",
        "mechanism": "one_click",
        "https_uri": HTTPS,
        "mailto": MAILTO_FIELDS,
        "authenticated": True,
        "covered_headers": [LU, LUP],
        **changes,
    }


async def _evidence(headers: list[Header]) -> dict[str, Any]:
    mailbox = Mailbox()
    mailbox.add("thread-1", "message-1", headers)
    return _output(await _invoke(mailbox, "get_unsubscribe", {"message_id": "message-1"}))


def _census_mailbox() -> Mailbox:
    mailbox = Mailbox()
    mailbox.add("thread-one-click", "message-one-click", _message())
    mailbox.add(
        "thread-mailto",
        "message-mailto",
        _message(unsubscribe=f"<{MAILTO}>", post=None, list_id=None),
    )
    mailbox.add(
        "thread-link",
        "message-link",
        _message(unsubscribe=f"<{HTTPS}>", post=None, list_id="deals.example.test"),
    )
    mailbox.add(
        "thread-none",
        "message-none",
        [("From", "Friend <friend@example.test>"), ("Subject", "Lunch"), ("Date", DATE)],
    )
    mailbox.add("thread-replied", "message-received", _message())
    mailbox.add(
        "thread-replied",
        "message-reply",
        [("From", "Owner <owner@example.test>"), ("Subject", "Re: Weekly news"), ("Date", DATE)],
        labels=("SENT",),
    )
    mailbox.add(
        "thread-sent-only",
        "message-sent",
        _message(sender="Owner <owner@example.test>"),
        labels=("SENT",),
    )
    return mailbox


def _summary(
    thread_id: str,
    message_id: str,
    *,
    bulk: dict[str, str],
    sender: str = FROM,
    subject: str = "Weekly news",
    labels: tuple[str, ...] = ("INBOX",),
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "senders": [sender],
        "subject": subject,
        "date": DATE,
        "snippet": f"snippet of {message_id}",
        "label_ids": list(labels),
        "bulk": bulk,
    }


def _bulk(message_id: str, list_id: str, unsubscribe: str, sender: str = FROM) -> dict[str, str]:
    return {
        "message_id": message_id,
        "from": sender,
        "date": DATE,
        "list_id": list_id,
        "unsubscribe": unsubscribe,
    }


async def test_unsubscribe_read_contract_is_closed_and_google_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gate.email.unsubscribe_read_contract."""

    mailbox = _census_mailbox()
    server = _server(mailbox)
    search = await _call(server, "search_threads", {"query": "newer_than:90d", "max_results": 25})
    assert _output(search) == {
        "threads": [
            _summary(
                "thread-one-click",
                "message-one-click",
                bulk=_bulk("message-one-click", SENDER, "one_click"),
            ),
            _summary("thread-mailto", "message-mailto", bulk=_bulk("message-mailto", "", "mailto")),
            _summary(
                "thread-link",
                "message-link",
                bulk=_bulk("message-link", "deals.example.test", "link"),
            ),
            _summary(
                "thread-none",
                "message-none",
                sender="Friend <friend@example.test>",
                subject="Lunch",
                bulk=_bulk("message-none", "", "none", "Friend <friend@example.test>"),
            ),
            # The summary still describes the newest message; the block skips the owner's reply.
            _summary(
                "thread-replied",
                "message-reply",
                sender="Owner <owner@example.test>",
                subject="Re: Weekly news",
                labels=("SENT",),
                bulk=_bulk("message-received", SENDER, "one_click"),
            ),
            _summary(
                "thread-sent-only",
                "message-sent",
                sender="Owner <owner@example.test>",
                labels=("SENT",),
                bulk=NO_BULK,
            ),
        ]
    }
    # The model-visible tool says which results are bulk mail, never where they point.
    rendered = json.dumps(search.structured_content) + "".join(_texts(search))
    for destination in ("https://", "mailto:", "recipient-token", "unsub@", "lists.example.test"):
        assert destination not in rendered
    fanout = [request for request in mailbox.requests if "/threads/" in request.url.path]
    assert len(fanout) == len(mailbox.threads)
    for request in fanout:
        assert request.url.params["format"] == "metadata"
        assert sorted(request.url.params.get_list("metadataHeaders")) == sorted(SEARCH_HEADERS)

    before = len(mailbox.requests)
    evidence = await _call(server, "get_unsubscribe", {"message_id": "message-one-click"})
    expected = _block(message_id="message-one-click", thread_id="thread-one-click")
    assert _output(evidence) == expected
    assert [json.loads(text) for text in _texts(evidence)] == [expected]
    (read,) = mailbox.requests[before:]
    assert read.method == "GET"
    assert read.url.path == "/gmail/v1/users/me/messages/message-one-click"
    assert read.url.params["format"] == "metadata"
    assert sorted(read.url.params.get_list("metadataHeaders")) == sorted(EVIDENCE_HEADERS)

    assert set(ROSTERS["read"]) == {
        *MODEL_VISIBLE_READ_TOOLS,
        "get_profile",
        "sync_changes",
        "get_thread_page",
        "get_message_body",
        "get_unsubscribe",
    }
    assert ROSTERS["write"] == ("create_draft", "modify_labels", "trash_thread", "untrash_thread")
    assert ROSTERS["send"] == ("send_message",)
    for mode in ("write", "send"):
        names = {
            tool.name
            for tool in await create_server(mode, cast(GmailClient, object())).list_tools()
        }
        assert names == set(ROSTERS[mode])
        assert "get_unsubscribe" not in names

    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == set(ROSTERS["read"])
    assert (tools["get_unsubscribe"].meta or {}).get(APPLICATION_ONLY) is True
    assert set(tools["get_unsubscribe"].input_schema["properties"]) == {"message_id"}
    assert tools["get_unsubscribe"].input_schema["required"] == ["message_id"]
    assert {
        name for name, tool in tools.items() if (tool.meta or {}).get(APPLICATION_ONLY) is not True
    } == MODEL_VISIBLE_READ_TOOLS
    declared = tuple(
        MCPRemoteTool.model_validate(
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
                "application_only": (tool.meta or {}).get(APPLICATION_ONLY) is True,
            }
        )
        for tool in tools.values()
    )
    config = next(row for row in email_server_configs("local") if row.server_id == "gmail_read")
    report = map_discovered_tools(config, declared)
    assert {
        mapped.remote_name
        for mapped in report.accepted
        if mapped.spec.model_dump().get("model_visible")
    } == MODEL_VISIBLE_READ_TOOLS

    assert {request.url.host for request in mailbox.requests} <= GOOGLE_HOSTS

    # The package's own client construction, with only its transport replaced.
    redirecting = _census_mailbox()
    redirecting.statuses.update({"message-one-click": 302, "thread-link": 307})
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(redirecting)),
    )
    confined = create_server("read", GmailClient(_credential("read")))
    for name, arguments, resource in (
        ("get_unsubscribe", {"message_id": "message-one-click"}, "/messages/message-one-click"),
        ("search_threads", {"query": "newer_than:90d", "max_results": 25}, "/threads/thread-link"),
    ):
        refused = await _call(confined, name, arguments)
        assert refused.is_error is True
        assert _texts(refused) == ["gmail.provider_rejected"]
        assert "attacker" not in str(refused)
        assert len([item for item in redirecting.requests if item.url.path.endswith(resource)]) == 1
    assert {request.url.host for request in redirecting.requests} <= GOOGLE_HOSTS


_HTTPS_URIS: tuple[tuple[str | None, str], ...] = (
    (None, ""),
    (HTTPS, HTTPS),
    (
        "HTTPS://Unsubscribe.Example.Test/U/Recipient-Token",
        "HTTPS://Unsubscribe.Example.Test/U/Recipient-Token",
    ),
    ("https://unsubscribe.example.test/" + "a" * 2048, ""),
    ("http://unsubscribe.example.test/u/recipient-token", ""),
)
_MARKERS: tuple[tuple[str | None, bool], ...] = (
    (None, False),
    (MARKER, True),
    (f"\r\n {MARKER}  ", True),
    ("list-unsubscribe=one-click", False),
    (f"{MARKER}, List-Unsubscribe=Later", False),
)
_LIST_IDS: tuple[tuple[str | None, str], ...] = (
    (None, ""),
    (f"Example News <{SENDER}>", SENDER),
    ("<NEWS.Example.Test>", SENDER),
    ("  bare-list.example.test  ", "bare-list.example.test"),
    ("Moved <old.example.test> now <New.Example.Test>", "new.example.test"),
    ("L" * 300, "l" * 255),
)
_CLOSED_MAILTO: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "mailto:unsub@lists.example.test",
        {"to": "unsub@lists.example.test", "subject": "", "body": ""},
    ),
    (MAILTO, MAILTO_FIELDS),
    (
        "MAILTO:unsub@lists.example.test?SUBJECT=Stop%20now&Body=token%3Aabc%0Anext+line",
        {"to": "unsub@lists.example.test", "subject": "Stop now", "body": "token:abc\nnext+line"},
    ),
    (
        "mailto:unsub%2Btoken@lists.example.test?body=",
        {"to": "unsub+token@lists.example.test", "subject": "", "body": ""},
    ),
)
_OPEN_MAILTO: tuple[str, ...] = (
    "mailto:unsub@lists.example.test?cc=victim@example.test",
    "mailto:unsub@lists.example.test?subject=unsubscribe&BCC=victim@example.test",
    "mailto:unsub@lists.example.test?to=victim@example.test",
    "mailto:unsub@lists.example.test,victim@example.test",
    "mailto:unsub@lists.example.test?In-Reply-To=%3Cparent@example.test%3E",
    "mailto:unsub@lists.example.test?subject=stop%0D%0ABcc:%20victim@example.test",
    "mailto:unsub@lists.example.test?subject=" + "s" * 257,
    "mailto:unsub@lists.example.test?body=" + "b" * 1025,
    "mailto:unsub@lists.example.test?body=first%0D%0Asecond",
    "mailto:unsub%20list@lists.example.test",
    "mailto:unsubscribe.example.test",
    "mailto:" + "u" * 250 + "@lists.example.test",
)
_EXTRA_FIELD = st.from_regex(r"[A-Za-z][A-Za-z0-9-]{0,15}", fullmatch=True).filter(
    lambda name: name.lower() not in {"subject", "body"}
)
_MAILTOS: st.SearchStrategy[tuple[str | None, dict[str, str] | None]] = st.one_of(
    st.just((None, None)),
    st.sampled_from(_CLOSED_MAILTO),
    st.sampled_from(_OPEN_MAILTO).map(lambda uri: (uri, None)),
    st.tuples(_EXTRA_FIELD, st.booleans()).map(
        lambda drawn: (
            "mailto:unsub@lists.example.test?"
            + (
                f"{drawn[0]}=1&subject=unsubscribe"
                if drawn[1]
                else f"subject=unsubscribe&{drawn[0]}=1"
            ),
            None,
        )
    ),
)


@st.composite
def _header_sets(draw: st.DrawFn) -> tuple[list[Header], dict[str, Any], dict[str, Any]]:
    """Render independently drawn clauses to headers, with the block they must yield."""
    fold = draw(st.booleans())
    https, https_uri = draw(st.sampled_from(_HTTPS_URIS))
    mailto, fields = draw(_MAILTOS)
    post, marked = draw(st.sampled_from(_MARKERS))
    list_id, normalized_list_id = draw(st.sampled_from(_LIST_IDS))
    gmail = draw(
        st.sampled_from(("absent", "pass", "pass_subdomain", "pass_unrelated", "fail", "no_dkim"))
    )
    style = draw(st.sampled_from(("i", "i_local", "d", "both")))
    passing = {
        "pass": {SENDER},
        "pass_subdomain": {f"bounce.{SENDER}"},
        "pass_unrelated": {UNRELATED},
    }.get(gmail, set())

    verdicts: list[str] = []
    forgery = (_dkim(SENDER), _dkim(UNRELATED), _dkim(NEVER_PASSING))
    if draw(st.booleans()):
        # Another service's verdict above Gmail's is skipped, never trusted.
        verdicts.append(_verdict(*forgery, authserv="inbound.example.test", fold=fold))
    if gmail in {"fail", "no_dkim"}:
        results = (_dkim(SENDER, "fail", style=style),) if gmail == "fail" else ()
        verdicts.append(_verdict(*results, fold=fold))
    elif gmail != "absent":
        verdicts.append(_verdict(*(_dkim(item, style=style) for item in passing), fold=fold))
    forged = draw(st.sampled_from(("none", "claims_google", "other_service")))
    if forged == "claims_google" and gmail != "absent":
        # Gmail prepends its verdict to received mail, so a sender's copy is always lower.
        verdicts.append(_verdict(*forgery, fold=fold))
    elif forged != "none":
        verdicts.append(_verdict(*forgery, authserv="mx.google.com.example.test", fold=fold))

    title = draw(st.booleans())
    sender_signed = draw(st.sampled_from((None, (), (LU,), (LUP,), (LU, LUP))))
    unrelated_signed = draw(st.sampled_from((None, (), (LU, LUP))))
    covered: set[str] = set()
    signatures = [_signature(NEVER_PASSING, LU, LUP, fold=fold, title=title)]
    for domain, signed in ((SENDER, sender_signed), (UNRELATED, unrelated_signed)):
        if signed is None:
            continue
        signatures.append(_signature(domain, *signed, fold=fold, title=title))
        if any(item == domain or item.endswith(f".{domain}") for item in passing):
            covered.update(signed)
    signatures = list(draw(st.permutations(signatures)))

    uris = [item for item in (https, mailto) if item is not None]
    if draw(st.booleans()):
        uris.reverse()
    unsubscribe: str | None = (",\r\n " if fold else ", ").join(f"<{item}>" for item in uris)
    if not uris:
        unsubscribe = draw(st.sampled_from((None, "visit the preference centre")))

    if https_uri and marked:
        offered = "one_click"
    elif mailto is not None:
        offered = "mailto"
    elif https_uri:
        offered = "link"
    else:
        offered = "none"
    if offered == "one_click" and covered == {LU, LUP}:
        mechanism = "one_click"
    elif fields is not None and LU in covered:
        mechanism = "mailto"
    else:
        mechanism = "none"
    headers = _message(
        unsubscribe=unsubscribe,
        post=post,
        verdicts=tuple(verdicts),
        signatures=tuple(signatures),
        list_id=list_id,
    )
    expected = _block(
        list_id=normalized_list_id,
        offered=offered,
        mechanism=mechanism,
        https_uri=https_uri,
        mailto=fields,
        authenticated=LU in covered,
        covered_headers=sorted(covered),
    )
    clauses = {
        "https": bool(https_uri),
        "marker": marked,
        "verified": bool(passing),
        "covered": covered,
        "closed_mailto": fields is not None,
    }
    return headers, expected, clauses


@given(case=_header_sets())
@hypothesis_settings(max_examples=400, deadline=None, derandomize=True)
def test_unsubscribe_eligibility_is_deterministic_and_authenticated(
    case: tuple[list[Header], dict[str, Any], dict[str, Any]],
) -> None:
    """gate.email.unsubscribe_eligibility."""

    headers, expected, clauses = case

    async def exercise() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        mailbox = Mailbox()
        mailbox.add("thread-1", "message-1", headers)
        server = _server(mailbox)
        arguments = {"message_id": "message-1"}
        first = _output(await _call(server, "get_unsubscribe", arguments))
        second = _output(await _call(server, "get_unsubscribe", arguments))
        search = _output(
            await _call(server, "search_threads", {"query": "in:inbox", "max_results": 1})
        )
        assert {request.url.host for request in mailbox.requests} <= GOOGLE_HOSTS
        return first, second, search

    block, again, search = asyncio.run(exercise())
    assert block == expected
    assert again == block
    one_click = (
        clauses["https"]
        and clauses["marker"]
        and clauses["verified"]
        and clauses["covered"] == {LU, LUP}
    )
    assert (block["mechanism"] == "one_click") == one_click
    assert (block["mechanism"] == "mailto") == (
        not one_click
        and clauses["closed_mailto"]
        and clauses["verified"]
        and LU in clauses["covered"]
    )
    if not clauses["verified"]:
        # Neither a lower forgery nor another service's verdict grants anything.
        assert block["authenticated"] is False
        assert block["covered_headers"] == []
        assert block["mechanism"] == "none"
    if not clauses["closed_mailto"]:
        assert block["mailto"] is None
    # The model-visible summary agrees with the unauthenticated offer and carries no destination.
    (summary,) = search["threads"]
    assert summary["bulk"] == _bulk("message-1", block["list_id"], block["offered"])
    rendered = json.dumps(summary).casefold()
    assert "unsubscribe.example.test" not in rendered
    assert "lists.example.test" not in rendered


async def test_m30_folded_headers_are_unfolded_before_parsing() -> None:
    folded = _message(
        unsubscribe=(
            "<https://unsubscribe.example.test/u/\r\n recipient-token?list=7>,\r\n\t"
            "<mailto:unsub@lists.example.test\r\n ?subject=unsubscribe>"
        ),
        post=f"\r\n {MARKER}",
        list_id=f"Example News\r\n <{SENDER}>",
        signatures=(
            "v=1; a=rsa-sha256; d = news.example.test ; s=s1;\r\n"
            "        h=from : subject :\r\n         List-Unsubscribe :\r\n"
            "         List-Unsubscribe-Post; bh=Ym9keQ==; b=AbCd",
        ),
    )
    assert await _evidence(folded) == _block()


@pytest.mark.parametrize("https_first", [True, False])
async def test_m30_uri_order_changes_neither_mechanism(https_first: bool) -> None:
    uris = [f"<{HTTPS}>", f"<{MAILTO}>"]
    if not https_first:
        uris.reverse()
    assert await _evidence(_message(unsubscribe=", ".join(uris))) == _block()


async def test_m30_only_the_first_uri_of_each_scheme_is_read() -> None:
    later_https = "https://unsubscribe.example.test/u/second"
    open_first = "mailto:unsub@lists.example.test?cc=victim@example.test"
    block = await _evidence(
        _message(unsubscribe=f"<{open_first}>, <{HTTPS}>, <{MAILTO}>, <{later_https}>")
    )
    # A later closed address never rescues an open first one.
    assert block == _block(mailto=None)


async def test_m30_schemes_match_without_regard_to_case() -> None:
    https = "HTTPS://unsubscribe.example.test/u/recipient-token"
    block = await _evidence(
        _message(unsubscribe=f"<MAILTO:unsub@lists.example.test?Subject=unsubscribe>, <{https}>")
    )
    assert block == _block(https_uri=https)


async def test_m30_an_overlong_https_uri_is_absent_rather_than_truncated() -> None:
    longest = "https://unsubscribe.example.test/" + "a" * (2048 - 33)
    assert len(longest) == 2048
    assert await _evidence(_message(unsubscribe=f"<{longest}>")) == _block(
        https_uri=longest, mailto=None
    )
    overlong = longest + "a"
    assert await _evidence(_message(unsubscribe=f"<{overlong}>")) == _block(
        offered="none", mechanism="none", https_uri="", mailto=None
    )
    # An absent address is skipped, so the next HTTPS address is the first.
    assert await _evidence(_message(unsubscribe=f"<{overlong}>, <{HTTPS}>")) == _block(mailto=None)
    assert await _evidence(_message(unsubscribe=f"<{overlong}>, <{MAILTO}>")) == _block(
        offered="mailto", mechanism="mailto", https_uri=""
    )


@pytest.mark.parametrize(
    "uri",
    [
        "mailto:unsub@lists.example.test?cc=victim@example.test",
        "mailto:unsub@lists.example.test,victim@example.test",
        "mailto:unsub@lists.example.test?to=victim@example.test",
        "mailto:unsub@lists.example.test?subject=stop%0D%0ABcc:%20victim@example.test",
        "mailto:unsub@lists.example.test?subject=stop%0Anow",
        "mailto:unsub@lists.example.test?body=bell%07",
        "mailto:unsub@lists.example.test?body=%FF",
        # Two values for one closed field are ambiguous, so they are refused as well.
        "mailto:unsub@lists.example.test?subject=first&subject=second",
        "mailto:@lists.example.test",
        "mailto:?subject=unsubscribe",
    ],
)
async def test_m30_an_open_mailto_normalizes_to_no_mailto_at_all(uri: str) -> None:
    block = await _evidence(_message(unsubscribe=f"<{uri}>", post=None))
    # The header still offers one; nothing authenticated remains of it.
    assert block == _block(offered="mailto", mechanism="none", https_uri="", mailto=None)


async def test_m30_a_covered_mailto_is_the_fallback_when_post_is_not_covered() -> None:
    block = await _evidence(_message(signatures=(_signature(SENDER, LU),)))
    assert block == _block(mechanism="mailto", covered_headers=[LU])
    uncovered = await _evidence(_message(signatures=(_signature(SENDER, LUP),)))
    assert uncovered == _block(mechanism="none", authenticated=False, covered_headers=[LUP])


@pytest.mark.parametrize(
    ("passing", "signing", "authenticated"),
    [
        ("sub.example.com", "example.com", True),
        ("example.com", "example.com", True),
        ("EXAMPLE.com", "Example.COM", True),
        # A signing subdomain is not vouched for by its parent, nor a lookalike by its suffix.
        ("example.com", "sub.example.com", False),
        ("notexample.com", "example.com", False),
    ],
)
async def test_m30_an_auid_subdomain_of_the_signing_domain_counts(
    passing: str, signing: str, authenticated: bool
) -> None:
    block = await _evidence(
        _message(verdicts=(_verdict(_dkim(passing)),), signatures=(_signature(signing, LU, LUP),))
    )
    expected = (
        _block()
        if authenticated
        else _block(mechanism="none", authenticated=False, covered_headers=[])
    )
    assert block == expected


async def test_m30_a_passing_signature_for_another_domain_covers_nothing() -> None:
    block = await _evidence(
        _message(
            verdicts=(_verdict(_dkim(UNRELATED), _dkim(SENDER, "fail")),),
            signatures=(_signature(UNRELATED), _signature(SENDER, LU, LUP)),
        )
    )
    assert block == _block(mechanism="none", authenticated=False, covered_headers=[])


async def test_m30_another_services_verdict_above_gmails_is_skipped() -> None:
    block = await _evidence(
        _message(
            verdicts=(
                _verdict(_dkim(UNRELATED), authserv="inbound.example.test"),
                _verdict(_dkim(SENDER)),
            ),
            signatures=(_signature(UNRELATED, LU, LUP), _signature(SENDER, LU)),
        )
    )
    assert block == _block(mechanism="mailto", covered_headers=[LU])


@pytest.mark.parametrize(
    "gmail",
    [_verdict(), _verdict(_dkim(SENDER, "fail"))],
    ids=["no_dkim_result", "dkim_fail"],
)
async def test_m30_gmails_first_verdict_is_final_even_without_a_pass(gmail: str) -> None:
    block = await _evidence(_message(verdicts=(gmail, _verdict(_dkim(SENDER)))))
    assert block == _block(mechanism="none", authenticated=False, covered_headers=[])


@pytest.mark.parametrize(
    "smuggled",
    [
        f"spf=pass (google.com: domain of x; dkim=pass header.d={SENDER} ) smtp.mailfrom=x",
        f'spf=pass smtp.mailfrom="x; dkim=pass header.d={SENDER} y"@attacker.example.test',
        f"dkim=fail (dkim=pass header.d={SENDER}) header.i=@{SENDER}",
        f"arc=pass (i=1 dkim=pass header.d={SENDER})",
    ],
    ids=["comment", "quoted_string", "failed_result_comment", "arc_comment"],
)
async def test_m30_sender_text_inside_gmails_verdict_cannot_forge_a_pass(smuggled: str) -> None:
    block = await _evidence(_message(verdicts=(_verdict(smuggled),)))
    assert block == _block(mechanism="none", authenticated=False, covered_headers=[])


async def test_m30_the_last_list_unsubscribe_is_the_one_a_signature_covers() -> None:
    # DKIM signs upward from the bottom, so a header prepended in transit is never the covered one.
    headers = _message()
    headers.insert(1, ("List-Unsubscribe", "<https://attacker.example.test/collect>"))
    assert await _evidence(headers) == _block()


async def test_m30_evidence_values_are_bounded() -> None:
    block = await _evidence(_message(sender="S" * 9000, list_id="L" * 300))
    assert block == _block(**{"from": "S" * 8192, "list_id": "l" * 255})
    assert len(json.dumps(block).encode()) < OUTPUT_MAXIMUM_BYTES


async def test_m30_a_message_without_list_headers_offers_nothing() -> None:
    block = await _evidence([("From", "Friend <friend@example.test>"), ("Date", DATE)])
    assert block == _block(
        **{"from": "Friend <friend@example.test>"},
        list_id="",
        offered="none",
        mechanism="none",
        https_uri="",
        mailto=None,
        authenticated=False,
        covered_headers=[],
    )


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "gmail.credential_rejected"),
        (404, "gmail.provider_rejected"),
        (429, "gmail.rate_limited"),
        (503, "gmail.provider_unavailable"),
    ],
)
async def test_m30_failures_keep_the_read_servers_stable_codes(status: int, code: str) -> None:
    mailbox = Mailbox()
    mailbox.add("thread-1", "message-1", _message())
    mailbox.statuses["message-1"] = status
    result = await _invoke(mailbox, "get_unsubscribe", {"message_id": "message-1"})
    assert result.is_error is True
    assert _texts(result) == [code]
    assert result.structured_content == {"effect_status": "not_applied"}
    assert "private upstream" not in str(result)


@pytest.mark.parametrize(
    "override", [{"id": "another-message"}, {"threadId": None}, {"historyId": "not-a-revision"}]
)
async def test_m30_malformed_provider_identity_is_invalid_output(override: dict[str, Any]) -> None:
    mailbox = Mailbox()
    mailbox.add("thread-1", "message-1", _message())
    mailbox.overrides["message-1"] = override
    result = await _invoke(mailbox, "get_unsubscribe", {"message_id": "message-1"})
    assert result.is_error is True
    assert _texts(result) == ["gmail.provider_output_invalid"]


@pytest.mark.parametrize("message_id", ["", "   ", "m" * 1025, "message\x00id"])
async def test_m30_arguments_fail_before_network(message_id: str) -> None:
    mailbox = Mailbox()
    result = await _invoke(mailbox, "get_unsubscribe", {"message_id": message_id})
    assert result.is_error is True
    assert _texts(result) == ["gmail.arguments_invalid"]
    assert mailbox.requests == []
