"""The published document yields only confined, read-only operations (ADR-0152)."""

from __future__ import annotations

from typing import Any

import pytest

from svp_mcp.errors import SvpError
from svp_mcp.specification import describe, load_operations, request_target


def document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/companies": {
                "get": {
                    "operationId": "list_companies",
                    "summary": "List companies",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "tag", "in": "query", "schema": {"type": "array"}},
                        {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Page"}
                                }
                            }
                        }
                    },
                },
                "post": {"operationId": "create_company"},
            },
            "/api/v1/companies/{company_id}": {
                "parameters": [
                    {"$ref": "#/components/parameters/CompanyId"},
                ],
                "get": {"summary": "Read one company"},
                "delete": {"operationId": "delete_company"},
            },
            "/notes": {"post": {"operationId": "create_note"}},
        },
        "components": {
            "parameters": {
                "CompanyId": {
                    "name": "company_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            },
            "schemas": {
                "Page": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Co"}}
                    },
                },
                "Co": {
                    "type": "object",
                    "properties": {"parent": {"$ref": "#/components/schemas/Co"}},
                },
            },
        },
    }


def test_only_get_operations_are_indexed() -> None:
    operations = load_operations(document())

    assert sorted(operations) == ["get_api_v1_companies_company_id", "list_companies"]
    assert operations["list_companies"].summary == "List companies"


def test_header_and_cookie_parameters_are_never_request_inputs() -> None:
    operation = load_operations(document())["list_companies"]

    assert [(item.name, item.location) for item in operation.parameters] == [
        ("limit", "query"),
        ("tag", "query"),
    ]


def test_path_level_parameter_references_are_resolved() -> None:
    operation = load_operations(document())["get_api_v1_companies_company_id"]

    assert [(item.name, item.location, item.required) for item in operation.parameters] == [
        ("company_id", "path", True)
    ]


@pytest.mark.parametrize("value", [None, [], "text", {"paths": []}, {"openapi": "3.1.0"}])
def test_a_document_without_a_paths_object_is_refused(value: object) -> None:
    with pytest.raises(SvpError, match=r"^svp\.specification_invalid$"):
        load_operations(value)


def test_targets_stay_under_the_api_root_whichever_way_the_document_spells_paths() -> None:
    operations = load_operations(document())

    assert request_target(operations["list_companies"], {}, {"limit": 5, "tag": ["a", "b"]}) == (
        "https://scalevp-mcp.com/api/v1/companies",
        [("limit", "5"), ("tag", "a"), ("tag", "b")],
    )
    assert request_target(
        operations["get_api_v1_companies_company_id"], {"company_id": "acme/1 ?"}, {}
    ) == ("https://scalevp-mcp.com/api/v1/companies/acme%2F1%20%3F", [])


@pytest.mark.parametrize("value", ["", ".", "..", 1.5, True, None, ["a"]])
def test_a_path_value_that_could_leave_its_segment_is_refused(value: object) -> None:
    operation = load_operations(document())["get_api_v1_companies_company_id"]

    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        request_target(operation, {"company_id": value}, {})


@pytest.mark.parametrize(
    ("path_parameters", "query"),
    [
        ({}, {}),
        ({"company_id": "a", "other": "b"}, {}),
        ({"company_id": "a"}, {"undeclared": "1"}),
        ({"company_id": "a"}, {"limit": {"nested": 1}}),
    ],
    ids=["missing-path", "unknown-path", "undeclared-query", "structured-query"],
)
def test_undeclared_or_missing_arguments_are_refused(
    path_parameters: dict[str, object], query: dict[str, object]
) -> None:
    operations = load_operations(document())
    operation = (
        operations["list_companies"]
        if "limit" in query
        else operations["get_api_v1_companies_company_id"]
    )

    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        request_target(operation, path_parameters, query)


def test_a_missing_required_query_parameter_is_refused() -> None:
    spec = document()
    spec["paths"]["/companies"]["get"]["parameters"][0]["required"] = True
    operation = load_operations(spec)["list_companies"]

    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        request_target(operation, {}, {})


@pytest.mark.parametrize(
    "path",
    ["//evil.example/api/v1/x", "/api/v1/../register", "companies", "/a b", "/x#y", "/x?y=1"],
)
def test_a_document_path_that_escapes_the_api_root_is_not_indexed(path: str) -> None:
    spec = {"paths": {path: {"get": {"operationId": "escape"}}}}

    assert load_operations(spec) == {}


def test_duplicate_or_unusable_operation_ids_fall_back_to_the_path() -> None:
    spec = {
        "paths": {
            "/a": {"get": {"operationId": "same"}},
            "/b": {"get": {"operationId": "same"}},
            "/c": {"get": {"operationId": "not usable!"}},
        }
    }

    assert sorted(load_operations(spec)) == ["get_b", "get_c", "same"]


def test_description_resolves_local_references_and_stops_at_cycles() -> None:
    spec = document()
    described = describe(spec, load_operations(spec)["list_companies"])

    assert described["method"] == "GET"
    assert described["path"] == "/companies"
    assert [item["name"] for item in described["parameters"]] == ["limit", "tag"]
    items = described["response_schema"]["properties"]["items"]["items"]
    assert items["properties"]["parent"] == {"$ref": "#/components/schemas/Co"}


def test_an_oversized_description_drops_the_response_schema() -> None:
    spec = document()
    spec["components"]["schemas"]["Page"]["description"] = "x" * 200_000
    described = describe(spec, load_operations(spec)["list_companies"])

    assert described["response_schema"] is None
    assert described["response_schema_omitted"] is True
