"""Private stdio entry point for the two confined Bland tool rosters."""

import argparse
import getpass
import json
import os
from pathlib import Path

from bland_mcp.client import BlandClient, BlandError
from bland_mcp.server import create_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m bland_mcp")
    parser.add_argument("--mode", choices=("read", "call"))
    commands = parser.add_subparsers(dest="command")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--output-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "bootstrap":
        path = arguments.output_file
        if (
            arguments.mode is not None
            or not path.is_absolute()
            or path.exists()
            or path.is_symlink()
        ):
            raise SystemExit("bootstrap requires a new absolute private-file path")
        key = getpass.getpass("Bland API key (hidden): ").strip()
        if not key.isascii() or not 16 <= len(key) <= 4096 or any(char.isspace() for char in key):
            raise SystemExit("bland.credential_invalid")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(key + "\n")
        except OSError:
            raise SystemExit("could not create the private Bland credential file") from None
        print(f"Saved private credential to {path}")
        return
    if arguments.mode is None:
        parser.error("choose --mode or bootstrap")
    try:
        credential = json.loads(os.environ.pop("BLAND_MCP_CREDENTIAL", ""))
        if not isinstance(credential, dict) or set(credential) != {"api_key", "configuration"}:
            raise ValueError
        configuration = credential["configuration"]
        server = create_server(
            arguments.mode,
            BlandClient(credential["api_key"], configuration["phone_number"]),
            configuration,
        )
    except (ValueError, TypeError, KeyError, BlandError):
        raise SystemExit("bland.configuration_invalid") from None
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
