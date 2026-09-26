"""ADR-0130: a tool result carries a platform evidence key that is never serialized."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.domain.tools import ToolResult


def test_evidence_key_is_carried_but_never_serialized() -> None:
    result = ToolResult(ok=True, content=[], evidence_key="a" * 32)

    assert getattr(result, "evidence_key", None) == "a" * 32
    assert "evidence_key" not in result.model_dump()
    assert "evidence_key" not in result.model_dump(mode="json")
    assert "evidence_key" not in result.model_dump_json()
    assert "a" * 32 not in result.model_dump_json()


def test_evidence_key_defaults_to_none_and_is_bounded() -> None:
    assert getattr(ToolResult(ok=True, content=[]), "evidence_key", "absent") is None
    with pytest.raises(ValidationError):
        ToolResult(ok=True, content=[], evidence_key="a" * 65)
