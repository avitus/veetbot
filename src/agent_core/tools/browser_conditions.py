"""Finite read-only checks of visible evidence, without replaying an action."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from agent_core.domain.browser import (
    BrowserCondition,
    BrowserConditionResult,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.browser_diagnostics import browser_phase
from agent_core.domain.browser_evidence import BrowserRowEvidence, evidence_status
from agent_core.ports.browser import BrowserProvider, extract_browser_observation


def browser_condition_schema() -> dict[str, Any]:
    """Compact closed wire shape; the domain validates each evidence kind before I/O."""
    return {
        "$defs": {
            "e": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {
                    "kind": {"enum": ["text", "region", "location", "row"]},
                    "text": {"type": "string", "minLength": 1, "maxLength": 512},
                    "region_kind": {
                        "enum": ["dialog", "alert", "status", "form", "heading", "main", "section"]
                    },
                    "url": {"type": "string", "maxLength": 4096},
                    "collection_kind": {"enum": ["table", "list", "form"]},
                    "index": {"type": "integer", "minimum": 0, "maximum": 15},
                    "row_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "fields": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "column", "type", "value"],
                            "properties": {
                                "name": {"type": "string", "maxLength": 64},
                                "column": {"type": "integer", "minimum": 0, "maximum": 15},
                                "type": {"enum": ["string", "integer", "number", "boolean"]},
                                "value": {"type": ["string", "number", "boolean"]},
                            },
                        },
                    },
                },
            }
        },
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "role": {"type": "string", "minLength": 1, "maxLength": 64},
            "name": {"type": "string", "minLength": 1, "maxLength": 1024},
            "disabled": {"type": "boolean"},
            "checked": {"type": "boolean"},
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
            "evidence": {"$ref": "#/$defs/e"},
            "failure_evidence": {"$ref": "#/$defs/e"},
        },
        "oneOf": [{"required": ["role", "name"]}, {"required": ["evidence"]}],
    }


def _control_status(
    observation: BrowserObservation, condition: BrowserCondition
) -> Literal["satisfied", "not_observed", "ambiguous"]:
    matches = [
        element
        for element in observation.elements
        if element.role == condition.role and element.name == condition.name
    ]
    if len(matches) > 1:
        return "ambiguous"
    if not matches:
        return "not_observed"
    target = matches[0]
    if condition.disabled is not None and target.disabled != condition.disabled:
        return "not_observed"
    if condition.checked is not None and target.checked != condition.checked:
        return "not_observed"
    return "satisfied"


def _status(
    observation: BrowserObservation, condition: BrowserCondition
) -> Literal["satisfied", "not_observed", "ambiguous", "failed"]:
    success = (
        evidence_status(observation, condition.evidence)
        if condition.evidence is not None
        else _control_status(observation, condition)
    )
    failure = (
        evidence_status(observation, condition.failure_evidence)
        if condition.failure_evidence is not None
        else "not_observed"
    )
    if failure == "ambiguous" or (failure == "satisfied" and success != "not_observed"):
        return "ambiguous"
    return "failed" if failure == "satisfied" else success


async def check_browser_condition(
    provider: BrowserProvider,
    observation: BrowserObservation,
    condition: BrowserCondition,
) -> BrowserObservation:
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + condition.timeout_ms / 1000
    count = 1
    row = condition.evidence if isinstance(condition.evidence, BrowserRowEvidence) else None

    async def capture(previous: BrowserObservation) -> BrowserObservation:
        nonlocal count

        async def read(*, extract: bool, revision: str) -> BrowserObservation:
            nonlocal count
            if count >= 21:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            count += 1
            observed = (
                await extract_browser_observation(provider, row.extraction_request(revision))
                if extract and row is not None
                else await provider.observe()
            )
            if not provider.allows(observed.url):
                raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
            return observed

        try:
            async with asyncio.timeout_at(deadline):
                try:
                    return await read(extract=row is not None, revision=previous.revision)
                except BrowserProviderError as error:
                    if error.reason_code not in {
                        "tool.browser.page_changed",
                        "tool.browser.element_not_found",
                    }:
                        raise
                    # One fresh read can reconcile a DOM mutation. It consumes
                    # the same deadline and attempt allowance, never a write.
                    fresh = await read(extract=False, revision=previous.revision)
                    return (
                        fresh if row is None else await read(extract=True, revision=fresh.revision)
                    )
        except TimeoutError as error:
            raise BrowserProviderError("tool.browser.page_changed", retryable=False) from error

    with browser_phase("postcondition", "orchestrator") as phase:
        if not provider.allows(observation.url):
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
        if row is not None and condition.timeout_ms > 0:
            observation = await capture(observation)
        while True:
            status = _status(observation, condition)
            remaining = deadline - loop.time()
            if status in {"satisfied", "failed"} or remaining <= 0.25 or count >= 21:
                break
            await asyncio.sleep(0.25)
            observation = await capture(observation)
        if status != "satisfied":
            phase.outcome = "bound_expired"
        return observation.model_copy(
            update={
                "condition": BrowserConditionResult(
                    status=status,
                    observations=count,
                    elapsed_ms=min(5000, max(0, int((loop.time() - started) * 1000))),
                )
            }
        )
