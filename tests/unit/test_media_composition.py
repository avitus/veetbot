"""Media tools are available in new chats only with the configured credential."""

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    ToolCallItem,
)
from agent_core.domain.policies import IdempotencyClass, TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus, Step
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.domain.views import ImageContentBlock, TextContentBlock
from agent_core.runtime.cancellation import RunCancellationToken
from tests.contract.test_media_generation_provider_contract import MP4, PNG, tensorscale
from tests.unit.test_config import base_environment


@pytest.mark.parametrize("video", [False, True])
@pytest.mark.parametrize("outcome", ["success", "denied", "scope", "unavailable"])
async def test_chat_images_are_sent_only_after_approval_and_attach_the_result(
    tmp_path: Path, video: bool, outcome: str
) -> None:
    name = "video.generate" if video else "image.generate"
    seen: list[httpx.Request] = []

    def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=MP4 if video else PNG,
            headers={"content-type": "video/mp4" if video else "image/png"},
        )

    call = ScriptedToolCall(
        name=name, arguments={"prompt": "Use image 1 then image 2", "reference_image_ids": []}
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(tool_calls=[call], stop_reason=StopReason.TOOL_USE),
            ScriptedTurn(text="Attached."),
        ]
    )
    settings = replace(
        load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        artifact_root=tmp_path,
        attachment_uploads_enabled=True,
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client,
        build(
            settings=settings, script=script, media_provider_override=tensorscale(client)
        ) as composition,
    ):
        principal = composition.principal
        session = await composition.services.sessions.create(principal, "general", {})
        first, _ = await composition.services.artifacts.upload(
            principal,
            session.id,
            content=PNG,
            filename="first.png",
            declared_media_type="image/png",
            idempotency_key="first",
        )
        last, _ = await composition.services.artifacts.upload(
            principal,
            session.id,
            content=PNG + b"last",
            filename="last.png",
            declared_media_type="image/png",
            idempotency_key="last",
        )
        assert isinstance(call.arguments, dict)
        call.arguments["reference_image_ids"] = [str(first.id), str(last.id)]
        if outcome == "scope":
            principal.scopes.discard("artifact.read")
        if outcome == "unavailable":
            call.arguments["reference_image_ids"] = [str(UUID(int=99999))]
        submitted = await composition.services.runs.submit(
            principal,
            session.id,
            [
                TextContentBlock(text="Use these pictures."),
                ImageContentBlock(artifact_id=first.id, media_type="image/png"),
                ImageContentBlock(artifact_id=last.id, media_type="image/png"),
            ],
            None,
            None,
        )
        [approval] = await composition.approvals.list_pending(run_id=submitted.run_id)
        assert seen == []
        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.DENY
            if outcome == "denied"
            else ApprovalResolutionType.APPROVE_ONCE,
        )
        await composition.runs.wait_terminal(submitted.run_id)
        if outcome != "success":
            assert seen == []
            async with composition.uow_factory() as uow:
                artifacts = await uow.artifacts.list_for_run(submitted.run_id, principal)
                [invocation] = await uow.invocations.list_for_run(submitted.run_id, principal)
            assert not any(artifact.origin == "model_output" for artifact in artifacts)
            assert invocation.status is not ToolInvocationStatus.SUCCEEDED
            assert invocation.outcome is not None
            if outcome == "unavailable":
                assert "No generation request was sent" in invocation.outcome.message
            return
        [sent] = seen
        payload = json.loads(sent.content)
        refs = [entry["data"] for entry in payload["images"]] if video else payload["images"]
        assert [base64.b64decode(uri.split(",", 1)[1]) for uri in refs] == [PNG, PNG + b"last"]
        events = await composition.runs.events(submitted.run_id)
        serialized = json.dumps([e.model_dump(mode="json") for e in events])
        assert "data:image/" not in serialized
        assert base64.b64encode(PNG).decode("ascii") not in serialized
        async with composition.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(submitted.run_id, principal)
        [output] = [artifact for artifact in artifacts if artifact.origin == "model_output"]
        assert output.expires_at is None
        [reply] = [
            e.payload["message"] for e in events if e.event_type == "assistant.message.completed"
        ]
        assert [part["artifact_id"] for part in reply["content"] if part["kind"] == "file"] == [
            str(output.id)
        ]


async def test_tensorscale_key_enables_both_media_tools() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "SANDBOX_MECHANISM": "fake",
            "TENSORSCALE_API_KEY": "synthetic-media-credential",
        }
    )
    async with build(settings=settings) as composition:
        for name in ("image.generate", "video.generate"):
            assert composition.tool_pipeline._registry.get(name).spec.name == name
            assert composition.tool_pipeline._registry.get(name).spec.version == "1.1.0"
            assert composition.tool_pipeline._registry.get(name, "1.0.0").spec.version == "1.0.0"


async def test_missing_key_advertises_neither_media_tool() -> None:
    settings = load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"})
    async with build(settings=settings) as composition:
        for name in ("image.generate", "video.generate"):
            with pytest.raises(NotFoundError):
                composition.tool_pipeline._registry.get(name)


@pytest.mark.parametrize("video", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success", "denied", "timeout", "deadline", "scope", "invalid", "partial"]
)
async def test_media_chat_approval_delivery_and_failures(
    tmp_path: Path, video: bool, outcome: str
) -> None:
    name = "video.generate" if video else "image.generate"
    media_type = "video/mp4" if video else "image/png"
    data = MP4 if video else PNG
    seen: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic-media-credential private prompt")
        if outcome == "deadline":
            await asyncio.sleep(2)
        headers = {"content-type": media_type}
        if outcome == "partial":
            headers["content-length"] = str(len(data) + 1)
        return httpx.Response(200, content=data, headers=headers)

    settings = replace(
        load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}), artifact_root=tmp_path
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name=name,
                        arguments={
                            "prompt": "A lighthouse",
                            **({"url": "https://example.org"} if outcome == "invalid" else {}),
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Finished."),
        ]
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client,
        build(
            settings=settings, script=script, media_provider_override=tensorscale(client)
        ) as composition,
    ):
        if outcome == "deadline":
            tool = composition.tool_pipeline._registry.get(name)
            tool.spec = tool.spec.model_copy(update={"timeout_seconds": 1})
        if outcome == "scope":
            # The composition principal is passed by identity to the pipeline.
            composition.principal.scopes.discard("media.generate")
        run_id = await composition.runs.submit("Generate a lighthouse.")
        pending = await composition.approvals.list_pending(run_id=run_id)
        assert seen == []
        if outcome in {"scope", "invalid"}:
            assert pending == []
        else:
            assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
            [approval] = pending
            await composition.approvals.resolve(
                approval.id,
                ApprovalResolutionType.DENY
                if outcome == "denied"
                else ApprovalResolutionType.APPROVE_ONCE,
            )
        run = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            artifacts = await uow.artifacts.list_for_run(run_id, composition.principal)
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            agent = await uow.agents.get_version(run.agent_id, run.agent_version)
        assert name in agent.enabled_tools
        spec = composition.tool_pipeline._registry.get(name).spec
        assert spec.idempotency is IdempotencyClass.NON_IDEMPOTENT
        assert not spec.allow_parallel
        assert "synthetic-media-credential" not in json.dumps(
            [e.model_dump(mode="json") for e in events]
        )
        [reply] = [
            event.payload["message"]
            for event in events
            if event.event_type == "assistant.message.completed"
        ]
        files = [part for part in reply["content"] if part["kind"] == "file"]
        assert run.status is RunStatus.COMPLETED
        if outcome in {"scope", "invalid"}:
            assert invocations == []  # rejected before invocation admission
            assert artifacts == []
            assert files == []
            return
        [invocation] = invocations
        # Replaying the same paid invocation returns its stored outcome, even
        # when that outcome was a timeout or incomplete media.
        replay = await composition.tool_pipeline.dispatch(
            run=run,
            checkpoint=RunCheckpoint(
                run_id=run.id,
                version=1,
                status=RunStatus.RUNNING,
                conversation=[],
                created_at=run.created_at,
            ),
            tool_calls=[
                ToolCallItem(
                    call_id=invocation.call_id,
                    item_index=0,
                    name=name,
                    arguments=json.loads(invocation.raw_arguments),
                    raw_arguments=invocation.raw_arguments,
                )
            ],
            principal=composition.principal,
            agent=agent,
            step=Step(run_id=run.id, step_number=invocation.step_number, started_at=run.created_at),
            token=RunCancellationToken(composition.clock, None),
        )
        assert replay == [invocation.result_item]
        if outcome == "success":
            [artifact] = artifacts
            assert artifact.origin == ArtifactOrigin.MODEL_OUTPUT.value
            assert artifact.trust is TrustLevel.EXTERNAL_UNTRUSTED
            assert artifact.expires_at is None
            assert artifact.size_bytes == len(data)
            assert [part["artifact_id"] for part in files] == [str(artifact.id)]
            assert files[0]["media_type"] == media_type
            assert invocation.status is ToolInvocationStatus.SUCCEEDED
            assert invocation.effect_sent_at is not None
        else:
            assert artifacts == []
            assert files == []
            assert invocation.status is not ToolInvocationStatus.SUCCEEDED
            if outcome in {"timeout", "deadline", "partial"}:
                assert invocation.outcome is not None
                assert invocation.outcome.retryable is False
                assert "charge" in invocation.outcome.message
    assert len(seen) == (1 if outcome in {"success", "timeout", "deadline", "partial"} else 0)
