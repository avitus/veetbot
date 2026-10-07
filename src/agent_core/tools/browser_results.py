"""Shared bounded result and failure conversion for browser tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_core.domain.browser import (
    BrowserConditionResult,
    BrowserObservation,
    BrowserObservationCoverage,
    BrowserObservationExpansion,
    BrowserObservationFocus,
    BrowserProviderError,
    BrowserRegionCoverage,
    BrowserSemanticRegion,
    BrowserTextCoverage,
)
from agent_core.domain.browser_extraction import BrowserExtractionResult
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tool_output import content_bytes
from agent_core.domain.tools import ToolFailure, ToolFailureKind, ToolResult
from agent_core.ports.browser import BrowserProvider

MAX_TEXT_BYTES = 256 * 1024
MAX_PROVIDER_BYTES = 128
MAX_URL_BYTES = 4 * 1024
MAX_TITLE_BYTES = 4 * 1024
MAX_REVISION_BYTES = 512
ELEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "role": {"type": "string"},
        "name": {"type": "string"},
        "disabled": {"type": "boolean"},
        "checked": {"type": ["boolean", "null"]},
    },
    "required": ["ref", "role", "name", "disabled", "checked"],
    "additionalProperties": False,
}
_EXTRACTION_SCHEMA = BrowserExtractionResult.model_json_schema()
_EXTRACTION_DEFINITIONS = _EXTRACTION_SCHEMA.pop("$defs")
OUTPUT_SCHEMA: dict[str, Any] = {
    "$defs": _EXTRACTION_DEFINITIONS,
    "type": "object",
    "properties": {
        "provider": {"type": "string"},
        "url": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "revision": {"type": "string"},
        "text": {"type": "string"},
        "text_coverage": BrowserTextCoverage.model_json_schema(),
        "focus": BrowserObservationFocus.model_json_schema(),
        "interruption": {"type": "string", "enum": ["needs_user"]},
        "next_text": BrowserObservationExpansion.model_json_schema(),
        "elements": {"type": "array", "items": ELEMENT_SCHEMA, "maxItems": 256},
        "coverage": BrowserObservationCoverage.model_json_schema(),
        "readiness": {"type": "string", "enum": ["dom_quiet", "bound_expired"]},
        "condition": BrowserConditionResult.model_json_schema(),
        "regions": {
            "type": "array",
            "items": BrowserSemanticRegion.model_json_schema(),
            "maxItems": 32,
        },
        "region_coverage": BrowserRegionCoverage.model_json_schema(),
        "extraction": _EXTRACTION_SCHEMA,
        "extraction_omitted": {"type": "boolean"},
        "next_observe": {
            "type": "object",
            "properties": {
                "after": {"type": "string"},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["provider", "url", "title", "revision", "text", "elements"],
    "additionalProperties": False,
}

_FAILURE_KINDS = {
    "tool.browser.profile_unavailable": ToolFailureKind.PERMISSION,
    "tool.browser.authentication_required": ToolFailureKind.PERMISSION,
    "tool.browser.needs_user": ToolFailureKind.PERMISSION,
    "tool.browser.output_invalid": ToolFailureKind.OUTPUT_INVALID,
    "tool.browser.provider_unavailable": ToolFailureKind.TRANSPORT,
    "tool.browser.outcome_unknown": ToolFailureKind.OUTCOME_UNKNOWN,
    "tool.browser.element_not_found": ToolFailureKind.NOT_FOUND,
    "tool.browser.page_changed": ToolFailureKind.INVALID_ARGUMENTS,
    "tool.browser.navigation_cancelled": ToolFailureKind.INVALID_ARGUMENTS,
    "tool.browser.action_not_allowed": ToolFailureKind.INVALID_ARGUMENTS,
    # ADR-0129: the runtime refused before dispatch; nothing happened, and the
    # model observes again, so the outcome is failed, never uncertain.
    "tool.browser.grant_not_applicable": ToolFailureKind.INVALID_ARGUMENTS,
}


def browser_failure(
    error_or_kind: BrowserProviderError | ToolFailureKind,
    reason_code: str | None = None,
    *,
    retryable: bool | None = None,
) -> ToolResult:
    if isinstance(error_or_kind, BrowserProviderError):
        reason = error_or_kind.reason_code
        kind = _FAILURE_KINDS.get(reason, ToolFailureKind.UPSTREAM_ERROR)
        can_retry = error_or_kind.retryable
    else:
        if reason_code is None or retryable is None:
            raise ValueError("explicit browser failures require reason and retryability")
        kind = error_or_kind
        reason = reason_code
        can_retry = retryable
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason,
            detail="browser access failed at a platform-controlled boundary",
            retryable=can_retry,
        ),
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


def _bounded_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _serialized_observation(structured: dict[str, Any]) -> str:
    return json.dumps(structured, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _observation_bytes(structured: dict[str, Any]) -> int:
    return len(content_bytes([TextPart(text=_serialized_observation(structured))]))


def bounded_observation_payload(
    provider: BrowserProvider,
    observation: BrowserObservation,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str]:
    elements = [element.model_dump(mode="json") for element in observation.elements[:256]]
    base: dict[str, Any] = {
        "provider": _bounded_utf8(provider.name, MAX_PROVIDER_BYTES),
        "url": _bounded_utf8(observation.url, MAX_URL_BYTES),
        "title": (
            _bounded_utf8(observation.title, MAX_TITLE_BYTES)
            if observation.title is not None
            else None
        ),
        "revision": _bounded_utf8(observation.revision, MAX_REVISION_BYTES),
        "text": "",
        "elements": elements,
    }
    if observation.focus is not None:
        base["focus"] = observation.focus.model_dump(mode="json")
        if observation.focus.text_offset < observation.focus.text_total_bytes:
            base["next_text"] = {
                "cursor": f"{observation.focus.text_cursor}.{observation.focus.text_offset}"
            }

    original_text_bytes = len(observation.text.encode("utf-8"))
    if observation.text_coverage is not None:
        base["text_coverage"] = observation.text_coverage.model_dump(mode="json")
        base["text_coverage"]["omitted_text_bytes"] += original_text_bytes
    if observation.extraction is not None:
        base["extraction"] = observation.extraction.model_dump(mode="json")
    if observation.interruption is not None:
        base["interruption"] = observation.interruption
    if observation.readiness is not None:
        base["readiness"] = observation.readiness
    if observation.condition is not None:
        base["condition"] = observation.condition.model_dump(mode="json")
    if observation.region_coverage is not None:
        base["regions"] = [region.model_dump(mode="json") for region in observation.regions]
        base["region_coverage"] = observation.region_coverage.model_dump(mode="json")
    if observation.coverage is not None:
        base["coverage"] = observation.coverage.model_dump(mode="json")
        if observation.coverage.next_cursor is not None:
            base["next_observe"] = {"cursor": observation.coverage.next_cursor}
    while base.get("regions") and _observation_bytes(base) > maximum_bytes:
        base["regions"].pop()
        base["region_coverage"]["omitted_regions"] += 1
    if _observation_bytes(base) > maximum_bytes:
        low = 0
        high = len(elements)
        while low < high:
            candidate_count = (low + high + 1) // 2
            base["elements"] = elements[:candidate_count]
            if observation.coverage is not None:
                base["next_observe"] = {"after": elements[candidate_count - 1]["ref"]}
            if _observation_bytes(base) <= maximum_bytes:
                low = candidate_count
            else:
                high = candidate_count - 1
        base["elements"] = elements[:low]
        if observation.coverage is not None:
            if low:
                base["next_observe"] = {"after": elements[low - 1]["ref"]}
            else:
                base.pop("next_observe", None)

    while base.get("extraction", {}).get("rows") and _observation_bytes(base) > maximum_bytes:
        base["extraction"]["rows"].pop()
        base["extraction"]["omitted_rows"] += 1
        base["extraction"]["byte_limit_reached"] = True
    if "extraction" in base and _observation_bytes(base) > maximum_bytes:
        base.pop("extraction")
        base["extraction_omitted"] = True

    low = 0
    high = min(original_text_bytes, MAX_TEXT_BYTES)

    def admit_text(candidate_bytes: int) -> None:
        base["text"] = _bounded_utf8(observation.text, candidate_bytes)
        if observation.focus is not None:
            offset = observation.focus.text_offset + len(base["text"].encode("utf-8"))
            base.pop("next_text", None)
            if offset < observation.focus.text_total_bytes:
                base["next_text"] = {"cursor": f"{observation.focus.text_cursor}.{offset}"}
        if observation.text_coverage is not None:
            base["text_coverage"]["omitted_text_bytes"] = (
                observation.text_coverage.omitted_text_bytes
                + original_text_bytes
                - len(base["text"].encode("utf-8"))
            )

    while low <= high:
        candidate_bytes = (low + high) // 2
        admit_text(candidate_bytes)
        if _observation_bytes(base) <= maximum_bytes:
            low = candidate_bytes + 1
        else:
            high = candidate_bytes - 1
    admit_text(max(0, high))
    return base, _serialized_observation(base)


def observation_result(
    provider: BrowserProvider,
    observation: BrowserObservation,
    maximum_bytes: int,
) -> ToolResult:
    if not provider.allows(observation.url):
        return browser_failure(
            ToolFailureKind.OUTPUT_INVALID,
            "tool.browser.output_invalid",
            retryable=False,
        )
    structured, serialized = bounded_observation_payload(provider, observation, maximum_bytes)
    return ToolResult(
        ok=True,
        content=[TextPart(text=serialized)],
        structured=structured,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        evidence_key=observation_evidence_key(structured),
    )


def observation_evidence_key(structured: dict[str, Any]) -> str:
    """ADR-0130: a digest of what the model saw, without provider, revision or refs.

    Two observations of the same page share it even though every observation
    gets a new revision; a page that changed gets a new one. The loop breaker
    reads it; it is never model-visible and never stored.
    """

    condition = structured.get("condition")
    if isinstance(condition, dict):
        # A task predicate is the relevant progress signal. Timers, adverts,
        # capture counts and unrelated controls cannot extend a failed wait.
        canonical = json.dumps({"condition_status": condition.get("status")}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]
    evidence: dict[str, Any] = {
        "url": structured.get("url"),
        "title": structured.get("title"),
        "text": structured.get("text"),
        "elements": [
            [
                element.get("role"),
                element.get("name"),
                element.get("disabled"),
                element.get("checked"),
            ]
            for element in structured.get("elements", ())
        ],
    }
    coverage = structured.get("coverage")
    if isinstance(coverage, dict):
        evidence["candidate_offset"] = coverage.get("candidate_offset")
    if structured.get("region_coverage") is not None:
        evidence["regions"] = [
            [region.get("kind"), region.get("text"), region.get("text_truncated")]
            for region in structured.get("regions", ())
        ]
    extracted = structured.get("extraction")
    if isinstance(extracted, dict):
        evidence["extraction"] = {
            key: value
            for key, value in extracted.items()
            if key not in {"revision", "source_ref", "rows"}
        }
        evidence["extraction"]["rows"] = [
            {
                "schema_valid": row["schema_valid"],
                "cells": [
                    {key: value for key, value in cell.items() if key != "ref"}
                    for cell in row["cells"]
                ],
            }
            for row in extracted.get("rows", ())
        ]
    canonical = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
