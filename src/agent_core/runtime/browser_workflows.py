"""Finite recipe execution through the normal, individually governed tool pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agent_core.domain.browser import (
    BrowserCondition,
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserProfileStatus,
    browser_origin,
)
from agent_core.domain.browser_evidence import browser_condition_status
from agent_core.domain.browser_workflows import BrowserWorkflowCatalog, BrowserWorkflowRecipe
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolValidationError,
    UserInputRequiredError,
)
from agent_core.domain.messages import TextPart, ToolCallItem, ToolResultItem, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import BudgetScope, RunOutcome, Step
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY
from agent_core.domain.tools import ToolOutcome, ToolSpec
from agent_core.runtime.loop import (
    RunContext,
    apply_tool_evidence,
    checkpoint,
    recover_browser_failure,
    with_event_sequences,
)

type ArgumentValidator = Callable[[dict[str, Any], dict[str, Any]], object]


def _intent(value: str) -> str:
    return " ".join(value.casefold().split()).rstrip(".!?")


def _definition(recipe: BrowserWorkflowRecipe) -> str:
    # Optional defaults must not change legacy recipe/checkpoint identity.
    defaults = {
        "branches": None,
        "target_region": None,
        "precondition": None,
        "minimum_rows": 0,
        "require_complete": False,
        "recheck_destination": False,
    }
    excluded = {
        "steps": {
            index: {field for field, default in defaults.items() if getattr(step, field) == default}
            for index, step in enumerate(recipe.steps)
        }
    }
    return hashlib.sha256(recipe.model_dump_json(exclude=excluded).encode()).hexdigest()


def _condition(condition: BrowserCondition) -> dict[str, Any]:
    value = condition.model_dump(mode="json", exclude_none=True)
    for evidence in (value.get("evidence"), value.get("failure_evidence")):
        if isinstance(evidence, dict):
            for field in evidence.get("fields", []):
                field.pop("required", None)
    return value


def _supports_recipe(
    recipe: BrowserWorkflowRecipe,
    available: set[str],
    specs: tuple[ToolSpec, ...],
    *,
    validate_arguments: ArgumentValidator,
) -> bool:
    """Check pinned argument shapes only; placeholders never reach a provider."""
    schemas = {spec.name: spec.input_schema for spec in specs}
    placeholder = "0" * 32
    calls: list[tuple[str, dict[str, Any]]] = []
    for step in recipe.steps:
        if step.kind == "navigate":
            calls.append(("browser.navigate", {"url": step.url}))
            if step.postcondition is not None:
                calls.append(("browser.observe", {"wait_for": _condition(step.postcondition)}))
            continue
        if step.kind == "observe":
            calls.append(
                (
                    "browser.observe",
                    {}
                    if step.postcondition is None
                    else {"wait_for": _condition(step.postcondition)},
                )
            )
            continue
        calls.append(("browser.observe", {}))
        if step.kind == "act":
            assert step.postcondition is not None
            if step.precondition is not None:
                calls.append(("browser.observe", {"wait_for": _condition(step.precondition)}))
            if step.target_region is not None:
                calls.append(
                    (
                        "browser.observe",
                        {"region_ref": placeholder, "expected_revision": placeholder},
                    )
                )
            calls.extend(
                [
                    (
                        "browser.act",
                        {
                            "kind": step.action,
                            "expected_revision": placeholder,
                            "ref": placeholder,
                            **step.action_values(),
                            "postcondition": _condition(step.postcondition),
                        },
                    ),
                    ("browser.observe", {"wait_for": _condition(step.postcondition)}),
                ]
            )
        elif step.kind == "extract":
            calls.append(
                (
                    "browser.observe",
                    {
                        "extract": {
                            "expected_revision": placeholder,
                            "kind": step.collection_kind,
                            "index": step.index,
                            "fields": [field.model_dump() for field in step.fields],
                            "row_limit": step.row_limit,
                        }
                    },
                )
            )
        elif step.kind == "collect_feed":
            from agent_core.runtime.browser_feed import FIELDS, HOME

            calls.extend(
                [
                    (
                        "browser.observe",
                        {
                            "extract": {
                                "expected_revision": placeholder,
                                "kind": "articles",
                                "fields": FIELDS,
                                "row_limit": 3,
                            }
                        },
                    ),
                    (
                        "browser.act",
                        {
                            "kind": "scroll",
                            "expected_revision": placeholder,
                            "ref": placeholder,
                            "delta_y": 1557,
                            "postcondition": {
                                "evidence": {"kind": "location", "url": HOME},
                                "timeout_ms": 0,
                            },
                        },
                    ),
                ]
            )
    for name, arguments in calls:
        if name not in available or name not in schemas:
            return False
        try:
            validate_arguments(arguments, schemas[name])
        except ToolValidationError:
            return False
    return True


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


def _destination(
    recipe: BrowserWorkflowRecipe, state: dict[str, Any], index: int
) -> dict[str, Any] | None:
    """The wait an action directly after a verified navigation repeats on its target read."""
    previous = recipe.steps[index - 1] if index else None
    branch = state.get("last_branch")
    if (
        previous is None
        or previous.kind != "navigate"
        or previous.postcondition is None
        # A branch that jumped here skipped that navigation: nothing was verified.
        or (isinstance(branch, dict) and branch.get("next_step") == index)
    ):
        return None
    return {"wait_for": _condition(previous.postcondition)}


MAXIMUM_NAVIGATION_RETRIES = 2


def _retryable_navigation(result: ToolResultItem | None) -> bool:
    """A navigation that only ran out of time read nothing and can repeat.

    Exhausted startup recovery (``provider_unavailable``) stays final: its three
    acquisition attempts are the whole allowance.
    """
    if result is None or not result.is_error:
        return False
    for part in result.content[:1]:
        if isinstance(part, TextPart):
            try:
                outcome = ToolOutcome.model_validate_json(part.text)
            except ValueError:
                return False
            return outcome.retryable and outcome.reason_code == "tool.timeout"
    return False


def _control_continuation(
    result: ToolResultItem, observation: BrowserObservation
) -> dict[str, str] | None:
    """Copy only a current, advertised control continuation, never a guessed cursor."""
    if result.is_error or observation.coverage is None:
        return None
    if not result.content or not isinstance(result.content[0], TextPart):
        return None
    try:
        payload = json.loads(result.content[0].text)
        if not isinstance(payload, dict):
            return None
        request = BrowserObservationExpansion.model_validate(payload.get("next_observe"))
    except ValueError:
        return None
    if request.after is not None and any(
        element.ref == request.after for element in observation.elements
    ):
        return {"after": request.after}
    if request.cursor is not None and request.cursor == observation.coverage.next_cursor:
        return {"cursor": request.cursor}
    return None


class BrowserWorkflowRunner:
    def __init__(
        self,
        catalog: BrowserWorkflowCatalog,
        *,
        policy_version: str,
        validate_arguments: ArgumentValidator,
    ) -> None:
        self._catalog = catalog
        self._policy_version = policy_version
        self._validate_arguments = validate_arguments

    async def __call__(self, context: RunContext) -> RunOutcome | None:
        state = context.checkpoint.working_state.get("browser_workflow")
        if isinstance(state, dict) and state.get("status") == "steered":
            # The general loop owns this turn now, including subsequent approval resumes.
            return None
        if not self._catalog.recipes and state is None:
            return None
        async with context.uow_factory() as uow:
            session = await uow.sessions.get(context.run.session_id, context.principal)
            selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
            if not isinstance(selected, str):
                if state is not None:
                    raise ConflictError("the browser workflow profile is no longer selected")
                return None
            try:
                profile = await uow.browser_profiles.get(UUID(selected), context.principal)
            except NotFoundError:
                # Deleting a login leaves its chats bound to it; such a chat has no website.
                if state is not None:
                    raise ConflictError(
                        "the browser workflow profile is no longer selected"
                    ) from None
                return None
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
            text = _intent("\n".join(inputs))
            digest = hashlib.sha256(text.encode()).hexdigest()
            candidates = [
                recipe
                for recipe in self._catalog.recipes
                if recipe.origin in profile.allowed_origins
                and len(inputs) == len(latest_input.content)
                and (
                    text in {_intent(intent) for intent in recipe.intents}
                    or digest in recipe.intent_sha256
                )
            ]
            if len(candidates) != 1 or profile.status is not BrowserProfileStatus.READY:
                return None
            recipe = candidates[0]
            if not _supports_recipe(
                recipe,
                set(context.context_plan.tool_names)
                | set(context.context_plan.deferred_tool_names),
                context.context_plan.pinned_tool_specs,
                validate_arguments=self._validate_arguments,
            ):
                return None
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
                "report": (
                    "x_home_briefing"
                    if any(step.kind == "collect_feed" for step in recipe.steps)
                    else None
                ),
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
        suspended_at = state.pop("suspended_at", None)
        if isinstance(suspended_at, str):
            # The ceiling bounds the workflow's own activity, not the owner's sign-in:
            # the run deadline still limits the whole run.
            paused = context.clock.now() - datetime.fromisoformat(suspended_at)
            if paused > timedelta(0):
                state["expires_at"] = (
                    datetime.fromisoformat(state["expires_at"]) + paused
                ).isoformat()
        if profile.status is not BrowserProfileStatus.READY:
            raise ConflictError("the browser workflow profile is not ready")
        if profile.generation != state["generation"]:
            if any(
                call.get("name") == "browser.act" for call in context.checkpoint.pending_tool_calls
            ):
                raise ConflictError("browser workflow action belongs to an old profile generation")
            state["generation"] = profile.generation
            if state.get("report") == "x_home_briefing":
                uncertain = state["phase"] == "feed_scroll" or state.get("feed", {}).get(
                    "uncertain_scroll", False
                )
                state.update(completed_steps=0, phase="start", call_id=None)
                state["feed"] = {
                    "post_ids": [],
                    "row_digests": [],
                    "evidence_call_ids": [],
                    "windows": 0,
                    "scrolls": state.get("feed", {}).get("scrolls", 0),
                    "retries": state.get("feed", {}).get("retries", 0),
                    "uncertain_scroll": uncertain,
                }
            # An interrupted write is reconciled below. Only pre-action reads can restart.
            if state["phase"] not in {"action", "verify"}:
                index = state.get("next_step", state["completed_steps"])
                pending = context.checkpoint.pending_tool_calls
                if (
                    state["phase"] == "verify_navigation"
                    and index < len(recipe.steps)
                    and recipe.steps[index].kind == "navigate"
                    and len(pending) == 1
                    and pending[0].get("name") == "browser.navigate"
                    and pending[0].get("arguments") == {"url": recipe.steps[index].url}
                ):
                    # Verified sign-in already queued this exact fresh navigation.
                    # Dispatch it normally, then verify it instead of loading twice.
                    state.update(phase="read", call_id=pending[0]["call_id"])
                else:
                    state.update(phase="start", call_id=None)
        while state["status"] == "running" and state.get(
            "next_step", state["completed_steps"]
        ) < len(recipe.steps):
            context.token.raise_if_cancelled()
            bounded = (
                context.clock.now() >= datetime.fromisoformat(state["expires_at"])
                or state["calls"] >= 64
                or context.run.step_count >= context.run.limits.max_steps - 1
                or context.run.tool_call_count >= context.run.limits.max_tool_calls
            )
            if context.checkpoint.pending_tool_calls:
                if bounded:
                    # Returning normally would let the generic executor resume
                    # this call after the recipe's own admission window closed.
                    raise ConflictError("the pending browser workflow exceeded its bounds")
                # Includes a pending approved call or the fresh read queued by verified sign-in.
                await self._dispatch_pending(context, state)
            step_index = state.get("next_step", state["completed_steps"])
            step = recipe.steps[step_index]
            result = next(
                (
                    item
                    for item in reversed(context.checkpoint.conversation)
                    if isinstance(item, ToolResultItem) and item.call_id == state["call_id"]
                ),
                None,
            )
            observed = _observation(result)
            # The bounds admit dispatch and new calls. An observation an admitted call
            # already returned is still read; _queue refuses whatever it would need next.
            if bounded and observed is None:
                await self._stop(context, state, "bounded_stop")
                return None
            destination = _destination(recipe, state, step_index) if step.kind == "act" else None
            if step.kind == "collect_feed":
                from agent_core.runtime.browser_feed import advance_feed

                await advance_feed(
                    context, state, step, result, observed, queue=self._queue, stop=self._stop
                )
                if state["status"] != "running":
                    return None
                continue
            if (
                step.kind == "navigate"
                and state["phase"] == "read"
                and _retryable_navigation(result)
                and state.get("navigation_retries", 0) < MAXIMUM_NAVIGATION_RETRIES
            ):
                # The first call also acquires and verifies the signed-in browser, which
                # can leave the page load too little of the tool's time. The lease then
                # exists, so the same read-only navigation is repeated, inside every bound.
                state["navigation_retries"] = state.get("navigation_retries", 0) + 1
                await context.clock.sleep(1)
                await self._queue(context, state, "read", "browser.navigate", {"url": step.url})
                continue
            if result is not None:
                if state["phase"] == "action":
                    if (
                        observed is None
                        or observed.condition is None
                        or observed.condition.status not in {"satisfied", "failed"}
                    ):
                        # Missing or inconclusive evidence leads only to a read.
                        # Positive success/failure evidence needs no reconciliation.
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
                if (
                    step.kind == "navigate"
                    and state["phase"] == "read"
                    and step.postcondition is not None
                ):
                    # Loading a same-origin page is not proof of the reviewed
                    # destination state. Verify in a separate, persisted read.
                    await self._queue(
                        context,
                        state,
                        "verify_navigation",
                        "browser.observe",
                        {"wait_for": _condition(step.postcondition)},
                    )
                    continue
                if state["phase"] == "before_ready":
                    assert step.precondition is not None
                    if observed.condition is None or observed.condition.status != "satisfied":
                        await self._stop(
                            context,
                            state,
                            "observed_failure"
                            if observed.condition is not None
                            and observed.condition.status == "failed"
                            and step.precondition.failure_evidence is not None
                            else "unverified",
                        )
                        return None
                    # Readiness is not a reusable target or a completed action step.
                    if not await self._read_target_window(
                        context,
                        state,
                        destination or {},
                        phase="before_region"
                        if step.target_region is not None
                        else "before_action",
                    ):
                        return None
                    continue
                if state["phase"] == "before_extract":
                    await self._queue(
                        context,
                        state,
                        "read",
                        "browser.observe",
                        {
                            "extract": {
                                "expected_revision": observed.revision,
                                "kind": step.collection_kind,
                                "index": step.index,
                                "fields": [field.model_dump() for field in step.fields],
                                "row_limit": step.row_limit,
                            }
                        },
                    )
                    continue
                if destination is not None and state["phase"] in {
                    "before_region",
                    "before_action",
                }:
                    asked = next(
                        (
                            item.arguments
                            for item in reversed(context.checkpoint.conversation)
                            if isinstance(item, ToolCallItem) and item.call_id == state["call_id"]
                        ),
                        None,
                    )
                    status = None
                    if asked == destination and observed.condition is not None:
                        status = observed.condition.status
                    elif (
                        step.recheck_destination
                        and asked is not None
                        and any(key in asked for key in ("region_ref", "after", "cursor"))
                    ):
                        # The later capture must prove its own destination. Its
                        # condition field and historical evidence cannot stand in.
                        status = browser_condition_status(
                            observed,
                            BrowserCondition.model_validate(destination["wait_for"]),
                            projected=True,
                        )
                    if status != "satisfied":
                        await self._stop(
                            context,
                            state,
                            "observed_failure"
                            if status == "failed" and "failure_evidence" in destination["wait_for"]
                            else "unverified",
                        )
                        return None
                if state["phase"] == "before_region":
                    assert step.target_region is not None
                    regions = [
                        region
                        for region in observed.regions
                        if region.kind == step.target_region.region_kind
                        and not region.text_truncated
                        and " ".join(region.text.split())
                        == " ".join(step.target_region.text.split())
                    ]
                    if len(regions) != 1:
                        await self._stop(
                            context, state, "ambiguous_region" if regions else "region_not_observed"
                        )
                        return None
                    if destination is not None and not step.recheck_destination:
                        # The region's controls need a focus read, which cannot carry the
                        # destination evidence: as for any region target read without focus.
                        await self._stop(context, state, "unverified")
                        return None
                    if not await self._read_target_window(
                        context,
                        state,
                        {"region_ref": regions[0].ref, "expected_revision": observed.revision},
                    ):
                        return None
                    continue
                if state["phase"] == "before_action":
                    if step.target_region is not None and observed.focus is None:
                        await self._stop(context, state, "unverified")
                        return None
                    targets = [
                        element
                        for element in observed.elements
                        if element.role == step.role
                        and element.name == step.name
                        and not element.disabled
                    ]
                    if not targets:
                        continuation = (
                            None
                            if destination is not None and not step.recheck_destination
                            else _control_continuation(result, observed)
                        )
                        if continuation is None:
                            await self._stop(context, state, "target_not_observed")
                            return None
                        if not await self._read_target_window(context, state, continuation):
                            return None
                        continue
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
                if step.branches is not None:
                    if (
                        state["phase"] != "read"
                        or observed.condition is None
                        or observed.condition.status not in {"satisfied", "failed"}
                    ):
                        await self._stop(context, state, "unverified")
                        return None
                    status = observed.condition.status
                    destination = (
                        step.branches.satisfied if status == "satisfied" else step.branches.failed
                    )
                    state["completed_steps"] += 1
                    state.update(
                        next_step=destination,
                        last_branch={
                            "step": step_index,
                            "status": status,
                            "next_step": destination,
                        },
                        phase="start",
                        call_id=None,
                    )
                    if destination == len(recipe.steps) and status == "failed":
                        await self._stop(context, state, "observed_failure")
                        return None
                    # Persist the decision before any target read, approval or dispatch.
                    await checkpoint(context, "browser_workflow_progress")
                    continue
                if observed.condition is not None and observed.condition.status != "satisfied":
                    terminal_status = (
                        "observed_failure"
                        if observed.condition.status == "failed"
                        and step.postcondition is not None
                        and step.postcondition.failure_evidence is not None
                        else "unverified"
                    )
                    await self._stop(context, state, terminal_status)
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
                if step.kind == "extract":
                    extracted = observed.extraction
                    assert extracted is not None
                    if len(extracted.rows) < step.minimum_rows or (
                        step.require_complete
                        and (
                            extracted.omitted_rows
                            or extracted.source_scan_limit_reached
                            or extracted.row_scan_limit_reached
                            or extracted.byte_limit_reached
                        )
                    ):
                        await self._stop(context, state, "insufficient_evidence")
                        return None
                state["completed_steps"] += 1
                if "next_step" in state:
                    state["next_step"] = step_index + 1
                state.update(phase="start", call_id=None)
                await checkpoint(context, "browser_workflow_progress")
                continue
            if step.kind == "navigate":
                await self._queue(context, state, "read", "browser.navigate", {"url": step.url})
            elif step.kind == "act":
                if step.precondition is not None:
                    await self._queue(
                        context,
                        state,
                        "before_ready",
                        "browser.observe",
                        {"wait_for": _condition(step.precondition)},
                    )
                elif not await self._read_target_window(
                    context,
                    state,
                    destination or {},
                    phase="before_region" if step.target_region is not None else "before_action",
                ):
                    return None
            elif step.kind == "observe":
                args = (
                    {}
                    if step.postcondition is None
                    else {"wait_for": _condition(step.postcondition)}
                )
                await self._queue(context, state, "read", "browser.observe", args)
            else:
                await self._queue(context, state, "before_extract", "browser.observe", {})
        if state["status"] == "running":
            await self._stop(context, state, "verified")
        return None

    async def _yield_to_follow_up(
        self, context: RunContext, state: dict[str, Any], *, verified: bool = False
    ) -> bool:
        if context.checkpoint.pending_tool_calls or (
            not verified
            and (
                state["phase"] in {"action", "verify", "feed_scroll"}
                or state.get("feed", {}).get("uncertain_scroll", False)
            )
        ):
            return False
        async with context.uow_factory() as uow:
            pending = await uow.pending_inputs.list_for_run(context.run.id, context.principal)
        if not pending:
            return False
        # Retire durably before the ordinary loop consumes the inbox. A crash on
        # either side cannot restart the old recipe or lose the held instruction.
        state["status"] = "steered"
        await checkpoint(context, "browser_workflow_progress")
        return True

    async def _read_target_window(
        self,
        context: RunContext,
        state: dict[str, Any],
        arguments: dict[str, Any],
        *,
        phase: str = "before_action",
    ) -> bool:
        step_index = state.get("next_step", state["completed_steps"])
        search = state.get("target_search")
        if search is not None and (
            not isinstance(search, dict)
            or set(search) != {"step", "windows"}
            or type(search["step"]) is not int
            or not 0 <= search["step"] < 16
            or type(search["windows"]) is not int
            or not 0 <= search["windows"] <= 4
        ):
            raise ConflictError("the browser workflow target search is malformed")
        if search is None or search["step"] != step_index:
            # Legacy checkpoints may already contain an initial observation; a
            # destination wait is itself that first window.
            search = {
                "step": step_index,
                "windows": int(bool(arguments) and "wait_for" not in arguments),
            }
            state["target_search"] = search
        if search["windows"] >= 4:
            await self._stop(context, state, "target_not_observed")
            return False
        search["windows"] += 1
        # _queue persists this allowance with the pending read before dispatch.
        await self._queue(context, state, phase, "browser.observe", arguments)
        return bool(state["status"] == "running")

    async def _queue(
        self,
        context: RunContext,
        state: dict[str, Any],
        phase: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        # A delayed retry must recheck admission after sleeping, not only at the loop head.
        context.token.raise_if_cancelled()
        if (
            context.clock.now() >= datetime.fromisoformat(state["expires_at"])
            or state["calls"] >= 64
            or context.run.step_count >= context.run.limits.max_steps - 1
            or context.run.tool_call_count >= context.run.limits.max_tool_calls
        ):
            await self._stop(context, state, "bounded_stop")
            return
        if await self._yield_to_follow_up(context, state):
            return
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
        results = await with_event_sequences(context, results)
        context.checkpoint.conversation.extend(results)
        apply_tool_evidence(context.checkpoint.working_state, calls)
        context.checkpoint.pending_tool_calls = []
        before_recovery = context.run.tool_call_count
        try:
            await recover_browser_failure(context, step, calls, results)
        except UserInputRequiredError:
            state["suspended_at"] = context.clock.now().isoformat()
            raise
        finally:
            # Recovery reads/questions spend this workflow's ceiling too, including
            # a question that suspends before returning to this dispatcher.
            state["calls"] += context.run.tool_call_count - before_recovery
        await checkpoint(context, "browser_workflow_progress")

    async def _stop(self, context: RunContext, state: dict[str, Any], status: str) -> None:
        if status == "verified" and await self._yield_to_follow_up(context, state, verified=True):
            return
        state["status"] = status
        context.checkpoint.working_state["browser_workflow_report_only"] = True
        await checkpoint(context, "browser_workflow_progress")
