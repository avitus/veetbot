"""Transfer one approved image from this conversation to the current website."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.browser import BrowserProviderError
from agent_core.domain.browser_act_views import describe_browser_action
from agent_core.domain.browser_task_grants import TaskGrantNotCovered
from agent_core.domain.browser_upload import BrowserImageFile, BrowserUploadArguments
from agent_core.domain.media import MediaInputError
from agent_core.domain.policies import (
    AuthorizationTurn,
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.browser import (
    BrowserProvider,
    bind_browser_execution,
    browser_snapshot_in_session,
)
from agent_core.ports.browser_upload import BrowserImageUploader, upload_browser_image
from agent_core.ports.dispatch import CancellationToken
from agent_core.ports.media import MediaInputResolver
from agent_core.tools.browser_results import OUTPUT_SCHEMA, browser_failure, observation_result


class BrowserUploadTool:
    spec = ToolSpec(
        name="browser.upload",
        version="1.0.0",
        description=(
            "Upload one chat image artifact (PNG/JPEG/WebP, <=5 MiB) to a current revision/ref "
            "file input or chooser control. Requires individual approval; selection may transfer "
            "bytes immediately. Returns the updated page without clicking Post. Never repeat an "
            "uncertain upload."
        ),
        input_schema=BrowserUploadArguments.model_json_schema(),
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"artifact.read"},
        timeout_seconds=45,
        maximum_output_bytes=512 * 1024,
        allow_parallel=False,
        target_kind="browser_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, provider: BrowserProvider, *, image_resolver: MediaInputResolver) -> None:
        self._provider = provider
        self._images = image_resolver

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        *,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
        not_covered: TaskGrantNotCovered | None,
    ) -> ApprovalPresentation:
        del principal, turn, not_covered
        request = BrowserUploadArguments.model_validate(arguments)
        snapshot = await browser_snapshot_in_session(self._provider, run.session_id)
        view = describe_browser_action(request.action(), snapshot)
        shown = {
            **view.arguments,
            "view": "browser.upload.v1",
            "kind": "upload",
            "image_id": request.image_id,
            "consequence": "file_transfer",
        }
        destination = str(shown.get("page_origin", "the current website"))
        return ApprovalPresentation(
            summary=f"Upload one image to {destination}; the website may send it immediately.",
            arguments=shown,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if "artifact.read" not in context.principal.scopes:
            return browser_failure(
                ToolFailureKind.PERMISSION, "policy.scope.missing", retryable=False
            )
        if context.dispatch_constraint is not None:
            return browser_failure(
                ToolFailureKind.PERMISSION, "tool.browser.grant_not_applicable", retryable=False
            )
        if not isinstance(self._provider, BrowserImageUploader):
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.browser.action_not_allowed",
                retryable=False,
            )
        try:
            request = BrowserUploadArguments.model_validate(arguments)
        except ValidationError:
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS, "tool.arguments_invalid", retryable=False
            )
        try:
            cancellation = cast(CancellationToken, context.cancellation)
            cancellation.raise_if_cancelled()
            images = await self._images.resolve(
                [UUID(request.image_id)],
                run_id=context.run_id,
                principal=context.principal,
                video=False,
            )
            if len(images) != 1:
                raise MediaInputError("invalid")
            image = BrowserImageFile.for_artifact(request.image_id, images[0])
            await bind_browser_execution(self._provider, context)
            cancellation.raise_if_cancelled()
            await context.mark_effect_sent()
            observation = await upload_browser_image(self._provider, request.action(), image)
        except MediaInputError as error:
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                f"tool.browser.image_{error.code}",
                retryable=False,
            )
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_result(self._provider, observation, self.spec.maximum_output_bytes)
