"""The bridge advertises three fixed read tools over the published document (ADR-0153)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from svp_mcp.client import SvpClient
from svp_mcp.constants import API_ROOT, DOCUMENT_URL
from svp_mcp.server import create_server

DOCUMENT = {
    "paths": {
        "/v1/companies": {
            "get": {
                "operationId": "list_companies",
                "summary": "List companies",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            },
            "post": {"operationId": "create_company"},
        },
        "/v1/companies/{company_id}": {
            "get": {
                "operationId": "get_company",
                "parameters": [{"name": "company_id", "in": "path", "required": True}],
            }
        },
        "/v1/companies/_search": {
            "post": {
                "operationId": "search_companies",
                "summary": "Search companies",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["query"],
                                "properties": {"query": {"type": "string"}},
                            }
                        }
                    },
                },
            }
        },
        "/v1/tasks": {"post": {"operationId": "create_task"}},
    }
}


class Tokens:
    def access_token(self, *, rejected: str | None = None) -> str:
        return "access-1"


class Service:
    def __init__(self, *, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == DOCUMENT_URL:
            return httpx.Response(200, json=DOCUMENT)
        return httpx.Response(self.status, json={"name": "Acme", "detail": "private diagnostic"})


def _server(service: Service) -> Any:
    return create_server(
        SvpClient(Tokens(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(service)))
    )


def _text(result: object) -> str:
    assert isinstance(result, CallToolResult)
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_the_catalog_is_three_fixed_tools() -> None:
    tools = await _server(Service()).list_tools()

    assert [tool.name for tool in tools] == [
        "list_operations",
        "describe_operation",
        "call_operation",
    ]
    assert all(tool.description for tool in tools)


async def test_listing_offers_only_read_operations() -> None:
    result = await _server(Service()).call_tool("list_operations", {})

    assert json.loads(_text(result)) == {
        "operations": [
            {
                "id": "get_company",
                "method": "GET",
                "path": "/v1/companies/{company_id}",
                "summary": "",
            },
            {
                "id": "list_companies",
                "method": "GET",
                "path": "/v1/companies",
                "summary": "List companies",
            },
            {
                "id": "search_companies",
                "method": "POST",
                "path": "/v1/companies/_search",
                "summary": "Search companies",
            },
        ],
        "truncated": False,
    }
    assert result.is_error is not True


async def test_describing_names_the_declared_inputs() -> None:
    result = await _server(Service()).call_tool(
        "describe_operation", {"operation_id": "list_companies"}
    )

    described = json.loads(_text(result))
    assert described["id"] == "list_companies"
    assert [item["name"] for item in described["parameters"]] == ["limit"]


async def test_a_call_reads_one_confined_url_and_returns_its_data() -> None:
    service = Service()

    result = await _server(service).call_tool(
        "call_operation",
        {
            "operation_id": "get_company",
            "path_parameters": {"company_id": "acme/1"},
        },
    )

    assert json.loads(_text(result)) == {"data": {"name": "Acme", "detail": "private diagnostic"}}
    assert [(request.method, str(request.url)) for request in service.requests] == [
        ("GET", DOCUMENT_URL),
        ("GET", API_ROOT + "companies/acme%2F1"),
    ]


async def test_an_operation_that_is_not_a_read_cannot_be_called() -> None:
    service = Service()

    for tool in ("describe_operation", "call_operation"):
        result = await _server(service).call_tool(tool, {"operation_id": "create_company"})
        assert json.loads(_text(result))["refused"] == "svp.operation_unknown"
        assert result.is_error is not True

    assert {request.method for request in service.requests} == {"GET"}
    assert {str(request.url) for request in service.requests} == {DOCUMENT_URL}


async def test_a_refusal_tells_the_model_how_to_correct_the_call() -> None:
    """The platform hides failure text, so a refusal the model can fix is an answer."""
    refused = await _server(Service()).call_tool(
        "call_operation", {"operation_id": "list_companies", "query": {"undeclared": "1"}}
    )

    answer = json.loads(_text(refused))
    assert refused.is_error is not True
    assert answer["refused"] == "svp.arguments_invalid"
    assert "describe_operation" in answer["hint"]


@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_the_services_own_refusal_is_returned_with_its_bounded_message(status: int) -> None:
    answered = await _server(Service(status=status)).call_tool(
        "call_operation", {"operation_id": "list_companies", "query": {"limit": 5}}
    )

    assert answered.is_error is not True
    assert json.loads(_text(answered)) == {"status": status, "problem": "private diagnostic"}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (302, "svp.provider_rejected"),
        (401, "svp.credential_rejected"),
        (403, "svp.credential_rejected"),
        (429, "svp.rate_limited"),
        (500, "svp.provider_unavailable"),
    ],
)
async def test_failures_the_model_cannot_fix_stay_content_free(status: int, code: str) -> None:
    failed = await _server(Service(status=status)).call_tool(
        "call_operation", {"operation_id": "list_companies", "query": {"limit": 5}}
    )

    assert failed.is_error is True
    assert _text(failed) == code
    assert failed.structured_content == {"effect_status": "not_applied"}


async def test_a_search_is_posted_with_its_declared_body() -> None:
    service = Service()

    result = await _server(service).call_tool(
        "call_operation", {"operation_id": "search_companies", "body": {"query": "robotics"}}
    )

    assert result.is_error is not True
    assert json.loads(_text(result))["data"]["name"] == "Acme"
    search = service.requests[-1]
    assert (search.method, str(search.url)) == ("POST", API_ROOT + "companies/_search")
    assert json.loads(search.content) == {"query": "robotics"}


async def test_a_body_is_refused_where_it_is_not_declared() -> None:
    service = Service()

    for arguments in (
        {"operation_id": "list_companies", "body": {"query": "robotics"}},
        {"operation_id": "search_companies", "body": {"query": "robotics", "undeclared": 1}},
        {"operation_id": "search_companies"},
        {"operation_id": "create_task", "body": {"title": "x"}},
    ):
        result = await _server(service).call_tool("call_operation", arguments)
        assert result.is_error is not True
        assert json.loads(_text(result))["refused"] in {
            "svp.arguments_invalid",
            "svp.operation_unknown",
        }

    assert {(request.method, str(request.url)) for request in service.requests} == {
        ("GET", DOCUMENT_URL)
    }
