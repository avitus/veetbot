"""The bridge advertises three fixed read tools over the published document (ADR-0152)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import CallToolResult, TextContent

from svp_mcp.client import SvpClient
from svp_mcp.constants import API_ROOT, DOCUMENT_URL
from svp_mcp.server import create_server

DOCUMENT = {
    "paths": {
        "/companies": {
            "get": {
                "operationId": "list_companies",
                "summary": "List companies",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            },
            "post": {"operationId": "create_company"},
        },
        "/companies/{company_id}": {
            "get": {
                "operationId": "get_company",
                "parameters": [{"name": "company_id", "in": "path", "required": True}],
            }
        },
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
            {"id": "get_company", "path": "/companies/{company_id}", "summary": ""},
            {"id": "list_companies", "path": "/companies", "summary": "List companies"},
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
        assert _text(result) == "svp.operation_unknown"
        assert result.is_error is True

    assert {request.method for request in service.requests} == {"GET"}
    assert {str(request.url) for request in service.requests} == {DOCUMENT_URL}


async def test_refused_arguments_and_upstream_failures_are_content_free() -> None:
    refused = await _server(Service()).call_tool(
        "call_operation", {"operation_id": "list_companies", "query": {"undeclared": "1"}}
    )
    failed = await _server(Service(status=422)).call_tool(
        "call_operation", {"operation_id": "list_companies", "query": {"limit": 5}}
    )

    assert _text(refused) == "svp.arguments_invalid"
    assert _text(failed) == "svp.provider_rejected"
    assert refused.is_error is True and failed.is_error is True
    assert failed.structured_content == {"effect_status": "not_applied"}
