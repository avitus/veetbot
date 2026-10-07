"""Semantic evidence survives tool and model byte limits as whole records."""

import json
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from agent_core.domain.browser import BrowserObservation
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.tool_output import content_bytes
from agent_core.tools.browser_results import (
    OUTPUT_SCHEMA,
    observation_evidence_key,
    observation_result,
)
from tests.unit.test_browser_tools import FakeBrowserProvider


def structured_page(
    *, revision: str = "revision", message: str = "Changes saved"
) -> BrowserObservation:
    return BrowserObservation.model_validate(
        {
            "url": "https://example.org/lesson",
            "revision": revision,
            "text": "Unrelated navigation " * 2000,
            "readiness": "dom_quiet",
            "coverage": {"candidate_offset": 0, "scanned_candidates": 10},
            "elements": [{"ref": revision + ":0", "role": "button", "name": "Continue"}],
            "regions": [
                {
                    "ref": revision + ":region:0",
                    "kind": "status",
                    "text": message,
                    "text_truncated": False,
                },
                *[
                    {
                        "ref": revision + f":region:{i}",
                        "kind": "heading",
                        "text": "雪" * 512,
                        "text_truncated": True,
                    }
                    for i in range(1, 32)
                ],
            ],
            "region_coverage": {
                "version": 1,
                "scope": "main_document",
                "scanned_nodes": 200,
                "scan_limit_reached": False,
                "omitted_regions": 5,
            },
        }
    )


@pytest.mark.parametrize("budget", [1024, 4096])
def test_model_projection_retains_status_and_control_or_explicit_region_omissions(
    budget: int,
) -> None:
    observation = structured_page()
    result = observation_result(FakeBrowserProvider(), observation, 512 * 1024)
    assert result.structured is not None
    assert result.structured.get("regions"), "tool discarded page structure"
    Draft202012Validator(OUTPUT_SCHEMA).validate(result.structured)
    projected = browser_context_projection(
        result.structured,
        budget=budget,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    assert len(content_bytes(projected)) <= budget
    payload = json.loads(projected[0].text)
    assert payload["revision"] == observation.revision and payload["elements"]
    assert payload["coverage"]["omitted_regions"] == 32 - len(payload["regions"])
    assert payload["provider_region_coverage"]["omitted_regions"] == 5
    if budget == 4096:
        assert payload["regions"][0] == result.structured["regions"][0]
    assert all(r in result.structured["regions"] for r in payload["regions"])


def test_canonical_bounding_counts_region_omissions_and_progress_ignores_reference_churn() -> None:
    page = structured_page()
    result = observation_result(FakeBrowserProvider(), page, 4096)
    assert result.structured is not None
    assert result.structured.get("regions"), "canonical output discarded every region"
    assert len(content_bytes(result.content)) <= 4096
    assert result.structured["region_coverage"]["omitted_regions"] == 5 + 32 - len(
        result.structured["regions"]
    )
    first = page.model_dump(mode="json")
    renewed = structured_page(revision="new").model_dump(mode="json")
    changed = structured_page(message="Changes rejected").model_dump(mode="json")
    assert observation_evidence_key(first) == observation_evidence_key(renewed)
    assert observation_evidence_key(first) != observation_evidence_key(changed)


@pytest.mark.parametrize("malformation", ["missing_coverage", "duplicate_ref", "control_ref"])
def test_region_records_cannot_lose_coverage_or_alias_action_references(malformation: str) -> None:
    from pydantic import ValidationError

    raw = structured_page().model_dump(mode="json")
    if malformation == "missing_coverage":
        raw.pop("region_coverage")
    elif malformation == "duplicate_ref":
        raw["regions"][1]["ref"] = raw["regions"][0]["ref"]
    else:
        raw["regions"][0]["ref"] = raw["elements"][0]["ref"]
    with pytest.raises(ValidationError):
        BrowserObservation.model_validate(raw)


@pytest.mark.parametrize(
    "malformation", ["unknown_kind", "extra_field", "text_bound", "count_bound", "scan_bound"]
)
def test_region_boundary_refuses_malformed_or_overbound_evidence(malformation: str) -> None:
    from pydantic import ValidationError

    raw = structured_page().model_dump(mode="json")
    if malformation == "unknown_kind":
        raw["regions"][0]["kind"] = "script"
    elif malformation == "extra_field":
        raw["regions"][0]["html"] = "untrusted canary"
    elif malformation == "text_bound":
        raw["regions"][0]["text"] = "x" * 513
    elif malformation == "count_bound":
        raw["regions"].append(raw["regions"][0])
    else:
        raw["region_coverage"]["scanned_nodes"] = 8193
    with pytest.raises(ValidationError):
        BrowserObservation.model_validate(raw)
