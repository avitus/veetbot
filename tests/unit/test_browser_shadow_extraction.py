"""Pinned extraction scope, provider conformance and transport compatibility."""

import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent_core.domain.browser import BrowserObservation
from agent_core.domain.browser_extraction import BrowserExtractionRequest, BrowserExtractionResult
from agent_core.tools.browser_observe import (
    ArticleBrowserObserveTool,
    BrowserObserveTool,
    ShadowBrowserObserveTool,
)
from tests.contract.support import tool_context
from tests.unit.test_browser_extraction import extracted_page, extraction_arguments
from tests.unit.test_browser_tools import FakeBrowserProvider


@pytest.mark.parametrize(
    "tool", [BrowserObserveTool, ArticleBrowserObserveTool, ShadowBrowserObserveTool]
)
@pytest.mark.parametrize("kind", ["table", "list", "form", "articles"])
async def test_tool_version_selects_scope_without_new_model_arguments(
    tool: type[BrowserObserveTool], kind: str
) -> None:
    requests: list[BrowserExtractionRequest] = []

    class Provider(FakeBrowserProvider):
        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            requests.append(request)
            return extracted_page(request)

    provider = Provider()
    result = await tool(provider).execute(extraction_arguments(kind=kind), tool_context())
    if kind == "articles" and tool is BrowserObserveTool:
        assert not result.ok and not requests
        return
    shadow = tool is ShadowBrowserObserveTool and kind != "articles"
    assert result.ok and result.structured is not None
    assert requests[0].scope == ("main_document_and_open_shadow" if shadow else "main_document")
    assert result.structured["extraction"]["version"] == (2 if shadow else 1)
    assert tool.spec.output_schema is not None
    Draft202012Validator(tool.spec.output_schema).validate(result.structured)
    assert not provider.actions and provider.observation_count == 0
    assert "scope" not in tool.spec.input_schema["properties"]["extract"]["properties"]


@pytest.mark.parametrize("tool", [BrowserObserveTool, ArticleBrowserObserveTool])
def test_pinned_output_scope_is_not_widened(tool: type[BrowserObserveTool]) -> None:
    assert tool.spec.output_schema is not None
    schema = tool.spec.output_schema["properties"]["extraction"]["properties"]
    assert schema["version"].get("const") == 1
    assert schema["scope"]["enum"] == ["main_document", "main_document_viewport_articles"]


@pytest.mark.parametrize(
    "tool", [BrowserObserveTool, ArticleBrowserObserveTool, ShadowBrowserObserveTool]
)
async def test_model_cannot_override_the_pinned_scope(tool: type[BrowserObserveTool]) -> None:
    provider = FakeBrowserProvider()
    args = extraction_arguments()
    args["extract"]["scope"] = "main_document_and_open_shadow"
    result = await tool(provider).execute(args, tool_context())
    assert not result.ok and not provider.execution_contexts


@pytest.mark.parametrize("widen", [False, True])
async def test_provider_must_honor_the_exact_requested_scope(widen: bool) -> None:
    class Provider(FakeBrowserProvider):
        async def extract(self, request: BrowserExtractionRequest) -> BrowserObservation:
            return extracted_page(
                request.model_copy(
                    update={"scope": "main_document_and_open_shadow" if widen else "main_document"}
                )
            )

    tool = BrowserObserveTool if widen else ShadowBrowserObserveTool
    result = await tool(Provider()).execute(extraction_arguments(), tool_context())
    assert not result.ok and result.failure is not None
    assert result.failure.reason_code == "tool.browser.output_invalid"


@pytest.mark.parametrize(
    "version,scope", [(1, "main_document_and_open_shadow"), (2, "main_document")]
)
def test_extraction_version_must_describe_its_scope(version: int, scope: str) -> None:
    request = BrowserExtractionRequest.model_validate(extraction_arguments()["extract"])
    extraction = extracted_page(request).extraction
    assert extraction is not None
    with pytest.raises(ValidationError, match="scope"):
        BrowserExtractionResult.model_validate(
            {**extraction.model_dump(), "version": version, "scope": scope}
        )


def test_article_scope_cannot_be_widened() -> None:
    args = extraction_arguments(kind="articles")["extract"]
    with pytest.raises(ValidationError, match="document-only"):
        BrowserExtractionRequest.model_validate({**args, "scope": "main_document_and_open_shadow"})


@pytest.mark.parametrize("shadow", [False, True])
async def test_hosted_wire_preserves_old_requests_and_new_scope(shadow: bool) -> None:
    from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
    from agent_core.adapters.credentials import MappingCredentialResolver

    request = BrowserExtractionRequest.model_validate(extraction_arguments()["extract"])
    if shadow:
        request = request.model_copy(update={"scope": "main_document_and_open_shadow"})

    def service(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert ("scope" in payload["extraction"]) is shadow
        parsed = BrowserExtractionRequest.model_validate(payload["extraction"])
        assert parsed == request
        return httpx.Response(200, json=extracted_page(parsed).model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        control = HostedBrowserSessionControlPlane(
            base_url="https://browser.example.test",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=client,
        )
        result = await control.extract("lease", request)
    assert result.extraction is not None and result.extraction.version == (2 if shadow else 1)


@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_hosted_service_preserves_requested_scope_in_both_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: bool, stream: bool
) -> None:
    from tests.unit.test_browser_dispatch_constraint import SERVICE_AUTH, _app, leased
    from tests.unit.test_browser_progress import PROGRESS_MEDIA_TYPE

    sessions, runtime, lease = await leased(tmp_path)
    seen: list[BrowserExtractionRequest] = []

    async def extract(request: BrowserExtractionRequest) -> BrowserObservation:
        seen.append(request)
        return extracted_page(request)

    monkeypatch.setattr(runtime, "extract", extract, raising=False)
    arguments = extraction_arguments()["extract"]
    if shadow:
        arguments["scope"] = "main_document_and_open_shadow"
    headers = {"Authorization": f"Bearer {SERVICE_AUTH}"}
    if stream:
        headers["Accept"] = PROGRESS_MEDIA_TYPE
    async with _app(sessions) as client:
        response = await client.post(
            "/v1/browser-sessions:observe",
            headers=headers,
            json={"lease_ref": lease, "extraction": arguments},
        )
    assert response.status_code == 200
    body = json.loads(response.text.splitlines()[-1])["body"] if stream else response.json()
    assert body["extraction"]["version"] == (2 if shadow else 1)
    assert body["extraction"]["scope"] == seen[0].scope
    assert len(seen) == 1
