"""Print the -only-testing options for one shard of the simulator UI suite.

The suite is one XCTest class, so Xcode's parallel testing, which divides by
class, cannot split it. CI instead runs each device on two executors, and each
takes every second enabled case in name order (ADR-0230). The input is the
JSON that ``xcodebuild -enumerate-tests -test-enumeration-format json`` writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _enabled_identifiers(node: object) -> set[str]:
    """Every identifier listed under an enabledTests key, at any depth."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "enabledTests" and isinstance(value, list):
                found.update(
                    # -only-testing names an XCTest case without a call suffix.
                    str(test["identifier"]).removesuffix("()")
                    for test in value
                    if isinstance(test, dict) and "identifier" in test
                )
            else:
                found |= _enabled_identifiers(value)
    elif isinstance(node, list):
        for item in node:
            found |= _enabled_identifiers(item)
    return found


def shard_options(enumeration: object, shard: str) -> list[str]:
    """The -only-testing options of shard ``index/total``."""
    index_text, separator, total_text = shard.partition("/")
    if not (separator and index_text.isdigit() and total_text.isdigit()):
        raise ValueError(f"shard {shard!r} is not INDEX/TOTAL")
    index, total = int(index_text), int(total_text)
    if not 0 <= index < total:
        raise ValueError(f"shard {shard!r} is out of range")
    identifiers = sorted(_enabled_identifiers(enumeration))
    if not identifiers:
        raise ValueError("the enumeration lists no enabled tests")
    return [f"-only-testing:{identifier}" for identifier in identifiers[index::total]]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scripts.ios_ui_shard ENUMERATION.json INDEX/TOTAL", file=sys.stderr)
        return 2
    try:
        enumeration = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        options = shard_options(enumeration, argv[1])
    except (OSError, ValueError) as error:
        print(f"ios_ui_shard: {error}", file=sys.stderr)
        return 1
    total = len(_enabled_identifiers(enumeration))
    print(
        f"ios_ui_shard: shard {argv[1]} runs {len(options)} of {total} enabled cases",
        file=sys.stderr,
    )
    print(" ".join(options))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
