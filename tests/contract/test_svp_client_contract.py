"""Provider contract starts with confinement and content-free failures (ADR-0153)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from svp_mcp import constants
from svp_mcp.client import SvpClient
from svp_mcp.constants import API_ROOT, DOCUMENT_URL
from svp_mcp.errors import SvpError

URL = API_ROOT + "companies"


class Tokens:
    """A grant holder that hands out a fresh token whenever one is rejected."""

    def __init__(self) -> None:
        self.rejected: list[str | None] = []

    def access_token(self, *, rejected: str | None = None) -> str:
        self.rejected.append(rejected)
        return f"access-{len([item for item in self.rejected if item is not None]) + 1}"


def _client(
    handler: Callable[[httpx.Request], httpx.Response], tokens: Tokens | None = None
) -> SvpClient:
    return SvpClient(
        tokens or Tokens(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_a_read_carries_the_bearer_and_exactly_its_query() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": [1, 2]})

    result = await _client(handle).get(URL, [("tag", "a"), ("tag", "b")])

    assert result == {"items": [1, 2]}
    assert [request.method for request in requests] == ["GET"]
    assert str(requests[0].url) == URL + "?tag=a&tag=b"
    assert requests[0].headers["authorization"].split() == ["Bearer", "access-1"]
    assert requests[0].content == b""


async def test_one_rejected_token_is_replaced_and_retried_once() -> None:
    tokens = Tokens()
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": True})

    assert await _client(handle, tokens).get(URL) == {"ok": True}
    assert [value.split() for value in seen] == [["Bearer", "access-1"], ["Bearer", "access-2"]]
    assert tokens.rejected == [None, "access-1"]


async def test_a_second_rejection_is_final() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"error": "private diagnostic"})

    with pytest.raises(SvpError, match=r"^svp\.credential_rejected$"):
        await _client(handle).get(URL)
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (302, "svp.provider_rejected"),
        (403, "svp.credential_rejected"),
        (404, "svp.provider_rejected"),
        (422, "svp.provider_rejected"),
        (429, "svp.rate_limited"),
        (500, "svp.provider_unavailable"),
    ],
)
async def test_failures_are_fixed_codes_and_redirects_are_never_followed(
    status: int, code: str
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.example/api/v1/companies"},
            json={"detail": "private diagnostic"},
        )

    with pytest.raises(SvpError) as caught:
        await _client(handle).get(URL)

    assert str(caught.value) == code
    assert len(seen) == 1


async def test_an_unreachable_service_is_reported_as_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private diagnostic", request=request)

    with pytest.raises(SvpError, match=r"^svp\.provider_unavailable$"):
        await _client(handle).get(URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://scalevp-mcp.com/api/v1/companies",
        "https://scalevp-mcp.com.evil.example/api/v1/companies",
        "https://scalevp-mcp.com/token",
        "https://scalevp-mcp.com/api/openapi.json",
        "https://scalevp-mcp.com/api/v2/companies",
        "https://scalevp-mcp.com/api/v1/../token",
        "https://user@scalevp-mcp.com/api/v1/companies",
        "https://scalevp-mcp.com:8443/api/v1/companies",
    ],
)
async def test_a_request_outside_the_api_root_is_never_sent(url: str) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        await _client(handle).get(url)
    assert seen == []


async def test_an_oversized_or_non_json_body_is_refused() -> None:
    def large(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"padding": "x" * 600_000})

    def text(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>private page</html>")

    with pytest.raises(SvpError, match=r"^svp\.response_too_large$"):
        await _client(large).get(URL)
    with pytest.raises(SvpError, match=r"^svp\.response_invalid$"):
        await _client(text).get(URL)


async def test_the_published_document_is_fetched_once() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"paths": {}})

    client = _client(handle)

    assert await client.document() == {"paths": {}}
    assert await client.document() == {"paths": {}}
    assert seen == [DOCUMENT_URL]


async def test_a_document_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(SvpError, match=r"^svp\.specification_invalid$"):
        await _client(lambda _request: httpx.Response(200, json=[1])).document()


def test_the_document_is_one_fixed_location_beside_the_callable_root() -> None:
    assert DOCUMENT_URL == "https://scalevp-mcp.com/api/openapi.json"
    assert API_ROOT == "https://scalevp-mcp.com/api/v1/"


SEARCH_URL = "https://scalevp-mcp.com/api/v1/companies/_search"


async def test_a_read_post_sends_its_json_body_only_to_an_allowlisted_url() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = _client(handle)
    post = getattr(client, "post", None)
    assert callable(post), "read posts must go through the confined client"

    assert await post(SEARCH_URL, {"query": "robotics"}) == {"results": []}
    assert [(request.method, str(request.url)) for request in requests] == [("POST", SEARCH_URL)]
    assert json.loads(requests[0].content) == {"query": "robotics"}
    assert requests[0].headers["authorization"].split() == ["Bearer", "access-1"]
    for url in (
        API_ROOT + "companies",
        API_ROOT + "web-cache/_search",
        API_ROOT + "tasks",
        SEARCH_URL + "/",
        SEARCH_URL + "/../../tasks",
        "https://elsewhere.example/api/v1/companies/_search",
    ):
        with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
            await post(url, {"query": "robotics"})
    assert len(requests) == 1
    assert SEARCH_URL.removeprefix("https://scalevp-mcp.com/api") in constants.READ_POST_PATHS


async def test_a_rejected_token_is_replaced_once_for_a_read_post() -> None:
    bodies: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(401 if len(bodies) == 1 else 200, json={"results": [1]})

    client = _client(handle)
    post = getattr(client, "post", None)
    assert callable(post), "read posts must go through the confined client"

    assert await post(SEARCH_URL, {"query": "robotics"}) == {"results": [1]}
    assert len(bodies) == 2 and bodies[0] == bodies[1]


async def test_a_client_error_keeps_its_status_and_a_bounded_message_off_its_text() -> None:
    def detailed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Refine your input. " + "x" * 5000})

    def structured(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"loc": ["query", "limit"], "msg": "bad"}]})

    def opaque(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>private page</html>")

    for handler, status, start in (
        (detailed, 400, "Refine your input."),
        (structured, 422, '[{"loc"'),
        (opaque, 404, ""),
    ):
        with pytest.raises(SvpError) as caught:
            await _client(handler).get(URL)
        assert str(caught.value) == "svp.provider_rejected"
        assert getattr(caught.value, "status", None) == status
        problem = getattr(caught.value, "problem", None)
        assert isinstance(problem, str) and problem.startswith(start)
        assert len(problem) <= 2000
