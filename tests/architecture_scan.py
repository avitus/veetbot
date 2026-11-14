"""One architecture scan of the checkout per test process.

``architecture_errors`` parses every module under ``src/agent_core`` and walks
each tree several times, which costs seconds. Nine gates across five modules
read the same result for the repository root, so they share one scan per
process. Gates that build a fixture tree in ``tmp_path`` still call
``architecture_errors`` directly.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from scripts.architecture_checks import architecture_errors

ROOT = Path(__file__).resolve().parents[1]


@cache
def repository_architecture_errors() -> tuple[str, ...]:
    """The scan of the repository checkout, computed once per process."""

    return tuple(architecture_errors(ROOT))
