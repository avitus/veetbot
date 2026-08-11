"""Build the dependency-free Veetbot client as an executable zipapp."""

from __future__ import annotations

import argparse
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "client"
DEFAULT_OUTPUT = ROOT / "build" / "veetbot-client.pyz"


def _include(path: Path) -> bool:
    return "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(
        SOURCE,
        output,
        interpreter="/usr/bin/env python3",
        main="veetbot_client.__main__:main",
        filter=_include,
        compressed=True,
    )
    output.chmod(0o755)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
