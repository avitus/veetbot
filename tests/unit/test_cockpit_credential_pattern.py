"""The Cockpit builder screens widget code with the hardline credential pattern."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _hardline_compiled_patterns() -> list[tuple[str, int]]:
    tree = ast.parse((ROOT / "src/agent_core/policy/hardline.py").read_text(encoding="utf-8"))
    patterns: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            flags = 0
            if len(node.args) > 1 and isinstance(node.args[1], ast.Attribute):
                flags = int(getattr(re, node.args[1].attr))
            patterns.append((node.args[0].value, flags))
    return patterns


def test_builder_credential_pattern_is_the_hardline_pattern() -> None:
    document = json.loads(
        (ROOT / "cockpit/schema/credential-pattern.json").read_text(encoding="utf-8")
    )
    flags = re.I if document["flags"] == "i" else 0
    assert (document["pattern"], int(flags)) in _hardline_compiled_patterns()
