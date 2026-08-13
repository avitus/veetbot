"""Command-line entry point for the downloadable Veetbot client."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence

from . import __version__
from .api import ApiClient, ApiError, ClientError
from .chat import ChatApplication, Console


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veetbot-client",
        description="A small terminal client for the Veetbot HTTP API.",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("VEETBOT_API_URL", "http://127.0.0.1:8000"),
        help="API base URL (default: VEETBOT_API_URL or http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Resume an existing session ID instead of creating one.",
    )
    parser.add_argument("--agent", default="general", help="Agent ID for a new session.")
    parser.add_argument("--once", help="Send one prompt and exit after the run finishes.")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _run(args: argparse.Namespace, client: ApiClient) -> int:
    console = Console(sys.stdout, sys.stderr)
    application = ChatApplication(
        client,
        console,
        agent_id=str(args.agent),
        session_id=str(args.session) if args.session else None,
    )
    return application.run(once=str(args.once) if args.once is not None else None)


def _check_readiness(client: ApiClient) -> None:
    ready = client.health_ready()
    if ready.get("status") != "ready":
        raise ClientError("API is not ready")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get("VEETBOT_API_TOKEN")
    try:
        client = ApiClient(str(args.api_url), token=token)
        try:
            _check_readiness(client)
        except ApiError as exc:
            if exc.status != 401 or client.has_token or not sys.stdin.isatty():
                raise
            supplied = getpass.getpass("API token: ")
            client.set_token(supplied)
            _check_readiness(client)
        return _run(args, client)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (ClientError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
