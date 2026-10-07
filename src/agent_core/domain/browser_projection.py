"""Whole browser references inside the ordinary model-context byte budget."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from agent_core.domain.browser import BrowserObservation
from agent_core.domain.messages import ContentPart, FileReferencePart, TextPart
from agent_core.domain.tool_output import content_bytes


def browser_context_projection(
    structured: dict[str, Any], *, budget: int, reference: FileReferencePart
) -> list[ContentPart] | None:
    """Project validated observation data, never clipping an identity or element.

    Canonical provider output is retained separately. Coverage describes only
    this projection, not DOM nodes the provider itself never observed.
    """
    try:
        observation = BrowserObservation.model_validate(structured)
    except (ValidationError, ValueError):
        return None
    elements = [element.model_dump(mode="json") for element in observation.elements]
    regions = [region.model_dump(mode="json") for region in observation.regions]
    text_bytes = observation.text.encode("utf-8")
    payload: dict[str, Any] = {
        "revision": observation.revision,
        "url": observation.url,
        "title": observation.title,
        "text": "",
        "elements": [],
        "coverage": {
            "scope": "provider_observation",
            "omitted_elements": len(elements),
            "omitted_text_bytes": len(text_bytes),
            "omitted_fields": [],
        },
    }

    if observation.focus is not None:
        payload["coverage"]["scope"] = "focused_region"
        payload["coverage"]["omitted_fields"].append("focus")

    if observation.interruption is not None:
        payload["interruption"] = observation.interruption
    if observation.readiness is not None:
        payload["readiness"] = observation.readiness
    if observation.condition is not None:
        payload["condition"] = observation.condition.model_dump(mode="json")
    if observation.text_coverage is not None:
        payload["provider_text_coverage"] = {
            "version": observation.text_coverage.version,
            "scope": observation.text_coverage.scope,
            **observation.text_coverage.model_dump(mode="json", exclude_defaults=True),
        }
    if observation.coverage is not None:
        payload["provider_coverage"] = observation.coverage.model_dump(
            mode="json", exclude_defaults=True
        )
    if observation.region_coverage is not None:
        payload["provider_region_coverage"] = {
            "version": observation.region_coverage.version,
            "scope": observation.region_coverage.scope,
            **observation.region_coverage.model_dump(mode="json", exclude_defaults=True),
        }
        payload["regions"] = []
        payload["coverage"]["omitted_regions"] = len(regions)

    extracted_rows = []
    if observation.extraction is not None:
        payload["extraction"] = observation.extraction.model_dump(mode="json")
        extracted_rows = payload["extraction"]["rows"]
        payload["extraction"]["rows"] = []
        payload["extraction"]["omitted_rows"] += len(extracted_rows)
        if extracted_rows:
            payload["extraction"]["byte_limit_reached"] = True
    elif structured.get("extraction_omitted"):
        payload["coverage"]["omitted_fields"].append("extraction")

    def content() -> list[ContentPart]:
        if observation.focus is not None and "coverage" in payload:
            payload.pop("next_text", None)
            offset = observation.focus.text_offset + len(payload["text"].encode("utf-8"))
            if offset < observation.focus.text_total_bytes:
                payload["next_text"] = {"cursor": f"{observation.focus.text_cursor}.{offset}"}
        if observation.coverage is not None and "coverage" in payload:
            payload.pop("next_observe", None)
            payload["coverage"].pop("expansion_blocked", None)
            selected = {element["ref"] for element in payload["elements"]}
            prefix = []
            for element in elements:
                if element["ref"] not in selected:
                    break
                prefix.append(element)
            if len(prefix) < len(elements):
                if prefix:
                    payload["next_observe"] = {"after": prefix[-1]["ref"]}
                else:
                    payload["coverage"]["expansion_blocked"] = "inline_control_too_large"
            elif structured.get("next_observe"):
                # Canonical byte bounding can itself retain only a prefix.
                continuation = structured["next_observe"]
                if isinstance(continuation, dict) and (
                    continuation == {"cursor": observation.coverage.next_cursor}
                    or (elements and continuation == {"after": elements[-1]["ref"]})
                ):
                    payload["next_observe"] = continuation
            elif observation.coverage.next_cursor is not None:
                payload["next_observe"] = {"cursor": observation.coverage.next_cursor}
        return [
            TextPart(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            reference,
        ]

    def fits() -> bool:
        return len(content_bytes(content())) <= budget

    # Keep scope, version, all limits and omission counts inline. Detailed
    # scan counters live in the canonical artifact at small budgets (ADR-0163).
    if regions and budget <= 2048:
        for field in ("provider_text_coverage", "provider_region_coverage", "provider_coverage"):
            for counter in (
                "scanned_nodes",
                "scanned_text_characters",
                "candidate_offset",
                "scanned_candidates",
            ):
                payload.get(field, {}).pop(counter, None)
        # Only next_observe exposes the eligible continuation. Duplicating a
        # provider cursor both wastes space and invites skipping inline controls.
        payload.get("provider_coverage", {}).pop("next_cursor", None)
        if payload.get("provider_coverage") == {}:
            payload.pop("provider_coverage")
        payload["coverage"]["diagnostics_in_artifact"] = True

    # A scoped read is reachable only if the model can see a region identity.
    # Preserve the highest-priority region with a bounded, explicitly clipped
    # label before filling prose. Never clip its opaque reference.
    preferred_region = None
    if regions and observation.focus is None:
        region = dict(regions[0])
        original = region["text"].encode("utf-8")
        region["text"] = original[: (24 if budget <= 2048 else 512)].decode(
            "utf-8", errors="ignore"
        )
        region["text_truncated"] = region["text_truncated"] or len(original) > (
            24 if budget <= 2048 else 512
        )
        preferred_region = region

    if observation.focus is not None and budget <= 2048:
        # The task already selected its location. Give scoped prose useful
        # space instead of retaining a URL/title beside a two-byte text window.
        for field in ("url", "title"):
            payload.pop(field, None)
            payload["coverage"]["omitted_fields"].append(field)

    # Choose the earliest usable discovery anchor with optional location context
    # removed. A slightly smaller later control must not steal its admission and
    # leave the model unable to advance through the omitted prefix.
    preferred_control = None
    if observation.coverage is not None:
        location = {field: payload.pop(field) for field in ("title", "url") if field in payload}
        omitted_fields = list(payload["coverage"]["omitted_fields"])
        payload["coverage"]["omitted_fields"].extend(location)
        for element in elements:
            payload["elements"] = [element]
            payload["coverage"]["omitted_elements"] = len(elements) - 1
            if fits():
                preferred_control = element
                break
        payload.update(location)
        payload["coverage"]["omitted_fields"] = omitted_fields
        payload["elements"] = []
        payload["coverage"]["omitted_elements"] = len(elements)

    def has_room_for_control() -> bool:
        if not elements:
            return True
        choices = (
            elements
            if observation.coverage is None
            else ([] if preferred_control is None else [preferred_control])
        )
        for element in choices:
            payload["elements"] = [element]
            payload["coverage"]["omitted_elements"] = len(elements) - 1
            available = fits()
            payload["elements"] = []
            payload["coverage"]["omitted_elements"] = len(elements)
            if available:
                return True
        return False

    if preferred_region is not None:
        payload["regions"] = [preferred_region]
        payload["coverage"]["omitted_regions"] -= 1

    # Location and title are optional context; the revision cannot be clipped.
    for field in ("title", "url"):
        if fits() and has_room_for_control():
            break
        if field in payload:
            del payload[field]
            payload["coverage"]["omitted_fields"].append(field)
    if preferred_region is not None:
        while preferred_region["text"] and (not fits() or not has_room_for_control()):
            preferred_region["text"] = preferred_region["text"][:-1]
            preferred_region["text_truncated"] = True
        if not fits() or not has_room_for_control():
            payload["regions"] = []
            payload["coverage"]["omitted_regions"] += 1
    if "extraction" in payload and not fits():
        payload.pop("extraction")
        payload["coverage"]["omitted_fields"].append("extraction")
    if not fits():
        payload = {
            "observation_omitted": True,
            "reason": "inline_budget_too_small",
            "elements": [],
        }
        return content() if fits() else None

    if "extraction" in payload and observation.extraction is not None:
        for row in extracted_rows:
            payload["extraction"]["rows"].append(row)
            payload["extraction"]["omitted_rows"] -= 1
            payload["extraction"]["byte_limit_reached"] = (
                len(payload["extraction"]["rows"]) < len(extracted_rows)
                or observation.extraction.byte_limit_reached
            )
            if not fits():
                payload["extraction"]["rows"].pop()
                payload["extraction"]["omitted_rows"] += 1
                payload["extraction"]["byte_limit_reached"] = True
                break

    # Nested summaries must not reduce a focused text continuation to a few
    # bytes. Their omissions remain explicit; the canonical artifact keeps them.
    if observation.region_coverage is not None and not (
        observation.focus is not None and budget <= 2048
    ):
        region_bytes = sum(
            len(json.dumps(region, ensure_ascii=False).encode("utf-8"))
            for region in payload["regions"]
        )
        for region in regions:
            if any(selected["ref"] == region["ref"] for selected in payload["regions"]):
                continue
            size = len(json.dumps(region, ensure_ascii=False).encode("utf-8"))
            if region_bytes + size > budget // 3:
                continue
            payload["regions"].append(region)
            payload["coverage"]["omitted_regions"] -= 1
            if fits() and has_room_for_control():
                region_bytes += size
            else:
                payload["regions"].pop()
                payload["coverage"]["omitted_regions"] += 1

    # Reserve bounded prose space so dense controls cannot displace the task's
    # question. Every size check includes JSON escaping and the artifact ref.
    text_allowance = (
        0
        if observation.extraction is not None
        else min(len(text_bytes), budget // (8 if regions and observation.focus is None else 4))
    )
    low, high = 0, text_allowance
    while low < high:
        middle = (low + high + 1) // 2
        payload["text"] = text_bytes[:middle].decode("utf-8", errors="ignore")
        payload["coverage"]["omitted_text_bytes"] = len(text_bytes) - len(
            payload["text"].encode("utf-8")
        )
        if fits() and has_room_for_control():
            low = middle
        else:
            high = middle - 1
    payload["text"] = text_bytes[:low].decode("utf-8", errors="ignore")
    payload["coverage"]["omitted_text_bytes"] = len(text_bytes) - len(
        payload["text"].encode("utf-8")
    )

    # Prefer whole records in provider order, skipping an individually oversized
    # record instead of letting it hide every later actionable control.
    for element in elements:
        payload["elements"].append(element)
        payload["coverage"]["omitted_elements"] -= 1
        if not fits():
            payload["elements"].pop()
            payload["coverage"]["omitted_elements"] += 1
    rendered = content()
    if observation.focus is not None and not payload["text"] and "next_text" in payload:
        payload.pop("next_text")
        payload["coverage"]["text_expansion_blocked"] = True
        rendered = [
            TextPart(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            reference,
        ]
    return rendered
