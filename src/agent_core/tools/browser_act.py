"""Perform one approved, revision-bound browser interaction."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.browser import (
    BROWSER_AUTH_INTERRUPTION_MARKER,
    BrowserAction,
    BrowserCondition,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.browser_act_views import (
    BROWSER_ACT_VIEW,
    UNDESCRIBED_SUMMARY,
    TaskGrantSessionContext,
    describe_browser_action,
    session_is_task_grant_eligible,
    task_grant_offer,
)
from agent_core.domain.browser_download import EXPORT_TIMEOUT_GUIDANCE, BrowserDownload
from agent_core.domain.browser_extraction import BrowserExtractionScope
from agent_core.domain.browser_follow import profile_handle
from agent_core.domain.browser_request_grants import request_grant_offer
from agent_core.domain.browser_task_grants import TaskGrantNotCovered
from agent_core.domain.captures import (
    CAPTURE_LITERAL_KEY,
    CAPTURE_METADATA_KEY,
    CAPTURE_READER_TOOL_NAME,
    CaptureSections,
    capture_text,
    reader_call,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    AuthorizationTurn,
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.artifacts import ArtifactWriter
from agent_core.ports.browser import (
    GRANT_NOT_APPLICABLE,
    BrowserProvider,
    bind_browser_execution,
    browser_snapshot_in_session,
)
from agent_core.tools.browser_conditions import browser_condition_schema, check_browser_condition
from agent_core.tools.browser_results import (
    ACT_OUTPUT_SCHEMA,
    OUTPUT_SCHEMA,
    SHADOW_ACT_OUTPUT_SCHEMA,
    browser_failure,
    observation_result,
    serialized_observation,
)
from agent_core.tools.tool_call import TOOL_CALL_TOOL_NAME

logger = logging.getLogger(__name__)

_CONDITION_SCHEMA = browser_condition_schema()
_CONDITION_DEFINITIONS = _CONDITION_SCHEMA.pop("$defs")
# Room in a result for the saved-file note, whatever the page's size.
_SAVED_FILE_NOTE_BYTES = 1024
# Storing a file gives up this long before the tool's limit, to report the click.
_RESULT_RESERVE_SECONDS = 1.0

INPUT_SCHEMA: dict[str, Any] = {
    "$defs": _CONDITION_DEFINITIONS,
    "type": "object",
    "properties": {
        "postcondition": _CONDITION_SCHEMA,
        "kind": {
            "type": "string",
            "enum": ["click", "type", "select", "check", "press", "scroll"],
        },
        "expected_revision": {"type": "string", "minLength": 1, "maxLength": 128},
        "ref": {"type": "string", "minLength": 1, "maxLength": 128},
        "value": {"type": "string", "maxLength": 4096},
        "key": {
            "type": "string",
            "enum": [
                "Enter",
                "Escape",
                "Tab",
                "Space",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
            ],
        },
        "delta_y": {"type": "integer", "minimum": -2000, "maximum": 2000},
    },
    "required": ["kind", "expected_revision", "ref"],
    "additionalProperties": False,
}


TaskGrantContextReader = Callable[[UUID, Principal], Awaitable[TaskGrantSessionContext]]


class BrowserActApprovalPresenter:
    """Describe a pending browser action and, when every rule holds, offer a
    task grant (ADR-0129 section 6). Without the flag it never offers."""

    def __init__(
        self,
        provider: BrowserProvider,
        *,
        context_reader: TaskGrantContextReader | None = None,
        enabled: bool = False,
        request_enabled: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._context_reader = context_reader
        self._enabled = enabled
        self._request_enabled = request_enabled
        self._now = now

    async def present(
        self,
        arguments: dict[str, Any],
        *,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
        not_covered: TaskGrantNotCovered | None,
    ) -> ApprovalPresentation:
        del not_covered
        try:
            action = BrowserAction.model_validate(arguments)
        except ValidationError:
            return ApprovalPresentation(
                summary=UNDESCRIBED_SUMMARY,
                arguments={"view": BROWSER_ACT_VIEW, "described": False},
            )
        snapshot = await browser_snapshot_in_session(self._provider, run.session_id)
        view = describe_browser_action(action, snapshot)
        offer = None
        request_offer = None
        context = None
        if (
            (self._enabled or self._request_enabled)
            and view.described
            and self._context_reader is not None
        ):
            context = await self._context_reader(run.session_id, principal)
        if self._request_enabled and context is not None:
            # ADR-0302: one approval for the rest of the request.
            request_offer = request_grant_offer(
                view=view,
                action=action,
                run=run,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                turn=turn,
                context=context,
            )
        if self._enabled and context is not None:
            session = context.session
            eligible = session is not None and session_is_task_grant_eligible(
                run,
                session_tenant_id=session.tenant_id,
                session_principal_id=session.principal_id,
                session_metadata=session.metadata,
                owner_run_id=context.owner_run_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                turn=turn,
            )
            offer = task_grant_offer(
                enabled=True,
                view=view,
                action=action,
                scopes=context.scopes,
                eligible=eligible,
                turn=turn,
                profile=context.profile,
                active_grant=context.active_grant,
            )
        return ApprovalPresentation(
            summary=view.summary,
            arguments=view.arguments,
            task_grant_offer=offer,
            request_grant_offer=request_offer,
        )


class BrowserActTool:
    _row_scope: BrowserExtractionScope = "main_document"
    spec = ToolSpec(
        name="browser.act",
        version="1.8.0",
        description=(
            "Act once with approval: current revision+elements[].ref, including scroll; "
            "regions[].ref is read-only. Returns settled page/fresh refs. "
            "postcondition=observe wait_for. Refresh missing targets for overlays. "
            "Never replay uncertain writes; success is not causal proof."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=ACT_OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_seconds=30,
        maximum_output_bytes=512 * 1024,
        allow_parallel=False,
        target_kind="browser_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    _reports_downloads = True

    def __init__(
        self, provider: BrowserProvider, *, presenter: BrowserActApprovalPresenter | None = None
    ) -> None:
        self._provider = provider
        self._presenter = presenter or BrowserActApprovalPresenter(provider)

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        *,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
        not_covered: TaskGrantNotCovered | None,
    ) -> ApprovalPresentation:
        return await self._presenter.present(
            arguments, run=run, principal=principal, turn=turn, not_covered=not_covered
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Dispatch one action, check its postcondition, and report any file it saved."""

        call_deadline = context.monotonic_deadline
        if call_deadline is None:
            call_deadline = asyncio.get_running_loop().time() + context.timeout_seconds
        store_deadline = call_deadline - _RESULT_RESERVE_SECONDS
        try:
            if set(arguments) - set(self.spec.input_schema["properties"]):
                raise ValueError("unknown action fields")
            condition = (
                BrowserCondition.model_validate(arguments["postcondition"])
                if "postcondition" in arguments
                else None
            )
            action = BrowserAction.model_validate(
                {key: value for key, value in arguments.items() if key != "postcondition"}
            )
        except (ValidationError, ValueError):
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            # ADR-0129: a grant's constraint is checked before the watermark,
            # so a refusal never counts as a sent effect.
            constraint = _checked_constraint(self._provider, context.dispatch_constraint)
            await bind_browser_execution(self._provider, context)
            snapshot = await browser_snapshot_in_session(self._provider, context.session_id)
            if (
                snapshot is not None
                and snapshot.observation.revision == action.expected_revision
                and any(region.ref == action.ref for region in snapshot.observation.regions)
            ):
                return browser_failure(
                    ToolFailureKind.INVALID_ARGUMENTS,
                    "tool.browser.region_not_actionable",
                    retryable=False,
                )
            await context.mark_effect_sent()
            observation = await _act(self._provider, action, constraint)
        except BrowserProviderError as error:
            return browser_failure(error)
        # A postcondition read returns a new observation, which has no file.
        download = observation.download if self._reports_downloads else None
        if observation.interruption is not None:
            interrupted = browser_failure(
                BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
            )
            assert interrupted.failure is not None
            interrupted.failure.external_text = BROWSER_AUTH_INTERRUPTION_MARKER
            return interrupted
        if constraint is not None and constraint.grant_kind == "follow":
            condition = BrowserCondition(
                role="button", name=f"Following @{constraint.follow_handle}"
            )
        if condition is not None:
            try:
                observation = await check_browser_condition(
                    self._provider, observation, condition, row_scope=self._row_scope
                )
            except Exception as error:
                # The action already returned; evidence loss cannot prove it was unsent.
                uncertain = browser_failure(
                    BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
                )
                if isinstance(error, BrowserProviderError) and error.reason_code in {
                    "tool.browser.needs_user",
                    "tool.browser.authentication_required",
                }:
                    # Preserve the trusted sign-in signal for the existing
                    # suspension path without making the sent action retryable.
                    assert uncertain.failure is not None
                    uncertain.failure.external_text = BROWSER_AUTH_INTERRUPTION_MARKER
                return uncertain
        if (
            constraint is not None
            and constraint.grant_kind == "follow"
            and (
                observation.condition is None
                or observation.condition.status != "satisfied"
                or profile_handle(observation.url) != constraint.follow_handle
            )
        ):
            return browser_failure(
                BrowserProviderError("tool.browser.outcome_unknown", retryable=False)
            )
        result = observation_result(
            self._provider,
            observation,
            self.spec.maximum_output_bytes - (0 if download is None else _SAVED_FILE_NOTE_BYTES),
        )
        if download is None or not result.ok or result.structured is None:
            return result
        saved_file = await self._saved_file(download, context, store_deadline)
        structured = {**result.structured, "saved_file": saved_file}
        content = [TextPart(text=serialized_observation(structured))]
        if download.reason == "timed_out":
            content.append(TextPart(text=EXPORT_TIMEOUT_GUIDANCE))
        return result.model_copy(
            update={
                "content": content,
                "structured": structured,
            }
        )

    async def _saved_file(
        self, download: BrowserDownload, context: ToolExecutionContext, deadline: float
    ) -> dict[str, Any]:
        """Store an admitted file as a capture of this chat and describe it to the model."""

        if download.status == "refused":
            return {"status": "refused", "reason": download.reason}
        reachable = {CAPTURE_READER_TOOL_NAME, TOOL_CALL_TOOL_NAME} <= context.available_tools
        create = getattr(context.artifacts, "create", None)
        if not reachable or create is None or download.filename is None:
            return {"status": "refused", "reason": "reader_unavailable"}
        data = download.data()
        media_type = str(download.media_type)
        writer = cast(ArtifactWriter, context.artifacts)
        try:
            async with asyncio.timeout_at(deadline):
                ref = await writer.create(
                    _single(data),
                    download.filename,
                    media_type,
                    TrustLevel.EXTERNAL_UNTRUSTED,
                    metadata={
                        CAPTURE_METADATA_KEY: {
                            "tool": self.spec.name,
                            "required_scopes": sorted(self.spec.required_scopes),
                            CAPTURE_LITERAL_KEY: True,
                        }
                    },
                )
        except Exception:
            # The click already ran, so a lost file must not cost it its result.
            logger.exception("browser_download_capture_failed", extra={"size_bytes": len(data)})
            return {"status": "refused", "reason": "failed"}
        sections = CaptureSections(
            capture_text(data, literal=True), artifact_id=ref.artifact_id, tool=self.spec.name
        ).count
        return {
            "status": "saved",
            "artifact": str(ref.artifact_id),
            "filename": download.filename,
            "media_type": media_type,
            "size_bytes": ref.size_bytes,
            "sections": sections,
            "read_with": f"tool.call {reader_call(ref.artifact_id)}",
        }


class ShadowBrowserActTool(BrowserActTool):
    """Verify rows in open roots without changing pinned action behavior."""

    _row_scope = "main_document_and_open_shadow"
    spec = BrowserActTool.spec.model_copy(
        update={
            "version": "1.9.0",
            "output_schema": SHADOW_ACT_OUTPUT_SCHEMA,
            "description": (
                "Act once with approval: current revision+elements[].ref, including scroll; "
                "regions[].ref is read-only. Returns settled page/fresh refs. "
                "postcondition=observe wait_for; rows include open roots. "
                "Refresh missing targets for overlays. Never replay uncertain writes; "
                "success is not causal proof."
            ),
        }
    )


class PinnedBrowserActTool(BrowserActTool):
    """The published 1.7.0 spec, for a chat pinned to it; it reports no file (ADR-0294)."""

    spec = BrowserActTool.spec.model_copy(
        update={"version": "1.7.0", "output_schema": OUTPUT_SCHEMA}
    )
    _reports_downloads = False


async def _single(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _checked_constraint(
    provider: BrowserProvider, constraint: BrowserDispatchConstraint | None
) -> BrowserDispatchConstraint | None:
    """Revalidate a grant's constraint before anything is marked as sent.

    One that fails its validator, or a provider whose runtime cannot recheck
    it against the live page, is refused before dispatch (ADR-0129).
    """

    if constraint is None:
        return None
    try:
        checked = BrowserDispatchConstraint.model_validate(constraint.model_dump())
    except (ValidationError, ValueError, TypeError) as exc:
        raise BrowserProviderError(GRANT_NOT_APPLICABLE, retryable=False) from exc
    if not _rechecks_constraints(provider):
        raise BrowserProviderError(GRANT_NOT_APPLICABLE, retryable=False)
    return checked


def _rechecks_constraints(provider: BrowserProvider) -> bool:
    try:
        parameter = inspect.signature(provider.act).parameters.get("constraint")
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind is inspect.Parameter.KEYWORD_ONLY


async def _act(
    provider: BrowserProvider,
    action: BrowserAction,
    constraint: BrowserDispatchConstraint | None,
) -> BrowserObservation:
    """Dispatch one action; an approved action carries no constraint."""

    if constraint is None:
        return await provider.act(action)
    return await provider.act(action, constraint=constraint)
