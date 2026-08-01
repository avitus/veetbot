"""The in-memory adapter's declared guarantees and gaps are reviewable."""

from pathlib import Path

import yaml

from agent_core.adapters.persistence.memory import (
    MEMORY_ADAPTER_CAPABILITIES,
    MEMORY_ADAPTER_GAPS,
)


def test_memory_adapter_capabilities_match_checked_in_table() -> None:
    path = Path(__file__).with_name("adapter-capabilities.yaml")
    table = yaml.safe_load(path.read_text(encoding="utf-8"))["in_memory"]
    assert frozenset(table["supported"]) == MEMORY_ADAPTER_CAPABILITIES
    assert frozenset(table["gaps"]) == MEMORY_ADAPTER_GAPS
    assert MEMORY_ADAPTER_CAPABILITIES.isdisjoint(MEMORY_ADAPTER_GAPS)
