"""Public photos are discoverable when public web reads are enabled."""

import base64
import io
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from PIL import Image

from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.common import rendered_attachments
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.web.public_image import PublicImageReader
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.artifacts import AttachmentRead
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.web_inspect_image import WebInspectImageTool
from tests.contract.support import tool_context
from tests.contract.test_model_gateway_contract import ATTEMPT, resolved
from tests.unit.test_config import base_environment
from tests.unit.test_web_tools import FakeWebProvider


async def test_public_image_inspection_is_registered() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    async with build(settings=settings, web_fetch_provider_override=FakeWebProvider()) as app:
        assert app.tool_pipeline._registry.get("web.inspect_image") is not None


async def test_smugmug_selected_photo_reaches_model_without_upload(tmp_path: Path) -> None:
    """Trace the selected gallery photo through both vision-capable wire formats."""
    page = "https://owner.smugmug.com/Travel/Japan/i-selected/A"
    direct = "https://photos.smugmug.com/Travel/i-selected/XL/selected.jpg"
    source = io.BytesIO()
    Image.new("RGB", (47, 31), "red").save(source, "JPEG")
    hosts: list[str] = []

    async def resolve(host: str) -> tuple[str, ...]:
        """Return deterministic addresses for the requested fixture host."""
        hosts.append(host)
        return ("8.8.8.8",)

    def serve(request: httpx.Request) -> httpx.Response:
        """Serve the fixture response and verify the expected request path."""
        assert request.url.host == "8.8.8.8"
        if request.headers["host"] == "owner.smugmug.com":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(f'<img src="/unselected.jpg"><meta property="og:image" content="{direct}">'),
            )
        assert request.url.path.endswith("/selected.jpg")
        return httpx.Response(200, content=source.getvalue())

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="tool.call",
                        arguments={"name": "web.inspect_image", "arguments": {"url": page}},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Done.", stop_reason=StopReason.END_TURN),
        ]
    )
    settings = load_settings(
        {**base_environment(), "SANDBOX_MECHANISM": "fake", "AGENT_ARTIFACT_ROOT": str(tmp_path)}
    )
    async with build(
        settings=settings, script=script, web_fetch_provider_override=FakeWebProvider()
    ) as app:
        tool = cast(
            WebInspectImageTool,
            cast(
                RegisteredTool, app.tool_pipeline._registry.get("web.inspect_image")
            ).implementation,
        )
        tool.read = PublicImageReader(resolve, httpx.MockTransport(serve)).read
        run_id = await app.runs.submit(f"Inspect this photo: {page}")
        await app.runs.wait_terminal(run_id)
        async with app.uow_factory() as uow:
            [call] = await uow.invocations.list_for_run(run_id, app.principal)
        assert call.status.value == "SUCCEEDED", call.outcome
        request = cast(FakeModelProvider, app.executor._model_provider).requests[-1]
        model = resolved("openai")
        model.capabilities.images = True
        attempt = ATTEMPT.model_copy(update={"run_id": run_id})
        rendered = await rendered_attachments(app.attachment_resolver, request, model, attempt)
        wire = OpenAIResponsesProvider._request_payload(request, model, rendered)
        [result] = [v for v in wire["input"] if v.get("type") == "function_call_output"]
        [url] = [v["image_url"] for v in result["output"] if v["type"] == "input_image"]
        with Image.open(io.BytesIO(base64.b64decode(url.split(",")[1]))) as image:
            assert image.size == (47, 31)
            pixel = image.convert("RGB").getpixel((0, 0))
            assert isinstance(pixel, tuple) and pixel[0] > 245
        anthropic, _, _ = AnthropicMessagesProvider._request_payload(request, model, rendered)
        [block] = [
            b for m in anthropic["messages"] for b in m["content"] if b["type"] == "tool_result"
        ]
        assert any(b["type"] == "image" for b in block["content"])
        assert (
            await rendered_attachments(app.attachment_resolver, request, model, attempt) == rendered
        )
        assert call.structured_result and app.attachment_resolver
        artifact_id = UUID(call.structured_result["artifact_id"])
        forged = AttachmentRead(artifact_id, 4096, inspection_call="different-call")
        assert not await app.attachment_resolver.resolve([forged], run_id=run_id)
        other = await app.runs.submit("Another session.")
        await app.runs.wait_terminal(other)
        assert not await app.attachment_resolver.resolve(
            [AttachmentRead(artifact_id, 4096, inspection_call=call.call_id)], run_id=other
        )
        model.capabilities.images = False
        unavailable = await rendered_attachments(app.attachment_resolver, request, model, attempt)
        assert all(a.data is None and a.marker for a in unavailable.values())
        assert hosts == ["owner.smugmug.com", "photos.smugmug.com"]


@pytest.mark.parametrize(
    "case", ["ambiguous", "private", "redirect", "invalid", "oversize", "wrong_photo"]
)
async def test_refuses_unusable_or_unsafe_image(case: str) -> None:
    """Refuse unsafe network hops and invalid or ambiguous image responses."""
    connections = 0

    async def resolve(host: str) -> tuple[str, ...]:
        """Return deterministic addresses for the requested fixture host."""
        return (
            ("127.0.0.1",) if case == "private" or host == "private.example.com" else ("8.8.8.8",)
        )

    def serve(request: httpx.Request) -> httpx.Response:
        """Serve the fixture response and verify the expected request path."""
        nonlocal connections
        connections += 1
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://private.example.com/a"})
        if case in {"ambiguous", "wrong_photo"}:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<meta property="og:image" content="/cover.jpg">',
            )
        return httpx.Response(200, content=b"x" * (12_000_001 if case == "oversize" else 1))

    with pytest.raises((ValueError, OSError)):
        path = "/album/i-selected/A" if case == "wrong_photo" else "/album"
        await PublicImageReader(resolve, httpx.MockTransport(serve)).read(
            "https://owner.smugmug.com" + path
        )
    assert connections == int(case != "private")


async def test_missing_artifact_scope_refuses_before_network() -> None:
    """Check artifact read authority before making a network request."""

    async def unexpected(url: str) -> bytes:
        pytest.fail(f"Unexpected request for {url}")

    result = await WebInspectImageTool(unexpected).execute(
        {"url": "https://photos.example.com/photo.jpg"}, tool_context()
    )
    assert result.failure and result.failure.reason_code == "policy.scope.missing"
