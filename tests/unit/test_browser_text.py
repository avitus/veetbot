"""Readable text coverage survives the separate canonical and model budgets."""

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain.browser import (
    BrowserElement,
    BrowserObservation,
    BrowserObservationCoverage,
    BrowserRegionCoverage,
    BrowserTextCoverage,
)
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.tool_output import content_bytes
from agent_core.tools.browser_results import OUTPUT_SCHEMA, bounded_observation_payload
from tests.unit.test_browser_tools import FakeBrowserProvider


@pytest.mark.parametrize("budget", [700, 999, 1024, 4000])
def test_canonical_and_model_text_omissions_are_separate(budget: int) -> None:
    observation = BrowserObservation(
        url="https://example.org",
        revision="revision-1",
        text='雪🦉"\\\n' * 4000,
        text_coverage=BrowserTextCoverage(
            scanned_nodes=15,
            scanned_text_characters=28000,
            node_limit_reached=True,
            omitted_text_bytes=7,
        ),
    )
    canonical, serialized = bounded_observation_payload(FakeBrowserProvider(), observation, budget)
    assert "text_coverage" in OUTPUT_SCHEMA["properties"]
    assert json.loads(serialized) == canonical
    assert len(content_bytes([TextPart(text=serialized)])) <= budget
    admitted = len(canonical["text"].encode())
    assert 0 < admitted < len(observation.text.encode())
    assert canonical["text_coverage"]["omitted_text_bytes"] == (
        len(observation.text.encode()) - admitted + 7
    )
    assert canonical["text_coverage"]["node_limit_reached"]
    projected = browser_context_projection(
        canonical,
        budget=1024,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    assert len(content_bytes(projected)) <= 1024
    payload = json.loads(projected[0].text)
    assert BrowserTextCoverage.model_validate(payload["provider_text_coverage"]) == (
        BrowserTextCoverage.model_validate(canonical["text_coverage"])
    )
    assert payload["coverage"]["omitted_text_bytes"] == admitted - len(payload["text"].encode())


@pytest.mark.parametrize("text,omitted", [("雪" * 100000, 0), ("x", 262144)], ids=["bytes", "sum"])
def test_coverage_bearing_text_requires_a_bounded_original_capture(text: str, omitted: int) -> None:
    with pytest.raises(ValidationError, match="text capture"):
        BrowserObservation(
            url="https://example.org",
            revision="current",
            text=text,
            text_coverage=BrowserTextCoverage(
                scanned_nodes=1,
                scanned_text_characters=1,
                omitted_text_bytes=omitted,
            ),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("scanned_nodes", 8193),
        ("scanned_text_characters", 262145),
        ("omitted_text_bytes", -1),
        ("node_limit_reached", "false"),
        ("scope", "all_frames"),
    ],
)
def test_text_coverage_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        BrowserTextCoverage.model_validate(
            {
                "scanned_nodes": 1,
                "scanned_text_characters": 1,
                field: value,
            }
        )


def test_tiny_model_budget_omits_observation_instead_of_hiding_capture_limits() -> None:
    observation = BrowserObservation(
        url="https://example.org",
        revision="current",
        text="Partial evidence",
        text_coverage=BrowserTextCoverage(
            scanned_nodes=8192,
            scanned_text_characters=16,
            node_limit_reached=True,
        ),
    )
    projected = browser_context_projection(
        observation.model_dump(),
        budget=400,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    payload = json.loads(projected[0].text)
    assert payload["observation_omitted"] and "revision" not in payload
    assert len(content_bytes(projected)) <= 400


@pytest.mark.parametrize("limited", [False, True])
def test_compact_model_coverage_is_lossless(limited: bool) -> None:
    observation = BrowserObservation(
        url="https://example.org",
        revision="current",
        coverage=BrowserObservationCoverage(
            candidate_offset=4096,
            scanned_candidates=256,
            next_cursor="c" * 32 if limited else None,
            scan_limit_reached=limited,
        ),
        region_coverage=BrowserRegionCoverage(
            scanned_nodes=8192,
            omitted_regions=9,
            scan_limit_reached=limited,
        ),
        text_coverage=BrowserTextCoverage(
            scanned_nodes=8192,
            scanned_text_characters=262144,
            node_limit_reached=limited,
            text_limit_reached=limited,
            omitted_text_bytes=100,
        ),
    )
    projected = browser_context_projection(
        observation.model_dump(),
        budget=4096,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    payload = json.loads(projected[0].text)
    assert (
        BrowserObservationCoverage.model_validate(payload["provider_coverage"])
        == observation.coverage
    )
    assert (
        BrowserRegionCoverage.model_validate(payload["provider_region_coverage"])
        == observation.region_coverage
    )
    assert (
        BrowserTextCoverage.model_validate(payload["provider_text_coverage"])
        == observation.text_coverage
    )


def test_small_budget_keeps_discovery_prefix_before_optional_page_context() -> None:
    revision = "r" * 32
    observation = BrowserObservation(
        url="https://site.test/",
        title="Lesson",
        revision=revision,
        text="AnswerSubmit",
        readiness="dom_quiet",
        elements=(
            BrowserElement(ref=f"{revision}:0", role="textbox", name="Answer"),
            BrowserElement(ref=f"{revision}:1", role="button", name="Submit"),
        ),
        coverage=BrowserObservationCoverage(candidate_offset=0, scanned_candidates=2),
        region_coverage=BrowserRegionCoverage(scanned_nodes=7, omitted_regions=0),
        text_coverage=BrowserTextCoverage(scanned_nodes=8, scanned_text_characters=12),
    )
    projected = browser_context_projection(
        observation.model_dump(),
        budget=1024,
        reference=FileReferencePart(artifact_id=UUID(int=1), media_type="application/json"),
    )
    assert projected is not None and isinstance(projected[0], TextPart)
    payload = json.loads(projected[0].text)
    assert payload["elements"][0]["ref"] == f"{revision}:0"
    assert payload["next_observe"] == {"after": payload["elements"][-1]["ref"]}
    assert len(content_bytes(projected)) <= 1024
