"""Content-hazard predicates shared by every surface that stores user text.

The patterns are the write-path guards the memory formation service has
always applied; they live in the domain so the application layer - the
persona write surfaces - can refuse before persistence without importing
the formation pipeline. `agent_core.memory.formation` reads them from here,
so the two surfaces cannot drift.
"""

from __future__ import annotations

import re

SECRET_PATTERN = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential|bearer)\s*[:=]\s*\S+",
    re.I,
)
INJECTION_PATTERN = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"<\s*/?\s*(?:system|memory|untrusted)|override\s+(?:policy|instructions))",
    re.I,
)


def contains_secret_material(value: str) -> bool:
    """True when a statement carries credential-shaped material."""

    return SECRET_PATTERN.search(value) is not None


def contains_injection_pattern(value: str) -> bool:
    """True when a statement carries an instruction-injection pattern."""

    return INJECTION_PATTERN.search(value) is not None
