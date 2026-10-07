"""Finite recipe execution through the normal, individually governed tool pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agent_core.domain.browser import (
    BrowserCondition,
    BrowserObservation,
    BrowserProfileStatus,
    browser_origin,
)
from agent_core.domain.browser_workflows import BrowserWorkflowCatalog, BrowserWorkflowRecipe
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import TextPart, ToolCallItem, ToolResultItem, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import BudgetScope, RunOutcome, Step
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY
from agent_core.runtime.loop import (
    RunContext,
    apply_tool_evidence,
    checkpoint,
    recover_browser_failure,
)


def _intent(value: str) -> str:
    return " ".join(value.casefold().split()).rstrip(".!?")


def _definition(recipe: BrowserWorkflowRecipe) -> str:
    return hashlib.sha256(recipe.model_dump_json().encode()).hexdigest()


def _condition(condition: BrowserCondition) -> dict[str, Any]:
    value = condition.model_dump(mode="json", exclude_none=True)
    for evidence in (value.get("evidence"), value.get("failure_evidence")):
        if isinstance(evidence, dict):
            for field in evidence.get("fields", []):
                field.pop("required", None)
    return value


def _observation(result: ToolResultItem | None) -> BrowserObservation | None:
    if result is None or result.is_error:
        return None
    for part in result.content[:1]:
        if isinstance(part, TextPart):
            try:
                return BrowserObservation.model_validate_json(part.text)
            except ValueError:
                pass
    return None


class BrowserWorkflowRunner:
    def __init__(self, catalog: BrowserWorkflowCatalog, *, policy_version: str) -> None:
        self._catalog = catalog
        self._policy_version = policy_version

    async def __call__(self, context: RunContext) -> RunOutcome | None:
        state = context.checkpoint.working_state.get("browser_workflow")
        if not self._catalog.recipes and state is None:
            return None
        async with context.uow_factory() as uow:
            session = await uow.sessions.get(context.run.session_id, context.principal)
            selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
            if not isinstance(selected, str):
                if state is not None:
                    raise ConflictError("the browser workflow profile is no longer selected")
                return None
            profile = await uow.browser_profiles.get(UUID(selected), context.principal)
        if state is None:
            latest_input = next(
                (
                    item
                    for item in reversed(context.checkpoint.conversation)
                    if isinstance(item, UserMessage)
                ),
                None,
            )
            if latest_input is None or latest_input.trust is not TrustLevel.USER:
                return None
            inputs = [part.text for part in latest_input.content if isinstance(part, TextPart)]
            candidates = [
                recipe
                for recipe in self._catalog.recipes
                if recipe.origin in profile.allowed_origins
                and any(
                    _intent(text) in {_intent(intent) for intent in recipe.intents}
                    for text in inputs[-1:]
                )
            ]
            if len(candidates) != 1 or profile.status is not BrowserProfileStatus.READY:
                return None
            recipe = candidates[0]
            state = {
                "id": recipe.id,
                "definition": _definition(recipe),
                "profile_id": str(profile.id),
                "generation": profile.generation,
                "agent_version": context.agent.version,
                "policy_version": self._policy_version,
                "completed_steps": 0,
                "calls": 0,
                "phase": "start",
                "call_id": None,
                "status": "running",
                "expires_at": (
                    context.clock.now() + timedelta(seconds=recipe.maximum_seconds)
                ).isoformat(),
            }
            context.checkpoint.working_state["browser_workflow"] = state
        bound_recipe = next(
            (entry for entry in self._catalog.recipes if entry.id == state["id"]), None
        )
        if bound_recipe is None:
            raise ConflictError("the reviewed browser workflow definition is unavailable")
        recipe = bound_recipe
        if (
            recipe is None
            or _definition(recipe) != state["definition"]
            or str(profile.id) != state["profile_id"]
            or context.agent.version != state["agent_version"]
            or self._policy_version != state["policy_version"]
        ):
            raise ConflictError("the reviewed browser workflow binding changed")
        if state["status"] != "running":
            return None
        if profile.status is not BrowserProfileStatus.READY:
            raise ConflictError("the browser workflow profile is not ready")
        if profile.generation != state["generation"]:
            if any(
                call.get("name") == "browser.act" for call in context.checkpoint.pending_tool_calls
            ):
                raise ConflictError("browser workflow action belongs to an old profile generation")
            state["generation"] = profile.generation
            # An interrupted write is reconciled below. Only pre-action reads can restart.
            if state["phase"] not in {"action", "verify"}:
                state.update(phase="start", call_id=None)
        while state["completed_steps"] < len(recipe.steps):
            context.token.raise_if_cancelled()
            if (
                context.clock.now() >= datetime.fromisoformat(state["expires_at"])
                or state["calls"] >= 64
                or context.run.step_count >= context.run.limits.max_steps - 1
                or context.run.tool_call_count >= context.run.limits.max_tool_calls
            ):
                if context.checkpoint.pending_tool_calls:
                    # Returning normally would let the generic executor resume
                    # this call after the recipe's own admission window closed.
                    raise ConflictError("the pending browser workflow exceeded its bounds")
                await self._stop(context, state, "bounded_stop")
                return None
            if context.checkpoint.pending_tool_calls:
                # Includes a pending approved call or the fresh read queued by verified sign-in.
                await self._dispatch_pending(context, state)
            step = recipe.steps[state["completed_steps"]]
            result = next(
                (
                    item
                    for item in reversed(context.checkpoint.conversation)
                    if isinstance(item, ToolResultItem) and item.call_id == state["call_id"]
                ),
                None,
            )
            observed = _observation(result)
            if result is not None:
                if state["phase"] == "action":
                    if (
                        observed is None
                        or observed.condition is None
                        or observed.condition.status != "satisfied"
                    ):
                        # Once dispatched, missing/failed/uncertain evidence leads only to a read.
                        assert step.postcondition is not None
                        await self._queue(
                            context,
                            state,
                            "verify",
                            "browser.observe",
                            {"wait_for": _condition(step.postcondition)},
                        )
                        continue
                elif observed is None:
                    await self._stop(context, state, "unverified")
                    return None
                if (
                    observed is None
                    or observed.interruption is not None
                    or browser_origin(observed.url) != recipe.origin
                ):
                    await self._stop(context, state, "unverified")
                    return None
                if state["phase"] == "before_action":
                    targets = [
                        element
                        for element in observed.elements
                        if element.role == step.role
                        and element.name == step.name
                        and not element.disabled
                    ]
                    if len(targets) != 1:
                        await self._stop(context, state, "ambiguous_target")
                        return None
                    assert step.postcondition is not None
                    await self._queue(
                        context,
                        state,
                        "action",
                        "browser.act",
                        {
                            "kind": step.action,
                            "expected_revision": observed.revision,
                            "ref": targets[0].ref,
                            **step.action_values(),
                            "postcondition": _condition(step.postcondition),
                        },
                    )
                    continue
                if observed.condition is not None and observed.condition.status != "satisfied":
                    await self._stop(context, state, "unverified")
                    return None
                if step.postcondition is not None and observed.condition is None:
                    await self._stop(context, state, "unverified")
                    return None
                if step.kind == "extract" and (
                    observed.extraction is None
                    or observed.extraction.status != "extracted"
                    or any(not row.schema_valid for row in observed.extraction.rows)
                ):
                    await self._stop(context, state, "unverified")
                    return None
                state["completed_steps"] += 1
                state.update(phase="start", call_id=None)
                await checkpoint(context, "browser_workflow_progress")
                continue
            if step.kind == "navigate":
                await self._queue(context, state, "read", "browser.navigate", {"url": step.url})
            elif step.kind == "act":
                await self._queue(context, state, "before_action", "browser.observe", {})
            elif step.kind == "observe":
                args = (
                    {}
                    if step.postcondition is None
                    else {"wait_for": _condition(step.postcondition)}
                )
                await self._queue(context, state, "read", "browser.observe", args)
            else:
                previous = next(
                    (
                        _observation(item)
                        for item in reversed(context.checkpoint.conversation)
                        if isinstance(item, ToolResultItem) and _observation(item) is not None
                    ),
                    None,
                )
                if previous is None:
                    await self._stop(context, state, "unverified")
                    return None
                await self._queue(
                    context,
                    state,
                    "read",
                    "browser.observe",
                    {
                        "extract": {
                            "expected_revision": previous.revision,
                            "kind": step.collection_kind,
                            "index": step.index,
                            "fields": [field.model_dump() for field in step.fields],
                            "row_limit": step.row_limit,
                        }
                    },
                )
        await self._stop(context, state, "verified")
        return None

    async def _queue(
        self,
        context: RunContext,
        state: dict[str, Any],
        phase: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        available = set(context.context_plan.tool_names) | set(
            context.context_plan.deferred_tool_names
        )
        if name not in available:
            raise ConflictError("a reviewed browser workflow tool is unavailable")
        context.budgets.check(context.run, BudgetScope.TOOL_CALL)
        context.budgets.check(context.run, BudgetScope.STEP)
        context.run.step_count += 1
        context.run.updated_at = context.clock.now()
        async with context.uow_factory() as uow:
            await uow.runs.update_counters(context.run, lease=context.lease)
        call = ToolCallItem(
            call_id=f"browser-workflow:{context.ids.new_id()}",
            item_index=0,
            name=name,
            arguments=arguments,
            raw_arguments=json.dumps(arguments),
        )
        state.update(phase=phase, call_id=call.call_id)
        context.checkpoint.conversation.append(call)
        context.checkpoint.pending_tool_calls = [call.model_dump(mode="json")]
        await checkpoint(context, "browser_workflow")
        await self._dispatch_pending(context, state)

    async def _dispatch_pending(self, context: RunContext, state: dict[str, Any]) -> None:
        context.token.raise_if_cancelled()
        context.budgets.check(context.run, BudgetScope.TOOL_CALL)
        calls = [
            ToolCallItem.model_validate(call) for call in context.checkpoint.pending_tool_calls
        ]
        if len(calls) != 1 or calls[0].name not in {
            "browser.navigate",
            "browser.observe",
            "browser.act",
        }:
            raise ConflictError("browser workflow has unexpected pending work")
        context.checkpoint.pending_approval_ids = []
        step = Step(
            run_id=context.run.id,
            step_number=max(1, context.run.step_count),
            started_at=context.clock.now(),
        )
        results = await context.dispatch_tools(
            run=context.run,
            checkpoint=context.checkpoint,
            tool_calls=calls,
            principal=context.principal,
            step=step,
            agent=context.agent,
            token=context.token,
            lease=context.lease,
        )
        step.tool_call_count = len(results)
        baseline = context.checkpoint.budget_state.get("tool_call_count", 0)
        if not isinstance(baseline, int):
            raise ConflictError("checkpoint tool-usage watermark is malformed")
        recorded = context.run.tool_call_count - baseline
        if recorded == 0:
            await context.budgets.record_tool_usage(context.run, len(results), step=step)
        elif recorded != len(results):
            raise ConflictError("persisted tool usage does not match the pending workflow call")
        state["calls"] += len(results)
        context.checkpoint.conversation.extend(results)
        apply_tool_evidence(context.checkpoint.working_state, calls)
        context.checkpoint.pending_tool_calls = []
        await recover_browser_failure(context, step, calls, results)
        await checkpoint(context, "browser_workflow_progress")

    async def _stop(self, context: RunContext, state: dict[str, Any], status: str) -> None:
        state["status"] = status
        context.checkpoint.working_state["browser_workflow_report_only"] = True
        await checkpoint(context, "browser_workflow_progress")
