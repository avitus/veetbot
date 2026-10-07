"""Bounded extraction is a typed browser read, never action authority."""

from typing import Any

import pytest

from agent_core.domain.browser import BrowserObservation
from agent_core.tools.browser_observe import BrowserObserveTool
from tests.contract.support import tool_context
from tests.unit.test_browser_tools import FakeBrowserProvider


def extraction_arguments(revision: str = "revision-1", *, kind: str = "table") -> dict[str, Any]:
    return {
        "extract": {
            "expected_revision": revision,
            "kind": kind,
            "index": 0,
            "fields": [{"name": "item", "column": 0, "type": "string"}],
            "row_limit": 20,
        }
    }


async def test_extraction_dispatches_only_the_optional_read_capability() -> None:
    class Provider(FakeBrowserProvider):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[Any] = []

        async def extract(self, request: Any) -> BrowserObservation:
            self.requests.append(request)
            return extracted_page(request)

    provider = Provider()
    result = await BrowserObserveTool(provider).execute(extraction_arguments(), tool_context())
    assert result.ok, "browser.observe does not yet accept typed extraction"
    assert len(provider.requests) == 1
    assert provider.requests[0].kind == "table"
    assert provider.observation_count == 0


async def test_legacy_provider_refuses_extraction_instead_of_silently_observing() -> None:
    provider = FakeBrowserProvider()
    result = await BrowserObserveTool(provider).execute(extraction_arguments(), tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.action_not_allowed"
    assert provider.observation_count == 0


@pytest.mark.parametrize(
    "change",
    [
        "script",
        "profile",
        "mixed",
        "bool_index",
        "large_rows",
        "duplicate",
        "empty",
        "unknown_type",
    ],
)
async def test_invalid_extraction_is_refused_before_binding(change: str) -> None:
    provider = FakeBrowserProvider()
    arguments = extraction_arguments()
    if change in {"script", "profile"}:
        arguments["extract"][change] = "canary"
    elif change == "mixed":
        arguments["after"] = "old-ref"
    elif change == "bool_index":
        arguments["extract"]["index"] = True
    elif change == "large_rows":
        arguments["extract"]["row_limit"] = 51
    elif change == "duplicate":
        arguments["extract"]["fields"] *= 2
    elif change == "empty":
        arguments["extract"]["fields"] = []
    else:
        arguments["extract"]["fields"][0]["type"] = "javascript"
    result = await BrowserObserveTool(provider).execute(arguments, tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.arguments_invalid"
    assert provider.observation_count == 0 and not provider.execution_contexts


async def test_adapter_cannot_silently_drop_the_extraction_request() -> None:
    class IgnoringProvider(FakeBrowserProvider):
        async def extract(self, request: Any) -> BrowserObservation:
            del request
            return self._observation("https://example.org/lesson")

    result = await BrowserObserveTool(IgnoringProvider()).execute(
        extraction_arguments(), tool_context()
    )
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.output_invalid"


def extracted_page(request: Any, revision: str = "new-revision") -> BrowserObservation:
    from agent_core.adapters.browser.extraction import extracted_observation

    raw = {
        "status": "extracted",
        "source_name": "Fixture",
        "rows": [],
        "source_nodes": 2,
        "source_scan_limit_reached": False,
        "row_nodes": 0,
        "row_scan_limit_reached": False,
        "omitted_rows": 0,
    }
    return BrowserObservation(
        url="https://example.org/lesson",
        revision=revision,
        extraction=extracted_observation(raw, request, revision),
    )


@pytest.mark.parametrize(
    ("kind", "text", "required", "status", "value", "valid"),
    [
        ("integer", "23", True, "present", 23, True),
        ("integer", "01", True, "invalid", None, False),
        ("integer", "9007199254740992", True, "invalid", None, False),
        ("number", "-1.25e2", True, "present", -125.0, True),
        ("number", "1e309", True, "invalid", None, False),
        ("number", "$12.00", True, "invalid", None, False),
        ("boolean", "false", True, "present", False, True),
        ("boolean", "yes", True, "invalid", None, False),
        ("string", "", True, "missing", None, False),
        ("string", "", False, "missing", None, True),
    ],
)
def test_primitive_conversion_is_explicit_and_schema_checked(
    kind: str, text: str, required: bool, status: str, value: Any, valid: bool
) -> None:
    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser_extraction import BrowserExtractionRequest

    args = extraction_arguments()["extract"]
    args["fields"][0].update(type=kind, required=required)
    request = BrowserExtractionRequest.model_validate(args)
    raw = {
        "status": "extracted",
        "source_name": "Fixture",
        "rows": [[{"text": text, "text_truncated": False}]],
        "source_nodes": 2,
        "source_scan_limit_reached": False,
        "row_nodes": 1,
        "row_scan_limit_reached": False,
        "omitted_rows": 0,
    }
    result = extracted_observation(raw, request, "new-revision")
    assert result.rows[0].schema_valid is valid
    assert result.rows[0].cells[0].status == status
    assert result.rows[0].cells[0].value == value


@pytest.mark.parametrize("budget", [512, 1024, 4096, 8192])
def test_extraction_projection_keeps_whole_typed_rows_or_explicit_omissions(budget: int) -> None:
    import json
    from uuid import UUID

    from jsonschema import Draft202012Validator

    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser_extraction import (
        BrowserExtractionRequest,
        BrowserExtractionResult,
    )
    from agent_core.domain.browser_projection import browser_context_projection
    from agent_core.domain.messages import FileReferencePart, TextPart
    from agent_core.domain.tool_output import content_bytes
    from agent_core.tools.browser_results import (
        OUTPUT_SCHEMA,
        observation_evidence_key,
        observation_result,
    )

    request = BrowserExtractionRequest.model_validate(extraction_arguments()["extract"])
    raw = {
        "status": "extracted",
        "source_name": "Fixture",
        "rows": [[{"text": f"Item {i}", "text_truncated": False}] for i in range(20)],
        "source_nodes": 2,
        "source_scan_limit_reached": False,
        "row_nodes": 20,
        "row_scan_limit_reached": False,
        "omitted_rows": 3,
    }
    extracted = extracted_observation(raw, request, "new-revision")
    page = extracted_page(request).model_copy(update={"extraction": extracted})
    original = observation_result(FakeBrowserProvider(), page, 512 * 1024)
    assert original.structured is not None
    Draft202012Validator(OUTPUT_SCHEMA).validate(original.structured)
    projected = browser_context_projection(
        original.structured,
        budget=budget,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    assert len(content_bytes(projected)) <= budget
    data = json.loads(projected[0].text)
    if "extraction" in data:
        admitted = BrowserExtractionResult.model_validate(data["extraction"])
        assert admitted.omitted_rows == 3 + 20 - len(admitted.rows)
        assert all(row in extracted.rows for row in admitted.rows)
        if budget >= 4096:
            assert admitted.rows, "a normal model budget lost all requested rows"
    else:
        assert data.get("observation_omitted") or "extraction" in data["coverage"]["omitted_fields"]
    renewed = page.model_copy(
        update={"revision": "next", "extraction": extracted_observation(raw, request, "next")}
    )
    assert observation_evidence_key(page.model_dump()) == observation_evidence_key(
        renewed.model_dump()
    )
    bounded = observation_result(FakeBrowserProvider(), page, 4096)
    assert len(content_bytes(bounded.content)) <= 4096
    assert bounded.structured is not None
    Draft202012Validator(OUTPUT_SCHEMA).validate(bounded.structured)


def test_extraction_byte_ceiling_omits_whole_rows() -> None:
    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser_extraction import BrowserExtractionRequest

    args = extraction_arguments()["extract"]
    args["row_limit"] = 50
    args["fields"] = [{"name": f"field{i}", "column": i, "type": "string"} for i in range(8)]
    request = BrowserExtractionRequest.model_validate(args)
    raw = {
        "status": "extracted",
        "source_name": "Fixture",
        "rows": [
            [{"text": "雪" * 256, "text_truncated": False} for _ in range(8)] for _ in range(50)
        ],
        "source_nodes": 2,
        "source_scan_limit_reached": False,
        "row_nodes": 50,
        "row_scan_limit_reached": False,
        "omitted_rows": 0,
    }
    result = extracted_observation(raw, request, "new-revision")
    assert len(result.model_dump_json().encode("utf-8")) <= 65536
    assert result.byte_limit_reached and result.omitted_rows == 50 - len(result.rows)
    assert result.rows and all(len(row.cells) == 8 for row in result.rows)


def test_exact_projection_budget_includes_the_final_coverage_flag() -> None:
    from uuid import UUID

    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser_extraction import BrowserExtractionRequest
    from agent_core.domain.browser_projection import browser_context_projection
    from agent_core.domain.messages import FileReferencePart
    from agent_core.domain.tool_output import content_bytes

    request = BrowserExtractionRequest.model_validate(extraction_arguments()["extract"])
    raw = {
        "status": "extracted",
        "source_name": "Fixture",
        "rows": [[{"text": "Item", "text_truncated": False}]],
        "source_nodes": 2,
        "source_scan_limit_reached": False,
        "row_nodes": 1,
        "row_scan_limit_reached": False,
        "omitted_rows": 0,
    }
    page = extracted_page(request).model_copy(
        update={"extraction": extracted_observation(raw, request, "new-revision")}
    )
    reference = FileReferencePart(artifact_id=UUID(int=1), media_type="application/json")
    complete = browser_context_projection(page.model_dump(), budget=8192, reference=reference)
    assert complete is not None
    exact = len(content_bytes(complete))
    projected = browser_context_projection(page.model_dump(), budget=exact - 1, reference=reference)
    assert projected is not None
    assert len(content_bytes(projected)) <= exact - 1


@pytest.mark.parametrize("malformed", ["revision", "field", "type", "status", "validity"])
def test_extraction_results_reject_contradictory_typed_evidence(malformed: str) -> None:
    from pydantic import ValidationError

    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser_extraction import BrowserExtractionRequest

    args = extraction_arguments()["extract"]
    args["fields"][0]["type"] = "integer"
    request = BrowserExtractionRequest.model_validate(args)
    extracted = extracted_observation(
        {
            "status": "extracted",
            "source_name": "Fixture",
            "rows": [[{"text": "1", "text_truncated": False}]],
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": 1,
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        },
        request,
        "new-revision",
    )
    page = extracted_page(request).model_copy(update={"extraction": extracted}).model_dump()
    data = page["extraction"]
    if malformed == "revision":
        data["revision"] = "another-page"
    elif malformed == "field":
        data["rows"][0]["cells"][0]["field"] = "foreign"
    elif malformed == "type":
        data["rows"][0]["cells"][0]["value"] = True
    elif malformed == "status":
        data["rows"][0]["cells"][0]["status"] = "missing"
    else:
        data["rows"][0]["schema_valid"] = False
    with pytest.raises(ValidationError):
        BrowserObservation.model_validate(page)


def test_requested_extraction_rows_take_priority_over_optional_controls() -> None:
    from agent_core.adapters.browser.extraction import extracted_observation
    from agent_core.domain.browser import BrowserElement
    from agent_core.domain.browser_extraction import BrowserExtractionRequest
    from agent_core.tools.browser_results import observation_result

    request = BrowserExtractionRequest.model_validate(extraction_arguments()["extract"])
    extracted = extracted_observation(
        {
            "status": "extracted",
            "source_name": "Fixture",
            "rows": [[{"text": "Requested row", "text_truncated": False}]],
            "source_nodes": 2,
            "source_scan_limit_reached": False,
            "row_nodes": 1,
            "row_scan_limit_reached": False,
            "omitted_rows": 0,
        },
        request,
        "new-revision",
    )
    page = extracted_page(request).model_copy(
        update={
            "extraction": extracted,
            "elements": tuple(
                BrowserElement(ref=f"control-{i}", role="button", name="雪" * 256)
                for i in range(100)
            ),
        }
    )
    bounded = observation_result(FakeBrowserProvider(), page, 4096)
    assert bounded.ok and bounded.structured is not None
    assert bounded.structured["extraction"]["rows"] == extracted.model_dump(mode="json")["rows"]
