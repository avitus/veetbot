"""The published document yields only confined, read-only operations (ADR-0153)."""

from __future__ import annotations

from typing import Any

import pytest

from svp_mcp import constants, specification
from svp_mcp.errors import SvpError
from svp_mcp.specification import describe, load_operations, request_target


def document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/v1/companies": {
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
            "/v1/companies/{company_id}": {
                "parameters": [
                    {"$ref": "#/components/parameters/CompanyId"},
                ],
                "get": {"summary": "Read one company"},
                "delete": {"operationId": "delete_company"},
            },
            "/v1/notes": {"post": {"operationId": "create_note"}},
            "/v2/companies": {"get": {"operationId": "other_version"}},
            "/openapi.json": {"get": {"operationId": "the_document"}},
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

    assert sorted(operations) == ["get_v1_companies_company_id", "list_companies"]
    assert operations["list_companies"].summary == "List companies"


def test_header_and_cookie_parameters_are_never_request_inputs() -> None:
    operation = load_operations(document())["list_companies"]

    assert [(item.name, item.location) for item in operation.parameters] == [
        ("limit", "query"),
        ("tag", "query"),
    ]


def test_path_level_parameter_references_are_resolved() -> None:
    operation = load_operations(document())["get_v1_companies_company_id"]

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
        operations["get_v1_companies_company_id"], {"company_id": "acme/1 ?"}, {}
    ) == ("https://scalevp-mcp.com/api/v1/companies/acme%2F1%20%3F", [])


@pytest.mark.parametrize("value", ["", ".", "..", 1.5, True, None, ["a"]])
def test_a_path_value_that_could_leave_its_segment_is_refused(value: object) -> None:
    operation = load_operations(document())["get_v1_companies_company_id"]

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
        else operations["get_v1_companies_company_id"]
    )

    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        request_target(operation, path_parameters, query)


def test_a_missing_required_query_parameter_is_refused() -> None:
    spec = document()
    spec["paths"]["/v1/companies"]["get"]["parameters"][0]["required"] = True
    operation = load_operations(spec)["list_companies"]

    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        request_target(operation, {}, {})


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/v1/x",
        "/v1/../openapi.json",
        "v1/companies",
        "/companies",
        "/v1",
        "/v1/a b",
        "/v1/x#y",
        "/v1/x?y=1",
    ],
)
def test_a_document_path_that_escapes_the_api_root_is_not_indexed(path: str) -> None:
    spec = {"paths": {path: {"get": {"operationId": "escape"}}}}

    assert load_operations(spec) == {}


def test_duplicate_or_unusable_operation_ids_fall_back_to_the_path() -> None:
    spec = {
        "paths": {
            "/v1/a": {"get": {"operationId": "same"}},
            "/v1/b": {"get": {"operationId": "same"}},
            "/v1/c": {"get": {"operationId": "not usable!"}},
        }
    }

    assert sorted(load_operations(spec)) == ["get_v1_b", "get_v1_c", "same"]


def test_description_resolves_local_references_and_stops_at_cycles() -> None:
    spec = document()
    described = describe(spec, load_operations(spec)["list_companies"])

    assert described["method"] == "GET"
    assert described["path"] == "/v1/companies"
    assert [item["name"] for item in described["parameters"]] == ["limit", "tag"]
    items = described["response_schema"]["properties"]["items"]["items"]
    assert items["properties"]["parent"] == {"$ref": "#/components/schemas/Co"}


def test_an_oversized_description_drops_the_response_schema() -> None:
    spec = document()
    spec["components"]["schemas"]["Page"]["description"] = "x" * 200_000
    described = describe(spec, load_operations(spec)["list_companies"])

    assert described["response_schema"] is None
    assert described["response_schema_omitted"] is True


def search_document() -> dict[str, Any]:
    return {
        "paths": {
            "/v1/companies/_search": {
                "post": {
                    "operationId": "search_companies",
                    "summary": "Search companies",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Search"}}
                        },
                    },
                }
            },
            "/v1/contacts/_search": {
                "post": {
                    "operationId": "search_contacts",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"contact_name": {"type": "string"}},
                                }
                            }
                        }
                    },
                }
            },
            "/v1/companies": {
                "get": {"operationId": "browse_companies"},
                "post": {"operationId": "create_company"},
            },
            "/v1/web-cache/_search": {"post": {"operationId": "web_search"}},
            "/v1/tasks": {"post": {"operationId": "create_task"}},
        },
        "components": {
            "schemas": {
                "Search": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                }
            }
        },
    }


def test_the_post_allowlist_is_exactly_the_reviewed_read_operations() -> None:
    assert getattr(constants, "READ_POST_PATHS", None) == frozenset(
        {
            "/v1/companies/_lookup",
            "/v1/companies/_search",
            "/v1/contacts/_search",
            "/v1/deals/_search",
            "/v1/investors/_coinvestors",
            "/v1/investors/_portfolio",
            "/v1/search",
        }
    )


def test_only_allowlisted_posts_are_indexed_beside_the_gets() -> None:
    operations = load_operations(search_document())

    assert {name: getattr(item, "method", None) for name, item in operations.items()} == {
        "browse_companies": "GET",
        "search_companies": "POST",
        "search_contacts": "POST",
    }


def test_a_read_post_body_is_checked_against_its_declared_properties() -> None:
    request_body = getattr(specification, "request_body", None)
    assert callable(request_body), "read-post bodies must be validated"
    operations = load_operations(search_document())
    search = operations["search_companies"]

    assert request_body(search, {"query": "robotics", "limit": 5}) == {
        "query": "robotics",
        "limit": 5,
    }
    assert request_body(operations["search_contacts"], None) == {}
    for body in (
        None,
        {},
        {"limit": 5},
        {"query": "robotics", "undeclared": 1},
        ["robotics"],
        "robotics",
        {"query": float("nan")},
        {"query": "x" * 20_000},
    ):
        with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
            request_body(search, body)


def test_a_get_operation_takes_no_body() -> None:
    request_body = getattr(specification, "request_body", None)
    assert callable(request_body), "read-post bodies must be validated"
    browse = load_operations(search_document())["browse_companies"]

    assert request_body(browse, None) is None
    with pytest.raises(SvpError, match=r"^svp\.arguments_invalid$"):
        request_body(browse, {"query": "robotics"})


def test_description_carries_the_method_and_resolved_request_body() -> None:
    spec = search_document()
    described = describe(spec, load_operations(spec)["search_companies"])

    assert described["method"] == "POST"
    assert described["request_body"]["required"] == ["query"]
    assert sorted(described["request_body"]["properties"]) == ["limit", "query"]
