"""The platform framing tells the model how to treat and cite recollections.

The retrieval design places the instruction that memory lines are
recollections the model cites by their short identifier in the cached
platform prefix (memory-retrieval-and-ranking.md, "Assembly and render").
Production never recorded a citation because that text did not exist, so
these pin that it does, that it can never be mistaken for a citation
itself, and that it fits the platform-policy class of the prefix.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_core.context import planner as planner_module
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.rendering import PLATFORM_FRAMING, build_prefix
from agent_core.domain.messages import SystemMessage, TextPart
from agent_core.memory.formation import _CITED_BELIEF
from tests.contract.support import agent


def test_platform_framing_tells_the_model_to_cite_recollections() -> None:
    assert "[m:xxxxxxxx]" in PLATFORM_FRAMING
    assert "rel" in PLATFORM_FRAMING and "identifier" in PLATFORM_FRAMING
    # Owner-stated lines are used, never doubted back at the owner.
    assert "affirmed" in PLATFORM_FRAMING and "user" in PLATFORM_FRAMING
    # The example placeholder is not hexadecimal, so copying it verbatim can
    # never match the extractor's citation form.
    assert _CITED_BELIEF.search(PLATFORM_FRAMING) is None


def test_platform_framing_is_row_zero_of_the_prefix_within_the_platform_cap() -> None:
    prefix = build_prefix(agent(), [])
    row = prefix[0]
    assert isinstance(row, SystemMessage)
    part = row.content[0]
    assert isinstance(part, TextPart) and part.text == PLATFORM_FRAMING
    plan = yaml.safe_load(
        Path(planner_module.__file__).with_name("plan.yaml").read_text(encoding="utf-8")
    )
    cap = plan["classes"]["platform_policy"]["max_tokens"]
    assert ConservativeTokenEstimator().estimate([row], "fake:scripted") <= cap


def test_builder_version_moved_with_the_framing_text() -> None:
    """A changed framing row is a changed prefix: every live plan re-plans once."""

    assert planner_module.BUILDER_VERSION == "context-builder@14"
    assert "context-builder@13" not in planner_module._EQUIVALENT_EARLIER_BUILDERS
