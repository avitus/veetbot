"""Upload arguments authorize one conversation image, never paths or broad grants."""

from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserAction, BrowserObservation
from agent_core.domain.browser_upload import BrowserImageFile, BrowserImagePayload
from agent_core.domain.media import MediaImage, MediaInputError
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.runs import RunStatus
from agent_core.tools.browser_upload import BrowserUploadTool
from agent_core.tools.registry import RegisteredTool, StaticToolRegistry
from tests.contract.support import tool_context
from tests.unit.test_browser_playwright import lesson_constraint
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_config import base_environment

IMAGE_ID = "00000000-0000-0000-0000-000000000001"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-image"
ARGUMENTS = {"image_id": IMAGE_ID, "expected_revision": "revision-1", "ref": "element-1"}


class UploadProvider(FakeBrowserProvider):
    async def upload(self, action: BrowserAction, image: BrowserImageFile) -> BrowserObservation:
        assert action.expected_revision == "revision-1"
        assert image.filename == f"{IMAGE_ID}.png"
        assert image.image.data == PNG
        self.actions.append(action)
        return self._observation("https://example.org/compose")


def test_upload_is_a_serial_high_risk_non_idempotent_external_write() -> None:
    spec = BrowserUploadTool.spec
    assert spec.side_effect is SideEffectClass.EXTERNAL_WRITE
    assert spec.risk is RiskLevel.HIGH
    assert spec.idempotency is IdempotencyClass.NON_IDEMPOTENT
    assert spec.required_scopes == {"artifact.read"}
    assert not spec.allow_parallel
    StaticToolRegistry().register(BrowserUploadTool(UploadProvider(), image_resolver=AsyncMock()))


@pytest.mark.parametrize(
    "case", ["ok", "scope", "grant", "foreign", "expired", "oversized", "signature", "path"]
)
async def test_upload_releases_only_verified_images_before_marking_an_effect(case: str) -> None:
    provider = UploadProvider()
    resolver = AsyncMock()
    resolver.resolve.return_value = (MediaImage("image/png", PNG),)
    if case in {"foreign", "expired"}:
        resolver.resolve.side_effect = MediaInputError("unavailable")
    if case == "oversized":
        resolver.resolve.return_value = (MediaImage("image/png", PNG + bytes(5 * 1024 * 1024)),)
    if case == "signature":
        resolver.resolve.return_value = (MediaImage("image/png", b"not an image"),)
    context = tool_context()
    marked = AsyncMock()
    context = replace(
        context,
        principal=context.principal.model_copy(
            update={"scopes": set() if case == "scope" else {"artifact.read"}}
        ),
        mark_effect_sent=marked,
        dispatch_constraint=lesson_constraint() if case == "grant" else None,
    )
    arguments = ARGUMENTS | ({"path": "/etc/private"} if case == "path" else {})
    result = await BrowserUploadTool(provider, image_resolver=resolver).execute(arguments, context)
    assert result.ok is (case == "ok")
    if case == "ok":
        marked.assert_awaited_once()
        assert len(provider.actions) == 1
        assert PNG.decode("latin-1") not in repr(result)
        assert resolver.resolve.call_args.kwargs["run_id"] == context.run_id
        assert resolver.resolve.call_args.kwargs["principal"] == context.principal
    else:
        marked.assert_not_awaited()
        assert not provider.actions


@pytest.mark.parametrize("case", ["path", "mime", "base64", "oversized"])
def test_service_image_envelope_refuses_paths_invalid_images_and_large_payloads(case: str) -> None:
    image = BrowserImageFile.for_artifact(IMAGE_ID, MediaImage("image/png", PNG))
    payload = BrowserImagePayload.encode(image)
    updates = {
        "path": {"filename": "../../private.png"},
        "mime": {"media_type": "text/plain"},
        "base64": {"data_base64": "not base64"},
        "oversized": {"data_base64": "A" * (7 * 1024 * 1024)},
    }
    with pytest.raises((MediaInputError, ValueError)):
        BrowserImagePayload.model_validate(payload.model_dump() | updates[case]).decode()


@pytest.mark.parametrize("approved", [False, True])
async def test_upload_waits_for_individual_approval_before_reading_image(
    tmp_path: Path,
    approved: bool,
) -> None:
    settings = load_settings(
        {**base_environment(), "SANDBOX_MECHANISM": "fake", "AUTH_SCOPES": "artifact.read"}
    )
    settings = replace(settings, artifact_root=tmp_path)
    provider = UploadProvider()
    resolver = AsyncMock()
    resolver.resolve.return_value = (MediaImage("image/png", PNG),)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="browser.upload", arguments=ARGUMENTS)],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Finished.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=settings,
        script=script,
        sequential_ids=True,
        enabled_tools=["browser.upload"],
        browser_provider_override=provider,
    ) as app:
        registered = cast(RegisteredTool, app.tool_pipeline._registry.get("browser.upload"))
        cast(BrowserUploadTool, registered.implementation)._images = resolver
        run_id = await app.runs.submit("Attach this image to the post.")
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        pending = await app.approvals.list_pending(run_id=run_id)
        assert len(pending) == 1
        resolver.resolve.assert_not_awaited()
        assert provider.actions == []
        await app.approvals.resolve(
            pending[0].id,
            ApprovalResolutionType.APPROVE_ONCE if approved else ApprovalResolutionType.DENY,
        )
        await app.runs.wait_terminal(run_id)
        assert len(provider.actions) == int(approved)
        assert resolver.resolve.await_count == int(approved)
